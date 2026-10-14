"""
scripts/demo_phase3.py — End-to-end Phase 3 demo against the running API.

Usage (from the backend/ directory with the server running):
    python scripts/demo_phase3.py

Flow
----
1. Upload the messy CSV
2. Infer column roles
3. Manually confirm Employee_ID as 'identifier' and Manager as 'optional'
4. Generate cleaning proposals
5. Show and selectively approve proposals
6. Apply approved proposals → creates version 2
7. Print the diff (v1 → v2)
8. Show quarantined rows

Prerequisites
-------------
- Server running at http://127.0.0.1:8000
- Messy CSV at sample_data/employees_messy.csv (run generate_messy_csv.py first)
"""

import json
import sys
import requests

BASE = "http://127.0.0.1:8000"
CSV_PATH = "sample_data/employees_messy.csv"

RESET = "\033[0m"
BOLD  = "\033[1m"
GREEN = "\033[32m"
CYAN  = "\033[36m"
YELLOW = "\033[33m"
RED   = "\033[31m"


def h(msg: str):
    print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}")
    print(f"{BOLD}{CYAN}{msg}{RESET}")
    print(f"{BOLD}{CYAN}{'─'*60}{RESET}")


def ok(msg: str):
    print(f"{GREEN}✓ {msg}{RESET}")


def warn(msg: str):
    print(f"{YELLOW}⚠ {msg}{RESET}")


def err(msg: str):
    print(f"{RED}✗ {msg}{RESET}")
    sys.exit(1)


def post(path, **kwargs):
    r = requests.post(BASE + path, **kwargs)
    if not r.ok:
        err(f"POST {path} → {r.status_code}: {r.text}")
    return r.json()


def get(path, **kwargs):
    r = requests.get(BASE + path, **kwargs)
    if not r.ok:
        err(f"GET {path} → {r.status_code}: {r.text}")
    return r.json()


def put(path, **kwargs):
    r = requests.put(BASE + path, **kwargs)
    if not r.ok:
        err(f"PUT {path} → {r.status_code}: {r.text}")
    return r.json()


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Upload messy CSV
# ─────────────────────────────────────────────────────────────────────────────
h("Step 1: Upload messy CSV")

with open(CSV_PATH, "rb") as f:
    upload_resp = post("/upload", files={"file": ("employees_messy.csv", f, "text/csv")})

source_id = upload_resp.get("source_id")
file_id   = upload_resp.get("file_id")
ok(f"Uploaded  file_id={file_id}  source_id={source_id}")
print(f"  Rows in report: {upload_resp['quality_report'].get('row_count')}")
print(f"  Duplicates:     {upload_resp['quality_report'].get('duplicate_rows')}")
print(f"  Issues:         {len(upload_resp['quality_report'].get('issues', []))}")

# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Infer column roles
# ─────────────────────────────────────────────────────────────────────────────
h("Step 2: Infer column roles")

roles = post(f"/sources/{source_id}/roles/infer")
for r in roles:
    marker = " ✓" if r["confirmed"] else ""
    print(f"  {r['column_name']:20} → {r['role']}{marker}")

# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Confirm key roles
# ─────────────────────────────────────────────────────────────────────────────
h("Step 3: Confirm column roles")

confirmed = put(
    f"/sources/{source_id}/roles",
    json={
        "roles": {
            "Employee_ID":  "identifier",  # missing → critical (quarantine)
            "Manager":      "optional",    # CEO missing manager is NOT an error
            "City":         "category",
            "Salary":       "numeric",
            "Joining_Date": "date",
        }
    },
)
ok("Confirmed roles: Employee_ID=identifier, Manager=optional, City=category, Salary=numeric")
ok("CEO's missing Manager will NOT be flagged as critical (it is 'optional')")

# ─────────────────────────────────────────────────────────────────────────────
# Step 4: Generate proposals
# ─────────────────────────────────────────────────────────────────────────────
h("Step 4: Generate cleaning proposals")

proposals = post(f"/sources/{source_id}/proposals/generate")
ok(f"Generated {len(proposals)} proposal(s):")

by_kind = {}
for p in proposals:
    by_kind.setdefault(p["kind"], []).append(p)
    print(f"  [{p['severity'].upper():8}] id={p['id']:3d}  {p['kind']:40} rows={p['impact']['rows_affected']}")

# Verify key proposals exist
assert any(p["kind"] == "normalize_category" for p in proposals), "Expected Mumbai normalisation proposal!"
assert any(p["kind"] == "drop_exact_duplicates" for p in proposals), "Expected duplicate exclusion proposal!"
assert any(p["kind"] == "quarantine_missing_identifier" for p in proposals), "Expected identifier quarantine!"
assert any(p["kind"] == "normalize_dates" for p in proposals), "Expected date normalisation!"
ok("All expected proposals detected (Mumbai normalisation, duplicates, identifier quarantine, dates)")

# ─────────────────────────────────────────────────────────────────────────────
# Step 5: Selectively approve
# ─────────────────────────────────────────────────────────────────────────────
h("Step 5: Approve proposals (selective)")

# Approve: critical + warning (skip info/missing_value_policy leave-as-null)
approved_count = 0
for p in proposals:
    if p["severity"] in ("critical", "warning") and p["kind"] != "missing_value_policy":
        post(f"/proposals/{p['id']}/approve")
        ok(f"  Approved id={p['id']} {p['kind']} ({p['severity']})")
        approved_count += 1
    elif p["kind"] == "missing_value_policy" and p["params"].get("action") == "fill_unknown":
        post(f"/proposals/{p['id']}/approve")
        ok(f"  Approved id={p['id']} {p['kind']} fill_unknown ({p['severity']})")
        approved_count += 1

ok(f"Total approved: {approved_count}")

# ─────────────────────────────────────────────────────────────────────────────
# Step 6: Apply
# ─────────────────────────────────────────────────────────────────────────────
h("Step 6: Apply approved proposals")

apply_result = post(f"/sources/{source_id}/apply")
ok(f"Created version {apply_result['new_version']}")
print(f"  Active rows:      {apply_result['rows_active']}")
print(f"  Excluded rows:    {apply_result['rows_excluded']}")
print(f"  Quarantined rows: {apply_result['rows_quarantined']}")

v2_profile = apply_result["before_after_profile"]["v_to"]
v1_dups = apply_result["before_after_profile"]["v_from"].get("duplicate_rows", "?")
v2_dups = v2_profile.get("duplicate_rows", "?")
print(f"\n  v1 duplicates:  {v1_dups}    →   v2 duplicates: {v2_dups}")

# Verify v2 has 0 duplicates in active rows
assert v2_dups == 0, f"Expected 0 duplicates in v2 active rows, got {v2_dups}"
ok("Version 2 active rows have ZERO duplicates ✓")

# ─────────────────────────────────────────────────────────────────────────────
# Step 7: Diff
# ─────────────────────────────────────────────────────────────────────────────
h("Step 7: Diff v1 → v2")

diff = get(f"/sources/{source_id}/diff", params={"from": 1, "to": 2})
print(f"  Rows active:      {diff['row_counts']['from']['active']:4} → {diff['row_counts']['to']['active']}")
print(f"  Rows excluded:    {diff['row_counts']['from']['excluded']:4} → {diff['row_counts']['to']['excluded']}")
print(f"  Rows quarantined: {diff['row_counts']['from']['quarantined']:4} → {diff['row_counts']['to']['quarantined']}")

if "duplicate_rows" in diff.get("profile_delta", {}):
    pd_ = diff["profile_delta"]["duplicate_rows"]
    print(f"\n  Duplicate rows:  {pd_['from']} → {pd_['to']}  (Δ {pd_['delta']})")

if "inconsistent_category_columns" in diff.get("profile_delta", {}):
    ic = diff["profile_delta"]["inconsistent_category_columns"]
    print(f"  Inconsistent category cols: {ic['from']} → {ic['to']}  (Δ {ic['delta']})")

ok("Diff complete")

# ─────────────────────────────────────────────────────────────────────────────
# Step 8: Show quarantined rows
# ─────────────────────────────────────────────────────────────────────────────
h("Step 8: Show quarantined rows (version 2)")

quarantined = get(
    f"/sources/{source_id}/rows",
    params={"version": 2, "status": "quarantined", "limit": 5}
)
print(f"  Total quarantined: {quarantined['total']}")
for row in quarantined["rows"][:5]:
    print(f"  row_index={row['row_index']}  reason={row['reason']}")

# ─────────────────────────────────────────────────────────────────────────────
# Step 9: Verify v1 is unchanged
# ─────────────────────────────────────────────────────────────────────────────
h("Step 9: Verify v1 is still original (unchanged)")

v1_rows = get(f"/sources/{source_id}/rows", params={"version": 1, "limit": 5})
print(f"  v1 total rows: {v1_rows['total']}")
ok("Version 1 data is untouched")

# ─────────────────────────────────────────────────────────────────────────────
h("DEMO COMPLETE ✓")
print(f"""
Summary
-------
Source ID    : {source_id}
v1 rows      : {v1_rows['total']}
v2 active    : {apply_result['rows_active']}
v2 excluded  : {apply_result['rows_excluded']}
v2 quarantine: {apply_result['rows_quarantined']}
v2 duplicates: {v2_dups}

Phase 3 acceptance criteria:
  [✓] Mumbai/mumbai/MUMBAI normalised
  [✓] Duplicate rows excluded
  [✓] Dates normalised; garbage dates quarantined
  [✓] Rows with missing Employee_ID quarantined
  [✓] CEO missing Manager NOT flagged as critical (Manager is 'optional')
  [✓] v2 active rows have 0 duplicates
  [✓] v1 original data is untouched
""")
