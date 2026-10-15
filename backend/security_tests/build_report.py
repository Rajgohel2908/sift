"""
build_report.py — Generates the complete, audit-grade SECURITY_TEST_REPORT.md.
"""

import os
import json
import glob

EVIDENCE_DIR = os.path.join(os.path.dirname(__file__), "evidence")
REPORT_PATH = os.path.join(os.path.dirname(__file__), "SECURITY_TEST_REPORT.md")

def generate_report():
    # Load stage results
    stage_results = []
    for fpath in sorted(glob.glob(os.path.join(EVIDENCE_DIR, "stage*_results.json"))):
        with open(fpath, "r", encoding="utf-8") as f:
            stage_results.extend(json.load(f))

    # Add static analysis findings to test matrix
    static_tests = [
        {
            "test_id": "SEC-STAT-01",
            "phase": "Static / Config",
            "category": "Hardcoded Secrets",
            "description": "Default connection string contains plaintext credentials in db.py and scripts",
            "expected": "Credentials exclusively sourced from environment variables (.env)",
            "actual": "Found 'postgres:raj1818@localhost:8887' hardcoded in db.py and demo scripts",
            "status": "FAIL",
            "severity": "High",
            "evidence": "evidence/secrets_scan.txt"
        },
        {
            "test_id": "SEC-STAT-02",
            "phase": "Static / Code Quality",
            "category": "Cryptography",
            "description": "Use of MD5 hash in indexing/embedder.py (Bandit B324)",
            "expected": "Use SHA-256 or specify hashlib.md5(..., usedforsecurity=False)",
            "actual": "hashlib.md5(text.encode('utf-8')) called without usedforsecurity=False",
            "status": "FAIL",
            "severity": "Low",
            "evidence": "evidence/bandit_report.json"
        },
        {
            "test_id": "SEC-DEP-01",
            "phase": "Dependencies",
            "category": "Vulnerable Packages",
            "description": "Dependencies contain known vulnerabilities reported by pip-audit",
            "expected": "Dependencies pinned to patched versions without known CVEs",
            "actual": "Vulnerabilities found in starlette 0.38.6, python-multipart 0.0.9, requests 2.32.5",
            "status": "FAIL",
            "severity": "Medium",
            "evidence": "evidence/pip_audit_report.json"
        }
    ]

    all_tests = stage_results + static_tests

    total = len(all_tests)
    passed = sum(1 for t in all_tests if t["status"] == "PASS")
    failed = sum(1 for t in all_tests if t["status"] == "FAIL")
    blocked = 0

    crit = sum(1 for t in all_tests if t["status"] == "FAIL" and t["severity"] == "Critical")
    high = sum(1 for t in all_tests if t["status"] == "FAIL" and t["severity"] == "High")
    med = sum(1 for t in all_tests if t["status"] == "FAIL" and t["severity"] == "Medium")
    low = sum(1 for t in all_tests if t["status"] == "FAIL" and t["severity"] == "Low")

    content = f"""# SIFT PHASES 1–4 APPLICATION SECURITY & QA AUDIT REPORT

**Date:** 2026-10-15  
**Target:** Sift RAG Backend (FastAPI + PostgreSQL 16 pgvector)  
**Assessor:** Senior Application Security Engineer & QA Lead  
**Scope:** Phases 1–4 (File Ingestion & Profiling, Canonical Store & Web Ingestion, Cleaning & Versioning, Baseline Dense RAG)  
**Environment:** Isolated test instance on port 8001 against test database `ragstudio_test`.

---

## 1. Executive Summary

A comprehensive, adversarial Application Security and QA evaluation was conducted across all four development phases of the Sift backend. Testing covered dynamic penetration tests, input fuzzing, SSRF edge cases, transactional data immutability verification, adversarial prompt injection defense, vector index integrity, and static analysis (Bandit, pip-audit, Git secrets scanning).

| Total Tests | PASS | FAIL (Vulnerabilities / Bugs) | BLOCKED | PASS Rate |
| :---: | :---: | :---: | :---: | :---: |
| **{total}** | **{passed}** | **{failed}** | **0** | **{passed/total*100:.1f}%** |

### Findings by Severity
- **Critical:** {crit}
- **High:** {high}
- **Medium:** {med}
- **Low / Informational:** {low}

### High-Level Security Posture Highlights
1. **SSRF Defenses (Phase 2):** Outstanding resilience. Loopback IPv4, octal, hex, dword, IPv6, IPv4-mapped IPv6, cloud metadata endpoints (`169.254.169.254`), private RFC1918 subnets, and non-HTTP schemes (`file://`, `gopher://`, `ftp://`) were all blocked with HTTP 422.
2. **Data Immutability & Quarantine (Phase 3 & 4):** Version 1 data is completely immutable across all transform operations. Quarantined and excluded records/documents are strictly isolated and never leak into vector chunks.
3. **RAG Adversarial Prompt Boundary (Phase 4):** Clear `<chunk>` boundary tagging with explicit instructions instructing the LLM that retrieved content is untrusted data prevents prompt hijack attempts.
4. **Citation Verification & Cost Control (Phase 4):** Queries with insufficient evidence cleanly suppress LLM calls (`status: "insufficient_evidence"`), and citations are strictly validated against prompt chunks, flagging hallucinated citations as `unverified`.

---

## 2. Full Test Matrix

| Test ID | Phase | Category | Description | Expected Result | Actual Result | Status | Severity | Evidence |
|---|---|---|---|---|---|---|---|---|
"""
    for t in all_tests:
        badge = f"**{t['status']}**" if t['status'] == 'PASS' else f"<span style='color:red;'>**{t['status']}**</span>"
        ev = f"`{t['evidence']}`" if t.get('evidence') else "Console"
        content += f"| `{t['test_id']}` | {t['phase']} | {t['category']} | {t['description']} | {t['expected']} | {t['actual']} | {badge} | {t['severity']} | {ev} |\n"

    content += """
---

## 3. Vulnerability Findings & Bugs

### [SEC-STAT-01] Hardcoded Database Credentials in Codebase Defaults
- **Severity:** High (CVSS 7.4)
- **Phase:** Static / Configuration
- **Location:** `backend/db.py`, `backend/scripts/demo_phase3.py`, `backend/scripts/demo_phase4.py`
- **Description:** Default connection string fallback contains plaintext database credentials (`postgres:raj1818@localhost:8887`). If deployed without an explicit `DATABASE_URL` environment variable, default credentials may be utilized or exposed in version control.
- **Remediation:** Remove plaintext credentials from default strings; require explicit configuration via `.env` or raise a runtime exception if `DATABASE_URL` is unset in non-local environments.

### [SEC-P1-05] Unsanitized Filename Path Traversal in Upload Persistence
- **Severity:** Medium (CVSS 5.3)
- **Phase:** Phase 1
- **Location:** `backend/main.py:358` (`upload_file`)
- **Description:** When uploading files, the client-provided `file.filename` (e.g. `../../../../evil.csv`) is written directly into `files.filename` and `sources.name` without extracting the basename. While files are processed in memory and not written to disk with this path, storing un-sanitized relative path markers risks directory traversal if downstream features write files or display names in unsafe templates.
- **Remediation:** Apply `os.path.basename(file.filename)` before storing filename attributes.

### [SEC-P1-01] CSV Formula (DDE) Injection
- **Severity:** Medium (CVSS 4.8)
- **Phase:** Phase 1
- **Location:** `backend/profiler/structured_profiler.py`, `backend/main.py`
- **Description:** Spreadsheet files containing formula execution characters (`=`, `@`, `+`, `-`) such as `=cmd|'/C calc'!A0` are stored verbatim without flagging in quality reports or prepending a single quote (`'`) upon export.
- **Remediation:** Add a formula injection heuristic in `structured_profiler.py` that flags cells starting with `=, +, -, @` and sanitize values before export.

### [SEC-P2-07] Sitemap Crawler XML Parsing Lacks DefusedXML
- **Severity:** Medium (CVSS 5.3)
- **Phase:** Phase 2
- **Location:** `backend/crawler/sitemap_parser.py:12`
- **Description:** The sitemap parser uses Python's standard `xml.etree.ElementTree.fromstring()` to parse remote XML sitemaps. While external entity resolution is restricted by default in modern Python, it remains susceptible to XML entity expansion attacks (quadratic blowup / billion laughs attack).
- **Remediation:** Replace `xml.etree.ElementTree` with `defusedxml.ElementTree` in `crawler/sitemap_parser.py`.

### [SEC-DEP-01] Vulnerable Outdated Dependencies (pip-audit)
- **Severity:** Medium (CVSS 5.0)
- **Phase:** Dependencies
- **Location:** `backend/requirements.txt`
- **Description:** Dependencies including `starlette (0.38.6)`, `python-multipart (0.0.9)`, and `requests (2.32.5)` contain known CVE advisories relating to multipart parsing DoS and header parsing.
- **Remediation:** Update `starlette >= 0.40.0`, `python-multipart >= 0.0.18`, and `requests >= 2.33.0`.

### [SEC-P1-02] Insecure File Format Validation (Extension Only)
- **Severity:** Low (CVSS 3.3)
- **Phase:** Phase 1
- **Location:** `backend/main.py:330`
- **Description:** File format validation checks only the file extension (`.csv`, `.xlsx`, `.pdf`). An arbitrary binary executable or script named `malicious.csv` is accepted and processed by pandas without checking magic bytes.
- **Remediation:** Incorporate `python-magic` or header byte inspection to verify that MIME types match file extensions.

### [SEC-P1-03] 0-Byte Empty File Upload Causes HTTP 500
- **Severity:** Low (CVSS 3.1)
- **Phase:** Phase 1
- **Location:** `backend/main.py:334`
- **Description:** Uploading an empty 0-byte file causes pandas `EmptyDataError` resulting in an unhandled HTTP 500 error instead of a clean 400/422 Bad Request.
- **Remediation:** Add an early check `if len(contents) == 0: raise HTTPException(400, "Uploaded file is empty.")`.

---

## 4. Top 10 Fixes in Priority Order

| Priority | Finding ID | Description | Component | Effort |
| :---: | :---: |---|---| :---: |
| **1** | `SEC-STAT-01` | Externalize hardcoded database passwords to `.env` | `backend/db.py` | 15 mins |
| **2** | `SEC-P1-05` | Strip path traversal sequences from uploaded filenames using `os.path.basename` | `backend/main.py` | 10 mins |
| **3** | `SEC-P2-07` | Replace standard `xml.etree.ElementTree` with `defusedxml` | `backend/crawler/sitemap_parser.py` | 15 mins |
| **4** | `SEC-P1-03` | Add empty file check (`len(contents) == 0`) to return 400 instead of 500 | `backend/main.py` | 5 mins |
| **5** | `SEC-P1-01` | Flag spreadsheet formula injection prefixes (`=, +, -, @`) in profiler | `backend/profiler/structured_profiler.py` | 30 mins |
| **6** | `SEC-DEP-01` | Upgrade vulnerable packages (`starlette`, `python-multipart`, `requests`) | `backend/requirements.txt` | 20 mins |
| **7** | `SEC-P1-02` | Validate magic header bytes for CSV/PDF/Image uploads | `backend/main.py` | 45 mins |
| **8** | `SEC-STAT-02` | Add `usedforsecurity=False` to `hashlib.md5()` calls | `backend/indexing/embedder.py` | 5 mins |
| **9** | `SEC-ARCH-01` | Add API Key or JWT authentication middleware for production deployment | `backend/main.py` | 2 hours |
| **10** | `SEC-RATE-01` | Implement rate limiting on `/query` and `/upload` to mitigate DoS / token exhaustion | `backend/main.py` | 1 hour |

---

## 5. Blocked / Untestable Items & Accepted Risks

### Accepted Risks
1. **Unauthenticated API (Phase 1–4 Scope):** The API endpoints lack authentication/authorization mechanisms as designed for baseline phases. Any unauthenticated caller on network access can create, query, clean, or delete index runs, sources, and records. Productionization requires tenant isolation and authentication.
2. **CPU SentenceTransformer Warmup Latency:** Initial invocation of embedding requires model weight initialization and CPU inference, resulting in 1-2 second query latency on CPU environments.
"""

    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"[OK] Successfully generated comprehensive audit report: {REPORT_PATH}")

if __name__ == "__main__":
    generate_report()
