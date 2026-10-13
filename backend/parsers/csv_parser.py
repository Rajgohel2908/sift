"""
parsers/csv_parser.py — Reads a CSV or XLSX file into a Pandas DataFrame.
Returns the DataFrame as-is; the profiler does all the analysis.
"""

import pandas as pd


def parse_csv(filepath: str) -> pd.DataFrame:
    """Read a CSV file. Keeps all columns as-is (no coercion)."""
    return pd.read_csv(filepath, dtype=str)   # dtype=str preserves raw values


def parse_xlsx(filepath: str) -> pd.DataFrame:
    """Read an Excel file. Returns the first sheet."""
    return pd.read_excel(filepath, dtype=str, engine="openpyxl")
