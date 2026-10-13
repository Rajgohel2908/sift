"""
sample_data/generate_messy_csv.py
Generates employees_messy.csv (~200 rows) with deliberate data-quality issues:
  - ~10% missing Manager
  - A few missing Employee_ID
  - ~8 duplicate rows
  - City spelled as Mumbai / mumbai / MUMBAI (inconsistent categories)
  - A few invalid Joining_Date values (e.g. "not-a-date", "31/02/2021")
  - A few outlier salaries (very high or negative)
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

N = 192   # clean rows; we'll add 8 duplicates = 200 total


def random_date(start_year=2010, end_year=2023) -> str:
    """Return a valid date string."""
    y = random.randint(start_year, end_year)
    m = random.randint(1, 12)
    d = random.randint(1, 28)
    return f"{y}-{m:02d}-{d:02d}"


rows = []
for i in range(1, N + 1):
    emp_id = f"E{i:04d}"
    name   = f"Employee_{i}"
    dept   = random.choice(DEPARTMENTS)
    salary = round(random.uniform(30_000, 120_000), 2)
    date   = random_date()
    mgr    = random.choice(MANAGERS)

    # City: mostly one of the clean cities, but ~30% chance it's a Mumbai variant
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

# 2. A few missing Employee_ID  (5 rows)
missing_id_idx = random.sample(range(N), 5)
df.loc[missing_id_idx, "Employee_ID"] = None

# 3. Invalid Joining_Date values (6 rows)
bad_dates = ["not-a-date", "31/02/2021", "2099-99-99", "N/A", "--", "TBD"]
bad_date_idx = random.sample(range(N), len(bad_dates))
for idx, bad in zip(bad_date_idx, bad_dates):
    df.loc[idx, "Joining_Date"] = bad

# 4. Outlier salaries (3 rows)
outlier_idx = random.sample(range(N), 3)
outlier_salaries = [-5000, 9_999_999, -1]
for idx, sal in zip(outlier_idx, outlier_salaries):
    df.loc[idx, "Salary"] = sal

# 5. ~8 duplicate rows: copy 8 existing rows and append them
dup_source_idx = random.sample(range(N), 8)
duplicates = df.iloc[dup_source_idx].copy()
df = pd.concat([df, duplicates], ignore_index=True)

# ── Save ──────────────────────────────────────────────────────────────────────
output_path = "sample_data/employees_messy.csv"
df.to_csv(output_path, index=False)

print(f"[OK] Saved {len(df)} rows to {output_path}")
print(f"   Duplicate rows injected : 8")
print(f"   Missing Manager         : ~19")
print(f"   Missing Employee_ID     : 5")
print(f"   Invalid Joining_Date    : {len(bad_dates)}")
print(f"   Outlier salaries        : {len(outlier_idx)}")
print(f"   Mumbai spelling variants: {MUMBAI_VARIANTS}")
