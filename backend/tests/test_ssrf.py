"""
tests/test_ssrf.py — Unit tests for crawler/ssrf_guard.py

Tests that the SSRF guard correctly rejects dangerous addresses
and allows legitimate public ones. No real network calls made —
we mock socket.getaddrinfo.
"""

import pytest
import socket
from unittest.mock import patch, MagicMock

from crawler.ssrf_guard import validate_url, check_ip_safe


def _mock_getaddrinfo(ip_str: str):
    """
    Return a mock result from socket.getaddrinfo for a given IP.
    getaddrinfo returns: (family, type, proto, canonname, sockaddr)
    sockaddr for IPv4 is (ip, port); for IPv6 is (ip, port, flow, scope).
    """
    return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip_str, 80))]


# ─────────────────────────────────────────────────────────────────────────────
# Addresses that MUST always be rejected (without demo flag)
# ─────────────────────────────────────────────────────────────────────────────

BLOCKED_IPS = [
    ("127.0.0.1",       "loopback IPv4"),
    ("127.0.0.2",       "loopback range"),
    ("10.0.0.5",        "private Class A"),
    ("192.168.1.1",     "private Class C"),
    ("172.16.0.1",      "private Class B"),
    ("169.254.169.254", "link-local / AWS metadata"),
    ("0.0.0.0",         "unspecified"),
]

BLOCKED_IPV6 = [
    ("::1",             "loopback IPv6"),
    ("fe80::1",         "link-local IPv6"),
    ("ff02::1",         "multicast IPv6"),
]


class TestBlockedIPs:
    """Each of these IPs must raise ValueError from check_ip_safe()."""

    @pytest.mark.parametrize("ip,label", BLOCKED_IPS)
    def test_blocked_ipv4(self, ip, label):
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", False):
            with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo(ip)):
                with pytest.raises(ValueError, match=r"(loopback|private|link-local|multicast|unspecified|reserved)"):
                    check_ip_safe("blocked-host")

    def test_loopback_ipv6(self):
        ipv6_result = [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("::1", 80, 0, 0))]
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", False):
            with patch("socket.getaddrinfo", return_value=ipv6_result):
                with pytest.raises(ValueError, match="loopback"):
                    check_ip_safe("localhost-ipv6")

    def test_link_local_ipv6(self):
        ipv6_result = [(socket.AF_INET6, socket.SOCK_STREAM, 0, "", ("fe80::1", 80, 0, 0))]
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", False):
            with patch("socket.getaddrinfo", return_value=ipv6_result):
                with pytest.raises(ValueError, match="link-local"):
                    check_ip_safe("fe80-host")


class TestBlockedURLs:
    """validate_url() must reject these full URLs."""

    def test_localhost_url(self):
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", False):
            with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("127.0.0.1")):
                with pytest.raises(ValueError):
                    validate_url("http://localhost/path")

    def test_127_url(self):
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", False):
            with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("127.0.0.1")):
                with pytest.raises(ValueError):
                    validate_url("http://127.0.0.1/admin")

    def test_metadata_endpoint(self):
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", False):
            with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("169.254.169.254")):
                with pytest.raises(ValueError, match="link-local"):
                    validate_url("http://169.254.169.254/latest/meta-data/")

    def test_private_ip_url(self):
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", False):
            with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("10.0.0.5")):
                with pytest.raises(ValueError, match="private"):
                    validate_url("http://10.0.0.5/secret")

    def test_192_168_url(self):
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", False):
            with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("192.168.1.1")):
                with pytest.raises(ValueError, match="private"):
                    validate_url("http://192.168.1.1/router")

    def test_credentials_in_url_rejected(self):
        """URLs with user:pass@ are rejected regardless of IP."""
        with pytest.raises(ValueError, match="credentials"):
            validate_url("http://user:pass@example.com/page")

    def test_ftp_scheme_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_url("ftp://example.com/file")

    def test_file_scheme_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_url("file:///etc/passwd")

    def test_data_scheme_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_url("data:text/html,<h1>hello</h1>")


class TestAllowedURLs:
    """Public URLs must NOT raise errors."""

    def test_public_ipv4(self):
        with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("93.184.216.34")):
            # Should not raise
            validate_url("http://example.com/page")

    def test_https_allowed(self):
        with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("93.184.216.34")):
            validate_url("https://example.com/page")


class TestDemoModeBypass:
    """With ALLOW_LOCALHOST_FOR_DEMO=True, loopback and private IPs are allowed."""

    def test_loopback_allowed_in_demo_mode(self):
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", True):
            with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("127.0.0.1")):
                # Should NOT raise
                check_ip_safe("localhost")

    def test_private_allowed_in_demo_mode(self):
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", True):
            with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("192.168.1.1")):
                check_ip_safe("router")

    def test_link_local_still_blocked_in_demo_mode(self):
        """Even in demo mode, link-local (169.254.x.x) MUST be blocked."""
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", True):
            with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("169.254.169.254")):
                with pytest.raises(ValueError, match="link-local"):
                    check_ip_safe("metadata")


class TestRedirectToPrivate:
    """
    Simulate a redirect that leads to a private IP.
    The fetcher calls validate_url on each hop — this tests that check_ip_safe
    correctly blocks the redirect target.
    """

    def test_redirect_to_private_blocked(self):
        """After following a redirect to 10.0.0.5, the SSRF guard must block it."""
        private_redirect_target = "http://10.0.0.5/internal"
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", False):
            with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("10.0.0.5")):
                with pytest.raises(ValueError, match="private"):
                    validate_url(private_redirect_target)

    def test_redirect_to_127_blocked(self):
        """Redirect to 127.0.0.1 must be blocked on re-validation."""
        with patch("crawler.ssrf_guard._ALLOW_LOCALHOST_FOR_DEMO", False):
            with patch("socket.getaddrinfo", return_value=_mock_getaddrinfo("127.0.0.1")):
                with pytest.raises(ValueError, match="loopback"):
                    validate_url("http://127.0.0.1/after-redirect")
