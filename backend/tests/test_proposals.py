"""
tests/test_proposals.py — Unit tests for cleaning/proposals.py

Tests each proposal generator with carefully constructed DataFrames.
No DB access, no network calls — pure function tests.
"""

import pytest
import pandas as pd
from cleaning.proposals import (
    generate_structured_proposals,
    generate_unstructured_proposals,
    _propose_trim_whitespace,
    _propose_normalize_category,
    _propose_normalize_dates,
    _propose_drop_exact_duplicates,
    _propose_quarantine_missing_identifier,
    _propose_flag_outliers,
    _propose_missing_value_policy,
    _propose_exclude_duplicate_documents,
    _propose_quarantine_injection_suspects,
    _propose_flag_stale_documents,
    _propose_quarantine_short_content,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_records(df: pd.DataFrame):
    """Convert a DataFrame to the list-of-dicts format expected by proposals."""
    return [{"id": i, "data": row.to_dict()} for i, (_, row) in enumerate(df.iterrows())]


# ═════════════════════════════════════════════════════════════════════════════
# 2. trim_whitespace
# ═════════════════════════════════════════════════════════════════════════════

class TestTrimWhitespace:
    def test_detects_leading_trailing_spaces(self):
        df = pd.DataFrame({"Name": ["  Alice  ", "Bob", "  Carol"]})
        props = _propose_trim_whitespace(df, [0, 1, 2])
        assert len(props) == 1
        assert props[0]["kind"] == "trim_whitespace"
        assert props[0]["impact"]["rows_affected"] == 2

    def test_detects_double_spaces(self):
        df = pd.DataFrame({"Dept": ["Eng  ineering", "Sales"]})
        props = _propose_trim_whitespace(df, [0, 1])
        assert len(props) == 1

    def test_no_proposal_for_clean_text(self):
        df = pd.DataFrame({"Name": ["Alice", "Bob", "Carol"]})
        props = _propose_trim_whitespace(df, [0, 1, 2])
        assert props == []

    def test_skips_numeric_columns(self):
        df = pd.DataFrame({"Salary": [100.0, 200.0]})
        props = _propose_trim_whitespace(df, [0, 1])
        assert props == []

    def test_examples_show_before_after(self):
        df = pd.DataFrame({"Name": ["  Alice  "]})
        props = _propose_trim_whitespace(df, [0])
        ex = props[0]["impact"]["examples"][0]
        assert ex["before"] == "  Alice  "
        assert ex["after"] == "Alice"


# ═════════════════════════════════════════════════════════════════════════════
# 1. normalize_category
# ═════════════════════════════════════════════════════════════════════════════

class TestNormalizeCategory:
    def _make_city_df(self):
        # 3 distinct spellings of Mumbai, each appearing several times
        cities = ["Mumbai"] * 10 + ["mumbai"] * 5 + ["MUMBAI"] * 3 + ["Delhi"] * 20
        df = pd.DataFrame({"City": cities})
        return df

    def test_detects_case_inconsistency(self):
        df = self._make_city_df()
        roles = {"City": "category"}
        props = _propose_normalize_category(df, [], roles)
        assert len(props) == 1
        assert props[0]["kind"] == "normalize_category"

    def test_preferred_is_most_frequent(self):
        """Most-frequent spelling should be the 'after' value."""
        df = self._make_city_df()
        roles = {"City": "category"}
        props = _propose_normalize_category(df, [], roles)
        params = props[0]["params"]
        # "Mumbai" (10 occurrences) is the most frequent
        mapping = params["mapping"]
        assert mapping.get("mumbai") == "Mumbai"
        assert mapping.get("MUMBAI") == "Mumbai"

    def test_no_proposal_when_clean(self):
        df = pd.DataFrame({"City": ["Mumbai", "Delhi", "Mumbai", "Delhi"]})
        roles = {"City": "category"}
        props = _propose_normalize_category(df, [], roles)
        assert props == []

    def test_only_runs_for_category_role(self):
        df = pd.DataFrame({"City": ["mumbai", "Mumbai"]})
        roles = {"City": "optional"}  # not a category role
        props = _propose_normalize_category(df, [], roles)
        assert props == []


# ═════════════════════════════════════════════════════════════════════════════
# 4. normalize_dates
# ═════════════════════════════════════════════════════════════════════════════

class TestNormalizeDates:
    def test_detects_non_iso_dates(self):
        df = pd.DataFrame({"Joining_Date": ["01/05/2023", "2023-01-05", "Jan 5 2023"]})
        roles = {"Joining_Date": "date"}
        props = _propose_normalize_dates(df, [], roles)
        assert len(props) == 1
        assert props[0]["kind"] == "normalize_dates"

    def test_detects_unparseable_dates(self):
        df = pd.DataFrame({"Joining_Date": ["not-a-date", "TBD", "N/A", "2023-01-05"]})
        roles = {"Joining_Date": "date"}
        props = _propose_normalize_dates(df, [], roles)
        assert len(props) == 1
        # Examples should contain quarantined entries
        example_afters = [e["after"] for e in props[0]["impact"]["examples"]]
        assert "(quarantined)" in example_afters

    def test_no_proposal_when_already_iso(self):
        df = pd.DataFrame({"Joining_Date": ["2023-01-05", "2022-06-15"]})
        roles = {"Joining_Date": "date"}
        props = _propose_normalize_dates(df, [], roles)
        assert props == []

    def test_only_runs_for_date_role(self):
        df = pd.DataFrame({"Name": ["01/05/2023"]})
        roles = {"Name": "optional"}
        props = _propose_normalize_dates(df, [], roles)
        assert props == []


# ═════════════════════════════════════════════════════════════════════════════
# 3. drop_exact_duplicates
# ═════════════════════════════════════════════════════════════════════════════

class TestDropExactDuplicates:
    def test_detects_duplicates(self):
        df = pd.DataFrame({
            "Employee_ID": ["E001", "E002", "E001"],
            "Name": ["Alice", "Bob", "Alice"],
        })
        props = _propose_drop_exact_duplicates(df, [0, 1, 2])
        assert len(props) == 1
        assert props[0]["kind"] == "drop_exact_duplicates"
        assert props[0]["impact"]["rows_affected"] == 1

    def test_no_proposal_when_clean(self):
        df = pd.DataFrame({"a": [1, 2, 3]})
        props = _propose_drop_exact_duplicates(df, [0, 1, 2])
        assert props == []

    def test_multiple_duplicates(self):
        df = pd.DataFrame({"a": [1, 1, 1, 2]})
        props = _propose_drop_exact_duplicates(df, list(range(4)))
        assert props[0]["impact"]["rows_affected"] == 2   # 2 dupes of row 1


# ═════════════════════════════════════════════════════════════════════════════
# 5. quarantine_missing_identifier
# ═════════════════════════════════════════════════════════════════════════════

class TestQuarantineMissingIdentifier:
    def test_detects_missing_identifier(self):
        df = pd.DataFrame({
            "Employee_ID": ["E001", None, "E003"],
            "Name": ["Alice", "Bob", "Carol"],
        })
        roles = {"Employee_ID": "identifier", "Name": "optional"}
        props = _propose_quarantine_missing_identifier(df, [], roles)
        assert len(props) == 1
        assert props[0]["kind"] == "quarantine_missing_identifier"
        assert props[0]["severity"] == "critical"
        assert props[0]["impact"]["rows_affected"] == 1

    def test_no_proposal_when_complete(self):
        df = pd.DataFrame({"Employee_ID": ["E001", "E002"]})
        roles = {"Employee_ID": "identifier"}
        props = _propose_quarantine_missing_identifier(df, [], roles)
        assert props == []

    def test_only_runs_for_identifier_role(self):
        """Manager column missing values should NOT trigger this if role=optional."""
        df = pd.DataFrame({"Manager": [None, "Mgr1"]})
        roles = {"Manager": "optional"}
        props = _propose_quarantine_missing_identifier(df, [], roles)
        assert props == []

    def test_ceo_with_no_manager_not_flagged_as_identifier(self):
        """Manager is 'optional' role → missing value does NOT trigger quarantine_missing_identifier."""
        df = pd.DataFrame({
            "Employee_ID": ["E001", "E9999"],
            "Name":        ["Alice", "CEO"],
            "Manager":     ["Mgr1", None],  # CEO has no manager
        })
        roles = {
            "Employee_ID": "identifier",
            "Name": "optional",
            "Manager": "optional",   # manager is optional, not identifier
        }
        props = _propose_quarantine_missing_identifier(df, [], roles)
        # Only Employee_ID is checked; Manager (optional) is skipped
        assert props == []


# ═════════════════════════════════════════════════════════════════════════════
# 6. flag_outliers
# ═════════════════════════════════════════════════════════════════════════════

class TestFlagOutliers:
    def test_detects_salary_outliers(self):
        salaries = [50_000, 55_000, 60_000, 58_000, 52_000, 9_999_999, -5_000]
        df = pd.DataFrame({"Salary": salaries})
        roles = {"Salary": "numeric"}
        props = _propose_flag_outliers(df, list(range(len(salaries))), roles)
        assert len(props) == 1
        assert props[0]["kind"] == "flag_outliers"
        assert props[0]["severity"] == "warning"

    def test_no_outliers_in_clean_data(self):
        df = pd.DataFrame({"Salary": [50_000, 55_000, 60_000, 58_000, 52_000]})
        roles = {"Salary": "numeric"}
        props = _propose_flag_outliers(df, [], roles)
        assert props == []

    def test_only_runs_for_numeric_role(self):
        df = pd.DataFrame({"City": ["Mumbai", "MUMBAI"]})
        roles = {"City": "category"}
        props = _propose_flag_outliers(df, [], roles)
        assert props == []


# ═════════════════════════════════════════════════════════════════════════════
# 7. missing_value_policy
# ═════════════════════════════════════════════════════════════════════════════

class TestMissingValuePolicy:
    def test_optional_column_gets_info_proposal(self):
        df = pd.DataFrame({"Notes": [None, "some text", None]})
        roles = {"Notes": "optional"}
        props = _propose_missing_value_policy(df, roles)
        assert len(props) == 1
        assert props[0]["severity"] == "info"
        assert props[0]["params"]["action"] == "leave"

    def test_required_text_column_gets_fill_unknown(self):
        df = pd.DataFrame({"Department": ["Eng", None, "HR"]})
        roles = {"Department": "required"}
        props = _propose_missing_value_policy(df, roles)
        assert len(props) == 1
        assert props[0]["params"]["action"] == "fill_unknown"
        assert props[0]["severity"] == "warning"

    def test_no_proposal_when_no_missing(self):
        df = pd.DataFrame({"Name": ["Alice", "Bob"]})
        roles = {"Name": "optional"}
        props = _propose_missing_value_policy(df, roles)
        assert props == []


# ═════════════════════════════════════════════════════════════════════════════
# 8–11. Unstructured proposals
# ═════════════════════════════════════════════════════════════════════════════

class TestUnstructuredProposals:
    def _doc(self, id, content="", quality=None, word_count=100):
        return {
            "id": id,
            "content": content,
            "content_hash": "",
            "quality": quality or {},
            "word_count": word_count,
        }

    def test_exact_duplicate_detected(self):
        from profiler.text_quality import compute_content_hash
        text = "hello world " * 20
        h = compute_content_hash(text)
        docs = [
            {"id": 1, "content": text, "content_hash": h, "quality": {}, "word_count": 40},
            {"id": 2, "content": text, "content_hash": h, "quality": {}, "word_count": 40},
        ]
        props = _propose_exclude_duplicate_documents(docs)
        kinds = [p["kind"] for p in props]
        assert "exclude_duplicate_documents" in kinds

    def test_injection_suspect_quarantined(self):
        docs = [
            {
                "id": 1, "content": "normal",
                "content_hash": "abc", "quality": {
                    "injection_suspect": {"flag": True}
                }, "word_count": 50,
            }
        ]
        props = _propose_quarantine_injection_suspects(docs)
        assert len(props) == 1
        assert props[0]["kind"] == "quarantine_injection_suspects"
        assert props[0]["severity"] == "critical"

    def test_no_injection_proposal_for_clean_doc(self):
        docs = [
            {"id": 1, "content": "clean", "content_hash": "x",
             "quality": {"injection_suspect": {"flag": False}}, "word_count": 50}
        ]
        props = _propose_quarantine_injection_suspects(docs)
        assert props == []

    def test_stale_flagged_only(self):
        docs = [
            {"id": 1, "content": "old", "content_hash": "y",
             "quality": {"freshness": {"flag": "stale"}}, "word_count": 200}
        ]
        props = _propose_flag_stale_documents(docs)
        assert len(props) == 1
        assert props[0]["kind"] == "flag_stale_documents"
        assert props[0]["severity"] == "info"

    def test_short_content_quarantined(self):
        docs = [
            {"id": 1, "content": "too short", "content_hash": "z",
             "quality": {}, "word_count": 5}
        ]
        props = _propose_quarantine_short_content(docs)
        assert len(props) == 1
        assert props[0]["kind"] == "quarantine_short_content"

    def test_no_proposals_for_clean_docs(self):
        from profiler.text_quality import compute_content_hash
        text1 = "completely different content " * 20
        text2 = "another unique document here " * 20
        docs = [
            {"id": 1, "content": text1, "content_hash": compute_content_hash(text1),
             "quality": {"injection_suspect": {"flag": False}, "freshness": {"flag": "fresh"}},
             "word_count": 40},
            {"id": 2, "content": text2, "content_hash": compute_content_hash(text2),
             "quality": {"injection_suspect": {"flag": False}, "freshness": {"flag": "fresh"}},
             "word_count": 40},
        ]
        props = generate_unstructured_proposals(docs)
        # Clean documents should generate no proposals
        assert props == []
