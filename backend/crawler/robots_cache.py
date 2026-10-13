"""
crawler/robots_cache.py — Robots.txt fetching and per-origin caching.

Uses urllib.robotparser from the standard library.
User-agent: SiftBot

Usage:
    cache = RobotsCache()
    cache.fetch("http://example.com", client)          # fetches /robots.txt once
    allowed = cache.is_allowed("http://example.com/page")
    sitemap_urls = cache.get_sitemap_urls("http://example.com")
"""

import re
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

USER_AGENT = "SiftBot"


class RobotsCache:
    """
    In-memory cache of RobotFileParser instances, one per origin.
    Fetching is done synchronously using httpx (passed in from caller).
    """

    def __init__(self):
        # origin (e.g. "http://example.com") -> RobotFileParser
        self._parsers: dict = {}
        # origin -> list of Sitemap: URLs found in robots.txt
        self._sitemaps: dict = {}

    def fetch(self, origin: str, client) -> None:
        """
        Fetch and parse robots.txt for `origin` if not already cached.

        Args:
            origin: scheme + host (e.g. "https://example.com")
            client: httpx.Client instance
        """
        if origin in self._parsers:
            return   # already cached

        robots_url = f"{origin.rstrip('/')}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)

        try:
            response = client.get(robots_url, timeout=10.0, follow_redirects=True)
            if response.status_code == 200:
                content = response.text
                # Feed the content to the parser line-by-line
                parser.parse(content.splitlines())
                # Also extract Sitemap: directives from the raw text
                self._sitemaps[origin] = _extract_sitemap_lines(content)
            else:
                # Non-200 → treat as "allow all" (standard practice)
                parser.parse([])
                self._sitemaps[origin] = []
        except Exception:
            # Network error → treat as "allow all"
            parser.parse([])
            self._sitemaps[origin] = []

        self._parsers[origin] = parser

    def is_allowed(self, url: str) -> bool:
        """
        Check whether SiftBot is allowed to fetch `url`.
        Returns True if robots.txt hasn't been fetched for this origin yet
        (caller should call fetch() first).
        """
        origin = _get_origin(url)
        parser = self._parsers.get(origin)
        if parser is None:
            return True   # no robots.txt loaded — assume allowed
        return parser.can_fetch(USER_AGENT, url)

    def get_sitemap_urls(self, origin: str) -> list:
        """Return Sitemap: URLs found in robots.txt for this origin."""
        return self._sitemaps.get(origin, [])


def _get_origin(url: str) -> str:
    """Return scheme + netloc (e.g. 'https://example.com')."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _extract_sitemap_lines(robots_text: str) -> list:
    """
    Extract all 'Sitemap: <url>' lines from a robots.txt body.
    Returns a list of URL strings.
    """
    urls = []
    for line in robots_text.splitlines():
        # robots.txt Sitemap directive (case-insensitive per spec)
        m = re.match(r"^\s*Sitemap\s*:\s*(.+)$", line, re.IGNORECASE)
        if m:
            urls.append(m.group(1).strip())
    return urls
