"""
tests/test_crawler_safety.py — Unit tests for URL validation and robots.txt.
No real network calls — all HTTP is mocked.
"""

import pytest
from unittest.mock import MagicMock, patch

from crawler.ssrf_guard import validate_url
from crawler.robots_cache import RobotsCache, _extract_sitemap_lines


# ─────────────────────────────────────────────────────────────────────────────
# URL Validation
# ─────────────────────────────────────────────────────────────────────────────

class TestURLValidation:

    def test_http_allowed(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 0, "", ("93.184.216.34", 80))]):
            validate_url("http://example.com")   # must not raise

    def test_https_allowed(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 0, "", ("93.184.216.34", 443))]):
            validate_url("https://example.com/path?q=1")

    def test_javascript_scheme_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_url("javascript:alert(1)")

    def test_empty_url_rejected(self):
        with pytest.raises(ValueError):
            validate_url("")

    def test_credentials_rejected(self):
        with pytest.raises(ValueError, match="credentials"):
            validate_url("https://admin:secret@example.com/")

    def test_user_only_credentials_rejected(self):
        with pytest.raises(ValueError, match="credentials"):
            validate_url("https://admin@example.com/")

    def test_no_hostname_rejected(self):
        with pytest.raises(ValueError):
            validate_url("http:///path")


# ─────────────────────────────────────────────────────────────────────────────
# robots.txt parsing — _extract_sitemap_lines
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractSitemapLines:

    def test_basic_sitemap_directive(self):
        robots_txt = "User-agent: *\nDisallow: /\nSitemap: http://example.com/sitemap.xml\n"
        result = _extract_sitemap_lines(robots_txt)
        assert result == ["http://example.com/sitemap.xml"]

    def test_multiple_sitemaps(self):
        robots_txt = (
            "Sitemap: http://example.com/sitemap1.xml\n"
            "Sitemap: http://example.com/sitemap2.xml\n"
        )
        result = _extract_sitemap_lines(robots_txt)
        assert len(result) == 2

    def test_case_insensitive(self):
        robots_txt = "SITEMAP: http://example.com/sitemap.xml\n"
        result = _extract_sitemap_lines(robots_txt)
        assert len(result) == 1

    def test_no_sitemaps(self):
        robots_txt = "User-agent: *\nDisallow: /admin\n"
        result = _extract_sitemap_lines(robots_txt)
        assert result == []

    def test_whitespace_trimmed(self):
        robots_txt = "Sitemap:    http://example.com/sitemap.xml   \n"
        result = _extract_sitemap_lines(robots_txt)
        assert result[0] == "http://example.com/sitemap.xml"


# ─────────────────────────────────────────────────────────────────────────────
# RobotsCache — is_allowed
# ─────────────────────────────────────────────────────────────────────────────

class TestRobotsCache:

    def _make_mock_client(self, body: str, status: int = 200):
        """Create a mock httpx client that returns a given robots.txt body."""
        response = MagicMock()
        response.status_code = status
        response.text = body
        client = MagicMock()
        client.get.return_value = response
        return client

    def test_disallowed_path_blocked(self):
        robots_body = "User-agent: SiftBot\nDisallow: /private/\n"
        client = self._make_mock_client(robots_body)

        cache = RobotsCache()
        cache.fetch("http://example.com", client)

        assert cache.is_allowed("http://example.com/private/data") is False

    def test_allowed_path_passes(self):
        robots_body = "User-agent: SiftBot\nDisallow: /private/\n"
        client = self._make_mock_client(robots_body)

        cache = RobotsCache()
        cache.fetch("http://example.com", client)

        assert cache.is_allowed("http://example.com/public/page") is True

    def test_disallow_all_blocks_all(self):
        robots_body = "User-agent: *\nDisallow: /\n"
        client = self._make_mock_client(robots_body)

        cache = RobotsCache()
        cache.fetch("http://example.com", client)

        assert cache.is_allowed("http://example.com/anything") is False

    def test_404_robots_allows_all(self):
        """If robots.txt returns 404, treat as 'allow all'."""
        client = self._make_mock_client("", status=404)

        cache = RobotsCache()
        cache.fetch("http://example.com", client)

        assert cache.is_allowed("http://example.com/anything") is True

    def test_fetch_only_once(self):
        """robots.txt should only be fetched once per origin."""
        client = self._make_mock_client("User-agent: *\nDisallow:\n")

        cache = RobotsCache()
        cache.fetch("http://example.com", client)
        cache.fetch("http://example.com", client)   # second call

        # Client.get should only have been called once
        assert client.get.call_count == 1

    def test_sitemap_urls_extracted(self):
        robots_body = (
            "User-agent: *\n"
            "Disallow: /admin\n"
            "Sitemap: http://example.com/sitemap.xml\n"
        )
        client = self._make_mock_client(robots_body)

        cache = RobotsCache()
        cache.fetch("http://example.com", client)

        sitemaps = cache.get_sitemap_urls("http://example.com")
        assert "http://example.com/sitemap.xml" in sitemaps

    def test_specific_user_agent_takes_precedence(self):
        """SiftBot-specific rules take precedence over wildcard."""
        robots_body = (
            "User-agent: *\n"
            "Disallow:\n\n"            # allow all for everyone
            "User-agent: SiftBot\n"
            "Disallow: /no-bots/\n"    # but block bots from /no-bots/
        )
        client = self._make_mock_client(robots_body)

        cache = RobotsCache()
        cache.fetch("http://example.com", client)

        assert cache.is_allowed("http://example.com/no-bots/page") is False
        assert cache.is_allowed("http://example.com/public/page") is True
