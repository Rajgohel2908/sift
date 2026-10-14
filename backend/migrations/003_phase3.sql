-- migrations/003_phase3.sql
-- Phase 3 cleaning workflow: versioning, roles, proposals, transforms.
-- Applied by init_db() on startup; all statements are idempotent.
-- Do NOT modify Phase 1 / Phase 2 tables except the two ALTER statements below.

-- ── 1. Add version_no + status to records ────────────────────────────────────
ALTER TABLE records
    ADD COLUMN IF NOT EXISTS version_no INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS status     TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'quarantined', 'excluded')),
    ADD COLUMN IF NOT EXISTS metadata   JSONB DEFAULT '{}';

-- ── 2. Add version_no + status to documents ──────────────────────────────────
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS version_no INT NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS status     TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'quarantined', 'excluded'));

-- ── 3. source_versions: one row per (source, version) ────────────────────────
-- Stores the re-profile result so we can diff across versions.
CREATE TABLE IF NOT EXISTS source_versions (
    id             SERIAL PRIMARY KEY,
    source_id      INT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    version_no     INT NOT NULL,
    parent_version INT NULL,      -- NULL for version 1
    note           TEXT,
    profile        JSONB,         -- profiler output for this version's active rows
    created_at     TIMESTAMP DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_source_version
    ON source_versions (source_id, version_no);

-- ── 4. column_roles: inferred or confirmed role for each column ───────────────
CREATE TABLE IF NOT EXISTS column_roles (
    id          SERIAL PRIMARY KEY,
    source_id   INT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    column_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN (
                    'identifier', 'required', 'optional',
                    'date', 'category', 'numeric', 'free_text')),
    confirmed   BOOLEAN DEFAULT FALSE,
    UNIQUE (source_id, column_name)
);

-- ── 5. cleaning_proposals: one row per proposed fix ──────────────────────────
CREATE TABLE IF NOT EXISTS cleaning_proposals (
    id           SERIAL PRIMARY KEY,
    source_id    INT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    base_version INT NOT NULL,
    kind         TEXT NOT NULL,
    target       TEXT NOT NULL,   -- column name or '*' for row-level proposals
    severity     TEXT NOT NULL CHECK (severity IN ('critical', 'warning', 'info')),
    description  TEXT,
    impact       JSONB,           -- {rows_affected, examples: [{before, after}]}
    params       JSONB,           -- proposal-type-specific params
    status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'approved', 'rejected', 'applied')),
    created_at   TIMESTAMP DEFAULT NOW(),
    decided_at   TIMESTAMP NULL
);

-- ── 6. transform_log: one row per applied proposal ───────────────────────────
CREATE TABLE IF NOT EXISTS transform_log (
    id            SERIAL PRIMARY KEY,
    proposal_id   INT NOT NULL REFERENCES cleaning_proposals(id),
    version_from  INT NOT NULL,
    version_to    INT NOT NULL,
    rows_affected INT NOT NULL DEFAULT 0,
    sample        JSONB,          -- [{before, after}] up to 5 rows
    applied_at    TIMESTAMP DEFAULT NOW()
);

-- ── 7. Backfill source_versions version 1 for all existing sources ───────────
-- Uses WHERE NOT EXISTS so it is safe to re-run without needing ON CONFLICT.
INSERT INTO source_versions (source_id, version_no, parent_version, note)
SELECT s.id, 1, NULL, 'initial version (backfill)'
FROM   sources s
WHERE  NOT EXISTS (
    SELECT 1 FROM source_versions sv
    WHERE  sv.source_id = s.id AND sv.version_no = 1
);

-- ── 8. Indexes ────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_records_source_version
    ON records(source_id, version_no);

CREATE INDEX IF NOT EXISTS idx_documents_source_version
    ON documents(source_id, version_no);

CREATE INDEX IF NOT EXISTS idx_proposals_source_status
    ON cleaning_proposals(source_id, status);

CREATE INDEX IF NOT EXISTS idx_column_roles_source
    ON column_roles(source_id);
