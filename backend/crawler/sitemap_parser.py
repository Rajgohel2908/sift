"""
crawler/sitemap_parser.py — Sitemap.xml and sitemap-index parsing.

Parses standard XML sitemaps and one level of sitemap index files.
Only returns URLs from the same domain as the seed.
Uses stdlib xml.etree.ElementTree — no third-party XML library needed.

Usage:
    urls = collect_urls(seed_url, client, robots_cache, max_pages=20)
"""

import xml.etree.ElementTree as ET
from urllib.parse import urlparse


# XML namespace used in sitemap.xml files
_NS_SITEMAP = "http://www.sitemaps.org/schemas/sitemap/0.9"


def collect_urls(seed_url: str, client, robots_cache, max_pages: int = 20) -> list:
    """
    Try to collect up to `max_pages` same-domain URLs via sitemap.

    Strategy:
      1. Check for Sitemap: directives in robots.txt (already fetched by caller)
      2. Try <origin>/sitemap.xml directly
      3. If the discovered sitemap is a sitemap index, fetch child sitemaps (1 level deep)

    Returns a list of URL strings (de-duplicated, same domain only, capped at max_pages).
    If no sitemap is found, returns [seed_url] so at least the seed itself is ingested.
    """
    origin = _get_origin(seed_url)
    seed_domain = _domain(seed_url)

    # Step 1: get candidate sitemap URLs
    sitemap_candidates = list(robots_cache.get_sitemap_urls(origin))

    # Step 2: always try the standard location too
    standard_sitemap = f"{origin.rstrip('/')}/sitemap.xml"
    if standard_sitemap not in sitemap_candidates:
        sitemap_candidates.insert(0, standard_sitemap)

    collected: list = []
    seen: set = set()

    for sitemap_url in sitemap_candidates:
        if len(collected) >= max_pages:
            break
        _parse_sitemap(
            sitemap_url, client, seed_domain, collected, seen, max_pages, depth=0
        )

    # Deduplicate while preserving order (already done via `seen`)
    if not collected:
        # No sitemap found — ingest only the seed URL
        return [seed_url]

    return collected[:max_pages]


def _parse_sitemap(
    sitemap_url: str,
    client,
    seed_domain: str,
    collected: list,
    seen: set,
    max_pages: int,
    depth: int,
) -> None:
    """
    Fetch and parse a single sitemap URL.
    `depth` controls how deep into sitemap indexes we recurse (max 1 level).
    """
    if len(collected) >= max_pages:
        return

    try:
        response = client.get(sitemap_url, timeout=10.0, follow_redirects=True)
        if response.status_code != 200:
            return
        content_type = response.headers.get("content-type", "")
        # Accept XML content types (some servers send text/html for sitemaps)
        body = response.text
    except Exception:
        return

    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return

    # Detect whether this is a sitemap index or a regular sitemap
    tag = root.tag.lower()

    if "sitemapindex" in tag:
        # Sitemap index: contains <sitemap><loc>...</loc></sitemap> entries
        if depth >= 1:
            return   # Only go one level deep
        for sitemap_elem in root.iter(_tag("sitemap")):
            loc_elem = sitemap_elem.find(_tag("loc"))
            if loc_elem is not None and loc_elem.text:
                child_url = loc_elem.text.strip()
                if len(collected) < max_pages:
                    _parse_sitemap(
                        child_url, client, seed_domain,
                        collected, seen, max_pages, depth=1
                    )
    else:
        # Regular sitemap: contains <url><loc>...</loc></url> entries
        for url_elem in root.iter(_tag("url")):
            if len(collected) >= max_pages:
                break
            loc_elem = url_elem.find(_tag("loc"))
            if loc_elem is None or not loc_elem.text:
                continue
            page_url = loc_elem.text.strip()
            # Same-domain filter
            if _domain(page_url) != seed_domain:
                continue
            if page_url not in seen:
                seen.add(page_url)
                collected.append(page_url)


def _tag(local: str) -> str:
    """Build a namespaced tag string for ElementTree queries."""
    return f"{{{_NS_SITEMAP}}}{local}"


def _get_origin(url: str) -> str:
    """Return scheme + netloc (e.g. 'https://example.com')."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _domain(url: str) -> str:
    """Return just the netloc (host:port or host) from a URL."""
    return urlparse(url).netloc
