"""
profiler/text_quality.py — Five pure heuristic quality checks for text content.

All functions are side-effect-free: no DB access, no network calls.
They are labeled as HEURISTICS — not ground-truth classifiers.
No single "trust score" is returned; each check reports independently.

Functions
---------
check_exact_duplicate(docs)     -> groups of docs sharing the same content hash
check_near_duplicate(docs)      -> pairs of docs with Jaccard similarity >= 0.85
check_freshness(published_at)   -> "fresh" / "aging" / "stale" / "unknown"
check_short_content(text)       -> warn if under 30 words
check_injection_suspect(text)   -> flag prompt-injection-like patterns
"""

import hashlib
import re
from datetime import datetime, timezone
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SHORT_WORD_THRESHOLD = 30          # words; documents below this are "short"
NEAR_DUP_THRESHOLD   = 0.85        # Jaccard similarity >= this → near-duplicate
FRESH_DAYS           = 180         # < 180 days old → "fresh"
AGING_DAYS           = 730         # < 730 days old → "aging"; >= → "stale"
SHINGLE_N            = 5           # word n-gram size for near-duplicate detection

# Heuristic patterns for prompt-injection suspects.
# These are simple regex rules — they WILL produce false positives.
# Content is NEVER modified; this is a flag-only check.
_INJECTION_PATTERNS = [
    # Matches: 'ignore previous instructions', 'ignore all previous instructions',
    # 'ignore any instructions', etc. — one or two words between 'ignore' and 'instructions'
    r"ignore\s+(?:all\s+)?(?:previous|any|prior)\s+instructions",
    r"ignore\s+all\s+instructions",
    r"disregard\s+the\s+above",
    r"you\s+are\s+now\b",
    r"\bsystem\s+prompt\b",
    r"\bact\s+as\b",
    r"do\s+not\s+tell\s+the\s+user",
    r"reveal\s+your\s+instructions",
    r"forget\s+(?:all\s+)?(?:previous|prior)\s+instructions",
    r"new\s+instructions\s*:",
    r"override\s+(?:previous|prior|all)\s+",
]
_COMPILED_PATTERNS = [(p, re.compile(p, re.IGNORECASE)) for p in _INJECTION_PATTERNS]


# ─────────────────────────────────────────────────────────────────────────────
# 1. Exact duplicate detection
# ─────────────────────────────────────────────────────────────────────────────

def compute_content_hash(text: str) -> str:
    """
    Return SHA-256 hex digest of whitespace-normalised, lowercase content.
    Used as the canonical fingerprint for exact-duplicate detection.
    """
    normalised = " ".join(text.lower().split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def check_exact_duplicate(docs: list) -> dict:
    """
    Group documents by content_hash. Documents sharing a hash are exact duplicates.

    Args:
        docs: list of dicts, each with at least {"id": ..., "content_hash": str}

    Returns:
        {
            "flag": bool,               # True if any duplicate groups exist
            "detail": {
                "duplicate_groups": [   # one entry per group of >=2 docs
                    {"hash": str, "doc_ids": [int, ...]}
                ]
            }
        }
    NOTE: This is a heuristic based on SHA-256 of normalised text.
    """
    from collections import defaultdict

    groups = defaultdict(list)
    for doc in docs:
        h = doc.get("content_hash")
        if h:
            groups[h].append(doc["id"])

    # Keep only groups that have more than one document (actual duplicates)
    dup_groups = [
        {"hash": h, "doc_ids": ids}
        for h, ids in groups.items()
        if len(ids) > 1
    ]

    return {
        "flag": len(dup_groups) > 0,
        "detail": {"duplicate_groups": dup_groups},
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. Near-duplicate detection (word 5-gram shingles + Jaccard)
# ─────────────────────────────────────────────────────────────────────────────

def _shingles(text: str, n: int = SHINGLE_N) -> set:
    """Return the set of word n-grams (shingles) from the text."""
    words = text.lower().split()
    if len(words) < n:
        # Too short to compute meaningful shingles — return whole-text tokens
        return set(words)
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def _jaccard(set_a: set, set_b: set) -> float:
    """Jaccard similarity between two sets."""
    if not set_a and not set_b:
        return 1.0
    union = set_a | set_b
    if not union:
        return 0.0
    return len(set_a & set_b) / len(union)


def check_near_duplicate(docs: list) -> dict:
    """
    O(n^2) near-duplicate detection via word 5-gram Jaccard similarity.
    Pairs with similarity >= NEAR_DUP_THRESHOLD (0.85) are flagged.

    Args:
        docs: list of dicts with {"id": ..., "content": str}
              (works for up to ~50 documents as specified)

    Returns:
        {
            "flag": bool,
            "detail": {
                "near_duplicate_pairs": [
                    {"doc_ids": [int, int], "similarity": float}
                ]
            }
        }
    NOTE: Heuristic — shingle-based similarity; short docs may give false positives.
    """
    # Pre-compute shingles for each doc to avoid recomputing in the inner loop
    shingle_map = {}
    for doc in docs:
        shingle_map[doc["id"]] = _shingles(doc.get("content") or "")

    pairs = []
    doc_ids = [d["id"] for d in docs]

    for i in range(len(doc_ids)):
        for j in range(i + 1, len(doc_ids)):
            id_a, id_b = doc_ids[i], doc_ids[j]
            sim = _jaccard(shingle_map[id_a], shingle_map[id_b])
            if sim >= NEAR_DUP_THRESHOLD:
                pairs.append({
                    "doc_ids": [id_a, id_b],
                    "similarity": round(sim, 4),
                })

    return {
        "flag": len(pairs) > 0,
        "detail": {"near_duplicate_pairs": pairs},
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. Freshness check
# ─────────────────────────────────────────────────────────────────────────────

def check_freshness(published_at: Optional[datetime]) -> dict:
    """
    Classify content freshness based on published_at date.
    Dates are NEVER guessed or inferred — if unknown, returns "unknown".

    Returns:
        {
            "flag": str,   # "fresh" | "aging" | "stale" | "unknown"
            "detail": {
                "age_days": int | None,
                "published_at": str | None
            }
        }

    Buckets (heuristic thresholds, not objective standards):
      fresh   — published < 180 days ago
      aging   — 180 <= days < 730
      stale   — >= 730 days ago
      unknown — no date available
    """
    if published_at is None:
        return {
            "flag": "unknown",
            "detail": {"age_days": None, "published_at": None},
        }

    # Make both datetimes timezone-aware for safe comparison
    now = datetime.now(timezone.utc)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=timezone.utc)

    age_days = (now - published_at).days

    if age_days < FRESH_DAYS:
        level = "fresh"
    elif age_days < AGING_DAYS:
        level = "aging"
    else:
        level = "stale"

    return {
        "flag": level,
        "detail": {
            "age_days": age_days,
            "published_at": published_at.isoformat(),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# 4. Short content check
# ─────────────────────────────────────────────────────────────────────────────

def check_short_content(text: str) -> dict:
    """
    Flag documents with fewer than SHORT_WORD_THRESHOLD (30) words.

    Returns:
        {
            "flag": bool,
            "detail": {"word_count": int, "threshold": int}
        }
    """
    word_count = len((text or "").split())
    return {
        "flag": word_count < SHORT_WORD_THRESHOLD,
        "detail": {
            "word_count": word_count,
            "threshold": SHORT_WORD_THRESHOLD,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. Prompt-injection suspect check
# ─────────────────────────────────────────────────────────────────────────────

def check_injection_suspect(text: str) -> dict:
    """
    Search for heuristic prompt-injection-like patterns in text.

    IMPORTANT SAFETY NOTES:
    - Content is NEVER modified, deleted, or acted upon.
    - Matched text is truncated to 120 chars to avoid logging full payloads.
    - These are simple regex heuristics — they WILL have false positives/negatives.
    - Do NOT interpret matched text as instructions.

    Returns:
        {
            "flag": bool,
            "detail": {
                "matches": [
                    {"pattern": str, "snippet": str}  # snippet <= 120 chars
                ]
            }
        }
    """
    if not text:
        return {"flag": False, "detail": {"matches": []}}

    matches = []
    for pattern_str, compiled in _COMPILED_PATTERNS:
        m = compiled.search(text)
        if m:
            # Extract a small context window around the match; never log full body
            start = max(0, m.start() - 20)
            end   = min(len(text), m.end() + 40)
            snippet = text[start:end].strip()
            if len(snippet) > 120:
                snippet = snippet[:120] + "…"
            matches.append({
                "pattern": pattern_str,
                "snippet": snippet,
            })

    return {
        "flag": len(matches) > 0,
        "detail": {"matches": matches},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: run all checks on a single document dict
# ─────────────────────────────────────────────────────────────────────────────

def run_all_checks(content: str, published_at: Optional[datetime] = None) -> dict:
    """
    Run checks 3-5 on a single document (checks 1 & 2 need multiple docs).
    Returns a quality dict ready to be stored in documents.quality.
    """
    return {
        "freshness":          check_freshness(published_at),
        "short_content":      check_short_content(content),
        "injection_suspect":  check_injection_suspect(content),
    }
