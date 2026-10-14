"""
routers/cleaning.py — Phase 3 human-approved cleaning workflow endpoints.

Endpoints
---------
  POST /sources/{id}/roles/infer           Infer and store column roles
  GET  /sources/{id}/roles                 Return current roles
  PUT  /sources/{id}/roles                 Set confirmed roles (user override)

  POST /sources/{id}/proposals/generate    Regenerate pending proposals
  GET  /sources/{id}/proposals             List proposals (filter by ?status=)
  POST /proposals/{id}/approve             Approve one proposal
  POST /proposals/{id}/reject              Reject one proposal
  POST /sources/{id}/proposals/approve-all Bulk approve by min_severity

  POST /sources/{id}/apply                 Apply approved proposals → new version
  GET  /sources/{id}/versions              List all versions
  GET  /sources/{id}/versions/{n}/profile  Profile for version n
  GET  /sources/{id}/diff                  Diff between two versions
  GET  /sources/{id}/rows                  Paginated rows for a version/status
"""

import json
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from db import (
    get_db, Source, Record, Document,
    ColumnRole, CleaningProposal, SourceVersion, TransformLog,
    safe_json,
)
from profiler.roles import infer_roles
from cleaning.proposals import (
    generate_structured_proposals,
    generate_unstructured_proposals,
)
from cleaning.engine import apply_proposals

router = APIRouter()

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


# ═══════════════════════════════════════════════════════════════════════════════
# Column Roles
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/sources/{source_id}/roles/infer",
    summary="Infer column roles for a structured source",
)
def infer_column_roles(source_id: int, db: Session = Depends(get_db)):
    """
    Run rule-based role inference on the latest version of a source's records.
    Does NOT overwrite roles that are already confirmed by the user.
    Returns the full role list after inference.
    """
    source = _get_source_or_404(source_id, db)

    # Only structured sources have column roles
    records = _load_latest_records(source_id, db)
    if not records:
        raise HTTPException(
            status_code=422,
            detail=f"Source {source_id} has no records (not a structured source or empty).",
        )

    import pandas as pd
    df = pd.DataFrame([r.data for r in records])

    inferred = infer_roles(df)   # { col: role }

    # Load existing confirmed roles so we don't overwrite them
    existing = {
        cr.column_name: cr
        for cr in db.query(ColumnRole).filter(ColumnRole.source_id == source_id).all()
    }

    for col, role in inferred.items():
        if col in existing:
            if existing[col].confirmed:
                continue   # user confirmed this — leave it alone
            existing[col].role = role   # update the guess
        else:
            cr = ColumnRole(
                source_id   = source_id,
                column_name = col,
                role        = role,
                confirmed   = False,
            )
            db.add(cr)

    db.commit()
    return _load_roles_response(source_id, db)


@router.get(
    "/sources/{source_id}/roles",
    summary="Get column roles for a source",
)
def get_column_roles(source_id: int, db: Session = Depends(get_db)):
    """Return all column roles for the source."""
    _get_source_or_404(source_id, db)
    return _load_roles_response(source_id, db)


class RoleUpdateBody(BaseModel):
    # { "column_name": "role" } mapping
    roles: Dict[str, str]


@router.put(
    "/sources/{source_id}/roles",
    summary="Manually set confirmed column roles",
)
def update_column_roles(
    source_id: int,
    body: RoleUpdateBody,
    db: Session = Depends(get_db),
):
    """
    Set one or more column roles, marking them confirmed=TRUE.
    These will not be overwritten by future infer calls.
    """
    _get_source_or_404(source_id, db)

    valid_roles = {"identifier", "required", "optional", "date", "category", "numeric", "free_text"}

    existing = {
        cr.column_name: cr
        for cr in db.query(ColumnRole).filter(ColumnRole.source_id == source_id).all()
    }

    for col, role in body.roles.items():
        if role not in valid_roles:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid role '{role}'. Must be one of: {sorted(valid_roles)}",
            )
        if col in existing:
            existing[col].role      = role
            existing[col].confirmed = True
        else:
            db.add(ColumnRole(
                source_id   = source_id,
                column_name = col,
                role        = role,
                confirmed   = True,
            ))

    db.commit()
    return _load_roles_response(source_id, db)


# ═══════════════════════════════════════════════════════════════════════════════
# Proposals
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/sources/{source_id}/proposals/generate",
    summary="Generate (or regenerate) cleaning proposals for a source",
)
def generate_proposals(source_id: int, db: Session = Depends(get_db)):
    """
    Analyse the latest version of a source and generate cleaning proposals.

    - Replaces any existing PENDING proposals (does not touch approved/rejected/applied).
    - Returns the new proposal list.
    """
    source = _get_source_or_404(source_id, db)

    # Determine the latest version we have data for
    latest_version = _latest_version_no(source_id, db)

    # Delete old PENDING proposals for this source
    db.query(CleaningProposal).filter(
        CleaningProposal.source_id == source_id,
        CleaningProposal.status == "pending",
    ).delete(synchronize_session=False)

    proposals_data = []

    if source.source_type == "file":
        # Structured source
        records = _load_version_records(source_id, latest_version, db, status_filter="active")
        roles = {
            cr.column_name: cr.role
            for cr in db.query(ColumnRole).filter(ColumnRole.source_id == source_id).all()
        }
        if not roles:
            raise HTTPException(
                status_code=422,
                detail="No column roles found. Call POST /sources/{id}/roles/infer first.",
            )
        proposals_data = generate_structured_proposals(records, roles)
    else:
        # Unstructured source (web)
        docs = _load_version_documents(source_id, latest_version, db, status_filter="active")
        proposals_data = generate_unstructured_proposals(docs)

    # Persist proposals
    saved = []
    for p in proposals_data:
        cp = CleaningProposal(
            source_id    = source_id,
            base_version = latest_version,
            kind         = p["kind"],
            target       = p["target"],
            severity     = p["severity"],
            description  = p["description"],
            impact       = safe_json(p["impact"]),
            params       = safe_json(p["params"]),
            status       = "pending",
        )
        db.add(cp)
        saved.append(cp)

    db.commit()
    # Refresh to get ids
    for cp in saved:
        db.refresh(cp)

    return [_proposal_dict(cp) for cp in saved]


@router.get(
    "/sources/{source_id}/proposals",
    summary="List proposals for a source",
)
def list_proposals(
    source_id: int,
    status: Optional[str] = Query(None, description="Filter by status: pending|approved|rejected|applied"),
    db: Session = Depends(get_db),
):
    """Return proposals for a source, optionally filtered by status."""
    _get_source_or_404(source_id, db)

    q = db.query(CleaningProposal).filter(CleaningProposal.source_id == source_id)
    if status:
        q = q.filter(CleaningProposal.status == status)
    proposals = q.order_by(CleaningProposal.id).all()

    return [_proposal_dict(p) for p in proposals]


@router.post(
    "/proposals/{proposal_id}/approve",
    summary="Approve a single cleaning proposal",
)
def approve_proposal(proposal_id: int, db: Session = Depends(get_db)):
    """Approve one pending proposal."""
    prop = _get_proposal_or_404(proposal_id, db)
    if prop.status != "pending":
        raise HTTPException(
            status_code=422,
            detail=f"Proposal {proposal_id} is already '{prop.status}'. Only pending proposals can be approved.",
        )
    prop.status     = "approved"
    prop.decided_at = datetime.utcnow()
    db.commit()
    db.refresh(prop)
    return _proposal_dict(prop)


@router.post(
    "/proposals/{proposal_id}/reject",
    summary="Reject a single cleaning proposal",
)
def reject_proposal(proposal_id: int, db: Session = Depends(get_db)):
    """Reject one pending proposal."""
    prop = _get_proposal_or_404(proposal_id, db)
    if prop.status != "pending":
        raise HTTPException(
            status_code=422,
            detail=f"Proposal {proposal_id} is '{prop.status}'. Only pending proposals can be rejected.",
        )
    prop.status     = "rejected"
    prop.decided_at = datetime.utcnow()
    db.commit()
    db.refresh(prop)
    return _proposal_dict(prop)


@router.post(
    "/sources/{source_id}/proposals/approve-all",
    summary="Bulk-approve proposals at or above a minimum severity",
)
def approve_all_proposals(
    source_id: int,
    min_severity: str = Query("warning", description="Approve proposals at this severity or more critical: critical|warning|info"),
    db: Session = Depends(get_db),
):
    """
    Convenience endpoint: approves all PENDING proposals whose severity is
    >= min_severity (critical > warning > info).

    This still requires an explicit POST /sources/{id}/apply to take effect.
    """
    _get_source_or_404(source_id, db)

    if min_severity not in SEVERITY_ORDER:
        raise HTTPException(status_code=422, detail=f"Invalid severity '{min_severity}'.")

    threshold = SEVERITY_ORDER[min_severity]   # lower number = more critical
    pending = (
        db.query(CleaningProposal)
        .filter(
            CleaningProposal.source_id == source_id,
            CleaningProposal.status == "pending",
        )
        .all()
    )

    approved_count = 0
    now = datetime.utcnow()
    for p in pending:
        if SEVERITY_ORDER.get(p.severity, 99) <= threshold:
            p.status     = "approved"
            p.decided_at = now
            approved_count += 1

    db.commit()
    return {
        "approved_count": approved_count,
        "min_severity":   min_severity,
        "message":        f"Approved {approved_count} proposal(s). Call POST /sources/{source_id}/apply to apply.",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Apply
# ═══════════════════════════════════════════════════════════════════════════════

@router.post(
    "/sources/{source_id}/apply",
    summary="Apply approved proposals and create a new version",
)
def apply_source_proposals(source_id: int, db: Session = Depends(get_db)):
    """
    Apply all approved proposals, create version N+1, and return a summary.

    Nothing is applied automatically — this endpoint must be called explicitly.
    """
    _get_source_or_404(source_id, db)

    try:
        result = apply_proposals(source_id, db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Versions
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/sources/{source_id}/versions",
    summary="List all versions for a source",
)
def list_versions(source_id: int, db: Session = Depends(get_db)):
    """Return all source_versions rows for this source, newest first."""
    _get_source_or_404(source_id, db)

    versions = (
        db.query(SourceVersion)
        .filter(SourceVersion.source_id == source_id)
        .order_by(SourceVersion.version_no.desc())
        .all()
    )
    return [
        {
            "version_no":     v.version_no,
            "parent_version": v.parent_version,
            "note":           v.note,
            "created_at":     str(v.created_at),
            "has_profile":    v.profile is not None,
        }
        for v in versions
    ]


@router.get(
    "/sources/{source_id}/versions/{version_no}/profile",
    summary="Get the stored quality profile for a specific version",
)
def get_version_profile(
    source_id: int,
    version_no: int,
    db: Session = Depends(get_db),
):
    """Return the stored profiler output for a given (source, version)."""
    _get_source_or_404(source_id, db)

    sv = (
        db.query(SourceVersion)
        .filter(
            SourceVersion.source_id == source_id,
            SourceVersion.version_no == version_no,
        )
        .first()
    )
    if not sv:
        raise HTTPException(
            status_code=404,
            detail=f"Version {version_no} not found for source {source_id}.",
        )

    return {
        "source_id":  source_id,
        "version_no": version_no,
        "note":       sv.note,
        "created_at": str(sv.created_at),
        "profile":    sv.profile or {},
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Diff
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/sources/{source_id}/diff",
    summary="Compare quality metrics between two versions",
)
def diff_versions(
    source_id: int,
    from_version: int = Query(..., alias="from", description="Base version number"),
    to_version: int   = Query(..., alias="to",   description="Target version number"),
    db: Session = Depends(get_db),
):
    """
    Return a diff of row counts and profile metrics between two versions.

    Output:
      - row_counts: {active, excluded, quarantined} for each version
      - status_delta: change in each status bucket
      - profile_delta: per-metric difference (from profile JSONs if available)
    """
    _get_source_or_404(source_id, db)

    def _row_counts(version_no: int):
        """Count records by status for a version."""
        rows = (
            db.query(Record.status, func.count(Record.id))
            .filter(Record.source_id == source_id, Record.version_no == version_no)
            .group_by(Record.status)
            .all()
        )
        if rows:
            return {status: cnt for status, cnt in rows}

        # Try documents if no records
        docs = (
            db.query(Document.status, func.count(Document.id))
            .filter(Document.source_id == source_id, Document.version_no == version_no)
            .group_by(Document.status)
            .all()
        )
        return {status: cnt for status, cnt in docs}

    from_counts = _row_counts(from_version)
    to_counts   = _row_counts(to_version)

    all_statuses = {"active", "excluded", "quarantined"}
    status_delta = {
        s: to_counts.get(s, 0) - from_counts.get(s, 0)
        for s in all_statuses
    }

    # Profile-level metrics delta
    def _get_profile(version_no: int):
        sv = (
            db.query(SourceVersion)
            .filter(
                SourceVersion.source_id == source_id,
                SourceVersion.version_no == version_no,
            )
            .first()
        )
        return sv.profile or {} if sv else {}

    from_profile = _get_profile(from_version)
    to_profile   = _get_profile(to_version)

    profile_delta = {}
    for key in ("duplicate_rows", "row_count"):
        v_from = from_profile.get(key)
        v_to   = to_profile.get(key)
        if v_from is not None or v_to is not None:
            profile_delta[key] = {
                "from":  v_from,
                "to":    v_to,
                "delta": (v_to or 0) - (v_from or 0),
            }

    # Missing pct per column
    missing_delta = {}
    from_cols = (from_profile.get("columns") or {})
    to_cols   = (to_profile.get("columns") or {})
    for col in set(list(from_cols.keys()) + list(to_cols.keys())):
        f_pct = (from_cols.get(col) or {}).get("missing_pct")
        t_pct = (to_cols.get(col) or {}).get("missing_pct")
        if f_pct is not None or t_pct is not None:
            missing_delta[col] = {
                "from":  f_pct,
                "to":    t_pct,
                "delta": round((t_pct or 0) - (f_pct or 0), 3),
            }

    # Inconsistent categories count
    ic_from = len(from_profile.get("inconsistent_categories") or {})
    ic_to   = len(to_profile.get("inconsistent_categories") or {})
    if ic_from or ic_to:
        profile_delta["inconsistent_category_columns"] = {
            "from": ic_from, "to": ic_to, "delta": ic_to - ic_from
        }

    return {
        "source_id":    source_id,
        "from_version": from_version,
        "to_version":   to_version,
        "row_counts": {
            "from": {s: from_counts.get(s, 0) for s in all_statuses},
            "to":   {s: to_counts.get(s, 0)   for s in all_statuses},
        },
        "status_delta":  status_delta,
        "profile_delta": profile_delta,
        "missing_pct_delta": missing_delta,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Rows (paginated)
# ═══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/sources/{source_id}/rows",
    summary="Get paginated rows for a specific version and status",
)
def get_rows(
    source_id: int,
    version: int = Query(1, description="Version number to read"),
    status: Optional[str] = Query(None, description="Filter by: active|excluded|quarantined"),
    limit: int  = Query(50,  ge=1, le=200),
    offset: int = Query(0,   ge=0),
    db: Session = Depends(get_db),
):
    """
    Return paginated record/document rows for a given version and optional status.
    Includes quarantine/exclude reason from row metadata.
    """
    _get_source_or_404(source_id, db)

    # Try records first
    q = db.query(Record).filter(
        Record.source_id == source_id,
        Record.version_no == version,
    )
    if status:
        q = q.filter(Record.status == status)
    total = q.count()
    rows = q.order_by(Record.row_index).offset(offset).limit(limit).all()

    if rows or total > 0:
        return {
            "source_id":  source_id,
            "version":    version,
            "status":     status,
            "total":      total,
            "offset":     offset,
            "limit":      limit,
            "rows": [
                {
                    "id":         r.id,
                    "row_index":  r.row_index,
                    "status":     r.status,
                    "data":       r.data,
                    "reason":     (r.metadata_ or {}).get("quarantine_reason")
                                  or (r.metadata_ or {}).get("exclude_reason"),
                }
                for r in rows
            ],
        }

    # Fall back to documents
    qd = db.query(Document).filter(
        Document.source_id == source_id,
        Document.version_no == version,
    )
    if status:
        qd = qd.filter(Document.status == status)
    total_d = qd.count()
    docs = qd.order_by(Document.id).offset(offset).limit(limit).all()

    return {
        "source_id":  source_id,
        "version":    version,
        "status":     status,
        "total":      total_d,
        "offset":     offset,
        "limit":      limit,
        "rows": [
            {
                "id":         d.id,
                "url":        d.url,
                "title":      d.title,
                "word_count": d.word_count,
                "status":     d.status,
                "reason":     (d.metadata_ or {}).get("quarantine_reason")
                              or (d.metadata_ or {}).get("exclude_reason"),
            }
            for d in docs
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _get_source_or_404(source_id: int, db: Session) -> Source:
    source = db.query(Source).filter(Source.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail=f"Source {source_id} not found.")
    return source


def _get_proposal_or_404(proposal_id: int, db: Session) -> CleaningProposal:
    p = db.query(CleaningProposal).filter(CleaningProposal.id == proposal_id).first()
    if not p:
        raise HTTPException(status_code=404, detail=f"Proposal {proposal_id} not found.")
    return p


def _load_roles_response(source_id: int, db: Session) -> List[Dict]:
    roles = (
        db.query(ColumnRole)
        .filter(ColumnRole.source_id == source_id)
        .order_by(ColumnRole.column_name)
        .all()
    )
    return [
        {
            "column_name": cr.column_name,
            "role":        cr.role,
            "confirmed":   cr.confirmed,
        }
        for cr in roles
    ]


def _latest_version_no(source_id: int, db: Session) -> int:
    """Return the highest version_no with actual records or documents."""
    max_rec = (
        db.query(func.max(Record.version_no))
        .filter(Record.source_id == source_id)
        .scalar()
    )
    if max_rec is not None:
        return max_rec
    max_doc = (
        db.query(func.max(Document.version_no))
        .filter(Document.source_id == source_id)
        .scalar()
    )
    return max_doc or 1


def _load_latest_records(source_id: int, db: Session):
    """Load all records for the latest version."""
    latest = _latest_version_no(source_id, db)
    return (
        db.query(Record)
        .filter(Record.source_id == source_id, Record.version_no == latest)
        .order_by(Record.row_index)
        .all()
    )


def _load_version_records(
    source_id: int,
    version_no: int,
    db: Session,
    status_filter: Optional[str] = None,
) -> List[Dict]:
    """Load records for a version as plain dicts for the proposal generators."""
    q = db.query(Record).filter(
        Record.source_id == source_id,
        Record.version_no == version_no,
    )
    if status_filter:
        q = q.filter(Record.status == status_filter)
    rows = q.order_by(Record.row_index).all()
    return [{"id": r.id, "data": r.data or {}} for r in rows]


def _load_version_documents(
    source_id: int,
    version_no: int,
    db: Session,
    status_filter: Optional[str] = None,
) -> List[Dict]:
    """Load documents for a version as plain dicts for the proposal generators."""
    q = db.query(Document).filter(
        Document.source_id == source_id,
        Document.version_no == version_no,
    )
    if status_filter:
        q = q.filter(Document.status == status_filter)
    docs = q.order_by(Document.id).all()
    return [
        {
            "id":           d.id,
            "content":      d.content or "",
            "content_hash": d.content_hash or "",
            "quality":      d.quality or {},
            "word_count":   d.word_count or 0,
        }
        for d in docs
    ]


def _proposal_dict(p: CleaningProposal) -> Dict:
    """Serialise a CleaningProposal ORM object to a plain dict."""
    return {
        "id":           p.id,
        "source_id":    p.source_id,
        "base_version": p.base_version,
        "kind":         p.kind,
        "target":       p.target,
        "severity":     p.severity,
        "description":  p.description,
        "impact":       p.impact,
        "params":       p.params,
        "status":       p.status,
        "created_at":   str(p.created_at),
        "decided_at":   str(p.decided_at) if p.decided_at else None,
    }
