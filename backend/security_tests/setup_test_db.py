"""
setup_test_db.py — Create and initialize isolated ragstudio_test database.
"""

import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
import os
import sys

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "8887")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASS = os.getenv("PG_PASS", "")

def ensure_test_database():
    conn = psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASS,
        dbname="postgres",
    )
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM pg_database WHERE datname = 'ragstudio_test'")
    exists = cur.fetchone()
    if not exists:
        cur.execute("CREATE DATABASE ragstudio_test")
        print("[OK] Created test database: ragstudio_test")
    else:
        print("[INFO] ragstudio_test database already exists.")
    cur.close()
    conn.close()

if __name__ == "__main__":
    ensure_test_database()
