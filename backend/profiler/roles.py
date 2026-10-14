"""
profiler/roles.py — Rule-based column role inference for structured sources.

Roles
-----
  identifier : columns whose name contains id / code / number → must be unique
  date       : name contains date / _at / dob
  numeric    : pandas dtype is numeric (int / float)
  category   : text column with low cardinality (unique_ratio < 0.2)
  required   : 0% missing AND not an identifier → must be present
  free_text  : long text (median length >= 40 chars)
  optional   : everything else

All inferences are stored with confirmed=FALSE.
A user can override via PUT /sources/{id}/roles (sets confirmed=TRUE).

Severity mapping (used by proposal generators)
-----------------------------------------------
  identifier missing → critical
  required missing   → warning
  optional missing   → info  (default)
"""

import re
import pandas as pd
from typing import Dict


# ── Keywords that signal each role ───────────────────────────────────────────

_IDENTIFIER_KEYWORDS = re.compile(
    r"\b(id|code|number|no|num|key|ref|uuid|emp_id|employee_id)\b",
    re.IGNORECASE,
)
_DATE_KEYWORDS = re.compile(
    r"(date|_at|_on|dob|born|joined|created|updated|timestamp)",
    re.IGNORECASE,
)

# Cardinality threshold: unique values / total values < this → category
CATEGORY_UNIQUE_RATIO = 0.2
# Median character length >= this → free_text
FREE_TEXT_MEDIAN_LEN = 40


def infer_roles(df: pd.DataFrame) -> Dict[str, str]:
    """
    Infer a role for every column in df.

    Returns a dict: { column_name: role_string }

    Designed to be called once and stored; the caller stores rows in
    column_roles with confirmed=FALSE so they can be overridden by the user.
    """
    roles: Dict[str, str] = {}
    total_rows = len(df)

    for col in df.columns:
        series = df[col]
        col_lower = col.lower().replace(" ", "_")

        # ── 1. Identifier? (name-based heuristic) ──────────────────────────
        if _IDENTIFIER_KEYWORDS.search(col_lower):
            roles[col] = "identifier"
            continue

        # ── 2. Date? (name-based heuristic) ────────────────────────────────
        if _DATE_KEYWORDS.search(col_lower):
            roles[col] = "date"
            continue

        # ── 3. Numeric dtype? ───────────────────────────────────────────────
        if pd.api.types.is_numeric_dtype(series):
            roles[col] = "numeric"
            continue

        # ── 4. Text-based analysis ─────────────────────────────────────────
        text_series = series.dropna().astype(str)
        unique_count = series.nunique(dropna=True)
        unique_ratio = unique_count / total_rows if total_rows else 1.0

        # Category: low cardinality text
        if unique_ratio < CATEGORY_UNIQUE_RATIO and len(text_series) > 0:
            roles[col] = "category"
            continue

        # Free-text: high median length
        if len(text_series) > 0:
            median_len = text_series.str.len().median()
            if median_len >= FREE_TEXT_MEDIAN_LEN:
                roles[col] = "free_text"
                continue

        # ── 5. Required? (no missing values at all) ─────────────────────────
        missing_count = series.isna().sum() + (series == "").sum()
        if missing_count == 0:
            roles[col] = "required"
            continue

        # ── 6. Default ──────────────────────────────────────────────────────
        roles[col] = "optional"

    return roles


def severity_for_role(role: str) -> str:
    """
    Return the appropriate severity for a missing-value proposal on this column.

    identifier → critical
    required   → warning
    everything else → info
    """
    if role == "identifier":
        return "critical"
    if role == "required":
        return "warning"
    return "info"
