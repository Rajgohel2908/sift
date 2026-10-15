-- migrations/004_phase4.sql
-- Phase 4 Baseline RAG tables: index_runs, chunks, queries, retrieval_results.
-- Applied by init_db() on startup; idempotent with IF NOT EXISTS.
-- Do NOT modify existing Phase 1 - 3 tables.

-- 1. Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. index_runs: track indexing executions per source and configuration
CREATE TABLE IF NOT EXISTS index_runs (
    id             SERIAL PRIMARY KEY,
    source_id      INT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    source_version INT NOT NULL,
    embed_model    TEXT NOT NULL,
    embed_dim      INT NOT NULL,
    chunk_size     INT NOT NULL,
    chunk_overlap  INT NOT NULL,
    chunk_count    INT DEFAULT 0,
    status         TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    error          TEXT NULL,
    created_at     TIMESTAMP DEFAULT NOW(),
    finished_at    TIMESTAMP NULL
);

-- 3. chunks: chunked text and row representations with dense vector embeddings
CREATE TABLE IF NOT EXISTS chunks (
    id             SERIAL PRIMARY KEY,
    index_run_id   INT NOT NULL REFERENCES index_runs(id) ON DELETE CASCADE,
    source_id      INT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    document_id    INT NULL REFERENCES documents(id) ON DELETE CASCADE,
    record_id      INT NULL REFERENCES records(id) ON DELETE CASCADE,
    chunk_index    INT NOT NULL,
    content        TEXT NOT NULL,
    content_hash   TEXT NOT NULL,
    token_estimate INT NOT NULL,
    embedding      vector(384),
    metadata       JSONB DEFAULT '{}'
);

-- 4. Vector HNSW index and standard B-tree lookup indexes
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_chunks_index_run_id ON chunks(index_run_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source_id ON chunks(source_id);

-- 5. queries: store search questions, configuration parameters, and generation outcomes
CREATE TABLE IF NOT EXISTS queries (
    id             SERIAL PRIMARY KEY,
    question       TEXT NOT NULL,
    source_ids     INT[] NOT NULL,
    index_run_ids  INT[] NOT NULL,
    top_k          INT NOT NULL,
    min_score      FLOAT NOT NULL,
    status         TEXT NOT NULL,
    answer         TEXT NULL,
    latency_ms     JSONB DEFAULT '{}',
    created_at     TIMESTAMP DEFAULT NOW()
);

-- 6. retrieval_results: per-query retrieved chunks, scores, prompt usage, and citation status
CREATE TABLE IF NOT EXISTS retrieval_results (
    id             SERIAL PRIMARY KEY,
    query_id       INT NOT NULL REFERENCES queries(id) ON DELETE CASCADE,
    chunk_id       INT NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    rank           INT NOT NULL,
    score          FLOAT NOT NULL,
    used_in_prompt BOOLEAN NOT NULL,
    cited          BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_retrieval_results_query_id ON retrieval_results(query_id);
