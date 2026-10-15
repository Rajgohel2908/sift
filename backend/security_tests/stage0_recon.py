"""
stage0_recon.py — Automated Stage 0 Reconnaissance & Static Analysis.
"""

import os
import sys
import json
import socket
import subprocess
import urllib.request
import psycopg2

EVIDENCE_DIR = os.path.join(os.path.dirname(__file__), "evidence")
os.makedirs(EVIDENCE_DIR, exist_ok=True)

def step(msg):
    print(f"\n[RECON] === {msg} ===")

def recon_openapi():
    step("1. Inspecting OpenAPI Schema")
    try:
        req = urllib.request.urlopen("http://127.0.0.1:8001/openapi.json")
        data = json.loads(req.read().decode("utf-8"))
        out_path = os.path.join(EVIDENCE_DIR, "openapi.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"Saved OpenAPI schema to {out_path}")

        endpoints = []
        for path, methods in data.get("paths", {}).items():
            for m, spec in methods.items():
                endpoints.append({
                    "method": m.upper(),
                    "path": path,
                    "summary": spec.get("summary", ""),
                    "params": [p.get("name") for p in spec.get("parameters", [])],
                })
        print(f"Total endpoints discovered: {len(endpoints)}")
        for ep in endpoints:
            print(f"  {ep['method']:6} {ep['path']:36} | {ep['summary']}")
        return endpoints
    except Exception as e:
        print(f"Failed to fetch OpenAPI: {e}")
        return []

def recon_db_privileges():
    step("2. Checking Database Bind & User Privileges")
    res = {}
    try:
        db_url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or "postgresql://postgres@localhost:8887/ragstudio_test"
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        cur.execute("SELECT usename, usesuper, usecreatedb, valuntil FROM pg_user WHERE usename = 'postgres';")
        user_info = cur.fetchone()
        res["user"] = {
            "username": user_info[0],
            "is_superuser": user_info[1],
            "create_db": user_info[2],
            "expiry": str(user_info[3]),
        }
        print(f"DB User: {user_info[0]} | Superuser: {user_info[1]} | CanCreateDB: {user_info[2]}")

        cur.execute("SHOW listen_addresses;")
        listen_addr = cur.fetchone()[0]
        res["listen_addresses"] = listen_addr
        print(f"PostgreSQL listen_addresses: {listen_addr}")

        cur.execute("SELECT extname, extversion FROM pg_extension;")
        extensions = cur.fetchall()
        res["extensions"] = {name: ver for name, ver in extensions}
        print(f"Installed PostgreSQL extensions: {res['extensions']}")

        cur.close()
        conn.close()

        with open(os.path.join(EVIDENCE_DIR, "db_recon.json"), "w", encoding="utf-8") as f:
            json.dump(res, f, indent=2)
    except Exception as e:
        print(f"DB Recon failed: {e}")

def recon_bandit():
    step("3. Running Bandit Static Security Analysis")
    try:
        cmd = ["bandit", "-r", "backend", "-f", "json", "-o", os.path.join(EVIDENCE_DIR, "bandit_report.json")]
        # Also run bandit on current directory
        p = subprocess.run(["bandit", "-r", ".", "-x", "./security_tests,./venv,./.pytest_cache", "-f", "json"],
                           cwd=os.path.dirname(os.path.dirname(__file__)),
                           capture_output=True, text=True)
        report_data = {}
        if p.stdout.strip():
            try:
                report_data = json.loads(p.stdout)
            except Exception:
                pass
        
        bandit_out = os.path.join(EVIDENCE_DIR, "bandit_report.json")
        with open(bandit_out, "w", encoding="utf-8") as f:
            f.write(p.stdout if p.stdout else p.stderr)
        
        results = report_data.get("results", [])
        print(f"Bandit scan complete. Findings count: {len(results)}")
        for r in results:
            print(f"  [{r.get('issue_severity')}] {r.get('test_name')} in {r.get('filename')}:{r.get('line_number')} - {r.get('issue_text')}")
    except Exception as e:
        print(f"Bandit run error: {e}")

def recon_pip_audit():
    step("4. Running pip-audit Dependency Vulnerability Scan")
    try:
        p = subprocess.run(["pip-audit", "-f", "json"],
                           cwd=os.path.dirname(os.path.dirname(__file__)),
                           capture_output=True, text=True)
        audit_out = os.path.join(EVIDENCE_DIR, "pip_audit_report.json")
        with open(audit_out, "w", encoding="utf-8") as f:
            f.write(p.stdout if p.stdout else p.stderr)
        
        try:
            data = json.loads(p.stdout)
            vulns = data.get("dependencies", [])
            vuln_count = sum(len(d.get("vulns", [])) for d in vulns)
            print(f"pip-audit scan complete. Vulnerabilities found: {vuln_count}")
            for d in vulns:
                for v in d.get("vulns", []):
                    print(f"  Vulnerability in {d.get('name')} {d.get('version')}: {v.get('id')} - {v.get('description', '')[:80]}...")
        except Exception:
            print(f"pip-audit output: {p.stdout[:300]}")
    except Exception as e:
        print(f"pip-audit error: {e}")

def recon_secrets():
    step("5. Scanning for Exposed Secrets in Git & Configs")
    try:
        proj_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        p = subprocess.run(["git", "log", "-p", "-n", "50"],
                           cwd=proj_root,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        diff_text = p.stdout or ""
        findings = []
        for line in diff_text.splitlines():
            ll = line.lower()
            if any(k in ll for k in ["api_key", "password", "raj1818", "secret", "private_key"]):
                if line.startswith("+") and not line.startswith("+++"):
                    findings.append(line.strip())
        
        secrets_out = os.path.join(EVIDENCE_DIR, "secrets_scan.txt")
        with open(secrets_out, "w", encoding="utf-8") as f:
            f.write("\n".join(findings))
        print(f"Secrets scan complete. Potentially sensitive added lines in recent git history: {len(findings)}")
        for f in findings[:10]:
            print(f"  {f}")
    except Exception as e:
        print(f"Secrets scan error: {e}")

if __name__ == "__main__":
    recon_openapi()
    recon_db_privileges()
    recon_bandit()
    recon_pip_audit()
    recon_secrets()
