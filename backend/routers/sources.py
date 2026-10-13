"""
routers/sources.py — Source listing and quality reporting endpoints.

GET /sources                       — list all sources with document/record counts
GET /sources/{source_id}/documents — list documents (no content field)
GET /sources/{source_id}/quality   — aggregate quality report
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from db import get_db, Source, Document, Record, safe_json
from profiler.text_quality import (
    check_exact_duplicate,
    check_near_duplicate,
    check_freshness,
)
from datetime import datetime, timezone

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# GET /sources
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/sources", summary="List all sources with document and record counts")
def list_sources(db: Session = Depends(get_db)):
    """
    Return all sources, each with a count of associated documents and records.
    """
    sources = db.query(Source).order_by(Source.id.desc()).all()
    result = []

    for s in sources:
        doc_count = db.query(func.count(Document.id)).filter(
            Document.source_id == s.id
        ).scalar()
        rec_count = db.query(func.count(Record.id)).filter(
            Record.source_id == s.id
        ).scalar()

        result.append({
            "id":            s.id,
            "source_type":   s.source_type,
            "name":          s.name,
            "origin":        s.origin,
            "file_id":       s.file_id,
            "created_at":    str(s.created_at),
            "document_count": doc_count,
            "record_count":   rec_count,
        })

    return result


# ─────────────────────────────────────────────────────────────────────────────
# GET /sources/{source_id}/documents
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/sources/{source_id}/documents",
    summary="List documents for a source (no content body)",
)
def list_documents(source_id: int, db: Session = Depends(get_db)):
    """
    Return document metadata for a source.
    The full content field is omitted — use GET /documents/{id} for that.
    """
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail=f"Source {source_id} not found.")

    docs = (
        db.query(Document)
        .filter(Document.source_id == source_id)
        .order_by(Document.id)
        .all()
    )

    return [
        {
            "id":           d.id,
            "modality":     d.modality,
            "title":        d.title,
            "url":          d.url,
            "page_number":  d.page_number,
            "word_count":   d.word_count,
            "content_hash": d.content_hash,
            "published_at": str(d.published_at) if d.published_at else None,
            "fetched_at":   str(d.fetched_at),
            # Quality flags only — no matches/snippets in the list view
            "quality_flags": _summarise_quality(d.quality),
        }
        for d in docs
    ]


# ─────────────────────────────────────────────────────────────────────────────
# GET /sources/{source_id}/quality
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/sources/{source_id}/quality",
    summary="Aggregate quality report for a source",
)
def source_quality(source_id: int, db: Session = Depends(get_db)):
    """
    Compute and return an aggregate quality report for all documents in a source.

    Includes:
      - document_count, total_words
      - duplicate_groups (exact), near_duplicate_pairs
      - freshness_distribution
      - short_documents list
      - injection_suspects list
      - issues: human-readable list with severity (critical / warning / info)

    All quality signals are heuristics — labeled as such in the output.
    """
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail=f"Source {source_id} not found.")

    docs = (
        db.query(Document)
        .filter(Document.source_id == source_id)
        .order_by(Document.id)
        .all()
    )

    if not docs:
        return {
            "source_id":       source_id,
            "document_count":  0,
            "total_words":     0,
            "note":            "No documents found for this source.",
            "issues":          [],
        }

    # ── Basic stats ───────────────────────────────────────────────────────────
    document_count = len(docs)
    total_words = sum((d.word_count or 0) for d in docs)

    # ── Exact duplicate check ─────────────────────────────────────────────────
    doc_list = [
        {"id": d.id, "content": d.content or "", "content_hash": d.content_hash or ""}
        for d in docs
    ]
    exact_dup = check_exact_duplicate(doc_list)
    dup_groups = exact_dup["detail"]["duplicate_groups"]

    # ── Near-duplicate check ──────────────────────────────────────────────────
    near_dup = check_near_duplicate(doc_list)
    near_pairs = near_dup["detail"]["near_duplicate_pairs"]

    # ── Freshness distribution ────────────────────────────────────────────────
    freshness_dist = {"fresh": 0, "aging": 0, "stale": 0, "unknown": 0}
    for d in docs:
        q = d.quality or {}
        level = q.get("freshness", {}).get("flag", "unknown")
        freshness_dist[level] = freshness_dist.get(level, 0) + 1

    # ── Short documents ───────────────────────────────────────────────────────
    short_docs = [
        {"id": d.id, "title": d.title, "url": d.url, "word_count": d.word_count}
        for d in docs
        if (d.quality or {}).get("short_content", {}).get("flag")
    ]

    # ── Injection suspects ────────────────────────────────────────────────────
    injection_docs = []
    for d in docs:
        q = d.quality or {}
        inj = q.get("injection_suspect", {})
        if inj.get("flag"):
            injection_docs.append({
                "id":      d.id,
                "title":   d.title,
                "url":     d.url,
                "matches": inj.get("detail", {}).get("matches", []),
            })

    # ── Build human-readable issues list ─────────────────────────────────────
    issues = []

    if dup_groups:
        issues.append({
            "severity": "critical",
            "message":  (
                f"[Heuristic] {len(dup_groups)} exact-duplicate group(s) detected "
                f"(SHA-256 of normalised text matches)."
            ),
        })

    if near_pairs:
        issues.append({
            "severity": "warning",
            "message":  (
                f"[Heuristic] {len(near_pairs)} near-duplicate pair(s) detected "
                f"(word 5-gram Jaccard >= 0.85)."
            ),
        })

    stale_count = freshness_dist.get("stale", 0)
    if stale_count:
        issues.append({
            "severity": "warning",
            "message":  (
                f"[Heuristic] {stale_count} document(s) are stale "
                f"(published >= 730 days ago)."
            ),
        })

    if freshness_dist.get("unknown", 0) == document_count:
        issues.append({
            "severity": "info",
            "message":  "[Heuristic] No publication dates found — freshness cannot be determined.",
        })

    if short_docs:
        issues.append({
            "severity": "warning",
            "message":  f"[Heuristic] {len(short_docs)} document(s) have fewer than 30 words.",
        })

    if injection_docs:
        issues.append({
            "severity": "critical",
            "message":  (
                f"[Heuristic] {len(injection_docs)} document(s) contain possible "
                "prompt-injection patterns. Review before using as LLM context."
            ),
        })

    return {
        "source_id":              source_id,
        "heuristics_note":        (
            "All quality signals are heuristic — they may produce false positives/negatives."
        ),
        "document_count":         document_count,
        "total_words":            total_words,
        "duplicate_groups":       dup_groups,
        "near_duplicate_pairs":   near_pairs,
        "freshness_distribution": freshness_dist,
        "short_documents":        short_docs,
        "injection_suspects":     injection_docs,
        "issues":                 issues,
    }


# ── Helper ────────────────────────────────────────────────────────────────────

def _summarise_quality(quality: dict) -> dict:
    """Return just the flag values from a quality dict (no snippets)."""
    if not quality:
        return {}
    return {
        "freshness":         (quality.get("freshness") or {}).get("flag"),
        "short_content":     (quality.get("short_content") or {}).get("flag"),
        "injection_suspect": (quality.get("injection_suspect") or {}).get("flag"),
    }
