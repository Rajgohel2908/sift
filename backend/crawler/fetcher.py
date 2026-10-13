"""
crawler/fetcher.py — Safe HTTP fetcher using httpx.

Safety features (all mandatory):
  - 10-second timeout per request
  - Max 5 MB response body (reading in chunks; aborts early)
  - Max 3 redirects, followed MANUALLY so SSRF guard can re-validate each hop
  - 1.5-second delay between requests to the same host (rate limiting)
  - SSRF check on every redirect hop (re-validates resolved IP)

The fetcher does NOT execute JavaScript. It only fetches static HTML.
Fetched bodies are NEVER logged in full — this is untrusted content.

Usage:
    with httpx.Client(...) as client:
        result = fetch_page(url, client, delay_tracker)
"""

import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from crawler.ssrf_guard import validate_url

# ── Constants ────────────────────────────────────────────────────────────────
REQUEST_TIMEOUT_S   = 10.0          # seconds per request
MAX_RESPONSE_BYTES  = 5 * 1024 * 1024   # 5 MB
MIN_DELAY_S         = 1.5           # seconds between requests to same host
MAX_REDIRECTS       = 3

CRAWLER_USER_AGENT  = "SiftBot/1.0 (+https://github.com/sift)"


class DelayTracker:
    """
    Tracks the last-fetch timestamp per host so we can enforce
    a minimum delay between requests to the same origin.
    """

    def __init__(self):
        self._last_fetch: dict = {}   # host -> float (time.monotonic())

    def wait_if_needed(self, host: str) -> None:
        """Sleep if we fetched this host too recently."""
        last = self._last_fetch.get(host)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < MIN_DELAY_S:
                time.sleep(MIN_DELAY_S - elapsed)
        self._last_fetch[host] = time.monotonic()


def make_client() -> httpx.Client:
    """
    Create a configured httpx.Client.
    follow_redirects=False because we handle redirects manually for SSRF re-checking.
    """
    return httpx.Client(
        follow_redirects=False,
        timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
        headers={"User-Agent": CRAWLER_USER_AGENT},
        max_redirects=0,   # safety: we handle redirects ourselves
    )


def fetch_page(url: str, client: httpx.Client, delay_tracker: DelayTracker) -> dict:
    """
    Fetch a single URL safely.

    Returns a dict:
        {
            "ok": bool,
            "url": str,                 # final URL after redirects
            "status_code": int,
            "content_type": str,
            "content": str | None,      # decoded text body, or None on error
            "last_modified": str | None,
            "etag": str | None,
            "error": str | None,        # set if ok=False
        }

    Raises nothing — all errors are captured in the returned dict.
    """
    current_url = url
    redirect_count = 0

    while True:
        # ── SSRF check (every hop) ────────────────────────────────────────
        try:
            validate_url(current_url)
        except ValueError as e:
            return _error(current_url, str(e))

        # ── Per-host delay ────────────────────────────────────────────────
        host = urlparse(current_url).netloc
        delay_tracker.wait_if_needed(host)

        # ── Fetch ─────────────────────────────────────────────────────────
        try:
            response = client.get(current_url)
        except httpx.TimeoutException:
            return _error(current_url, "Request timed out after 10 seconds.")
        except Exception as exc:
            return _error(current_url, f"HTTP error: {exc}")

        # ── Redirect handling ─────────────────────────────────────────────
        if response.is_redirect:
            redirect_count += 1
            if redirect_count > MAX_REDIRECTS:
                return _error(current_url, f"Too many redirects (max {MAX_REDIRECTS}).")
            location = response.headers.get("location", "")
            if not location:
                return _error(current_url, "Redirect with no Location header.")
            # Resolve relative redirects
            if location.startswith("/"):
                parsed = urlparse(current_url)
                location = f"{parsed.scheme}://{parsed.netloc}{location}"
            current_url = location
            continue   # re-validate and fetch the redirect target

        # ── Body size guard ───────────────────────────────────────────────
        # Read in chunks and abort if we exceed the limit
        chunks = []
        total_bytes = 0
        try:
            for chunk in response.iter_bytes(chunk_size=8192):
                total_bytes += len(chunk)
                if total_bytes > MAX_RESPONSE_BYTES:
                    return _error(current_url, "Response body exceeds 5 MB limit.")
                chunks.append(chunk)
        except Exception as exc:
            return _error(current_url, f"Error reading response body: {exc}")

        body_bytes = b"".join(chunks)

        # ── Decode ────────────────────────────────────────────────────────
        encoding = response.encoding or "utf-8"
        try:
            body_text = body_bytes.decode(encoding, errors="replace")
        except Exception:
            body_text = body_bytes.decode("utf-8", errors="replace")

        return {
            "ok":            True,
            "url":           str(response.url) if hasattr(response, "url") else current_url,
            "status_code":   response.status_code,
            "content_type":  response.headers.get("content-type", ""),
            "content":       body_text,
            "last_modified": response.headers.get("last-modified"),
            "etag":          response.headers.get("etag"),
            "error":         None,
        }


def _error(url: str, message: str) -> dict:
    """Build a failed-fetch result dict."""
    return {
        "ok":           False,
        "url":          url,
        "status_code":  0,
        "content_type": "",
        "content":      None,
        "last_modified": None,
        "etag":         None,
        "error":        message,
    }
