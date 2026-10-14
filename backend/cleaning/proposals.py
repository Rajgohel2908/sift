"""
cleaning/proposals.py — Pure, rule-based proposal generators.

Every function takes data as plain Python objects (lists/dicts/DataFrames)
and returns a list of proposal dicts.  No DB access.  All are unit-testable.

Each proposal dict has the shape expected by CleaningProposal:
    {
        "kind":        str,
        "target":      str,          # column name or "*" for row-level
        "severity":    str,          # "critical" | "warning" | "info"
        "description": str,
        "impact": {
            "rows_affected": int,
            "examples": [            # up to 5 before/after samples
                {"before": ..., "after": ...}
            ],
        },
        "params": dict,              # passed to the transform engine
    }

Generators
----------
Structured (records):
  1. normalize_category    — fix case/whitespace inconsistencies in category cols
  2. trim_whitespace       — strip leading/trailing/double spaces in text cols
  3. drop_exact_duplicates — mark exact dup rows as "excluded" (keep first)
  4. normalize_dates       — parse & rewrite date cols to ISO 8601
  5. quarantine_missing_identifier — rows with null identifier → "quarantined"
  6. flag_outliers         — IQR 1.5x flag in numeric cols (flag only, no status)
  7. missing_value_policy  — optional→leave null (info); required text→"Unknown"

Unstructured (documents):
  8. exclude_duplicate_documents   — exact + near-dup; keep earliest, rest "excluded"
  9. quarantine_injection_suspects — injection-flagged docs → "quarantined"
 10. flag_stale_documents          — stale freshness → metadata flag only
 11. quarantine_short_content      — <30 words → "quarantined"
"""

import json
import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

import pandas as pd

from profiler.roles import severity_for_role
from profiler.text_quality import check_exact_duplicate, check_near_duplicate

# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(obj) -> Any:
    """json round-trip to make sure any value is JSON-safe."""
    return json.loads(json.dumps(obj, default=str))


def _examples(pairs: List[Dict], n: int = 5) -> List[Dict]:
    """Return up to n before/after example pairs, JSON-safe."""
    return [_safe(p) for p in pairs[:n]]


# ═══════════════════════════════════════════════════════════════════════════════
# STRUCTURED PROPOSALS  (input: list of record dicts with "data" key)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_structured_proposals(
    records: List[Dict],           # each has {"id": int, "data": dict, ...}
    roles: Dict[str, str],         # { column_name: role }
) -> List[Dict]:
    """
    Run all structured proposal generators and return a combined list.

    Args:
        records: list of record rows (each has at least "id" and "data" keys)
        roles:   column → role mapping from column_roles table

    Returns:
        list of proposal dicts (may be empty if the data is already clean)
    """
    if not records:
        return []

    # Build a DataFrame from the data dicts for easier analysis
    df = pd.DataFrame([r["data"] for r in records])
    # Keep a parallel list of record ids in the same order as df rows
    rec_ids = [r["id"] for r in records]

    proposals = []
    proposals += _propose_trim_whitespace(df, rec_ids)
    proposals += _propose_normalize_category(df, rec_ids, roles)
    proposals += _propose_normalize_dates(df, rec_ids, roles)
    proposals += _propose_drop_exact_duplicates(df, rec_ids)
    proposals += _propose_quarantine_missing_identifier(df, rec_ids, roles)
    proposals += _propose_flag_outliers(df, rec_ids, roles)
    proposals += _propose_missing_value_policy(df, roles)
    return proposals


# ── 2. trim_whitespace ────────────────────────────────────────────────────────

def _propose_trim_whitespace(df: pd.DataFrame, rec_ids: List[int]) -> List[Dict]:
    """
    For every text (object dtype) column, find values that have leading/trailing
    whitespace or double spaces.  Propose stripping them.
    """
    proposals = []
    for col in df.columns:
        series = df[col]
        if series.dtype != object:
            continue  # only text columns

        text = series.dropna().astype(str)
        dirty_mask = text != text.str.strip().str.replace(r" {2,}", " ", regex=True)
        dirty_vals = text[dirty_mask]
        if dirty_vals.empty:
            continue

        examples = []
        for raw in dirty_vals.values[:5]:
            cleaned = re.sub(r" {2,}", " ", raw).strip()
            examples.append({"before": raw, "after": cleaned})

        proposals.append({
            "kind":        "trim_whitespace",
            "target":      col,
            "severity":    "info",
            "description": (
                f"Column '{col}' has {len(dirty_vals)} value(s) with "
                f"leading/trailing or double spaces. Propose trimming."
            ),
            "impact": {
                "rows_affected": int(len(dirty_vals)),
                "examples":      _examples(examples),
            },
            "params": {"column": col},
        })
    return proposals


# ── 1. normalize_category ─────────────────────────────────────────────────────

def _propose_normalize_category(
    df: pd.DataFrame,
    rec_ids: List[int],
    roles: Dict[str, str],
) -> List[Dict]:
    """
    For category columns with inconsistent spellings (case/whitespace),
    propose mapping every variant to the most-frequent spelling.
    """
    proposals = []
    for col, role in roles.items():
        if role != "category":
            continue
        if col not in df.columns:
            continue

        series = df[col].dropna().astype(str)

        # Group by canonical (lowercase + stripped) form
        groups: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for val in series:
            canon = val.strip().lower()
            groups[canon][val] += 1   # count occurrences of each spelling

        # Only care about canonical groups that have more than one distinct spelling
        messy_groups = {
            canon: spellings
            for canon, spellings in groups.items()
            if len(spellings) > 1
        }
        if not messy_groups:
            continue

        # Build the mapping: variant → most-frequent spelling
        mapping = {}
        for canon, spellings in messy_groups.items():
            preferred = max(spellings, key=spellings.get)  # most frequent
            for variant in spellings:
                if variant != preferred:
                    mapping[variant] = preferred

        rows_affected = int(df[col].isin(mapping).sum())

        examples = []
        for variant, preferred in list(mapping.items())[:5]:
            examples.append({"before": variant, "after": preferred})

        proposals.append({
            "kind":        "normalize_category",
            "target":      col,
            "severity":    "warning",
            "description": (
                f"Column '{col}' has {len(messy_groups)} canonical group(s) with "
                f"inconsistent spellings (e.g. case/whitespace). "
                f"Propose normalising to the most-frequent form."
            ),
            "impact": {
                "rows_affected": rows_affected,
                "examples":      _examples(examples),
            },
            "params": {"column": col, "mapping": _safe(mapping)},
        })
    return proposals


# ── 4. normalize_dates ────────────────────────────────────────────────────────

def _propose_normalize_dates(
    df: pd.DataFrame,
    rec_ids: List[int],
    roles: Dict[str, str],
) -> List[Dict]:
    """
    For date columns: parse each value with pandas (dayfirst=False by default).
    Valid ones → propose rewriting as YYYY-MM-DD.
    Invalid ones → propose quarantining the row.
    """
    proposals = []
    for col, role in roles.items():
        if role != "date":
            continue
        if col not in df.columns:
            continue

        series = df[col].astype(str)
        good_examples, bad_examples = [], []
        good_count, bad_count = 0, 0

        for val in series:
            stripped = val.strip()
            if not stripped or stripped.lower() in ("nan", "none", ""):
                continue
            try:
                parsed = pd.to_datetime(stripped, dayfirst=False)
                iso = parsed.strftime("%Y-%m-%d")
                if iso != stripped:
                    good_count += 1
                    if len(good_examples) < 5:
                        good_examples.append({"before": stripped, "after": iso})
            except (ValueError, TypeError):
                bad_count += 1
                if len(bad_examples) < 5:
                    bad_examples.append({"before": stripped, "after": "(quarantined)"})

        if good_count > 0:
            proposals.append({
                "kind":        "normalize_dates",
                "target":      col,
                "severity":    "warning",
                "description": (
                    f"Column '{col}': {good_count} date value(s) can be rewritten "
                    f"to ISO 8601 (YYYY-MM-DD) and {bad_count} are unparseable."
                ),
                "impact": {
                    "rows_affected": good_count + bad_count,
                    "examples":      _examples(good_examples + bad_examples),
                },
                "params": {"column": col, "dayfirst": False},
            })
        elif bad_count > 0:
            # Only bad dates, no valid ones to rewrite — still propose quarantine
            proposals.append({
                "kind":        "normalize_dates",
                "target":      col,
                "severity":    "warning",
                "description": (
                    f"Column '{col}': {bad_count} value(s) cannot be parsed as dates "
                    f"and will be quarantined."
                ),
                "impact": {
                    "rows_affected": bad_count,
                    "examples":      _examples(bad_examples),
                },
                "params": {"column": col, "dayfirst": False},
            })

    return proposals


# ── 3. drop_exact_duplicates ──────────────────────────────────────────────────

def _propose_drop_exact_duplicates(
    df: pd.DataFrame,
    rec_ids: List[int],
) -> List[Dict]:
    """
    Find rows that are exact duplicates (all column values identical).
    Propose marking them as "excluded" (keep first occurrence).
    """
    dup_mask = df.duplicated(keep="first")
    dup_count = int(dup_mask.sum())
    if dup_count == 0:
        return []

    examples = []
    for idx in df[dup_mask].head(5).index:
        row = df.loc[idx].to_dict()
        examples.append({"before": _safe(row), "after": "(excluded)"})

    return [{
        "kind":        "drop_exact_duplicates",
        "target":      "*",
        "severity":    "warning",
        "description": (
            f"Dataset has {dup_count} exact duplicate row(s). "
            f"Propose marking them as 'excluded' (first occurrence kept as active)."
        ),
        "impact": {
            "rows_affected": dup_count,
            "examples":      _examples(examples),
        },
        "params": {},
    }]


# ── 5. quarantine_missing_identifier ─────────────────────────────────────────

def _propose_quarantine_missing_identifier(
    df: pd.DataFrame,
    rec_ids: List[int],
    roles: Dict[str, str],
) -> List[Dict]:
    """
    For every identifier column, rows with a null/empty value → quarantine.
    """
    proposals = []
    for col, role in roles.items():
        if role != "identifier":
            continue
        if col not in df.columns:
            continue

        series = df[col]
        missing_mask = series.isna() | (series.astype(str).str.strip() == "")
        count = int(missing_mask.sum())
        if count == 0:
            continue

        examples = []
        for idx in df[missing_mask].head(5).index:
            row = df.loc[idx].to_dict()
            examples.append({"before": _safe(row), "after": "(quarantined)"})

        proposals.append({
            "kind":        "quarantine_missing_identifier",
            "target":      col,
            "severity":    "critical",
            "description": (
                f"Column '{col}' (role=identifier) has {count} row(s) with "
                f"missing values. These rows will be quarantined."
            ),
            "impact": {
                "rows_affected": count,
                "examples":      _examples(examples),
            },
            "params": {"column": col},
        })
    return proposals


# ── 6. flag_outliers ──────────────────────────────────────────────────────────

def _propose_flag_outliers(
    df: pd.DataFrame,
    rec_ids: List[int],
    roles: Dict[str, str],
) -> List[Dict]:
    """
    IQR rule (1.5×) for numeric columns.
    FLAG ONLY — writes into row metadata, does NOT change value or status.
    """
    proposals = []
    for col, role in roles.items():
        if role != "numeric":
            continue
        if col not in df.columns:
            continue

        numeric = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(numeric) < 4:
            continue   # too few values for IQR to be meaningful

        q1 = numeric.quantile(0.25)
        q3 = numeric.quantile(0.75)
        iqr = q3 - q1
        lower = q1 - 1.5 * iqr
        upper = q3 + 1.5 * iqr

        outlier_mask = (numeric < lower) | (numeric > upper)
        count = int(outlier_mask.sum())
        if count == 0:
            continue

        examples = []
        for val in numeric[outlier_mask].head(5):
            examples.append({"before": float(val), "after": "(flagged as outlier)"})

        proposals.append({
            "kind":        "flag_outliers",
            "target":      col,
            "severity":    "warning",
            "description": (
                f"Column '{col}': {count} value(s) are outside the IQR×1.5 fence "
                f"[{lower:.2f}, {upper:.2f}]. "
                f"These will be FLAGGED in row metadata only (no status change)."
            ),
            "impact": {
                "rows_affected": count,
                "examples":      _examples(examples),
            },
            "params": {
                "column": col,
                "lower":  float(lower),
                "upper":  float(upper),
            },
        })
    return proposals


# ── 7. missing_value_policy ───────────────────────────────────────────────────

def _propose_missing_value_policy(
    df: pd.DataFrame,
    roles: Dict[str, str],
) -> List[Dict]:
    """
    optional columns → "leave as null" (info, no action)
    required text columns → propose fill with "Unknown"
    numeric required → NEVER impute (skipped)
    """
    proposals = []
    for col, role in roles.items():
        if col not in df.columns:
            continue

        series = df[col]
        missing_count = int(series.isna().sum() + (series.astype(str).str.strip() == "").sum())
        if missing_count == 0:
            continue

        if role == "optional":
            proposals.append({
                "kind":        "missing_value_policy",
                "target":      col,
                "severity":    "info",
                "description": (
                    f"Column '{col}' (optional) has {missing_count} missing value(s). "
                    f"Policy: leave as null."
                ),
                "impact": {"rows_affected": missing_count, "examples": []},
                "params": {"column": col, "action": "leave"},
            })

        elif role == "required":
            # Only fill text columns; never impute numeric
            is_text = df[col].dtype == object
            if is_text:
                examples = []
                for idx in df[series.isna()].head(3).index:
                    examples.append({"before": None, "after": "Unknown"})
                proposals.append({
                    "kind":        "missing_value_policy",
                    "target":      col,
                    "severity":    "warning",
                    "description": (
                        f"Column '{col}' (required text) has {missing_count} missing "
                        f"value(s). Propose filling with 'Unknown'."
                    ),
                    "impact": {
                        "rows_affected": missing_count,
                        "examples":      _examples(examples),
                    },
                    "params": {"column": col, "action": "fill_unknown"},
                })

    return proposals


# ═══════════════════════════════════════════════════════════════════════════════
# UNSTRUCTURED PROPOSALS  (input: list of document dicts)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_unstructured_proposals(documents: List[Dict]) -> List[Dict]:
    """
    Run all unstructured proposal generators on a list of document dicts.

    Args:
        documents: list of dicts with at least:
            {"id": int, "content": str, "content_hash": str,
             "quality": dict, "fetched_at": str, "word_count": int}

    Returns:
        list of proposal dicts
    """
    if not documents:
        return []

    proposals = []
    proposals += _propose_exclude_duplicate_documents(documents)
    proposals += _propose_quarantine_injection_suspects(documents)
    proposals += _propose_flag_stale_documents(documents)
    proposals += _propose_quarantine_short_content(documents)
    return proposals


# ── 8. exclude_duplicate_documents ───────────────────────────────────────────

def _propose_exclude_duplicate_documents(documents: List[Dict]) -> List[Dict]:
    """
    Exact + near-duplicate documents.  Keep earliest fetched, mark rest "excluded".
    """
    proposals = []

    # ── Exact duplicates ──────────────────────────────────────────────────────
    exact = check_exact_duplicate(documents)
    for group in exact["detail"]["duplicate_groups"]:
        # Sort by id (proxy for fetch order) — keep the first one
        sorted_ids = sorted(group["doc_ids"])
        to_exclude = sorted_ids[1:]   # everything after the first

        examples = [{"before": f"doc_id={did}", "after": "(excluded)"} for did in to_exclude[:5]]

        proposals.append({
            "kind":        "exclude_duplicate_documents",
            "target":      "*",
            "severity":    "warning",
            "description": (
                f"Documents {sorted_ids} share the same content hash (exact duplicates). "
                f"Keep doc_id={sorted_ids[0]}, mark {to_exclude} as 'excluded'."
            ),
            "impact": {
                "rows_affected": len(to_exclude),
                "examples":      _examples(examples),
            },
            "params": {
                "kind":       "exact",
                "keep_id":    sorted_ids[0],
                "exclude_ids": to_exclude,
            },
        })

    # ── Near-duplicates ───────────────────────────────────────────────────────
    near = check_near_duplicate(documents)
    for pair in near["detail"]["near_duplicate_pairs"]:
        sorted_ids = sorted(pair["doc_ids"])
        to_exclude = [sorted_ids[1]]

        proposals.append({
            "kind":        "exclude_duplicate_documents",
            "target":      "*",
            "severity":    "info",
            "description": (
                f"Documents {sorted_ids} are near-duplicates "
                f"(Jaccard={pair['similarity']:.2f}). "
                f"Keep doc_id={sorted_ids[0]}, mark doc_id={sorted_ids[1]} as 'excluded'."
            ),
            "impact": {
                "rows_affected": 1,
                "examples":      [{"before": f"doc_id={sorted_ids[1]}", "after": "(excluded)"}],
            },
            "params": {
                "kind":        "near",
                "keep_id":     sorted_ids[0],
                "exclude_ids": to_exclude,
                "similarity":  pair["similarity"],
            },
        })

    return proposals


# ── 9. quarantine_injection_suspects ─────────────────────────────────────────

def _propose_quarantine_injection_suspects(documents: List[Dict]) -> List[Dict]:
    """
    Documents flagged by the Phase 2 injection heuristic → quarantine.
    """
    proposals = []
    for doc in documents:
        q = doc.get("quality") or {}
        inj = q.get("injection_suspect", {})
        if not inj.get("flag"):
            continue

        proposals.append({
            "kind":        "quarantine_injection_suspects",
            "target":      "*",
            "severity":    "critical",
            "description": (
                f"Document id={doc['id']} contains possible prompt-injection patterns "
                f"and will be quarantined."
            ),
            "impact": {
                "rows_affected": 1,
                "examples": [{"before": f"doc_id={doc['id']}", "after": "(quarantined)"}],
            },
            "params": {"doc_ids": [doc["id"]]},
        })
    return proposals


# ── 10. flag_stale_documents ──────────────────────────────────────────────────

def _propose_flag_stale_documents(documents: List[Dict]) -> List[Dict]:
    """
    Documents with freshness 'stale' → FLAG ONLY in metadata (no status change).
    """
    stale_ids = []
    for doc in documents:
        q = doc.get("quality") or {}
        freshness = q.get("freshness", {})
        if freshness.get("flag") == "stale":
            stale_ids.append(doc["id"])

    if not stale_ids:
        return []

    return [{
        "kind":        "flag_stale_documents",
        "target":      "*",
        "severity":    "info",
        "description": (
            f"{len(stale_ids)} document(s) are stale (published >= 730 days ago). "
            f"These will be FLAGGED in metadata only (no status change)."
        ),
        "impact": {
            "rows_affected": len(stale_ids),
            "examples": [{"before": f"doc_id={did}", "after": "(stale flag in metadata)"} for did in stale_ids[:5]],
        },
        "params": {"doc_ids": stale_ids},
    }]


# ── 11. quarantine_short_content ──────────────────────────────────────────────

def _propose_quarantine_short_content(documents: List[Dict]) -> List[Dict]:
    """
    Documents with fewer than 30 words → quarantine.
    """
    short_ids = []
    for doc in documents:
        wc = doc.get("word_count") or 0
        if wc < 30:
            short_ids.append(doc["id"])

    if not short_ids:
        return []

    return [{
        "kind":        "quarantine_short_content",
        "target":      "*",
        "severity":    "warning",
        "description": (
            f"{len(short_ids)} document(s) have fewer than 30 words and will be quarantined."
        ),
        "impact": {
            "rows_affected": len(short_ids),
            "examples": [{"before": f"doc_id={did}", "after": "(quarantined)"} for did in short_ids[:5]],
        },
        "params": {"doc_ids": short_ids},
    }]
