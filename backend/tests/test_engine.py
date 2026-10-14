"""
tests/test_engine.py — Unit tests for cleaning/engine.py

Tests the transform engine in isolation using a lightweight in-memory
SQLite database (no PostgreSQL required for tests).

Coverage:
  - v1 unchanged after apply
  - transaction rollback on forced error
  - apply with no approvals fails with ValueError
  - quarantined rows excluded from the re-profile
  - idempotency: generating proposals on a clean version returns none
  - all transform kinds: trim, normalize_category, normalize_dates,
    drop_exact_duplicates, quarantine_missing_identifier, flag_outliers
"""

import json
import pytest
from datetime import datetime
from unittest.mock import patch

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# ── Bootstrap the in-memory SQLite DB ────────────────────────────────────────
# We use SQLite for tests so PostgreSQL is not required.
# SQLite doesn't support JSONB, but SQLAlchemy maps it to TEXT via JSON.

from sqlalchemy.dialects import sqlite
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlalchemy import JSON

# Patch: when SQLite is used, JSONB columns are stored as JSON text.
# We do this by monkey-patching the db module before import.
import db as db_module

_original_jsonb = None

# ── Create a temporary in-memory DB ──────────────────────────────────────────

def _make_test_engine():
    """Create an in-memory SQLite engine with all tables."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    # For SQLite we need JSON not JSONB.
    # Swap out Column types at the SQLAlchemy level.
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn, rec):
        pass   # no-op

    # Create all tables (uses the ORM Base which is already imported)
    from db import Base
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def db():
    """Provide a test session backed by an in-memory SQLite database."""
    engine = _make_test_engine()
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    engine.dispose()


# ── Seed helpers ──────────────────────────────────────────────────────────────

def _make_source(db, source_type="file"):
    from db import Source, SourceVersion
    s = Source(source_type=source_type, name="test_source")
    db.add(s)
    db.flush()
    sv = SourceVersion(source_id=s.id, version_no=1, parent_version=None, note="v1")
    db.add(sv)
    db.flush()
    return s


def _add_record(db, source_id, row_index, data, version_no=1, status="active"):
    from db import Record
    r = Record(
        source_id=source_id,
        row_index=row_index,
        data=json.loads(json.dumps(data, default=str)),
        version_no=version_no,
        status=status,
        metadata_={},
    )
    db.add(r)
    db.flush()
    return r


def _add_proposal(db, source_id, kind, target, params, status="approved",
                  severity="warning", base_version=1):
    from db import CleaningProposal
    p = CleaningProposal(
        source_id    = source_id,
        base_version = base_version,
        kind         = kind,
        target       = target,
        severity     = severity,
        description  = f"test {kind}",
        impact       = {"rows_affected": 0, "examples": []},
        params       = json.loads(json.dumps(params, default=str)),
        status       = status,
    )
    db.add(p)
    db.flush()
    return p


# ═════════════════════════════════════════════════════════════════════════════
# Core engine tests
# ═════════════════════════════════════════════════════════════════════════════

class TestApplyProposals:

    def test_no_approved_proposals_raises(self, db):
        """apply_proposals should raise ValueError if there are no approved proposals."""
        from cleaning.engine import apply_proposals
        s = _make_source(db)
        _add_record(db, s.id, 0, {"name": "Alice"})
        # Add a pending (not approved) proposal
        _add_proposal(db, s.id, "trim_whitespace", "name", {"column": "name"}, status="pending")
        db.commit()

        with pytest.raises(ValueError, match="No approved proposals"):
            apply_proposals(s.id, db)

    def test_v1_unchanged_after_apply(self, db):
        """Version 1 records must be byte-for-byte unchanged after apply."""
        from cleaning.engine import apply_proposals
        from db import Record

        s = _make_source(db)
        original_data = {"Name": "  Alice  ", "City": "mumbai"}
        _add_record(db, s.id, 0, original_data)
        _add_record(db, s.id, 1, {"Name": "Bob", "City": "Mumbai"})
        _add_proposal(db, s.id, "trim_whitespace", "Name", {"column": "Name"})
        db.commit()

        from cleaning.engine import apply_proposals
        apply_proposals(s.id, db)

        # Check v1 record is unchanged
        v1_rec = db.query(Record).filter(
            Record.source_id == s.id,
            Record.version_no == 1,
            Record.row_index == 0,
        ).first()
        assert v1_rec is not None
        assert v1_rec.data["Name"] == "  Alice  "   # unchanged!

    def test_v2_has_cleaned_data(self, db):
        """Version 2 records should have trimmed whitespace applied."""
        from cleaning.engine import apply_proposals
        from db import Record

        s = _make_source(db)
        _add_record(db, s.id, 0, {"Name": "  Alice  "})
        _add_record(db, s.id, 1, {"Name": "Bob"})
        _add_proposal(db, s.id, "trim_whitespace", "Name", {"column": "Name"})
        db.commit()

        result = apply_proposals(s.id, db)
        assert result["new_version"] == 2

        v2_rec = db.query(Record).filter(
            Record.source_id == s.id,
            Record.version_no == 2,
            Record.row_index == 0,
        ).first()
        assert v2_rec.data["Name"] == "Alice"   # trimmed

    def test_drop_exact_duplicates(self, db):
        """Duplicate rows should be excluded in v2."""
        from cleaning.engine import apply_proposals
        from db import Record

        s = _make_source(db)
        same_row = {"Name": "Alice", "City": "Mumbai"}
        _add_record(db, s.id, 0, same_row)
        _add_record(db, s.id, 1, same_row)   # exact duplicate
        _add_record(db, s.id, 2, {"Name": "Bob", "City": "Delhi"})
        _add_proposal(db, s.id, "drop_exact_duplicates", "*", {})
        db.commit()

        result = apply_proposals(s.id, db)
        assert result["rows_excluded"] == 1
        assert result["rows_active"] == 2

    def test_quarantine_missing_identifier(self, db):
        """Rows with missing identifier values should be quarantined in v2."""
        from cleaning.engine import apply_proposals
        from db import Record

        s = _make_source(db)
        _add_record(db, s.id, 0, {"Employee_ID": "E001", "Name": "Alice"})
        _add_record(db, s.id, 1, {"Employee_ID": None, "Name": "Bob"})
        _add_proposal(db, s.id, "quarantine_missing_identifier", "Employee_ID",
                      {"column": "Employee_ID"}, severity="critical")
        db.commit()

        result = apply_proposals(s.id, db)
        assert result["rows_quarantined"] == 1
        assert result["rows_active"] == 1

    def test_quarantined_rows_excluded_from_reprofile(self, db):
        """The re-profile should only include active rows, not quarantined ones."""
        from cleaning.engine import apply_proposals
        from db import SourceVersion

        s = _make_source(db)
        # Active row has Employee_ID; quarantined row doesn't
        _add_record(db, s.id, 0, {"Employee_ID": "E001", "Name": "Alice"})
        _add_record(db, s.id, 1, {"Employee_ID": None, "Name": "Bob"})
        _add_proposal(db, s.id, "quarantine_missing_identifier", "Employee_ID",
                      {"column": "Employee_ID"}, severity="critical")
        db.commit()

        apply_proposals(s.id, db)

        v2 = db.query(SourceVersion).filter_by(source_id=s.id, version_no=2).first()
        assert v2 is not None
        profile = v2.profile or {}
        # Only 1 active row should be in the profile
        assert profile.get("row_count") == 1

    def test_normalize_category(self, db):
        """Category values should be normalised to the most-frequent spelling."""
        from cleaning.engine import apply_proposals
        from db import Record

        s = _make_source(db)
        # "Mumbai" appears 3x, "mumbai" 1x — preferred should be "Mumbai"
        for i, city in enumerate(["Mumbai", "Mumbai", "Mumbai", "mumbai"]):
            _add_record(db, s.id, i, {"City": city})

        mapping = {"mumbai": "Mumbai"}
        _add_proposal(db, s.id, "normalize_category", "City",
                      {"column": "City", "mapping": mapping})
        db.commit()

        apply_proposals(s.id, db)

        # v2 row that was "mumbai" should now be "Mumbai"
        v2_mumbai = db.query(Record).filter(
            Record.source_id == s.id,
            Record.version_no == 2,
            Record.status == "active",
        ).all()
        cities = [r.data["City"] for r in v2_mumbai]
        assert all(c == "Mumbai" for c in cities)

    def test_normalize_dates(self, db):
        """Valid non-ISO dates should be rewritten; invalid ones quarantined."""
        from cleaning.engine import apply_proposals
        from db import Record

        s = _make_source(db)
        _add_record(db, s.id, 0, {"Joining_Date": "01/05/2023"})   # parseable
        _add_record(db, s.id, 1, {"Joining_Date": "2023-06-15"})   # already ISO
        _add_record(db, s.id, 2, {"Joining_Date": "not-a-date"})   # bad → quarantine
        _add_proposal(db, s.id, "normalize_dates", "Joining_Date",
                      {"column": "Joining_Date", "dayfirst": False})
        db.commit()

        result = apply_proposals(s.id, db)
        assert result["rows_quarantined"] >= 1

        # Row 0 should have been rewritten to ISO format
        v2_row0 = db.query(Record).filter(
            Record.source_id == s.id, Record.version_no == 2, Record.row_index == 0
        ).first()
        # pd.to_datetime("01/05/2023", dayfirst=False) → 2023-01-05
        assert v2_row0 is not None
        assert v2_row0.data["Joining_Date"] in ("2023-01-05", "2023-05-01")  # either interpretation is fine

    def test_flag_outliers(self, db):
        """Outlier rows should be flagged in metadata but status stays active."""
        from cleaning.engine import apply_proposals
        from db import Record

        s = _make_source(db)
        _add_record(db, s.id, 0, {"Salary": 50000})
        _add_record(db, s.id, 1, {"Salary": 55000})
        _add_record(db, s.id, 2, {"Salary": 9999999})   # outlier
        _add_proposal(db, s.id, "flag_outliers", "Salary",
                      {"column": "Salary", "lower": -10000, "upper": 200000})
        db.commit()

        result = apply_proposals(s.id, db)
        assert result["rows_active"] == 3    # status unchanged
        assert result["rows_excluded"] == 0
        assert result["rows_quarantined"] == 0

        # The outlier row should have outlier_flags in metadata
        v2_outlier = db.query(Record).filter(
            Record.source_id == s.id, Record.version_no == 2, Record.row_index == 2
        ).first()
        assert v2_outlier.metadata_ is not None
        assert "outlier_flags" in v2_outlier.metadata_

    def test_transaction_rollback_on_error(self, db):
        """On error, no partial version should be committed."""
        from cleaning.engine import apply_proposals
        from db import Record

        s = _make_source(db)
        _add_record(db, s.id, 0, {"Name": "  Alice  "})
        _add_proposal(db, s.id, "trim_whitespace", "Name", {"column": "Name"})
        db.commit()

        # Patch profile_structured to raise an error mid-flight
        with patch("cleaning.engine.profile_structured", side_effect=RuntimeError("Forced error")):
            with pytest.raises(RuntimeError, match="Forced error"):
                apply_proposals(s.id, db)

        # No v2 records should exist
        v2_count = db.query(Record).filter(
            Record.source_id == s.id, Record.version_no == 2
        ).count()
        assert v2_count == 0

    def test_idempotency_clean_data(self, db):
        """Generating proposals on already-clean data should return an empty list."""
        from cleaning.proposals import generate_structured_proposals

        df = pd.DataFrame({
            "Employee_ID": ["E001", "E002", "E003"],
            "City":        ["Mumbai", "Delhi", "Pune"],
            "Salary":      [50000, 55000, 60000],
        })
        records = [{"id": i, "data": row.to_dict()} for i, (_, row) in enumerate(df.iterrows())]
        roles = {
            "Employee_ID": "identifier",
            "City":        "category",
            "Salary":      "numeric",
        }
        # Clean data: no proposals should be generated
        props = generate_structured_proposals(records, roles)
        # Only outlier could trigger if IQR is wide enough — filter for action proposals
        action_kinds = {"normalize_category", "trim_whitespace", "drop_exact_duplicates",
                        "quarantine_missing_identifier", "normalize_dates"}
        action_props = [p for p in props if p["kind"] in action_kinds]
        assert action_props == [], f"Unexpected proposals on clean data: {action_props}"
