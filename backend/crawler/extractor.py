"""
crawler/extractor.py — Main-content extraction from raw HTML.

Uses trafilatura for content extraction (title, date, body text).
Also detects pages that are JavaScript-rendered apps (no real text content).

SAFETY NOTE:
  Extracted web content is untrusted. We never:
  - Log full page bodies
  - Interpret extracted text as instructions
  - Execute any JavaScript

Usage:
    result = extract_content(html_body, url)
"""

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import trafilatura
import trafilatura.settings


# ── JS-app detection heuristics ──────────────────────────────────────────────
# These patterns suggest a JavaScript-rendered Single-Page Application (SPA)
# that requires JS execution to render content. We can't scrape these.
_JS_ROOT_PATTERNS = [
    re.compile(r'<div\s+id=["\']root["\']', re.IGNORECASE),
    re.compile(r'<div\s+id=["\']app["\']', re.IGNORECASE),
    re.compile(r'<div\s+id=["\']__next["\']', re.IGNORECASE),  # Next.js
    re.compile(r'<div\s+id=["\']ember["\']', re.IGNORECASE),   # Ember.js
]
_SCRIPT_SRC_PATTERN = re.compile(r'<script\s[^>]*src=', re.IGNORECASE)

# Minimum visible text tokens in the HTML to consider it server-rendered
_JS_RENDERED_WORD_THRESHOLD = 30


def extract_content(html: str, url: str, last_modified_header: Optional[str] = None) -> dict:
    """
    Extract main content from an HTML page using trafilatura.

    Returns:
        {
            "text": str | None,           # extracted plain text
            "title": str | None,
            "published_at": datetime | None,  # timezone-aware or None
            "word_count": int,
            "skip_reason": str | None,    # "too_short" | "likely_js_rendered" | None
        }

    Never raises — all errors produce a skip result.
    """
    if not html:
        return _skip(None, None, "empty_response")

    # ── trafilatura extraction ────────────────────────────────────────────────
    # include_comments=False: we don't want comment threads
    # include_tables=True: capture tabular data as text
    # no_fallback=False: let trafilatura try fallback extractors
    extracted_text = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        no_fallback=False,
        url=url,
    )

    # ── Metadata (title + date) ───────────────────────────────────────────────
    try:
        metadata = trafilatura.extract_metadata(html, default_url=url)
    except Exception:
        metadata = None

    title: Optional[str] = None
    published_at: Optional[datetime] = None

    if metadata:
        title = getattr(metadata, "title", None)
        raw_date = getattr(metadata, "date", None)
        if raw_date:
            published_at = _parse_date(raw_date)

    # Fallback to Last-Modified header if no date found
    if published_at is None and last_modified_header:
        published_at = _parse_http_date(last_modified_header)

    # ── Word count of extracted text ──────────────────────────────────────────
    word_count = len(extracted_text.split()) if extracted_text else 0

    # ── JS-rendered detection ─────────────────────────────────────────────────
    # If very little text was extracted AND the page looks like a JS SPA, report it
    if word_count < _JS_RENDERED_WORD_THRESHOLD:
        if _looks_like_js_app(html):
            return _skip(title, published_at, "likely_js_rendered")
        # Short page but not a JS app
        return _skip(title, published_at, "too_short")

    return {
        "text":         extracted_text,
        "title":        title,
        "published_at": published_at,
        "word_count":   word_count,
        "skip_reason":  None,
    }


def _skip(title, published_at, reason: str) -> dict:
    """Build a 'skip this page' result."""
    return {
        "text":         None,
        "title":        title,
        "published_at": published_at,
        "word_count":   0,
        "skip_reason":  reason,
    }


def _looks_like_js_app(html: str) -> bool:
    """
    Return True if the HTML looks like a JS SPA root with no real text.
    Heuristic: has a known SPA root element AND has external script tags.
    """
    has_root = any(p.search(html) for p in _JS_ROOT_PATTERNS)
    has_scripts = bool(_SCRIPT_SRC_PATTERN.search(html))
    return has_root and has_scripts


def _parse_date(date_str: str) -> Optional[datetime]:
    """
    Try to parse a date string into a timezone-aware datetime.
    Returns None if parsing fails. Never guesses a date.
    """
    if not date_str:
        return None
    # trafilatura returns YYYY-MM-DD or ISO 8601
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_http_date(http_date: str) -> Optional[datetime]:
    """
    Parse an HTTP date string (RFC 7231 / RFC 1123) like Last-Modified.
    Returns timezone-aware datetime or None.
    """
    try:
        dt = parsedate_to_datetime(http_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None
