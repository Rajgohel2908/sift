"""
profiler/export_utils.py — Utilities for spreadsheet export safety and formula injection prevention.

Mitigates CSV/XLSX Formula Injection (DDE injection):
Values beginning with =, +, -, @ can trigger formula execution in spreadsheet software
(Excel, LibreOffice, Google Sheets).
"""

from typing import Any


def is_formula_injection(value: Any) -> bool:
    """
    Check if a value looks like a spreadsheet formula injection.

    Detects cells starting with =, +, -, @ (also after leading whitespace/tab).
    Benign numbers (e.g. -5, -10.5) and phone numbers (e.g. +919876543210)
    in numeric/identifier-looking columns are NOT flagged as injection.
    Only values that look like a formula (e.g. contains parentheses, pipe, or starts with = or @)
    are flagged.
    """
    if not isinstance(value, str):
        return False

    stripped = value.lstrip(" \t\r\n")
    if not stripped:
        return False

    # Values starting with = or @ always look like formulas
    if stripped.startswith(("=", "@")):
        return True

    # Values starting with + or -: only flag if they contain formula indicators
    if stripped.startswith(("+", "-")):
        return any(ch in stripped for ch in ("(", ")", "|", "="))

    return False


def mask_formula(value: Any) -> str:
    """
    Produce a safe masked version of a formula value for reporting (e.g. '=1***', '=c***').
    """
    s = str(value).strip()
    prefix = s[:2] if len(s) >= 2 else s[:1]
    return f"{prefix}***"


def escape_for_spreadsheet(value: Any) -> Any:
    """
    Prefix a single quote (') for dangerous values to neutralize formula execution
    upon future spreadsheet export, while leaving safe values untouched.
    """
    if not isinstance(value, str):
        return value

    if is_formula_injection(value):
        return f"'{value}"

    return value
