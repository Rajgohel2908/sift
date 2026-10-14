"""
routers/ingest.py — POST /ingest/url endpoint.

Full crawl pipeline:
  1. Validate URL + SSRF check
  2. Fetch robots.txt
  3. Discover URLs via sitemap (or use the seed URL alone)
  4. For each URL:
     a. robots.txt allowed check
     b. Fetch page
     c. Skip non-HTML
     d. Extract content with trafilatura
     e. Skip short / JS-rendered pages
     f. Run text quality checks
     g. Store document in DB
  5. Create sources row
  6. Return summary

Hard constraints enforced here:
  - max_pages capped at 50
  - 1.5s inter-host delay (in fetcher)
  - 10s timeout (in fetcher)
  - 5MB response limit (in fetcher)
  - 3 redirect max (in fetcher)
  - SSRF validation on every hop (in fetcher via ssrf_guard)
"""

import json
from datetime import datetime
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db import get_db, Source, Document, SourceVersion, safe_json
from crawler.ssrf_guard import validate_url
from crawler.robots_cache import RobotsCache
from crawler.sitemap_parser import collect_urls
from crawler.fetcher import DelayTracker, make_client, fetch_page
from crawler.extractor import extract_content
from profiler.text_quality import (
    compute_content_hash, run_all_checks,
    check_exact_duplicate, check_near_duplicate,
)

router = APIRouter()

MAX_PAGES_HARD_CAP = 50


class IngestUrlRequest(BaseModel):
    url: str
    max_pages: int = 20


@router.post("/ingest/url", summary="Ingest a website via sitemap or single URL")
def ingest_url(body: IngestUrlRequest, db: Session = Depends(get_db)):
    """
    Crawl a website and store extracted text as documents.

    - Validates URL and performs SSRF protection.
    - Respects robots.txt (user-agent: SiftBot).
    - Discovers pages via sitemap.xml (with one level of sitemap index support).
    - Applies 1.5s inter-host delay, 10s timeout, 5MB response limit, max 3 redirects.
    - Extracts main content using trafilatura (no JavaScript execution).
    - Runs heuristic text quality checks on each document.
    - Returns a summary of stored and skipped pages.
    """
    seed_url = body.url.strip()
    max_pages = min(body.max_pages, MAX_PAGES_HARD_CAP)

    # ── 1. Initial URL validation ─────────────────────────────────────────────
    try:
        validate_url(seed_url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    origin = _get_origin(seed_url)
    pages_skipped = []
    stored_docs = []

    with make_client() as client:
        delay_tracker = DelayTracker()
        robots = RobotsCache()

        # ── 2. Fetch robots.txt ───────────────────────────────────────────────
        robots.fetch(origin, client)

        # ── 3. Discover URLs via sitemap ─────────────────────────────────────
        urls = collect_urls(seed_url, client, robots, max_pages=max_pages)

        # ── 4. Fetch and store each page ─────────────────────────────────────
        for page_url in urls:
            # a. robots.txt check
            if not robots.is_allowed(page_url):
                pages_skipped.append({"url": page_url, "reason": "robots_disallowed"})
                continue

            # b. Fetch the page
            fetch_result = fetch_page(page_url, client, delay_tracker)

            if not fetch_result["ok"]:
                pages_skipped.append({
                    "url": page_url,
                    "reason": f"fetch_error: {fetch_result['error']}",
                })
                continue

            # c. Skip non-HTML responses
            content_type = fetch_result.get("content_type", "")
            if "text/html" not in content_type.lower():
                pages_skipped.append({"url": page_url, "reason": "non_html"})
                continue

            # d. Extract content
            extracted = extract_content(
                fetch_result["content"],
                page_url,
                last_modified_header=fetch_result.get("last_modified"),
            )

            # e. Skip short / JS-rendered pages
            if extracted["skip_reason"] == "likely_js_rendered":
                pages_skipped.append({"url": page_url, "reason": "likely_js_rendered"})
                continue
            if extracted["skip_reason"] == "too_short":
                pages_skipped.append({"url": page_url, "reason": "skipped_short"})
                continue
            if extracted["skip_reason"]:
                pages_skipped.append({"url": page_url, "reason": extracted["skip_reason"]})
                continue

            # f. Run per-document quality checks (freshness, short, injection)
            text_content = extracted["text"] or ""
            quality = run_all_checks(text_content, extracted.get("published_at"))

            # g. Build the document record
            content_hash = compute_content_hash(text_content)
            word_count = extracted["word_count"]

            stored_docs.append({
                "url": page_url,
                "title": extracted.get("title"),
                "content": text_content,
                "content_hash": content_hash,
                "word_count": word_count,
                "published_at": extracted.get("published_at"),
                "quality": quality,
                "metadata": safe_json({
                    "status_code":    fetch_result["status_code"],
                    "content_type":   content_type,
                    "last_modified":  fetch_result.get("last_modified"),
                    "etag":           fetch_result.get("etag"),
                }),
            })

    # ── 5. Persist to DB ──────────────────────────────────────────────────────
    # Create the source row
    source = Source(
        source_type="web",
        name=seed_url,
        origin=origin,
    )
    db.add(source)
    db.flush()   # get source.id before inserting documents

    # Phase 3: create version 1 for this web source
    from db import SourceVersion
    db.add(SourceVersion(
        source_id=source.id, version_no=1,
        parent_version=None, note="initial version",
    ))

    # Insert documents
    for doc_data in stored_docs:
        doc = Document(
            source_id=source.id,
            modality="web",
            title=doc_data["title"],
            url=doc_data["url"],
            content=doc_data["content"],
            content_hash=doc_data["content_hash"],
            word_count=doc_data["word_count"],
            published_at=doc_data["published_at"],
            metadata_=doc_data["metadata"],
            quality=safe_json(doc_data["quality"]),
        )
        db.add(doc)

    db.commit()
    db.refresh(source)

    # ── 6. Run cross-document quality checks for the summary ─────────────────
    doc_list_for_checks = [
        {"id": i, "content": d["content"], "content_hash": d["content_hash"]}
        for i, d in enumerate(stored_docs)
    ]
    exact_dup = check_exact_duplicate(doc_list_for_checks)
    near_dup = check_near_duplicate(doc_list_for_checks)

    # ── 7. Build quality summary ──────────────────────────────────────────────
    freshness_dist = {"fresh": 0, "aging": 0, "stale": 0, "unknown": 0}
    injection_count = 0
    short_count = 0

    for d in stored_docs:
        q = d["quality"]
        freshness_level = q.get("freshness", {}).get("flag", "unknown")
        freshness_dist[freshness_level] = freshness_dist.get(freshness_level, 0) + 1
        if q.get("injection_suspect", {}).get("flag"):
            injection_count += 1
        if q.get("short_content", {}).get("flag"):
            short_count += 1

    quality_summary = {
        "exact_duplicate_groups":  len(exact_dup["detail"]["duplicate_groups"]),
        "near_duplicate_pairs":    len(near_dup["detail"]["near_duplicate_pairs"]),
        "freshness_distribution":  freshness_dist,
        "injection_suspects":      injection_count,
        "short_documents":         short_count,
    }

    return {
        "source_id":      source.id,
        "pages_fetched":  len(urls),
        "pages_stored":   len(stored_docs),
        "pages_skipped":  pages_skipped,
        "quality_summary": quality_summary,
    }


def _get_origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"
