"""
sample_data/generate_messy_csv.py
Generates employees_messy.csv (~200 rows) with deliberate data-quality issues:
  - ~10% missing Manager
  - A CEO row with legitimately missing Manager (NOT an error)
  - A few missing Employee_ID  → triggers quarantine_missing_identifier
  - ~8 duplicate rows         → triggers drop_exact_duplicates
  - City spelled as Mumbai / mumbai / MUMBAI / "Mumbai " (category inconsistency)
  - Mixed date formats: ISO, DD/MM/YYYY, "Jan 5 2023", and a few garbage values
  - Whitespace-padded values in Name and Department
  - A couple of salary outliers (very high or negative)
Run:  python sample_data/generate_messy_csv.py
"""

import random
import pandas as pd

random.seed(42)

# ── Base data pools ────────────────────────────────────────────────────────────
DEPARTMENTS = ["Engineering", "Marketing", "Sales", "HR", "Finance", "Operations"]
CITIES_CLEAN = ["Delhi", "Bangalore", "Hyderabad", "Chennai", "Pune"]
# Mumbai deliberately has spelling variants to trigger inconsistent-category flag
MUMBAI_VARIANTS = ["Mumbai", "mumbai", "MUMBAI", "Mumbai "]   # trailing space too
MANAGERS = [f"Manager_{i}" for i in range(1, 15)]

N = 191   # clean rows; CEO + 8 duplicates = 200 total


def random_date_iso(start_year=2010, end_year=2023) -> str:
    """Return a valid date string in ISO format."""
    y = random.randint(start_year, end_year)
    m = random.randint(1, 12)
    d = random.randint(1, 28)
    return f"{y}-{m:02d}-{d:02d}"


def random_date_slash(start_year=2010, end_year=2023) -> str:
    """Return a date in DD/MM/YYYY format (European style)."""
    y = random.randint(start_year, end_year)
    m = random.randint(1, 12)
    d = random.randint(1, 28)
    return f"{d:02d}/{m:02d}/{y}"


MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

def random_date_text(start_year=2010, end_year=2023) -> str:
    """Return a date like 'Jan 5 2023'."""
    y = random.randint(start_year, end_year)
    m = random.randint(1, 12)
    d = random.randint(1, 28)
    return f"{MONTH_NAMES[m-1]} {d} {y}"


rows = []
for i in range(1, N + 1):
    emp_id = f"E{i:04d}"
    name   = f"Employee_{i}"
    dept   = random.choice(DEPARTMENTS)
    salary = round(random.uniform(30_000, 120_000), 2)
    mgr    = random.choice(MANAGERS)

    # ── Mixed date formats (~60% ISO, ~20% slash, ~20% text) ─────────────────
    r = random.random()
    if r < 0.60:
        date = random_date_iso()
    elif r < 0.80:
        date = random_date_slash()
    else:
        date = random_date_text()

    # City: mostly clean cities, ~30% chance it's a Mumbai variant
    if random.random() < 0.30:
        city = random.choice(MUMBAI_VARIANTS)
    else:
        city = random.choice(CITIES_CLEAN)

    rows.append([emp_id, name, dept, salary, date, mgr, city])

df = pd.DataFrame(rows, columns=[
    "Employee_ID", "Name", "Department", "Salary", "Joining_Date", "Manager", "City"
])

# ── Inject deliberate quality issues ─────────────────────────────────────────

# 1. ~10% missing Manager  (~19 rows)
missing_mgr_idx = random.sample(range(N), 19)
df.loc[missing_mgr_idx, "Manager"] = None

# 2. A few missing Employee_ID  (5 rows) → will be quarantined
missing_id_idx = random.sample(range(N), 5)
df.loc[missing_id_idx, "Employee_ID"] = None

# 3. Invalid Joining_Date values (6 rows) → will be quarantined
bad_dates = ["not-a-date", "31/02/2021", "2099-99-99", "N/A", "--", "TBD"]
bad_date_idx = random.sample(range(N), len(bad_dates))
for idx, bad in zip(bad_date_idx, bad_dates):
    df.loc[idx, "Joining_Date"] = bad

# 4. Outlier salaries (3 rows) → will be flagged
outlier_idx = random.sample(range(N), 3)
outlier_salaries = [-5000, 9_999_999, -1]
for idx, sal in zip(outlier_idx, outlier_salaries):
    df.loc[idx, "Salary"] = sal

# 5. Whitespace-padded values in Name and Department
pad_idx = random.sample(range(N), 10)
for idx in pad_idx:
    # leading/trailing spaces in Name
    df.loc[idx, "Name"] = "  " + str(df.loc[idx, "Name"]) + "  "
for idx in random.sample(range(N), 5):
    # double-space in Department
    dept_val = str(df.loc[idx, "Department"])
    df.loc[idx, "Department"] = dept_val[0:3] + "  " + dept_val[3:]

# 6. ~8 duplicate rows: copy 8 existing rows and append them
dup_source_idx = random.sample(range(N), 8)
duplicates = df.iloc[dup_source_idx].copy()
df = pd.concat([df, duplicates], ignore_index=True)

# 7. CEO row: legitimate missing Manager (this employee has no manager)
ceo_row = {
    "Employee_ID": "E9999",
    "Name":        "CEO_Employee",
    "Department":  "Executive",
    "Salary":      500_000.00,
    "Joining_Date": "2010-01-01",
    "Manager":     None,    # CEO has no manager — NOT a data error
    "City":        "Mumbai",
}
df = pd.concat([df, pd.DataFrame([ceo_row])], ignore_index=True)

# ── Save ──────────────────────────────────────────────────────────────────────
output_path = "sample_data/employees_messy.csv"
df.to_csv(output_path, index=False)

print(f"[OK] Saved {len(df)} rows to {output_path}")
print(f"   Duplicate rows injected  : 8")
print(f"   Missing Manager          : ~19 + CEO (legitimate)")
print(f"   Missing Employee_ID      : 5  (will be quarantined)")
print(f"   Invalid Joining_Date     : {len(bad_dates)}  (will be quarantined)")
print(f"   Outlier salaries         : {len(outlier_idx)}  (will be flagged)")
print(f"   Whitespace-padded values : 15")
print(f"   Mumbai spelling variants : {MUMBAI_VARIANTS}")
print(f"   CEO row (no manager, OK) : 1")
