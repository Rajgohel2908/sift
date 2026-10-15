import json

with open("security_tests/evidence/bandit_report.json", "r", encoding="utf-8") as f:
    text = f.read()
    idx = text.find("{")
    data = json.loads(text[idx:])

results = data.get("results", [])
app_findings = [r for r in results if not r.get("filename", "").startswith(".\\tests")]
print(f"Total non-test Bandit findings: {len(app_findings)}")
for r in app_findings:
    sev = r.get("issue_severity")
    conf = r.get("issue_confidence")
    fname = r.get("filename")
    line = r.get("line_number")
    test = r.get("test_name")
    desc = r.get("issue_text")
    print(f"[{sev}/{conf}] {test} in {fname}:{line} -> {desc}")
