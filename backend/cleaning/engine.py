"""
cleaning/engine.py — Deterministic transform engine for Phase 3.

Reads approved proposals for a source, applies them in a fixed order,
creates version N+1 rows, writes transform_log entries, and re-profiles
only the 'active' rows of the new version.

Apply order (deterministic)
---------------------------
  1. trim_whitespace
  2. normalize_category
  3. normalize_dates
  4. missing_value_policy
  5. drop_exact_duplicates
  6. quarantine_missing_identifier
  7. flag_outliers
  8. exclude_duplicate_documents
  9. quarantine_injection_suspects
 10. quarantine_short_content
 11. flag_stale_documents

Rules
-----
- Version 1 rows are NEVER modified (not touched by this module).
- New records = copies of base_version rows, then transforms applied on top.
- Everything runs inside a single SQLAlchemy transaction.
  On any exception the transaction is rolled back; no partial version exists.
- Applying with zero approved proposals raises ValueError (returned as 400).
"""

import json
import re
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
from sqlalchemy.orm import Session

from db import (
    Record, Document, Source, SourceVersion,
    CleaningProposal, TransformLog, safe_json,
)
from profiler.structured_profiler import profile_structured
from profiler.text_quality import run_all_checks, compute_content_hash


# ── Apply order for proposals ─────────────────────────────────────────────────

STRUCTURED_ORDER = [
    "trim_whitespace",
    "normalize_category",
    "normalize_dates",
    "missing_value_policy",
    "drop_exact_duplicates",
    "quarantine_missing_identifier",
    "flag_outliers",
]

UNSTRUCTURED_ORDER = [
    "exclude_duplicate_documents",
    "quarantine_injection_suspects",
    "quarantine_short_content",
    "flag_stale_documents",
]


# ═══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ═══════════════════════════════════════════════════════════════════════════════

def apply_proposals(source_id: int, db: Session) -> Dict:
    """
    Apply all approved proposals for source_id.

    Returns a summary dict:
        {
            "new_version":     int,
            "rows_active":     int,
            "rows_excluded":   int,
            "rows_quarantined": int,
            "before_after_profile": {
                "v_from": <old profile>,
                "v_to":   <new profile>,
            }
        }

    Raises:
        ValueError: if no approved proposals exist, or source not found.
    """
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        raise ValueError(f"Source {source_id} not found.")

    # Get all approved proposals
    proposals = (
        db.query(CleaningProposal)
        .filter(
            CleaningProposal.source_id == source_id,
            CleaningProposal.status == "approved",
        )
        .all()
    )
    if not proposals:
        raise ValueError(
            "No approved proposals found. Approve at least one proposal before applying."
        )

    # Determine the base version (the highest version_no with actual data)
    base_version = proposals[0].base_version
    new_version = base_version + 1

    # Check new version doesn't already exist
    existing = (
        db.query(SourceVersion)
        .filter(
            SourceVersion.source_id == source_id,
            SourceVersion.version_no == new_version,
        )
        .first()
    )
    if existing:
        raise ValueError(
            f"Version {new_version} already exists for source {source_id}."
        )

    # ── Run everything in one transaction ────────────────────────────────────
    try:
        if source.source_type in ("file",) and _has_records(source_id, base_version, db):
            result = _apply_structured(
                source_id, base_version, new_version, proposals, db
            )
        else:
            result = _apply_unstructured(
                source_id, base_version, new_version, proposals, db
            )
        db.commit()
    except Exception:
        db.rollback()
        raise

    return result


def _has_records(source_id: int, version_no: int, db: Session) -> bool:
    """Return True if this source has records (structured data)."""
    return (
        db.query(Record)
        .filter(Record.source_id == source_id, Record.version_no == version_no)
        .first()
    ) is not None


# ═══════════════════════════════════════════════════════════════════════════════
# STRUCTURED  (records table)
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_structured(
    source_id: int,
    base_version: int,
    new_version: int,
    proposals: List,
    db: Session,
) -> Dict:
    """Copy base rows to new_version, apply transforms, log, re-profile."""

    # ── 1. Load base version rows ─────────────────────────────────────────────
    base_rows = (
        db.query(Record)
        .filter(
            Record.source_id == source_id,
            Record.version_no == base_version,
        )
        .order_by(Record.row_index)
        .all()
    )

    # Create mutable working copies: list of dicts with extra control keys
    # "_status" and "_meta" are used internally; not stored in "data"
    working = []
    for r in base_rows:
        working.append({
            "_rec_id":    r.id,        # original DB id (for references only)
            "_row_index": r.row_index,
            "_status":    "active",
            "_meta":      dict(r.metadata_ or {}),
            "data":       dict(r.data or {}),
        })

    # ── 2. Sort proposals by apply order ──────────────────────────────────────
    def _order_key(p):
        try:
            return STRUCTURED_ORDER.index(p.kind)
        except ValueError:
            return 99   # unknown kinds go last

    sorted_proposals = sorted(proposals, key=_order_key)

    # ── 3. Apply each proposal and collect transform logs ─────────────────────
    log_entries = []

    for prop in sorted_proposals:
        if prop.kind not in STRUCTURED_ORDER:
            continue   # unstructured proposal in a structured source — skip
        params = prop.params or {}

        before_snap = _snapshot(working)
        _apply_structured_proposal(working, prop.kind, params)
        after_snap = _snapshot(working)

        # Compute rows_affected by comparing snapshots
        changed = _diff_count(before_snap, after_snap)
        sample  = _diff_sample(before_snap, after_snap, n=5)

        log_entries.append((prop, changed, sample))

    # ── 4. Persist new version rows ───────────────────────────────────────────
    new_records = []
    for w in working:
        rec = Record(
            source_id  = source_id,
            row_index  = w["_row_index"],
            data       = safe_json(w["data"]),
            version_no = new_version,
            status     = w["_status"],
            metadata_  = safe_json(w["_meta"]),
        )
        db.add(rec)
        new_records.append(rec)
    db.flush()  # assign ids

    # ── 5. Mark proposals as applied; write transform_log ─────────────────────
    now = datetime.utcnow()
    for prop, rows_affected, sample in log_entries:
        prop.status     = "applied"
        prop.decided_at = now

        log = TransformLog(
            proposal_id   = prop.id,
            version_from  = base_version,
            version_to    = new_version,
            rows_affected = rows_affected,
            sample        = safe_json(sample),
        )
        db.add(log)

    # ── 6. Re-profile active rows ──────────────────────────────────────────────
    active_data = [w["data"] for w in working if w["_status"] == "active"]
    profile = {}
    if active_data:
        active_df = pd.DataFrame(active_data)
        profile = safe_json(profile_structured(active_df))

    # ── 7. Write source_versions row ──────────────────────────────────────────
    base_sv = (
        db.query(SourceVersion)
        .filter(
            SourceVersion.source_id == source_id,
            SourceVersion.version_no == base_version,
        )
        .first()
    )
    sv = SourceVersion(
        source_id      = source_id,
        version_no     = new_version,
        parent_version = base_version,
        note           = f"Applied {len(sorted_proposals)} proposal(s)",
        profile        = profile,
    )
    db.add(sv)

    # ── 8. Build counts ───────────────────────────────────────────────────────
    status_counts = {"active": 0, "excluded": 0, "quarantined": 0}
    for w in working:
        status_counts[w["_status"]] = status_counts.get(w["_status"], 0) + 1

    return {
        "new_version":      new_version,
        "rows_active":      status_counts["active"],
        "rows_excluded":    status_counts["excluded"],
        "rows_quarantined": status_counts["quarantined"],
        "before_after_profile": {
            "v_from": (base_sv.profile or {}) if base_sv else {},
            "v_to":   profile,
        },
    }


def _apply_structured_proposal(working: List[Dict], kind: str, params: Dict):
    """Mutate the working list in-place for a single proposal kind."""

    col = params.get("column")

    if kind == "trim_whitespace" and col:
        for w in working:
            if w["_status"] != "active":
                continue
            val = w["data"].get(col)
            if isinstance(val, str):
                cleaned = re.sub(r" {2,}", " ", val).strip()
                w["data"][col] = cleaned

    elif kind == "normalize_category" and col:
        mapping = params.get("mapping", {})
        for w in working:
            if w["_status"] != "active":
                continue
            val = w["data"].get(col)
            if val in mapping:
                w["data"][col] = mapping[val]

    elif kind == "normalize_dates" and col:
        dayfirst = params.get("dayfirst", False)
        for w in working:
            if w["_status"] != "active":
                continue
            val = str(w["data"].get(col, "") or "").strip()
            if not val or val.lower() in ("nan", "none", ""):
                continue
            try:
                parsed = pd.to_datetime(val, dayfirst=dayfirst)
                w["data"][col] = parsed.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                # Unparseable date → quarantine
                w["_status"] = "quarantined"
                w["_meta"]["quarantine_reason"] = f"Unparseable date in column '{col}': {val!r}"

    elif kind == "missing_value_policy" and col:
        action = params.get("action", "leave")
        if action == "fill_unknown":
            for w in working:
                if w["_status"] != "active":
                    continue
                val = w["data"].get(col)
                if val is None or (isinstance(val, float) and pd.isna(val)) or str(val).strip() == "":
                    w["data"][col] = "Unknown"

    elif kind == "drop_exact_duplicates":
        seen = set()
        for w in working:
            if w["_status"] != "active":
                continue
            # Freeze the data dict as a canonical key
            key = json.dumps(w["data"], sort_keys=True, default=str)
            if key in seen:
                w["_status"] = "excluded"
                w["_meta"]["exclude_reason"] = "exact_duplicate"
            else:
                seen.add(key)

    elif kind == "quarantine_missing_identifier" and col:
        for w in working:
            if w["_status"] != "active":
                continue
            val = w["data"].get(col)
            if val is None or (isinstance(val, float) and pd.isna(val)) or str(val).strip() == "":
                w["_status"] = "quarantined"
                w["_meta"]["quarantine_reason"] = f"Missing identifier: '{col}'"

    elif kind == "flag_outliers" and col:
        lower = params.get("lower")
        upper = params.get("upper")
        for w in working:
            if w["_status"] != "active":
                continue
            val = w["data"].get(col)
            try:
                fval = float(val)
                if (lower is not None and fval < lower) or (upper is not None and fval > upper):
                    if "outlier_flags" not in w["_meta"]:
                        w["_meta"]["outlier_flags"] = []
                    w["_meta"]["outlier_flags"].append({"column": col, "value": fval})
            except (TypeError, ValueError):
                pass


# ─── helpers for structured ───────────────────────────────────────────────────

def _snapshot(working: List[Dict]) -> List[Dict]:
    """Deep-copy essential fields from working list for before/after comparison."""
    return [
        {"_status": w["_status"], "data": dict(w["data"]), "_meta": dict(w["_meta"])}
        for w in working
    ]


def _diff_count(before: List[Dict], after: List[Dict]) -> int:
    """Count rows where status or data changed."""
    count = 0
    for b, a in zip(before, after):
        if b["_status"] != a["_status"] or b["data"] != a["data"]:
            count += 1
    return count


def _diff_sample(before: List[Dict], after: List[Dict], n: int = 5) -> List[Dict]:
    """Return up to n rows where something changed."""
    samples = []
    for b, a in zip(before, after):
        if b["_status"] != a["_status"] or b["data"] != a["data"]:
            samples.append({"before": safe_json(b), "after": safe_json(a)})
            if len(samples) >= n:
                break
    return samples


# ═══════════════════════════════════════════════════════════════════════════════
# UNSTRUCTURED  (documents table)
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_unstructured(
    source_id: int,
    base_version: int,
    new_version: int,
    proposals: List,
    db: Session,
) -> Dict:
    """Copy base documents to new_version, apply transforms, log, re-profile."""

    base_docs = (
        db.query(Document)
        .filter(
            Document.source_id == source_id,
            Document.version_no == base_version,
        )
        .order_by(Document.id)
        .all()
    )

    # Working list of mutable doc states
    working = []
    for d in base_docs:
        working.append({
            "_doc_id":  d.id,
            "_status":  "active",
            "_meta":    dict(d.metadata_ or {}),
            "content":  d.content or "",
            "quality":  dict(d.quality or {}),
        })
    id_to_idx = {w["_doc_id"]: i for i, w in enumerate(working)}

    def _order_key(p):
        try:
            return UNSTRUCTURED_ORDER.index(p.kind)
        except ValueError:
            return 99

    sorted_proposals = sorted(proposals, key=_order_key)
    log_entries = []

    for prop in sorted_proposals:
        if prop.kind not in UNSTRUCTURED_ORDER:
            continue
        params = prop.params or {}

        before_snap = [(w["_status"], dict(w["_meta"])) for w in working]
        _apply_unstructured_proposal(working, id_to_idx, prop.kind, params)
        after_snap = [(w["_status"], dict(w["_meta"])) for w in working]

        changed = sum(1 for b, a in zip(before_snap, after_snap) if b != a)
        sample = []
        for w, b, a in zip(working, before_snap, after_snap):
            if b != a and len(sample) < 5:
                sample.append({"doc_id": w["_doc_id"], "before_status": b[0], "after_status": a[0]})

        log_entries.append((prop, changed, safe_json(sample)))

    # ── Persist new version documents ─────────────────────────────────────────
    for w in working:
        # Find the original doc to copy metadata
        orig = next((d for d in base_docs if d.id == w["_doc_id"]), None)
        if not orig:
            continue

        new_meta = dict(orig.metadata_ or {})
        new_meta.update(w["_meta"])   # overlay any flags added by transforms

        doc = Document(
            source_id             = source_id,
            modality              = orig.modality,
            title                 = orig.title,
            url                   = orig.url,
            page_number           = orig.page_number,
            content               = orig.content,
            content_hash          = orig.content_hash,
            word_count            = orig.word_count,
            extraction_confidence = orig.extraction_confidence,
            published_at          = orig.published_at,
            fetched_at            = orig.fetched_at,
            metadata_             = safe_json(new_meta),
            quality               = safe_json(orig.quality or {}),
            version_no            = new_version,
            status                = w["_status"],
        )
        db.add(doc)
    db.flush()

    # ── Mark proposals applied + write logs ───────────────────────────────────
    now = datetime.utcnow()
    for prop, rows_affected, sample in log_entries:
        prop.status     = "applied"
        prop.decided_at = now
        log = TransformLog(
            proposal_id   = prop.id,
            version_from  = base_version,
            version_to    = new_version,
            rows_affected = rows_affected,
            sample        = safe_json(sample),
        )
        db.add(log)

    # ── Re-profile active documents ────────────────────────────────────────────
    active_docs = [w for w in working if w["_status"] == "active"]
    profile = safe_json({
        "document_count": len(active_docs),
        "total_words":    sum(
            d.word_count or 0 for d in base_docs
            if any(w["_doc_id"] == d.id and w["_status"] == "active" for w in working)
        ),
    })

    # ── Write source_versions ──────────────────────────────────────────────────
    base_sv = (
        db.query(SourceVersion)
        .filter(
            SourceVersion.source_id == source_id,
            SourceVersion.version_no == base_version,
        )
        .first()
    )
    sv = SourceVersion(
        source_id      = source_id,
        version_no     = new_version,
        parent_version = base_version,
        note           = f"Applied {len(sorted_proposals)} proposal(s)",
        profile        = profile,
    )
    db.add(sv)

    status_counts = {"active": 0, "excluded": 0, "quarantined": 0}
    for w in working:
        status_counts[w["_status"]] = status_counts.get(w["_status"], 0) + 1

    return {
        "new_version":      new_version,
        "rows_active":      status_counts["active"],
        "rows_excluded":    status_counts["excluded"],
        "rows_quarantined": status_counts["quarantined"],
        "before_after_profile": {
            "v_from": (base_sv.profile or {}) if base_sv else {},
            "v_to":   profile,
        },
    }


def _apply_unstructured_proposal(
    working: List[Dict],
    id_to_idx: Dict[int, int],
    kind: str,
    params: Dict,
):
    """Mutate the working list in-place for a single unstructured proposal."""

    if kind == "exclude_duplicate_documents":
        for did in params.get("exclude_ids", []):
            idx = id_to_idx.get(did)
            if idx is not None:
                working[idx]["_status"] = "excluded"
                working[idx]["_meta"]["exclude_reason"] = f"duplicate of doc_id={params.get('keep_id')}"

    elif kind == "quarantine_injection_suspects":
        for did in params.get("doc_ids", []):
            idx = id_to_idx.get(did)
            if idx is not None:
                working[idx]["_status"] = "quarantined"
                working[idx]["_meta"]["quarantine_reason"] = "injection_suspect"

    elif kind == "quarantine_short_content":
        for did in params.get("doc_ids", []):
            idx = id_to_idx.get(did)
            if idx is not None:
                working[idx]["_status"] = "quarantined"
                working[idx]["_meta"]["quarantine_reason"] = "short_content"

    elif kind == "flag_stale_documents":
        for did in params.get("doc_ids", []):
            idx = id_to_idx.get(did)
            if idx is not None:
                working[idx]["_meta"]["stale_flag"] = True
                # Status unchanged — flag only
