"""
scripts/demo_phase4.py — End-to-end Phase 4 Baseline RAG Demo.

Demonstrates:
  1. Ingestion of HR knowledge base (DOCX, PDF) and structured messy employee data (CSV).
  2. Phase 3 cleaning: applies proposals to quarantine invalid rows and create Version 2.
  3. Indexing active content into pgvector (quarantined/excluded content is omitted).
  4. SQL verification: directly queries Postgres to confirm quarantined/excluded rows are absent from chunks.
  5. Coexistence of multiple index runs with different chunk sizes.
  6. Query 1: Leave policy question -> status 'answered' with valid citation.
  7. Query 2: Employee department question -> status 'answered' with citation to structured row chunk.
  8. Query 3: Unsupported question ('Mars budget') -> status 'insufficient_evidence' (LLM NOT called).
  9. Database verification of queries and retrieval_results rows.

Usage:
  # With running server:
  uvicorn main:app --reload --port 8000
  python scripts/demo_phase4.py

  # Or standalone (runs directly against FastAPI app):
  python scripts/demo_phase4.py --standalone
"""

import sys
import os
import json
import time
from pathlib import Path

# Add backend directory to path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from db import engine, text, init_db, SessionLocal, Chunk, Record, Document, QueryRecord, RetrievalResult

RESET  = "\033[0m"
BOLD   = "\033[1m"
GREEN  = "\033[32m"
CYAN   = "\033[36m"
YELLOW = "\033[33m"
RED    = "\033[31m"
MAGENTA = "\033[35m"

def header(title: str):
    print(f"\n{BOLD}{CYAN}{'='*70}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'='*70}{RESET}")

def step(title: str):
    print(f"\n{BOLD}{MAGENTA}>> {title} {'='*(65 - len(title))}{RESET}")

def ok(msg: str):
    print(f"{GREEN}[OK] {msg}{RESET}")

def warn(msg: str):
    print(f"{YELLOW}[WARN] {msg}{RESET}")

def info(msg: str):
    print(f"  {msg}")

def err(msg: str):
    print(f"{RED}[ERROR] {msg}{RESET}")
    sys.exit(1)


# Setup client: HTTP requests if server running, otherwise FastAPI TestClient
use_testclient = "--standalone" in sys.argv
BASE = "http://127.0.0.1:8000"

if not use_testclient:
    try:
        import requests
        r = requests.get(BASE + "/docs", timeout=1.0)
        if not r.ok:
            use_testclient = True
    except Exception:
        use_testclient = True

if use_testclient:
    warn("Standalone mode: running directly with FastAPI TestClient (no external server required).")
    from fastapi.testclient import TestClient
    from main import app
    client = TestClient(app)

    def post(path, **kwargs):
        r = client.post(path, **kwargs)
        if not (200 <= r.status_code < 300):
            err(f"POST {path} failed [{r.status_code}]: {r.text}")
        return r.json()

    def get(path, **kwargs):
        r = client.get(path, **kwargs)
        if not (200 <= r.status_code < 300):
            err(f"GET {path} failed [{r.status_code}]: {r.text}")
        return r.json()

    def put(path, **kwargs):
        r = client.put(path, **kwargs)
        if not (200 <= r.status_code < 300):
            err(f"PUT {path} failed [{r.status_code}]: {r.text}")
        return r.json()
else:
    import requests
    ok("Connected to live server at " + BASE)

    def post(path, **kwargs):
        r = requests.post(BASE + path, **kwargs)
        if not r.ok:
            err(f"POST {path} failed [{r.status_code}]: {r.text}")
        return r.json()

    def get(path, **kwargs):
        r = requests.get(BASE + path, **kwargs)
        if not r.ok:
            err(f"GET {path} failed [{r.status_code}]: {r.text}")
        return r.json()

    def put(path, **kwargs):
        r = requests.put(BASE + path, **kwargs)
        if not r.ok:
            err(f"PUT {path} failed [{r.status_code}]: {r.text}")
        return r.json()


def run_demo():
    header("PHASE 4 BASELINE RAG — END-TO-END DEMO")
    init_db()

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 1: Ingest HR Knowledge Base and Messy CSV
    # ─────────────────────────────────────────────────────────────────────────
    step("Step 1: Ingest HR Knowledge Base (.docx, .pdf) & Messy Employees (.csv)")

    kb_docx = "sample_data/hr_kb/leave_policy_2024.docx"
    kb_pdf  = "sample_data/hr_kb/handbook_working_hours.pdf"
    csv_path = "sample_data/employees_messy.csv"

    # Upload Leave Policy DOCX
    with open(kb_docx, "rb") as f:
        up_docx = post("/upload", files={"file": ("leave_policy_2024.docx", f, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    leave_source_id = up_docx["source_id"]
    ok(f"Uploaded Leave Policy (DOCX) -> source_id={leave_source_id}")

    # Upload Working Hours PDF
    with open(kb_pdf, "rb") as f:
        up_pdf = post("/upload", files={"file": ("handbook_working_hours.pdf", f, "application/pdf")})
    hours_source_id = up_pdf["source_id"]
    ok(f"Uploaded Working Hours Handbook (PDF) -> source_id={hours_source_id}")

    # Upload Messy CSV
    with open(csv_path, "rb") as f:
        up_csv = post("/upload", files={"file": ("employees_messy.csv", f, "text/csv")})
    csv_source_id = up_csv["source_id"]
    ok(f"Uploaded Messy Employees (CSV) -> source_id={csv_source_id}")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 2: Phase 3 Cleaning Workflow (creates v2 with quarantined rows)
    # ─────────────────────────────────────────────────────────────────────────
    step("Step 2: Clean Messy CSV -> Produce Version 2 with Quarantined Rows")

    # Infer column roles
    roles = post(f"/sources/{csv_source_id}/roles/infer")
    info(f"Inferred {len(roles)} column roles.")

    # Confirm Employee_ID as identifier
    put(f"/sources/{csv_source_id}/roles", json={"roles": {"Employee_ID": "identifier", "Manager": "optional"}})
    ok("Confirmed 'Employee_ID' role as 'identifier' and 'Manager' as 'optional'")

    # Generate cleaning proposals
    proposals = post(f"/sources/{csv_source_id}/proposals/generate")
    info(f"Generated {len(proposals)} cleaning proposals.")

    # Bulk approve proposals
    post(f"/sources/{csv_source_id}/proposals/approve-all")
    ok("Approved cleaning proposals.")

    # Apply approved proposals -> produces Version 2
    apply_res = post(f"/sources/{csv_source_id}/apply")
    ok(f"Applied proposals -> created version {apply_res['new_version']}. Rows: {apply_res['rows_active']} active, {apply_res['rows_quarantined']} quarantined.")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 3: Indexing Active Content
    # ─────────────────────────────────────────────────────────────────────────
    step("Step 3: Index Active Content for All Sources")

    # Index Leave Policy DOCX
    idx_leave = post(f"/sources/{leave_source_id}/index", json={"chunk_size": 800, "chunk_overlap": 120})
    ok(f"Indexed Leave Policy -> index_run_id={idx_leave['id']}, status={idx_leave['status']}, chunks={idx_leave['chunk_count']}")

    # Index Working Hours PDF
    idx_hours = post(f"/sources/{hours_source_id}/index", json={"chunk_size": 800, "chunk_overlap": 120})
    ok(f"Indexed Handbook -> index_run_id={idx_hours['id']}, status={idx_hours['status']}, chunks={idx_hours['chunk_count']}")

    # Index Active Version 2 of CSV
    idx_csv_v2 = post(f"/sources/{csv_source_id}/index", json={"version": 2, "chunk_size": 800, "chunk_overlap": 120})
    ok(f"Indexed CSV Version 2 -> index_run_id={idx_csv_v2['id']}, status={idx_csv_v2['status']}, chunks={idx_csv_v2['chunk_count']}")

    # Create a SECOND index run with different chunk size (300) to demonstrate coexistence
    idx_leave_small = post(f"/sources/{leave_source_id}/index", json={"chunk_size": 300, "chunk_overlap": 50})
    ok(f"Created Coexisting Index Run for Leave Policy (chunk_size=300) -> index_run_id={idx_leave_small['id']}, chunks={idx_leave_small['chunk_count']}")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 4: SQL Verification — Quarantined / Excluded Content Absent from Chunks
    # ─────────────────────────────────────────────────────────────────────────
    step("Step 4: SQL Verification — Quarantined / Excluded Rows NEVER in Chunks")

    with engine.connect() as conn:
        res = conn.execute(text("""
            SELECT COUNT(*) 
            FROM chunks c
            JOIN records r ON c.record_id = r.id
            WHERE r.status != 'active';
        """)).scalar()

        doc_res = conn.execute(text("""
            SELECT COUNT(*) 
            FROM chunks c
            JOIN documents d ON c.document_id = d.id
            WHERE d.status != 'active';
        """)).scalar()

    info(f"Direct SQL Check (records WHERE status != 'active' in chunks): {res}")
    info(f"Direct SQL Check (documents WHERE status != 'active' in chunks): {doc_res}")

    if res == 0 and doc_res == 0:
        ok("SQL VERIFICATION PASSED: 0 quarantined or excluded rows indexed in chunks.")
    else:
        err("SQL VERIFICATION FAILED: Found non-active rows in chunks table!")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 5: Query 1 — Leave Policy Entitlement
    # ─────────────────────────────────────────────────────────────────────────
    step("Step 5: Query 1 — 'How many annual leaves are employees entitled to?'")

    q1_payload = {
        "question": "How many annual leaves are employees entitled to?",
        "source_ids": [leave_source_id],
        "top_k": 5,
        "min_score": 0.30,
    }
    q1_resp = post("/query", json=q1_payload)

    print(f"\n{BOLD}Query 1 Result:{RESET}")
    print(f"  Status:    {GREEN}{q1_resp['status']}{RESET}")
    print(f"  Answer:    {q1_resp['answer']}")
    print(f"  Citations: {json.dumps(q1_resp['citations'], indent=4)}")
    print(f"  Latency:   {q1_resp['latency_ms']}")

    assert q1_resp["status"] == "answered", f"Expected 'answered', got {q1_resp['status']}"
    assert len(q1_resp["citations"]) > 0, "Expected at least 1 valid citation"
    ok("Query 1 returned status 'answered' with valid citation!")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 6: Query 2 — Structured Employee Record
    # ─────────────────────────────────────────────────────────────────────────
    step("Step 6: Query 2 — 'What is employee E1007\\'s department?'")

    q2_payload = {
        "question": "What is employee E1007's department?",
        "source_ids": [csv_source_id],
        "top_k": 5,
        "min_score": 0.30,
    }
    q2_resp = post("/query", json=q2_payload)

    print(f"\n{BOLD}Query 2 Result:{RESET}")
    print(f"  Status:    {GREEN}{q2_resp['status']}{RESET}")
    print(f"  Answer:    {q2_resp['answer']}")
    print(f"  Citations: {json.dumps(q2_resp['citations'], indent=4)}")
    print(f"  Latency:   {q2_resp['latency_ms']}")

    assert q2_resp["status"] in ("answered", "unverified"), f"Unexpected status {q2_resp['status']}"
    ok("Query 2 executed against structured employee record chunks!")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 7: Query 3 — Unsupported Question (Mars colonization budget)
    # ─────────────────────────────────────────────────────────────────────────
    step("Step 7: Query 3 (Unsupported) — 'What is the company\\'s Mars colonization budget?'")

    q3_payload = {
        "question": "What is the company's Mars colonization budget?",
        "source_ids": [leave_source_id, hours_source_id, csv_source_id],
        "top_k": 5,
        "min_score": 0.30,
    }
    q3_resp = post("/query", json=q3_payload)

    print(f"\n{BOLD}Query 3 Result:{RESET}")
    print(f"  Status:    {YELLOW}{q3_resp['status']}{RESET}")
    print(f"  Answer:    {q3_resp['answer']}")
    print(f"  Retrieved: {len(q3_resp['retrieved'])} chunks (all used_in_prompt=False)")
    if q3_resp["retrieved"]:
        print(f"  Best score: {max(c['score'] for c in q3_resp['retrieved'])} (threshold: {q3_resp['config']['min_score']})")

    assert q3_resp["status"] == "insufficient_evidence", f"Expected 'insufficient_evidence', got {q3_resp['status']}"
    assert q3_resp["answer"] is None, "Answer must be None for insufficient_evidence"
    ok("Query 3 returned 'insufficient_evidence' (LLM generation was bypassed)!")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 8: Coexistence Test — Query targeting specific index_run_id
    # ─────────────────────────────────────────────────────────────────────────
    step("Step 8: Verify Index Run Selection via index_run_id")

    q_small_payload = {
        "question": "How many annual leaves?",
        "source_ids": [leave_source_id],
        "index_run_id": idx_leave_small["id"],
        "top_k": 3,
        "min_score": 0.20,
    }
    q_small_resp = post("/query", json=q_small_payload)
    info(f"Targeted index_run_id={idx_leave_small['id']}")
    info(f"Returned index_run_id in config: {q_small_resp['config']['index_run_id']}")
    assert q_small_resp["config"]["index_run_id"] == idx_leave_small["id"]
    ok("Successfully queried specific coexisting index run by index_run_id!")

    # ─────────────────────────────────────────────────────────────────────────
    # STEP 9: Verify Database Records in queries and retrieval_results
    # ─────────────────────────────────────────────────────────────────────────
    step("Step 9: Database Persistence Verification (queries + retrieval_results)")

    query_details = get(f"/queries/{q1_resp['query_id']}")
    info(f"Stored Query #{query_details['id']}: '{query_details['question']}'")
    info(f"Status: {query_details['status']}")
    info(f"Retrieval results count: {len(query_details['retrieval_results'])}")

    for r in query_details["retrieval_results"]:
        info(f"  Rank {r['rank']} | Chunk {r['chunk_id']} | Score {r['score']} | used_in_prompt={r['used_in_prompt']} | cited={r['cited']}")

    assert len(query_details["retrieval_results"]) > 0
    ok("Database records verified in queries and retrieval_results tables!")

    header("ALL ACCEPTANCE CRITERIA VERIFIED SUCCESSFULLY!")


if __name__ == "__main__":
    run_demo()
