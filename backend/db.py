"""
db.py — Database connection, table definitions, and init logic.
Phase 1: files, quality_reports
Phase 2: sources, documents, records  (added — Phase 1 tables untouched)
Phase 3: source_versions, column_roles, cleaning_proposals, transform_log
          records.version_no / records.status
          documents.version_no / documents.status
"""

import json
import os
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    create_engine, text,
    Column, Integer, String, Text, DateTime, ForeignKey, Float, Boolean, UniqueConstraint, JSON
)
from sqlalchemy.types import TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB

class JSONB(TypeDecorator):
    """JSONB for PostgreSQL, JSON for SQLite (enables unit testing with SQLite)."""
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_JSONB())
        return dialect.type_descriptor(JSON())

from sqlalchemy.orm import declarative_base, sessionmaker
from dotenv import load_dotenv

# Load .env from backend directory or project root if present
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path)
else:
    load_dotenv()

# ── Connection ──────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is required but not set. "
        "Please configure DATABASE_URL in your environment or backend/.env file. "
        "See backend/.env.example for reference."
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
    # Phase 3
    version_no            = Column(Integer, nullable=False, default=1)
    status                = Column(Text, nullable=False, default="active")


class Record(Base):
    """
    One row per CSV/XLSX data row (structured data, not text).
    Phase 3 adds version_no + status so old versions are kept intact.
    """
    __tablename__ = "records"

    id         = Column(Integer, primary_key=True, index=True)
    source_id  = Column(Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    row_index  = Column(Integer)
    data       = Column(JSONB)
    # Phase 3
    version_no = Column(Integer, nullable=False, default=1)
    status     = Column(Text, nullable=False, default="active")
    # Optional quarantine reason stored here
    metadata_  = Column("metadata", JSONB, default=dict)


# ── Phase 3 Table Models ──────────────────────────────────────────────────────

class SourceVersion(Base):
    """
    One row per (source, version). Stores the re-profile JSON so diffs are cheap.
    Version 1 is the original — it is never modified.
    """
    __tablename__ = "source_versions"
    __table_args__ = (UniqueConstraint("source_id", "version_no", name="uq_source_version"),)

    id             = Column(Integer, primary_key=True, index=True)
    source_id      = Column(Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    version_no     = Column(Integer, nullable=False)
    parent_version = Column(Integer, nullable=True)  # NULL for version 1
    note           = Column(Text)
    profile        = Column(JSONB)       # profiler output for this version
    created_at     = Column(DateTime, default=datetime.utcnow)


class ColumnRole(Base):
    """
    Inferred or user-confirmed role for a column in a structured source.
    Roles drive severity rules for missing-value proposals.
    """
    __tablename__ = "column_roles"
    __table_args__ = (UniqueConstraint("source_id", "column_name", name="uq_column_role"),)

    id          = Column(Integer, primary_key=True, index=True)
    source_id   = Column(Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    column_name = Column(Text, nullable=False)
    role        = Column(Text, nullable=False)   # see CHECK constraint in SQL
    confirmed   = Column(Boolean, default=False)


class CleaningProposal(Base):
    """
    One proposed data-quality fix. Status lifecycle:
      pending → approved | rejected
      approved → applied  (after POST /sources/{id}/apply)
    """
    __tablename__ = "cleaning_proposals"

    id           = Column(Integer, primary_key=True, index=True)
    source_id    = Column(Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    base_version = Column(Integer, nullable=False)
    kind         = Column(Text, nullable=False)
    target       = Column(Text, nullable=False)   # column name or '*'
    severity     = Column(Text, nullable=False)
    description  = Column(Text)
    impact       = Column(JSONB)    # {rows_affected, examples}
    params       = Column(JSONB)    # transform-specific parameters
    status       = Column(Text, nullable=False, default="pending")
    created_at   = Column(DateTime, default=datetime.utcnow)
    decided_at   = Column(DateTime, nullable=True)


class TransformLog(Base):
    """
    One row per applied proposal — records what rows changed between versions.
    """
    __tablename__ = "transform_log"

    id            = Column(Integer, primary_key=True, index=True)
    proposal_id   = Column(Integer, ForeignKey("cleaning_proposals.id"), nullable=False)
    version_from  = Column(Integer, nullable=False)
    version_to    = Column(Integer, nullable=False)
    rows_affected = Column(Integer, nullable=False, default=0)
    sample        = Column(JSONB)   # [{before, after}] up to 5 rows
    applied_at    = Column(DateTime, default=datetime.utcnow)


# ── Phase 4 Table Models ──────────────────────────────────────────────────────

from pgvector.sqlalchemy import Vector as PGVector
from sqlalchemy.dialects.postgresql import ARRAY

class VectorType(TypeDecorator):
    """Vector for PostgreSQL pgvector, JSON for SQLite (enables unit testing)."""
    impl = JSON
    cache_ok = True

    def __init__(self, dim=384, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dim = dim

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGVector(self.dim))
        return dialect.type_descriptor(JSON())


class IntArray(TypeDecorator):
    """INT[] for PostgreSQL, JSON for SQLite (enables unit testing)."""
    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(ARRAY(Integer))
        return dialect.type_descriptor(JSON())


class IndexRun(Base):
    """
    One row per indexing run for a source.
    Stores chunk size, overlap, model, status, chunk_count.
    """
    __tablename__ = "index_runs"

    id             = Column(Integer, primary_key=True, index=True)
    source_id      = Column(Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    source_version = Column(Integer, nullable=False)
    embed_model    = Column(Text, nullable=False)
    embed_dim      = Column(Integer, nullable=False)
    chunk_size     = Column(Integer, nullable=False)
    chunk_overlap  = Column(Integer, nullable=False)
    chunk_count    = Column(Integer, default=0)
    status         = Column(Text, nullable=False)  # 'running' | 'completed' | 'failed'
    error          = Column(Text, nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)
    finished_at    = Column(DateTime, nullable=True)


class Chunk(Base):
    """
    One row per chunk of content from a document or record.
    """
    __tablename__ = "chunks"

    id             = Column(Integer, primary_key=True, index=True)
    index_run_id   = Column(Integer, ForeignKey("index_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    source_id      = Column(Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True)
    document_id    = Column(Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=True)
    record_id      = Column(Integer, ForeignKey("records.id", ondelete="CASCADE"), nullable=True)
    chunk_index    = Column(Integer, nullable=False)
    content        = Column(Text, nullable=False)
    content_hash   = Column(Text, nullable=False)
    token_estimate = Column(Integer, nullable=False)
    embedding      = Column(VectorType(384), nullable=True)
    metadata_      = Column("metadata", JSONB, default=dict)


class QueryRecord(Base):
    """
    One row per search question evaluated against the knowledge base.
    """
    __tablename__ = "queries"

    id             = Column(Integer, primary_key=True, index=True)
    question       = Column(Text, nullable=False)
    source_ids     = Column(IntArray, nullable=False)
    index_run_ids  = Column(IntArray, nullable=False)
    top_k          = Column(Integer, nullable=False)
    min_score      = Column(Float, nullable=False)
    status         = Column(Text, nullable=False)  # answered | unverified | insufficient_evidence | llm_error
    answer         = Column(Text, nullable=True)
    latency_ms     = Column(JSONB, default=dict)
    created_at     = Column(DateTime, default=datetime.utcnow)


class RetrievalResult(Base):
    """
    One row per retrieved chunk for a query.
    """
    __tablename__ = "retrieval_results"

    id             = Column(Integer, primary_key=True, index=True)
    query_id       = Column(Integer, ForeignKey("queries.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_id       = Column(Integer, ForeignKey("chunks.id", ondelete="CASCADE"), nullable=False)
    rank           = Column(Integer, nullable=False)
    score          = Column(Float, nullable=False)
    used_in_prompt = Column(Boolean, nullable=False)
    cited          = Column(Boolean, default=False)


# ── Init ──────────────────────────────────────────────────────────────────────

def init_db():
    """
    Create extension + tables on startup.
    Phase 1: creates files, quality_reports via ORM (unchanged).
    Phase 2: applies 002_phase2.sql for sources, documents, records.
    Phase 3: applies 003_phase3.sql for versioning, roles, proposals.
    Phase 4: applies 004_phase4.sql for index_runs, chunks, queries, retrieval_results.
    Safe to call multiple times — all statements use IF NOT EXISTS.
    """
    with engine.connect() as conn:
        try:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        except Exception:
            pass   # vector extension optional; ignore if not installed
        conn.commit()

    # Idempotent: creates Phase 1 ORM tables if they don't exist yet
    Base.metadata.create_all(bind=engine)

    # Apply each SQL migration in order (all use IF NOT EXISTS / ON CONFLICT)
    for fname in ("002_phase2.sql", "003_phase3.sql", "004_phase4.sql"):
        migration_path = Path(__file__).parent / "migrations" / fname
        if migration_path.exists():
            sql = migration_path.read_text(encoding="utf-8-sig")
            with engine.connect() as conn:
                for statement in _split_sql(sql):
                    if statement.strip():
                        try:
                            conn.execute(text(statement))
                        except Exception as e:
                            if "vector" in statement.lower() and engine.dialect.name != "postgresql":
                                pass
                            else:
                                raise
                conn.commit()
            print(f"[OK] Migration {fname} applied.")
        else:
            print(f"[WARN] {fname} not found — skipping.")


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
