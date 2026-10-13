# Sift — Phase 1 + Phase 2

A FastAPI backend that ingests files and websites, runs data-quality profiling, persists all content to PostgreSQL, and surfaces heuristic quality/trust signals.

---

## What's New in Phase 2

| Feature | Details |
|---|---|
| **Content store** | Parsed text is now persisted: `sources`, `documents`, `records` tables |
| **Web ingestion** | `POST /ingest/url` crawls a site via sitemap.xml and stores pages |
| **Text quality checks** | Exact/near duplicates, freshness, short-content, prompt-injection suspects |
| **New endpoints** | `GET /sources`, `GET /sources/{id}/documents`, `GET /sources/{id}/quality`, `GET /documents/{id}` |

Phase 1 endpoints (`POST /upload`, `GET /reports`, `GET /reports/{id}`) are unchanged. `/upload` now also returns `source_id`.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | `python --version` to check |
| PostgreSQL | Running on `localhost:8887`, db `sift` or `ragstudio`, user `postgres` (or set `DATABASE_URL` env var) |
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

---

## Text Quality Checks (All Heuristics)

All checks are **heuristics** — they may produce false positives/negatives and are labeled as such. Content is **never modified or deleted**.

| Check | Method | Description |
|---|---|---|
| Exact duplicate | SHA-256 of normalised text | Identical content under different URLs |
| Near-duplicate | Word 5-gram Jaccard ≥ 0.85 | Very similar content |
| Freshness | Age from `published_at` | fresh / aging / stale / unknown |
| Short content | < 30 words | Possibly truncated or empty |
| Injection suspect | Regex patterns | "ignore previous instructions", "act as", etc. |

---

## Crawler Safety

The web crawler enforces mandatory safety constraints:
- **SSRF protection**: hostname resolved via `socket.getaddrinfo`; loopback, private, link-local, multicast, unspecified and reserved IPs are rejected
- **Redirect re-validation**: SSRF check performed on every redirect hop
- **robots.txt**: respected via `urllib.robotparser` (user-agent: SiftBot)
- **Rate limiting**: 1.5s delay between requests to the same host
- **Timeouts**: 10s per request
- **Size limit**: 5 MB per response
- **Redirect limit**: max 3 redirects
- **No JavaScript**: static HTML only (JS-rendered pages reported as `likely_js_rendered`)

---

## Design Notes

- The profiler and quality checks are **read-only** — they never modify uploaded data.
- `json.dumps(..., default=str)` applied before every JSONB insert.
- No embeddings, no LLM calls, no vector search, no chunking — Phase 2 only.
- No single "trust score" — each quality signal is reported independently as a heuristic.
- Spec ambiguities resolved: non-HTML responses skipped; near-duplicate check scoped per source; robots.txt 404 → allow all.
