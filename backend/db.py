"""
db.py — Database connection, table definitions, and init logic.
Phase 1: files, quality_reports
Phase 2: sources, documents, records  (added — Phase 1 tables untouched)
"""

import json
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    create_engine, text,
    Column, Integer, String, Text, DateTime, ForeignKey, Float
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, sessionmaker

# ── Connection ──────────────────────────────────────────────────────────────
# Default connection defaults to ragstudio for backwards compatibility with
# existing setups. Can be pointed to 'sift' via the DATABASE_URL environment variable.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://postgres:raj1818@localhost:8887/ragstudio"
)

engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


# ── Phase 1 Table Models ──────────────────────────────────────────────────────

class FileRecord(Base):
    """One row per uploaded file."""
    __tablename__ = "files"

    id          = Column(Integer, primary_key=True, index=True)
    filename    = Column(Text, nullable=False)
    format      = Column(String(20), nullable=False)   # csv / xlsx / pdf / docx / image
    uploaded_at = Column(DateTime, default=datetime.utcnow)


class QualityReport(Base):
    """One row per quality report, linked to a file."""
    __tablename__ = "quality_reports"

    id         = Column(Integer, primary_key=True, index=True)
    file_id    = Column(Integer, ForeignKey("files.id"), nullable=False)
    report     = Column(JSONB, nullable=False)          # full profiler output
    created_at = Column(DateTime, default=datetime.utcnow)


# ── Phase 2 Table Models ──────────────────────────────────────────────────────

class Source(Base):
    """
    One row per logical origin:
      source_type='file'  for uploaded files  (file_id set)
      source_type='web'   for web crawl runs  (origin set to seed URL)
    """
    __tablename__ = "sources"

    id          = Column(Integer, primary_key=True, index=True)
    source_type = Column(Text, nullable=False)   # 'file' | 'web'
    name        = Column(Text)                   # filename or seed URL label
    origin      = Column(Text)                   # base URL for web; NULL for file
    file_id     = Column(Integer, ForeignKey("files.id"), nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


class Document(Base):
    """
    One row per piece of text content:
      - one per PDF page
      - one per DOCX / TXT file
      - one per image (OCR result)
      - one per fetched web page
    """
    __tablename__ = "documents"

    id                    = Column(Integer, primary_key=True, index=True)
    source_id             = Column(Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    modality              = Column(Text)          # 'text' | 'ocr' | 'ocr_low_confidence' | 'web'
    title                 = Column(Text)
    url                   = Column(Text)          # set for web pages
    page_number           = Column(Integer, nullable=True)   # 1-based; set for PDF pages
    content               = Column(Text)
    content_hash          = Column(Text)          # SHA-256 of whitespace-normalised lowercase content
    word_count            = Column(Integer)
    extraction_confidence = Column(Float, nullable=True)    # OCR confidence 0-100; NULL otherwise
    published_at          = Column(DateTime, nullable=True)
    fetched_at            = Column(DateTime, default=datetime.utcnow)
    metadata_             = Column("metadata", JSONB, default=dict)   # raw fetch metadata
    quality               = Column(JSONB, default=dict)               # output of text_quality checks


class Record(Base):
    """One row per CSV/XLSX data row (structured data, not text)."""
    __tablename__ = "records"

    id        = Column(Integer, primary_key=True, index=True)
    source_id = Column(Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    row_index = Column(Integer)
    data      = Column(JSONB)


# ── Init ──────────────────────────────────────────────────────────────────────

def init_db():
    """
    Create extension + tables on startup.
    Phase 1: creates files, quality_reports via ORM (unchanged).
    Phase 2: applies 002_phase2.sql for sources, documents, records.
    Safe to call multiple times — all statements use IF NOT EXISTS.
    """
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        conn.commit()

    # Idempotent: creates Phase 1 tables if they don't exist yet
    Base.metadata.create_all(bind=engine)

    # Apply Phase 2 SQL migration (uses IF NOT EXISTS — safe to re-run)
    migration_path = Path(__file__).parent / "migrations" / "002_phase2.sql"
    if migration_path.exists():
        sql = migration_path.read_text(encoding="utf-8-sig")
        with engine.connect() as conn:
            for statement in _split_sql(sql):
                if statement.strip():
                    conn.execute(text(statement))
            conn.commit()
        print("[OK] Phase 2 migration applied.")
    else:
        print("[WARN] migrations/002_phase2.sql not found — skipping.")


def _split_sql(sql: str) -> list:
    """
    Split a SQL file into individual statements at semicolons.
    Skips comment-only lines so we don't hand empty strings to execute().
    """
    sql = sql.lstrip("\ufeff")
    statements = []
    current = []
    for line in sql.splitlines():
        stripped = line.strip().lstrip("\ufeff")
        if stripped.startswith("--") and not current:
            continue          # skip leading comment lines
        current.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
    return statements


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_db():
    """FastAPI dependency: yields a DB session and closes it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def safe_json(obj) -> dict:
    """
    Round-trip through json.dumps / json.loads with default=str so that
    numpy int64, float32, NaT, datetime, etc. don't crash psycopg2 JSONB inserts.
    """
    return json.loads(json.dumps(obj, default=str))
