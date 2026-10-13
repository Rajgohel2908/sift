"""
crawler/ssrf_guard.py — SSRF (Server-Side Request Forgery) protection.

Validates that a URL is safe to fetch:
  - Only http:// and https:// schemes
  - No credentials (user:pass@host) in the URL
  - Hostname resolves to a public, routable IP address

The IP address check is performed on EVERY redirect hop, not just the first URL.

Environment variable ALLOW_LOCALHOST_FOR_DEMO=true bypasses the loopback/private
IP check for local demo use. THIS MUST NEVER BE SET IN PRODUCTION.

Usage:
    from crawler.ssrf_guard import validate_url, check_ip_safe
    validate_url("http://example.com")        # raises ValueError if unsafe
    validate_url("http://127.0.0.1")          # raises ValueError
"""

import ipaddress
import os
import socket
from urllib.parse import urlparse

# Demo flag — MUST default to False. Only set to True for local demo server.
_ALLOW_LOCALHOST_FOR_DEMO = os.getenv("ALLOW_LOCALHOST_FOR_DEMO", "").lower() == "true"


def validate_url(url: str) -> None:
    """
    Validate a URL before fetching. Raises ValueError with a clear message
    if any check fails.

    Checks performed:
      1. Scheme must be http or https
      2. No credentials in the URL (user:pass@host)
      3. Hostname must resolve to a public IP (SSRF protection)
    """
    parsed = urlparse(url)

    # 1. Scheme check
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Rejected URL '{url}': only http and https schemes are allowed "
            f"(got '{parsed.scheme}')."
        )

    # 2. Credentials check
    if parsed.username or parsed.password:
        raise ValueError(
            f"Rejected URL '{url}': URLs with embedded credentials are not allowed."
        )

    # 3. SSRF IP check
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Rejected URL '{url}': could not extract a hostname.")

    check_ip_safe(hostname)


def check_ip_safe(hostname: str) -> None:
    """
    Resolve `hostname` to all its IP addresses and reject any that belong to
    a non-public range:
      - loopback     (127.0.0.0/8, ::1)
      - private      (10.x, 172.16-31.x, 192.168.x)
      - link-local   (169.254.x.x, fe80::/10)
      - multicast    (224.0.0.0/4, ff00::/8)
      - unspecified  (0.0.0.0, ::)
      - reserved     (other IANA special-purpose ranges)

    Raises ValueError if any resolved address is in a non-public range,
    UNLESS ALLOW_LOCALHOST_FOR_DEMO=true is set (loopback/private only).
    """
    try:
        # getaddrinfo returns (family, type, proto, canonname, sockaddr)
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ValueError(
            f"Rejected hostname '{hostname}': DNS resolution failed — {e}"
        )

    if not results:
        raise ValueError(
            f"Rejected hostname '{hostname}': DNS returned no addresses."
        )

    for family, _, _, _, sockaddr in results:
        # sockaddr is (ip, port) for IPv4 or (ip, port, flow, scope) for IPv6
        ip_str = sockaddr[0]

        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            # Malformed address — reject to be safe
            raise ValueError(
                f"Rejected hostname '{hostname}': could not parse resolved IP '{ip_str}'."
            )

        # Check each category explicitly for clear error messages
        _reject_if_private(hostname, ip)


def _reject_if_private(hostname: str, ip: ipaddress._BaseAddress) -> None:
    """
    Raise ValueError if `ip` is a non-public address.
    Respects ALLOW_LOCALHOST_FOR_DEMO for loopback/private addresses only.
    """
    # Link-local: 169.254.x.x (AWS metadata, GCP metadata, etc.) or fe80::/10
    # Must be checked before is_private because ipaddress classifies link-local as private too,
    # and link-local addresses must NEVER be bypassed, even if ALLOW_LOCALHOST_FOR_DEMO is True.
    if ip.is_link_local:
        raise ValueError(
            f"Rejected hostname '{hostname}' → {ip}: link-local address blocked "
            "(this range is commonly used for cloud metadata endpoints)."
        )

    # Loopback: 127.x.x.x or ::1
    if ip.is_loopback:
        if _ALLOW_LOCALHOST_FOR_DEMO:
            return   # demo mode bypass
        raise ValueError(
            f"Rejected hostname '{hostname}' → {ip}: loopback address blocked "
            "(set ALLOW_LOCALHOST_FOR_DEMO=true to bypass for local demo only)."
        )

    # Private: 10.x, 172.16-31.x, 192.168.x
    if ip.is_private:
        if _ALLOW_LOCALHOST_FOR_DEMO:
            return   # demo mode bypass — also covers RFC-1918 private ranges
        raise ValueError(
            f"Rejected hostname '{hostname}' → {ip}: private network address blocked."
        )

    # Multicast
    if ip.is_multicast:
        raise ValueError(
            f"Rejected hostname '{hostname}' → {ip}: multicast address blocked."
        )

    # Unspecified: 0.0.0.0 or ::
    if ip.is_unspecified:
        raise ValueError(
            f"Rejected hostname '{hostname}' → {ip}: unspecified address blocked."
        )

    # Reserved (IANA special-purpose ranges not covered above)
    if ip.is_reserved:
        raise ValueError(
            f"Rejected hostname '{hostname}' → {ip}: reserved address blocked."
        )
