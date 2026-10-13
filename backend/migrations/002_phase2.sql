-- migrations/002_phase2.sql
-- Phase 2 content store tables.
-- Applied by init_db() on startup; safe to run multiple times (IF NOT EXISTS).
-- Do NOT modify the existing files / quality_reports tables.

-- sources: one row per logical origin (file upload or web crawl run)
CREATE TABLE IF NOT EXISTS sources (
    id          SERIAL PRIMARY KEY,
    source_type TEXT NOT NULL CHECK (source_type IN ('file', 'web')),
    name        TEXT,
    origin      TEXT,
    file_id     INT NULL REFERENCES files(id),
    created_at  TIMESTAMP DEFAULT NOW()
);

-- documents: one row per page of text content
CREATE TABLE IF NOT EXISTS documents (
    id                     SERIAL PRIMARY KEY,
    source_id              INT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    modality               TEXT,
    title                  TEXT,
    url                    TEXT,
    page_number            INT NULL,
    content                TEXT,
    content_hash           TEXT,
    word_count             INT,
    extraction_confidence  FLOAT NULL,
    published_at           TIMESTAMP NULL,
    fetched_at             TIMESTAMP DEFAULT NOW(),
    metadata               JSONB DEFAULT '{}',
    quality                JSONB DEFAULT '{}'
);

-- records: one row per CSV/XLSX data row
CREATE TABLE IF NOT EXISTS records (
    id         SERIAL PRIMARY KEY,
    source_id  INT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    row_index  INT,
    data       JSONB
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_documents_source_id    ON documents(source_id);
CREATE INDEX IF NOT EXISTS idx_documents_content_hash ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_records_source_id      ON records(source_id);
