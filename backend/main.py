"""
main.py — FastAPI entry point for Sift (Phase 1 + Phase 2 + Phase 3).

Phase 1 endpoints (unchanged):
  POST /upload              — Upload a file, profile it, save to DB
  GET  /reports/{file_id}  — Retrieve one saved report
  GET  /reports             — List all uploads with a short summary

Phase 2 endpoints (unchanged):
  POST /ingest/url                    — Web ingestion via sitemap/URL
  GET  /sources                       — List all sources with counts
  GET  /sources/{id}/documents        — List documents for a source
  GET  /sources/{id}/quality          — Aggregate quality report
  GET  /documents/{id}               — Full document with content

Phase 3 endpoints (new):
  POST /sources/{id}/roles/infer              — Infer column roles
  GET  /sources/{id}/roles                    — Get column roles
  PUT  /sources/{id}/roles                    — Update column roles (confirmed)
  POST /sources/{id}/proposals/generate       — Generate cleaning proposals
  GET  /sources/{id}/proposals                — List proposals
  POST /proposals/{id}/approve                — Approve a proposal
  POST /proposals/{id}/reject                 — Reject a proposal
  POST /sources/{id}/proposals/approve-all    — Bulk approve
  POST /sources/{id}/apply                    — Apply approved proposals
  GET  /sources/{id}/versions                 — List versions
  GET  /sources/{id}/versions/{n}/profile     — Version profile
  GET  /sources/{id}/diff                     — Diff two versions
  GET  /sources/{id}/rows                     — Paginated rows
"""

import logging
import os
import re
import shutil
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from typing import List

import pandas as pd
from fastapi import FastAPI, File, UploadFile, HTTPException, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Local imports ─────────────────────────────────────────────────────────────
from db import (
    init_db, get_db,
    FileRecord, QualityReport,
    Source, Document, Record,
    SourceVersion,
    safe_json,
)

from parsers.csv_parser   import parse_csv, parse_xlsx
from parsers.pdf_parser   import parse_pdf
from parsers.docx_parser  import parse_docx
from parsers.image_parser import parse_image

from profiler.structured_profiler   import profile_structured
from profiler.unstructured_profiler import profile_unstructured
from profiler.text_quality import compute_content_hash, run_all_checks

# Phase 2 routers
from routers.ingest    import router as ingest_router
from routers.sources   import router as sources_router
from routers.documents import router as documents_router

# Phase 3 router
from routers.cleaning  import router as cleaning_router

# Phase 4 routers
from routers.indexing  import router as indexing_router
from routers.rag       import router as rag_router

# ── Constants ─────────────────────────────────────────────────────────────────
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB

ALLOWED_EXTENSIONS = {
    ".csv":  "csv",
    ".xlsx": "xlsx",
    ".pdf":  "pdf",
    ".docx": "docx",
    ".png":  "image",
    ".jpg":  "image",
    ".jpeg": "image",
    ".tiff": "image",
    ".bmp":  "image",
    ".webp": "image",
}

UPLOAD_DIR = Path("uploads")

# ── App init ──────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Sift — Phase 1-4 Baseline RAG",
    description="Data ingestion + quality profiling + content store + web crawler + cleaning workflow + baseline RAG",
    version="4.0.0",
)

# Register routers
app.include_router(ingest_router)
app.include_router(sources_router)
app.include_router(documents_router)
app.include_router(cleaning_router)   # Phase 3
app.include_router(indexing_router)   # Phase 4
app.include_router(rag_router)        # Phase 4


@app.get("/", include_in_schema=False)
def root():
    """Redirect root path to interactive Swagger documentation."""
    return RedirectResponse(url="/docs")



@app.on_event("startup")
def startup():
    """Verify environment, create uploads folder and initialise DB tables on first run."""
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL environment variable is required but not set. "
            "Please configure DATABASE_URL in your environment or backend/.env file."
        )
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not gemini_key:
        raise RuntimeError(
            "GEMINI_API_KEY environment variable is required but not set. "
            "Please configure GEMINI_API_KEY in your environment or backend/.env file."
        )
    UPLOAD_DIR.mkdir(exist_ok=True)
    init_db()
    # Phase 3: ensure version 1 exists for any source without one
    _backfill_source_versions()
    print("[OK] Sift ready. DB tables ready. Uploads dir ready.")


def _backfill_source_versions():
    """
    Ensure every source has a source_versions row for version 1.
    This is also handled by the SQL migration, but we run it in Python
    too so that tests using SQLite don't need the SQL backfill.
    """
    from db import Source, SourceVersion, SessionLocal
    db = SessionLocal()
    try:
        sources = db.query(Source).all()
        for s in sources:
            exists = (
                db.query(SourceVersion)
                .filter(
                    SourceVersion.source_id == s.id,
                    SourceVersion.version_no == 1,
                )
                .first()
            )
            if not exists:
                sv = SourceVersion(
                    source_id      = s.id,
                    version_no     = 1,
                    parent_version = None,
                    note           = "initial version (backfill)",
                )
                db.add(sv)
        db.commit()
    finally:
        db.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def sanitize_filename(filename: str) -> str:
    """
    Sanitize client-supplied filename:
      - Unquote URL encodings (e.g. %00)
      - Extract basename (supporting both / and \\ separators)
      - Strip control characters (including null bytes)
      - Preserve clean unicode characters
      - Bound maximum length to 255 chars preserving extension
      - Fallback to safe default if empty
    """
    if not filename:
        return "unnamed"

    try:
        decoded = urllib.parse.unquote(filename)
    except Exception:
        decoded = filename

    clean = os.path.basename(decoded.replace("\\", "/"))
    clean = re.sub(r"[\x00-\x1f\x7f]", "", clean)
    clean = clean.strip()
    if not clean or clean in (".", ".."):
        clean = "unnamed"

    if len(clean) > 255:
        p = Path(clean)
        ext = p.suffix[:20]
        stem = p.stem[:255 - len(ext)]
        clean = f"{stem}{ext}"

    return clean


def detect_format(filename: str) -> str:
    """Return canonical format string based on file extension."""
    ext = Path(filename).suffix.lower()
    fmt = ALLOWED_EXTENSIONS.get(ext)
    if fmt is None:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type '{ext}'. "
                   f"Allowed: {', '.join(ALLOWED_EXTENSIONS.keys())}",
        )
    return fmt


# ── Phase 2 persistence helpers ───────────────────────────────────────────────

def _persist_structured(db: Session, file_id: int, filename: str, df) -> int:
    """
    Persist CSV/XLSX data:
      - One sources row (source_type='file')
      - One records row per DataFrame row (NaN → None)

    Returns the new source_id.
    """
    source = Source(source_type="file", name=filename, file_id=file_id)
    db.add(source)
    db.flush()

    # Phase 3: create version 1 for this source
    db.add(SourceVersion(
        source_id=source.id, version_no=1,
        parent_version=None, note="initial version",
    ))

    for idx, row in df.iterrows():
        # Convert row to dict; replace NaN/NA with None for clean JSON
        row_dict = {}
        for col, val in row.items():
            import math
            try:
                if val is None or (isinstance(val, float) and math.isnan(val)):
                    row_dict[col] = None
                else:
                    row_dict[col] = val
            except (TypeError, ValueError):
                row_dict[col] = str(val)

        record = Record(
            source_id=source.id,
            row_index=int(idx),
            data=safe_json(row_dict),
            version_no=1,
            status="active",
        )
        db.add(record)

    return source.id


def _persist_pdf(db: Session, file_id: int, filename: str, parsed: dict) -> int:
    """
    Persist PDF content:
      - One sources row
      - One documents row per page

    Returns source_id.
    """
    source = Source(source_type="file", name=filename, file_id=file_id)
    db.add(source)
    db.flush()

    for page_num, page_text in enumerate(parsed.get("pages", []), start=1):
        text = page_text or ""
        quality = run_all_checks(text)
        doc = Document(
            source_id=source.id,
            modality="text",
            title=f"{filename} — Page {page_num}",
            page_number=page_num,
            content=text,
            content_hash=compute_content_hash(text),
            word_count=len(text.split()),
            quality=safe_json(quality),
        )
        db.add(doc)

    return source.id


def _persist_docx(db: Session, file_id: int, filename: str, parsed: dict) -> int:
    """
    Persist DOCX content as one documents row.
    Returns source_id.
    """
    source = Source(source_type="file", name=filename, file_id=file_id)
    db.add(source)
    db.flush()

    text = parsed.get("full_text", "") or ""
    quality = run_all_checks(text)

    doc = Document(
        source_id=source.id,
        modality="text",
        title=filename,
        content=text,
        content_hash=compute_content_hash(text),
        word_count=len(text.split()),
        quality=safe_json(quality),
    )
    db.add(doc)

    return source.id


def _persist_image(db: Session, file_id: int, filename: str, parsed: dict) -> int:
    """
    Persist image OCR content as one documents row.
    Returns source_id.
    """
    source = Source(source_type="file", name=filename, file_id=file_id)
    db.add(source)
    db.flush()

    text = parsed.get("text", "") or ""
    modality = parsed.get("modality", "ocr")
    confidence = parsed.get("confidence")
    quality = run_all_checks(text)

    doc = Document(
        source_id=source.id,
        modality=modality,
        title=filename,
        content=text,
        content_hash=compute_content_hash(text),
        word_count=len(text.split()),
        extraction_confidence=confidence,
        quality=safe_json(quality),
    )
    db.add(doc)

    return source.id


# ── Phase 1 Endpoints (unchanged contract, source_id added to response) ───────

@app.post("/upload", summary="Upload a file and receive a quality report")
async def upload_file(
    file: UploadFile = File(...),
    db:   Session    = Depends(get_db),
):
    """
    Accept a CSV, XLSX, PDF, DOCX, or image file.
    1. Validate extension and size.
    2. Parse the file.
    3. Profile the parsed data.
    4. Save metadata + report to PostgreSQL.
    5. Persist content to sources/documents/records (Phase 2 backfill).
    6. Return the report as JSON (now includes source_id).
    """
    # ── 1. Sanitize filename and validate extension ───────────────────────────
    cleaned_filename = sanitize_filename(file.filename)
    fmt = detect_format(cleaned_filename)

    # ── 2. Read into memory and check size ────────────────────────────────────
    contents = await file.read()
    if len(contents) == 0:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is empty (0 bytes).",
        )
    if len(contents) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds 20 MB limit ({len(contents) / 1e6:.1f} MB uploaded).",
        )

    # ── 3. Store on disk under a server-generated name (uuid4 + validated ext) ──
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    raw_ext = Path(cleaned_filename).suffix.lower()
    validated_ext = raw_ext if raw_ext in ALLOWED_EXTENSIONS else f".{fmt}"
    if fmt == "image" and validated_ext not in ALLOWED_EXTENSIONS:
        validated_ext = ".png"

    disk_filename = f"{uuid.uuid4().hex}{validated_ext}"
    disk_path = UPLOAD_DIR / disk_filename
    disk_path.write_bytes(contents)
    tmp_path = str(disk_path)

    try:
        # ── 4. Parse ─────────────────────────────────────────────────────────
        if fmt == "csv":
            df = parse_csv(tmp_path)
            if df.empty or len(df.columns) == 0 or len(df) == 0:
                raise HTTPException(
                    status_code=400,
                    detail="Uploaded file contains no data (zero rows or zero columns).",
                )
            quality_report = profile_structured(df)

        elif fmt == "xlsx":
            df = parse_xlsx(tmp_path)
            if df.empty or len(df.columns) == 0 or len(df) == 0:
                raise HTTPException(
                    status_code=400,
                    detail="Uploaded file contains no data (zero rows or zero columns).",
                )
            quality_report = profile_structured(df)

        elif fmt == "pdf":
            parsed = parse_pdf(tmp_path)
            if parsed.get("page_count", 0) == 0:
                raise HTTPException(
                    status_code=400,
                    detail="Uploaded PDF contains zero pages.",
                )
            quality_report = profile_unstructured(parsed, "pdf")

        elif fmt == "docx":
            parsed = parse_docx(tmp_path)
            quality_report = profile_unstructured(parsed, "docx")

        elif fmt == "image":
            parsed = parse_image(tmp_path)
            quality_report = profile_unstructured(parsed, "image")

    except HTTPException:
        raise
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        raise HTTPException(
            status_code=400,
            detail="Failed to parse file: empty or invalid table format.",
        )
    except Exception as e:
        logger.warning("Failed to parse uploaded file %s: %s", cleaned_filename, e)
        raise HTTPException(
            status_code=400,
            detail="Failed to parse uploaded file: corrupt or invalid file format.",
        )

    # ── 5. Save to DB (Phase 1) ───────────────────────────────────────────────
    file_record = FileRecord(filename=cleaned_filename, format=fmt)
    db.add(file_record)
    db.flush()

    safe_report = safe_json(quality_report)
    report_record = QualityReport(file_id=file_record.id, report=safe_report)
    db.add(report_record)
    db.flush()

    # ── 6. Persist content (Phase 2 backfill) ─────────────────────────────────
    source_id = None
    try:
        if fmt in ("csv", "xlsx"):
            source_id = _persist_structured(db, file_record.id, cleaned_filename, df)
        elif fmt == "pdf":
            source_id = _persist_pdf(db, file_record.id, cleaned_filename, parsed)
        elif fmt == "docx":
            source_id = _persist_docx(db, file_record.id, cleaned_filename, parsed)
        elif fmt == "image":
            source_id = _persist_image(db, file_record.id, cleaned_filename, parsed)
    except Exception as e:
        # Phase 2 persistence failure must NOT break the Phase 1 response
        print(f"[WARN] Phase 2 content persistence failed: {e}")

    db.commit()
    db.refresh(file_record)

    # ── 7. Return (source_id added; all Phase 1 fields kept) ─────────────────
    return JSONResponse({
        "file_id":        file_record.id,
        "filename":       file_record.filename,
        "format":         file_record.format,
        "source_id":      source_id,        # NEW in Phase 2
        "quality_report": safe_report,
    })


@app.get("/reports/{file_id}", summary="Get quality report by file ID")
def get_report(file_id: int, db: Session = Depends(get_db)):
    """Return the saved quality report for the given file_id."""
    report = (
        db.query(QualityReport)
        .filter(QualityReport.file_id == file_id)
        .first()
    )
    if not report:
        raise HTTPException(status_code=404, detail=f"No report found for file_id={file_id}")

    file_rec = db.query(FileRecord).filter(FileRecord.id == file_id).first()

    return {
        "file_id":        file_id,
        "filename":       file_rec.filename if file_rec else None,
        "format":         file_rec.format   if file_rec else None,
        "uploaded_at":    str(file_rec.uploaded_at) if file_rec else None,
        "quality_report": report.report,
    }


@app.get("/reports", summary="List all uploaded files with a short summary")
def list_reports(db: Session = Depends(get_db)):
    """
    Return all uploaded files, each with a one-line summary of the quality report.
    """
    files = db.query(FileRecord).order_by(FileRecord.id.desc()).all()

    result = []
    for f in files:
        report_rec = (
            db.query(QualityReport)
            .filter(QualityReport.file_id == f.id)
            .first()
        )
        report = report_rec.report if report_rec else {}

        issues     = report.get("issues", [])
        row_count  = report.get("row_count", None)
        word_count = report.get("word_count", None)

        if row_count is not None:
            summary = f"{row_count} rows | {len(issues)} issue(s) flagged"
        elif word_count is not None:
            summary = f"{word_count} words | {len(issues)} issue(s) flagged"
        else:
            summary = f"{len(issues)} issue(s) flagged"

        result.append({
            "file_id":     f.id,
            "filename":    f.filename,
            "format":      f.format,
            "uploaded_at": str(f.uploaded_at),
            "summary":     summary,
        })

    return result


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)

