"""
profiler/structured_profiler.py — Data-quality profiling for CSV/XLSX DataFrames.

Outputs per-column stats + dataset-level metrics + a human-readable issues list.
This profiler is READ-ONLY — it never modifies the DataFrame.
"""

import pandas as pd
from collections import defaultdict
from profiler.export_utils import is_formula_injection, mask_formula


# ── Severity thresholds ───────────────────────────────────────────────────────
CRITICAL_MISSING_PCT  = 10.0   # >= 10% missing → critical
WARNING_MISSING_PCT   = 1.0    # >= 1%  missing → warning (else info)
CRITICAL_DUPLICATE_PCT = 5.0   # >= 5% of rows are duplicates → critical


def profile_structured(df: pd.DataFrame) -> dict:
    """
    Run a full quality profile on a DataFrame.

    Returns a dict with:
      - row_count, column_count, duplicate_rows
      - columns: per-column stats
      - inconsistent_categories: groups with multiple spellings
      - invalid_dates: counts per date-like column
      - issues: list of {message, severity}
    """
    issues = []

    # ── Dataset-level metrics ─────────────────────────────────────────────────
    row_count      = len(df)
    column_count   = len(df.columns)
    duplicate_rows = int(df.duplicated().sum())

    dup_pct = (duplicate_rows / row_count * 100) if row_count else 0
    if duplicate_rows > 0:
        severity = "critical" if dup_pct >= CRITICAL_DUPLICATE_PCT else "warning"
        issues.append({
            "message":  f"Dataset has {duplicate_rows} duplicate rows ({dup_pct:.1f}%)",
            "severity": severity,
        })

    # ── Per-column stats ──────────────────────────────────────────────────────
    column_stats = {}

    for col in df.columns:
        series      = df[col]
        total       = len(series)
        missing     = int(series.isna().sum()) + int((series == "").sum())
        missing_pct = round(missing / total * 100, 2) if total else 0.0
        unique      = int(series.nunique(dropna=True))

        column_stats[col] = {
            "dtype":       str(series.dtype),
            "missing_pct": missing_pct,
            "unique_count": unique,
        }

        # Flag missing values
        if missing_pct >= CRITICAL_MISSING_PCT:
            issues.append({
                "message":  f"{col}: {missing_pct:.1f}% missing",
                "severity": "critical",
            })
        elif missing_pct >= WARNING_MISSING_PCT:
            issues.append({
                "message":  f"{col}: {missing_pct:.1f}% missing",
                "severity": "warning",
            })

    # ── Inconsistent categories ───────────────────────────────────────────────
    # For each text column, group values by their lowercase+stripped canonical form.
    # If a group has more than one distinct spelling → report it.
    inconsistent_categories = {}

    for col in df.columns:
        series = df[col].dropna().astype(str)
        # Skip columns that look numeric (all values parse as floats)
        try:
            series.apply(float)
            continue   # numeric-ish column, skip
        except ValueError:
            pass

        groups: dict[str, list] = defaultdict(set)
        for val in series:
            if val.strip():
                canonical = val.strip().lower()
                groups[canonical].add(val)   # track original spellings

        # Keep only groups with more than one spelling
        messy = {
            canon: sorted(spellings)
            for canon, spellings in groups.items()
            if len(spellings) > 1
        }
        if messy:
            inconsistent_categories[col] = messy
            issues.append({
                "message":  (
                    f"{col}: {len(messy)} value(s) have inconsistent casing/spacing "
                    f"(e.g. {list(messy.values())[0]})"
                ),
                "severity": "warning",
            })

    # ── Invalid dates ─────────────────────────────────────────────────────────
    # Only inspect columns whose name contains "date" (case-insensitive).
    invalid_dates = {}

    for col in df.columns:
        if "date" not in col.lower():
            continue

        series    = df[col].dropna().astype(str)
        bad_count = 0

        for val in series:
            if not val.strip():
                continue
            try:
                pd.to_datetime(val, infer_datetime_format=True)
            except (ValueError, TypeError):
                bad_count += 1

        if bad_count > 0:
            invalid_dates[col] = bad_count
            issues.append({
                "message":  f"{col}: {bad_count} value(s) cannot be parsed as a date",
                "severity": "warning",
            })

    # ── Formula injection detection (CSV / XLSX DDE injection) ────────────────
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col].dtype):
            continue

        series = df[col].dropna().astype(str)
        # Skip columns where all values parse as numeric floats
        try:
            series.apply(float)
            continue
        except (ValueError, TypeError):
            pass

        formula_cells = [val for val in series if is_formula_injection(val)]
        if formula_cells:
            count = len(formula_cells)
            masked = [mask_formula(v) for v in formula_cells[:3]]
            issues.append({
                "message": f"possible formula injection: {count} cells in column {col}",
                "severity": "warning",
                "examples": masked,
            })

    # ── Build final report ────────────────────────────────────────────────────
    return {
        "row_count":                row_count,
        "column_count":             column_count,
        "duplicate_rows":           duplicate_rows,
        "columns":                  column_stats,
        "inconsistent_categories":  inconsistent_categories,
        "invalid_dates":            invalid_dates,
        "issues":                   issues,
    }
