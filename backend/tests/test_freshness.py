"""
tests/test_freshness.py — Focused tests for check_freshness() freshness buckets.
Ensures all four buckets and edge cases work correctly.
"""

import pytest
from datetime import datetime, timezone, timedelta

from profiler.text_quality import check_freshness, FRESH_DAYS, AGING_DAYS


def _now():
    return datetime.now(timezone.utc)


class TestFreshnessBuckets:

    # ── unknown (no date) ─────────────────────────────────────────────────────

    def test_none_is_unknown(self):
        r = check_freshness(None)
        assert r["flag"] == "unknown"
        assert r["detail"]["age_days"] is None
        assert r["detail"]["published_at"] is None

    # ── fresh (< 180 days) ────────────────────────────────────────────────────

    def test_yesterday_is_fresh(self):
        r = check_freshness(_now() - timedelta(days=1))
        assert r["flag"] == "fresh"

    def test_one_week_ago_is_fresh(self):
        r = check_freshness(_now() - timedelta(days=7))
        assert r["flag"] == "fresh"

    def test_179_days_is_fresh(self):
        r = check_freshness(_now() - timedelta(days=179))
        assert r["flag"] == "fresh"

    # ── aging (180 <= days < 730) ─────────────────────────────────────────────

    def test_180_days_is_aging(self):
        """Exactly FRESH_DAYS old → "aging" (boundary is inclusive on aging side)."""
        r = check_freshness(_now() - timedelta(days=FRESH_DAYS))
        assert r["flag"] == "aging"

    def test_one_year_is_aging(self):
        r = check_freshness(_now() - timedelta(days=365))
        assert r["flag"] == "aging"

    def test_729_days_is_aging(self):
        r = check_freshness(_now() - timedelta(days=729))
        assert r["flag"] == "aging"

    # ── stale (>= 730 days) ───────────────────────────────────────────────────

    def test_730_days_is_stale(self):
        """Exactly AGING_DAYS old → "stale"."""
        r = check_freshness(_now() - timedelta(days=AGING_DAYS))
        assert r["flag"] == "stale"

    def test_five_years_is_stale(self):
        r = check_freshness(_now() - timedelta(days=5 * 365))
        assert r["flag"] == "stale"

    def test_2020_date_is_stale(self):
        """The demo server's page6 has Last-Modified 2020-01-01 — must be stale."""
        old_date = datetime(2020, 1, 1, tzinfo=timezone.utc)
        r = check_freshness(old_date)
        assert r["flag"] == "stale"

    # ── detail fields ─────────────────────────────────────────────────────────

    def test_detail_has_age_days_int(self):
        r = check_freshness(_now() - timedelta(days=10))
        assert isinstance(r["detail"]["age_days"], int)

    def test_detail_has_published_at_string(self):
        dt = _now() - timedelta(days=10)
        r = check_freshness(dt)
        assert isinstance(r["detail"]["published_at"], str)
        assert "T" in r["detail"]["published_at"]   # ISO 8601 format check

    # ── timezone handling ─────────────────────────────────────────────────────

    def test_naive_datetime_accepted(self):
        """Naive datetime (no tz) should not crash — treated as UTC."""
        naive = (datetime.now(timezone.utc) - timedelta(days=50)).replace(tzinfo=None)
        r = check_freshness(naive)
        assert r["flag"] == "fresh"

    def test_timezone_aware_datetime_accepted(self):
        aware = _now() - timedelta(days=200)
        r = check_freshness(aware)
        assert r["flag"] == "aging"
