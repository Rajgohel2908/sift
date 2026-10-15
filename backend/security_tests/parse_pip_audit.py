import json

with open('security_tests/evidence/pip_audit_report.json', 'r', encoding='utf-8') as f:
    text = f.read()

idx = text.find('{')
if idx != -1:
    data = json.loads(text[idx:])
    deps = data.get('dependencies', [])
    vulns = []
    for d in deps:
        if d.get('vulns'):
            vulns.append((d['name'], d['version'], d['vulns']))
    print(f"Total vulnerable packages: {len(vulns)}")
    for name, ver, vlist in vulns:
        ids = [v.get('id') for v in vlist]
        print(f"{name} ({ver}): {', '.join(ids)}")
else:
    print("No JSON found")
