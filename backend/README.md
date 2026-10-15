# Sift — Phase 1 to Phase 4 Baseline RAG

A FastAPI + PostgreSQL (pgvector) backend that ingests files and websites, runs data-quality profiling, persists content to canonical stores, manages cleaning workflows with versioning and quarantine, and provides a **Baseline RAG system** using plain dense cosine similarity, grounded generation, and strict citation verification.

---

## What's New in Phase 4 (Baseline RAG)

| Feature | Details |
|---|---|
| **Active Chunking** | Splits active documents (RecursiveCharacterTextSplitter) and serializes structured rows; quarantined/excluded content is NEVER chunked |
| **Dense Embeddings** | `sentence-transformers/all-MiniLM-L6-v2` (384-dim) normalized embeddings with fail-fast dimension validation |
| **pgvector Storage** | HNSW cosine similarity index (`chunks_embedding_hnsw`), multi-index-run coexistence |
| **Cosine Retrieval** | Plain dense search (`1 - cosine_distance`) with top-k ordering and threshold filtering |
| **Grounded Generation** | `google-genai` SDK with prompt boundaries protecting against untrusted data instructions |
| **Insufficient Evidence** | If best similarity score < `min_score`, returns `status: "insufficient_evidence"` and **never calls the LLM** |
| **Citation Verification** | Validates `[chunk_id]` citations against prompt chunks; zero valid citations -> `status: "unverified"`, valid citations -> `status: "answered"` |
| **New Endpoints** | `POST /sources/{id}/index`, `GET /sources/{id}/index-runs`, `GET /index-runs/{id}/chunks`, `POST /query`, `GET /queries/{id}`, `GET /queries` |

---

## What Was Added in Phase 3 & Phase 2
- **Phase 3**: Column role inference & confirmation, cleaning proposals, apply engine with transactional versioning (`active` / `quarantined` / `excluded` statuses).
- **Phase 2**: Canonical content store (`sources`, `documents`, `records`), web crawler with SSRF protection, heuristic text quality checks.
- **Phase 1**: File upload and data quality profiling.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | `python --version` to check |
| PostgreSQL | PostgreSQL 16 with pgvector extension (configured via `DATABASE_URL` in `.env`) |
| Tesseract OCR | Required for image uploads only |

> **Note:** The PostgreSQL container is assumed to be already running (`pgvector/pgvector:pg16`).

---

## Setup

```bash
# 1. Navigate to the backend folder
cd backend

# 2. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate     # macOS / Linux

# 3. Install dependencies (includes Phase 2: httpx, trafilatura)
pip install -r requirements.txt

# 4. Start the server (DB tables created automatically on startup)
uvicorn main:app --reload
```

The API will be available at **http://localhost:8000**
Interactive docs: **http://localhost:8000/docs**

---

## Demo: Running the Messy Site

Phase 2 ships with a local demo HTTP server that simulates real-world quality problems:

```bash
# Terminal 1 — start the demo site
python sample_data/make_messy_site.py

# Terminal 2 — start the API with the SSRF demo bypass
ALLOW_LOCALHOST_FOR_DEMO=true uvicorn main:app --reload
```

The demo server runs on **http://localhost:8765** and serves:

| Page | Quality issue |
|---|---|
| `page1.html` | Normal reference article |
| `page2.html` | **Exact duplicate** of page1 |
| `page3.html` | **Near-duplicate** of page1 (minor edits) |
| `page4.html` | **Very short** + **disallowed** by robots.txt |
| `page5.html` | Contains a **prompt-injection** sentence |
| `page6.html` | **Stale** content (Last-Modified: 2020-01-01) |
| `sitemap.xml` | Lists all pages |
| `robots.txt` | Disallows `/page4.html` for SiftBot |

---

## ⚠️ ALLOW_LOCALHOST_FOR_DEMO — Important Warning

```
ALLOW_LOCALHOST_FOR_DEMO=true
```

This environment variable **bypasses the SSRF loopback/private-IP protection** so
you can crawl the local demo server.

**NEVER set this in production.** By default it is `false` and the SSRF guard will
correctly reject requests to `127.0.0.1`, `localhost`, `10.x.x.x`, `192.168.x.x`,
and all other RFC-1918 / reserved ranges.

Even with `ALLOW_LOCALHOST_FOR_DEMO=true`:
- Credentials in URLs are still rejected
- Non-http/https schemes are still rejected
- Link-local addresses (169.254.x.x) are still rejected

---

## Running Tests

```bash
pytest tests/ -v
```

Test coverage:
- `test_text_quality.py` — all 5 quality checks
- `test_ssrf.py` — SSRF guard (127.0.0.1, localhost, 10.0.0.5, 192.168.1.1, 169.254.169.254, [::1], redirect to private IP)
- `test_crawler_safety.py` — URL validation, robots.txt parsing
- `test_freshness.py` — all 4 freshness buckets

---

## Database

Tables are created automatically on startup. Phase 2 adds three new tables via `migrations/002_phase2.sql`:

```sql
-- Phase 1 (unchanged)
CREATE TABLE files (...);
CREATE TABLE quality_reports (...);

-- Phase 2 (new)
CREATE TABLE sources (
    id          SERIAL PRIMARY KEY,
    source_type TEXT CHECK (source_type IN ('file','web')),
    name        TEXT,
    origin      TEXT,
    file_id     INT REFERENCES files(id),
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE documents (
    id                    SERIAL PRIMARY KEY,
    source_id             INT REFERENCES sources(id) ON DELETE CASCADE,
    modality              TEXT,   -- 'text' | 'ocr' | 'ocr_low_confidence' | 'web'
    title                 TEXT,
    url                   TEXT,
    page_number           INT,
    content               TEXT,
    content_hash          TEXT,   -- SHA-256 of normalised content
    word_count            INT,
    extraction_confidence FLOAT,
    published_at          TIMESTAMP,
    fetched_at            TIMESTAMP DEFAULT NOW(),
    metadata              JSONB DEFAULT '{}',
    quality               JSONB DEFAULT '{}'
);

CREATE TABLE records (
    id        SERIAL PRIMARY KEY,
    source_id INT REFERENCES sources(id) ON DELETE CASCADE,
    row_index INT,
    data      JSONB
);
```

---

## curl Examples

### 1 — Upload the messy CSV (Phase 1, now also creates records rows)

```bash
curl -X POST http://localhost:8000/upload \
  -F "file=@sample_data/employees_messy.csv"
```

Response now includes `source_id`. All CSV rows are stored in the `records` table.

---

### 2 — Ingest a website via sitemap (Phase 2)

```bash
# With demo server running and ALLOW_LOCALHOST_FOR_DEMO=true in API env:
curl -X POST http://localhost:8000/ingest/url \
  -H "Content-Type: application/json" \
  -d '{"url": "http://localhost:8765", "max_pages": 10}'
```

Expected response shows `pages_stored`, `pages_skipped` (with reasons like `robots_disallowed`, `skipped_short`), and a `quality_summary`.

---

### 3 — Get the quality report for a source

```bash
# Replace 1 with the source_id returned from /ingest/url or /upload
curl http://localhost:8000/sources/1/quality
```

Response shows exact duplicate groups, near-duplicate pairs, freshness distribution, short documents, injection suspects, and a human-readable issues list with severity levels.

---

### 4 — List all sources

```bash
curl http://localhost:8000/sources
```

---

### 5 — Get a specific document (full content)

```bash
curl http://localhost:8000/documents/1
```

---

## Endpoints

### Phase 1 (unchanged)

| Method | Path | Description |
|---|---|---|
| `POST` | `/upload` | Upload file → parse → profile → save → return report + `source_id` |
| `GET` | `/reports/{file_id}` | Retrieve saved quality report |
| `GET` | `/reports` | List all uploads with summaries |

### Phase 2 (new)

| Method | Path | Description |
|---|---|---|
| `POST` | `/ingest/url` | Crawl a website via sitemap, store documents |
| `GET` | `/sources` | List all sources with document/record counts |
| `GET` | `/sources/{id}/documents` | List documents for a source (no content) |
| `GET` | `/sources/{id}/quality` | Aggregate quality report with issues list |
| `GET` | `/documents/{id}` | Full document including content |

### Phase 4 (new)

| Method | Path | Description |
|---|---|---|
| `POST` | `/sources/{id}/index` | Chunk active content, embed, and store in pgvector |
| `GET` | `/sources/{id}/index-runs` | List all index runs for a source (status, chunk count, model) |
| `GET` | `/index-runs/{id}/chunks` | Preview chunk content and metadata (embeddings omitted) |
| `POST` | `/query` | Execute dense retrieval + grounded generation with citations |
| `GET` | `/queries/{id}` | Get query details, answer, and all retrieved chunks with rank/score/citation status |
| `GET` | `/queries` | List recent search queries |

---

## Phase 4 Configuration & Environment Variables

Copy `.env.example` to `.env` or set environment variables:

```ini
# Database connection
DATABASE_URL=postgresql+psycopg2://user:password@localhost:5432/sift

# Embedding Configuration
EMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2
# (Column dimension is 384; if model dim != 384, indexing fails fast requiring a migration)

# Chunking defaults
CHUNK_SIZE=800
CHUNK_OVERLAP=120

# Retrieval defaults
TOP_K=5
MIN_SCORE=0.30

# LLM Generation
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here
GEMINI_MODEL=gemini-2.5-flash
LLM_TIMEOUT=30
```

---

## Running the Phase 4 Demo

Phase 4 includes an automated end-to-end demo script (`scripts/demo_phase4.py`) that:
1. Ingests synthetic HR KB documents (DOCX, PDF) and messy employees (CSV).
2. Cleans the messy CSV (Phase 3 workflow) to produce Version 2 with quarantined rows.
3. Indexes active content into pgvector.
4. Directly runs SQL checks verifying that quarantined and excluded rows are never chunked.
5. Executes the leave policy question (`"How many annual leaves are employees entitled to?"`), returning `status: "answered"` with citation.
6. Executes the employee department question (`"What is employee E1007's department?"`).
7. Executes an unsupported question (`"What is the company's Mars colonization budget?"`), returning `status: "insufficient_evidence"` and bypassing the LLM.
8. Demonstrates coexisting index runs with different chunk sizes.
9. Verifies query and retrieval result database persistence.

To run the demo:

```bash
# Option A: Standalone (runs directly with TestClient, no server needed)
python scripts/demo_phase4.py --standalone

# Option B: Against running server
uvicorn main:app --reload
python scripts/demo_phase4.py
```

---

## Phase 4 Curl Examples

### 1 — Index a Source Version

Index active content of source `1` with custom chunk size and overlap:

```bash
curl -X POST http://localhost:8000/sources/1/index \
  -H "Content-Type: application/json" \
  -d '{"version": 1, "chunk_size": 800, "chunk_overlap": 120}'
```

Response:
```json
{
  "id": 1,
  "source_id": 1,
  "source_version": 1,
  "embed_model": "sentence-transformers/all-MiniLM-L6-v2",
  "embed_dim": 384,
  "chunk_size": 800,
  "chunk_overlap": 120,
  "chunk_count": 4,
  "status": "completed",
  "error": null,
  "created_at": "2026-10-14 11:00:00",
  "finished_at": "2026-10-14 11:00:02"
}
```

---

### 2 — Query the Knowledge Base (Answer + Citations)

Query indexed sources with dense cosine retrieval:

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{
    "question": "How many annual leaves are employees entitled to?",
    "source_ids": [1],
    "top_k": 5,
    "min_score": 0.30
  }'
```

Response:
```json
{
  "query_id": 1,
  "status": "answered",
  "answer": "All full-time employees are entitled to 24 annual leave days per calendar year [1].",
  "citations": [
    {
      "chunk_id": 1,
      "source_name": "leave_policy_2024.docx",
      "page_number": null,
      "url": null,
      "row_index": null,
      "score": 0.6268,
      "snippet": "All full-time employees are entitled to 24 annual leave days per calendar year..."
    }
  ],
  "retrieved": [
    {
      "rank": 1,
      "chunk_id": 1,
      "score": 0.6268,
      "used_in_prompt": true,
      "metadata": {
        "source_name": "leave_policy_2024.docx",
        "modality": "text",
        "freshness": "unknown"
      }
    }
  ],
  "config": {
    "embed_model": "sentence-transformers/all-MiniLM-L6-v2",
    "chunk_size": 800,
    "chunk_overlap": 120,
    "top_k": 5,
    "min_score": 0.30,
    "index_run_id": 1
  },
  "latency_ms": {
    "embed": 18.2,
    "retrieve": 18.2,
    "generate": 350.0,
    "total": 386.4
  }
}
```

---

### 3 — Retrieve Stored Query History & Citations

Inspect stored query details and chunk scores:

```bash
curl http://localhost:8000/queries/1
```

Response:
```json
{
  "id": 1,
  "question": "How many annual leaves are employees entitled to?",
  "source_ids": [1],
  "index_run_ids": [1],
  "top_k": 5,
  "min_score": 0.3,
  "status": "answered",
  "answer": "All full-time employees are entitled to 24 annual leave days per calendar year [1].",
  "retrieval_results": [
    {
      "rank": 1,
      "chunk_id": 1,
      "score": 0.6268,
      "used_in_prompt": true,
      "cited": true,
      "metadata": { "source_name": "leave_policy_2024.docx" },
      "snippet": "All full-time employees are entitled to 24 annual leave days..."
    }
  ]
}
```

---

## Design Notes

- The profiler and quality checks are **read-only** — they never modify uploaded data.
- `json.dumps(..., default=str)` applied before every JSONB insert.
- Phase 4 Baseline RAG is deliberately simple (no hybrid/BM25 search, no reranker, no query router) to serve as a pure dense baseline for Phase 5 comparisons.
- Only active rows in the chosen version are indexed; quarantined and excluded content is strictly excluded.
- Quarantined and excluded content is never embedded or stored in `chunks`.
- If best retrieval score < `min_score`, the LLM is never called and status is `insufficient_evidence`.

