"""
test_sec_fixes.py — Regression tests for Security Audit Fixes (SEC-STAT-01 through SEC-P1-03).
"""

import os
import sys
import subprocess
import pytest
from pathlib import Path

BACKEND_DIR = Path(__file__).parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ── Fix 1: SEC-STAT-01 Regression Tests ──────────────────────────────────────

def test_sec_stat_01_fail_fast_missing_database_url(monkeypatch):
    """Verify that omitting DATABASE_URL fails fast at startup with a clear message."""
    cmd = [
        sys.executable,
        "-c",
        (
            "import os, sys\n"
            "os.environ.pop('DATABASE_URL', None)\n"
            "# Force mock dotenv to not load a .env file\n"
            "import dotenv\n"
            "dotenv.load_dotenv = lambda *args, **kwargs: None\n"
            "if 'db' in sys.modules: del sys.modules['db']\n"
            "import db\n"
        )
    ]
    result = subprocess.run(cmd, cwd=str(BACKEND_DIR), capture_output=True, text=True)
    assert result.returncode != 0, f"Expected process to fail fast when DATABASE_URL is missing, but it succeeded:\n{result.stdout}"
    assert "DATABASE_URL environment variable is required" in (result.stderr + result.stdout), (
        f"Expected clear error message mentioning DATABASE_URL, got:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


def test_sec_stat_01_fail_fast_missing_gemini_api_key():
    """Verify that omitting GEMINI_API_KEY fails fast at startup with a clear message."""
    cmd = [
        sys.executable,
        "-c",
        (
            "import os, sys\n"
            "os.environ['DATABASE_URL'] = 'postgresql+psycopg2://mock:mock@localhost:5432/mock'\n"
            "os.environ.pop('GEMINI_API_KEY', None)\n"
            "import dotenv\n"
            "dotenv.load_dotenv = lambda *args, **kwargs: None\n"
            "from main import startup\n"
            "startup()\n"
        )
    ]
    result = subprocess.run(cmd, cwd=str(BACKEND_DIR), capture_output=True, text=True)
    assert result.returncode != 0, f"Expected process to fail fast when GEMINI_API_KEY is missing, but it succeeded:\n{result.stdout}"
    assert "GEMINI_API_KEY environment variable is required" in (result.stderr + result.stdout), (
        f"Expected clear error message mentioning GEMINI_API_KEY, got:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )


def test_sec_stat_01_no_tracked_secrets():
    """Ensure no real database password remains in git-tracked files."""
    repo_root = BACKEND_DIR.parent
    cmd = ["git", "grep", "-I", "-n", "raj1818"]
    result = subprocess.run(cmd, cwd=str(repo_root), capture_output=True, text=True)
    # The only place raj1818 should appear (if at all) is past git commits or test_sec_fixes assertion
    ignored = ("test_sec_fixes.py", "SECURITY_TEST_REPORT.md", "FIXES_REPORT.md", "build_report.py", "stage0_recon.py", "evidence/")
    lines = [line for line in result.stdout.splitlines() if not any(ign in line for ign in ignored)]
    assert len(lines) == 0, f"Found hardcoded credentials in tracked files:\n" + "\n".join(lines)


# ── Fix 2: SEC-P1-05 Regression Tests ──────────────────────────────────────

from fastapi.testclient import TestClient

def test_sec_p1_05_path_traversal_and_absolute_paths():
    """Verify ../../x.csv and absolute paths are sanitized to basename only."""
    from main import app
    client = TestClient(app)
    csv_bytes = b"col1,col2\nval1,val2\n"

    # Relative path traversal
    resp1 = client.post("/upload", files={"file": ("../../traversal.csv", csv_bytes, "text/csv")})
    assert resp1.status_code == 200
    assert resp1.json()["filename"] == "traversal.csv", f"Failed traversal sanitization: {resp1.json()['filename']}"

    # Absolute Unix path
    resp2 = client.post("/upload", files={"file": ("/etc/passwd.csv", csv_bytes, "text/csv")})
    assert resp2.status_code == 200
    assert resp2.json()["filename"] == "passwd.csv", f"Failed absolute path sanitization: {resp2.json()['filename']}"

    # Windows style path
    resp3 = client.post("/upload", files={"file": ("C:\\Windows\\System32\\calc.csv", csv_bytes, "text/csv")})
    assert resp3.status_code == 200
    assert resp3.json()["filename"] == "calc.csv", f"Failed Windows path sanitization: {resp3.json()['filename']}"


def test_sec_p1_05_null_bytes_and_unicode_and_long_names():
    """Verify null bytes stripped, unicode preserved, and length capped at 255."""
    from main import app
    client = TestClient(app)
    csv_bytes = b"a,b\n1,2\n"

    # Null bytes and control characters
    resp_null = client.post("/upload", files={"file": ("test\x00\x07evil.csv", csv_bytes, "text/csv")})
    assert resp_null.status_code == 200
    clean_name = resp_null.json()["filename"]
    assert "\x00" not in clean_name and "\x07" not in clean_name
    assert clean_name == "testevil.csv"

    # Clean Unicode
    resp_uni = client.post("/upload", files={"file": ("データ_2026.csv", csv_bytes, "text/csv")})
    assert resp_uni.status_code == 200
    assert resp_uni.json()["filename"] == "データ_2026.csv"

    # Very long filename (> 255 chars)
    long_name = ("x" * 300) + ".csv"
    resp_long = client.post("/upload", files={"file": (long_name, csv_bytes, "text/csv")})
    assert resp_long.status_code == 200
    assert len(resp_long.json()["filename"]) <= 255
    assert resp_long.json()["filename"].endswith(".csv")


def test_sec_p1_05_disk_storage_uuid_no_overwrite():
    """Two uploads with the exact same name must use unique server UUID names and never overwrite."""
    from main import app, UPLOAD_DIR
    client = TestClient(app)
    
    csv_a = b"id,val\n1,first_upload\n"
    csv_b = b"id,val\n2,second_upload\n"

    resp_a = client.post("/upload", files={"file": ("same_name.csv", csv_a, "text/csv")})
    resp_b = client.post("/upload", files={"file": ("same_name.csv", csv_b, "text/csv")})
    assert resp_a.status_code == 200 and resp_b.status_code == 200

    # Ensure files stored on disk under UPLOAD_DIR use uuid names, not same_name.csv
    same_name_path = UPLOAD_DIR / "same_name.csv"
    assert not same_name_path.exists(), "File was saved directly under user-supplied name!"
    
    # Check that disk files matching uuid4 pattern exist
    stored_files = list(UPLOAD_DIR.glob("*.csv"))
    assert len(stored_files) >= 2
    # Verify contents of both are preserved
    found_a = any(f.read_bytes() == csv_a for f in stored_files)
    found_b = any(f.read_bytes() == csv_b for f in stored_files)
    assert found_a and found_b


def test_sec_p2_07_defusedxml_entity_rejection(caplog):
    """SEC-P2-07: XML sitemap parsing must use defusedxml, reject entity expansion and external entities, and continue gracefully."""
    import logging
    from unittest.mock import MagicMock
    import crawler.sitemap_parser as sm
    
    # 1. Verify defusedxml is imported
    assert "defusedxml" in getattr(sm.ET, "__name__", str(sm.ET)), "sitemap_parser is not using defusedxml"

    # 2. Verify defusedxml is in requirements.txt
    req_path = Path(__file__).resolve().parent.parent / "requirements.txt"
    assert "defusedxml" in req_path.read_text(), "defusedxml not found in requirements.txt"

    # Mock client and robots_cache
    client = MagicMock()
    robots_cache = MagicMock()
    robots_cache.get_sitemap_urls.return_value = ["http://example.com/sitemap.xml"]

    # 3. Test entity-expansion payload (billion laughs / quadratic blowup)
    expansion_payload = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ELEMENT lolz (#PCDATA)>
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://example.com/&lol1;</loc></url>
</urlset>"""

    client.get.return_value = MagicMock(status_code=200, headers={"content-type": "application/xml"}, text=expansion_payload)
    with caplog.at_level(logging.WARNING):
        urls = sm.collect_urls("http://example.com/", client, robots_cache, max_pages=10)
    # Must reject without hanging and continue gracefully by returning seed URL fallback
    assert urls == ["http://example.com/"]
    assert any("sitemap" in record.message.lower() for record in caplog.records)

    # 4. Test external-entity payload (XXE)
    xxe_payload = """<?xml version="1.0"?>
<!DOCTYPE foo [
 <!ELEMENT foo ANY >
 <!ENTITY xxe SYSTEM "http://127.0.0.1:9999/secret" >
]>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>http://example.com/&xxe;</loc></url>
</urlset>"""

    caplog.clear()
    client.get.return_value = MagicMock(status_code=200, headers={"content-type": "application/xml"}, text=xxe_payload)
    with caplog.at_level(logging.WARNING):
        urls = sm.collect_urls("http://example.com/", client, robots_cache, max_pages=10)
    assert urls == ["http://example.com/"]
    assert any("sitemap" in record.message.lower() for record in caplog.records)


def test_sec_p1_01_escape_for_spreadsheet():
    """SEC-P1-01: escape_for_spreadsheet must prefix dangerous formula values with single quote and leave safe values untouched."""
    from profiler.export_utils import escape_for_spreadsheet

    # Dangerous formulas
    assert escape_for_spreadsheet("=1+1") == "'=1+1"
    assert escape_for_spreadsheet("  =cmd|'/C calc'!A0") == "'  =cmd|'/C calc'!A0"
    assert escape_for_spreadsheet("@SUM(1,2)") == "'@SUM(1,2)"
    assert escape_for_spreadsheet("-2+3+cmd|") == "'-2+3+cmd|"
    assert escape_for_spreadsheet("+SUM(A1:B1)") == "'+SUM(A1:B1)"
    assert escape_for_spreadsheet("\t@BIN2DEC(101)") == "'\t@BIN2DEC(101)"

    # Safe negative numbers and phone numbers
    assert escape_for_spreadsheet("-5") == "-5"
    assert escape_for_spreadsheet("-10.5") == "-10.5"
    assert escape_for_spreadsheet("+919876543210") == "+919876543210"
    assert escape_for_spreadsheet("+91 98765 43210") == "+91 98765 43210"
    assert escape_for_spreadsheet("Alice") == "Alice"
    assert escape_for_spreadsheet(100) == 100


def test_sec_p1_01_profiler_detects_formula_injection():
    """SEC-P1-01: Structured profiler must flag cells starting with =, @, +, - looking like formulas in text columns with masked examples, while preserving stored raw values."""
    import pandas as pd
    from profiler.structured_profiler import profile_structured

    df = pd.DataFrame({
        "formula_col": ["=1+1", "=cmd|'/C calc'!A0", "@SUM(1,2)", "-2+3+cmd|", "normal_text"],
        "safe_numbers": ["-5", "-10", "+919876543210", "-100.5", "10"],
        "names": ["Alice", "Bob", "Charlie", "David", "Eve"]
    })

    report = profile_structured(df)

    # 1. Stored values must NOT be altered
    assert df["formula_col"].iloc[0] == "=1+1"
    assert df["formula_col"].iloc[1] == "=cmd|'/C calc'!A0"

    # 2. Check detected formula injection issue
    formula_issues = [i for i in report["issues"] if "possible formula injection" in i.get("message", "")]
    assert len(formula_issues) == 1, f"Expected 1 formula injection issue, got: {formula_issues}"

    issue = formula_issues[0]
    assert issue["severity"] == "warning"
    assert issue["message"] == "possible formula injection: 4 cells in column formula_col"
    assert "examples" in issue
    assert len(issue["examples"]) <= 3
    # Check that examples are masked and don't reveal full raw payload verbatim
    for ex in issue["examples"]:
        assert "***" in ex

    # 3. Safe numbers column must NOT trigger formula injection
    assert not any("safe_numbers" in i.get("message", "") for i in report["issues"])


def test_sec_dep_01_vulnerable_dependencies_upgraded():
    """SEC-DEP-01: Verify vulnerable dependencies (starlette, python-multipart, requests) have been upgraded to non-vulnerable versions and pinned."""
    from packaging.version import parse
    import starlette
    try:
        import python_multipart as multipart
    except ImportError:
        import multipart
    import requests
    from pathlib import Path

    # Verify runtime versions
    assert parse(starlette.__version__) >= parse("0.40.0"), f"starlette version {starlette.__version__} is vulnerable (< 0.40.0)"
    assert parse(getattr(multipart, "__version__", "0.0.0")) >= parse("0.0.18"), f"python-multipart version {multipart.__version__} is vulnerable (< 0.0.18)"
    assert parse(requests.__version__) >= parse("2.33.0"), f"requests version {requests.__version__} is vulnerable (< 2.33.0)"

    # Verify requirements.txt
    req_text = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
    assert "starlette>=0.40.0" in req_text
    assert "python-multipart>=0.0.20" in req_text
    assert "requests>=2.33.0" in req_text


def test_sec_p1_03_zero_byte_and_empty_files():
    """SEC-P1-03: Uploads of 0-byte files, zero-row files, and corrupt files must return HTTP 400 with generic JSON, never 500 or leaked paths/traces."""
    from main import app
    client = TestClient(app)

    # 1. 0-byte file upload
    resp_zero = client.post("/upload", files={"file": ("empty.csv", b"", "text/csv")})
    assert resp_zero.status_code == 400, f"Expected 400 for 0-byte upload, got {resp_zero.status_code}: {resp_zero.text}"
    data_zero = resp_zero.json()
    assert "detail" in data_zero
    assert "Traceback" not in resp_zero.text
    assert "\\" not in data_zero["detail"] and "/" not in data_zero["detail"]

    # 2. File that parses to zero rows (headers only)
    resp_empty_rows = client.post("/upload", files={"file": ("headers_only.csv", b"col_a,col_b\n", "text/csv")})
    assert resp_empty_rows.status_code == 400, f"Expected 400 for 0-row file, got {resp_empty_rows.status_code}: {resp_empty_rows.text}"
    assert "Traceback" not in resp_empty_rows.text

    # 3. Corrupt PDF
    resp_corrupt_pdf = client.post("/upload", files={"file": ("corrupt.pdf", b"not-a-real-pdf-header", "application/pdf")})
    assert resp_corrupt_pdf.status_code == 400, f"Expected 400 for corrupt PDF, got {resp_corrupt_pdf.status_code}: {resp_corrupt_pdf.text}"
    assert "Traceback" not in resp_corrupt_pdf.text
    assert "SELECT " not in resp_corrupt_pdf.text

    # 4. Corrupt DOCX
    resp_corrupt_docx = client.post("/upload", files={"file": ("corrupt.docx", b"PK\x03\x04not-valid-docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert resp_corrupt_docx.status_code == 400, f"Expected 400 for corrupt DOCX, got {resp_corrupt_docx.status_code}: {resp_corrupt_docx.text}"
    assert "Traceback" not in resp_corrupt_docx.text

    # 5. Corrupt Image
    resp_corrupt_img = client.post("/upload", files={"file": ("corrupt.png", b"not-a-png-image", "image/png")})
    assert resp_corrupt_img.status_code in (400, 503), f"Expected 400 (or 503 if OCR unavailable) for corrupt image, got {resp_corrupt_img.status_code}: {resp_corrupt_img.text}"
    assert resp_corrupt_img.status_code != 500

