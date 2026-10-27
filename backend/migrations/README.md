# backend/migrations/README.md

## How Migrations Work in Sift

Sift uses a **hybrid migration strategy**: SQLAlchemy ORM `create_all` for Phase 1 tables, then ordered SQL files for subsequent phases.

---

### Migration Files

| File | Phase | What it creates |
|---|---|---|
| *(ORM)* | Phase 1 | `files`, `quality_reports` — created by `Base.metadata.create_all()` in `db.py` |
| `002_phase2.sql` | Phase 2 | `sources`, `documents`, `records` |
| `003_phase3.sql` | Phase 3 | `source_versions`, `column_roles`, `cleaning_proposals` |
| `004_phase4.sql` | Phase 4 | `index_runs`, `chunks`, `queries`, `retrieval_results` |

---

### How `init_db()` Applies Them

`db.py::init_db()` runs on every startup via the FastAPI `@app.on_event("startup")` hook:

```python
# Phase 1: ORM create_all (idempotent)
Base.metadata.create_all(bind=engine)

# Phase 2-4: SQL files applied in order
for fname in ("002_phase2.sql", "003_phase3.sql", "004_phase4.sql"):
    migration_path = Path(__file__).parent / "migrations" / fname
    ...
```

All SQL statements use `IF NOT EXISTS` / `ON CONFLICT DO NOTHING` so they are **fully idempotent** — safe to apply multiple times.

---

### Adding a New Migration (e.g., Phase 5)

1. Create `backend/migrations/005_phase5.sql` with all your `CREATE TABLE IF NOT EXISTS` statements.
2. Add `"005_phase5.sql"` to the **ordered list** in `init_db()` in `db.py`:

   ```python
   for fname in ("002_phase2.sql", "003_phase3.sql", "004_phase4.sql", "005_phase5.sql"):
   ```

3. Add any new SQLAlchemy ORM models to `db.py` and call `Base.metadata.create_all()` will pick them up automatically.
4. Update this README table above with the new file.

> **Rule:** Never edit existing migration files once they have been applied to any environment.
> Always add a new numbered file instead.

---

### Resetting the Database (Development Only)

```bash
# Drop and recreate the sift database
psql -U postgres -c "DROP DATABASE IF EXISTS sift;"
psql -U postgres -c "CREATE DATABASE sift;"
psql -U postgres -d sift -c "CREATE EXTENSION IF NOT EXISTS vector;"

# Restart the app — init_db() will re-apply all migrations
uvicorn main:app --reload
```