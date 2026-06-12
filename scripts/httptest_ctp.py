"""Exercise the Ongoing CtP Monitoring web flow via FastAPI TestClient."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from testutil import smtp_guard
smtp_guard()

from app.main import app
from app.services import holds

from app.tools import ongoing_ctp as ctp

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "ar_breakdown_sample.xlsx"
MASTER = ROOT / "samples" / "ar_master_sample.xlsx"
BANK = ROOT / "samples" / "bank_statement_sample.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
client = TestClient(app)

# Start without a master so the register-based behavior is tested first.
ctp.MASTER_PATH.unlink(missing_ok=True)


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# Upload page
r = client.get("/tools/ongoing-ctp-monitoring")
check("upload page 200", r.status_code == 200)
check("dunning shown as automated", "System (e-dunning)" in r.text)

# Analyze -> redirects to the dashboard
with open(SAMPLE, "rb") as fh:
    r = client.post("/tools/ongoing-ctp-monitoring/analyze",
                    data={"as_of": "2026-06-09", "default_rank": "60"},
                    files={"file": ("ar_breakdown_sample.xlsx", fh, XLSX)},
                    follow_redirects=False)
check("analyze -> redirect", r.status_code == 303)
loc = r.headers["location"]
token = re.search(r"results/([0-9a-f]+)/dashboard", loc).group(1)
print("   token:", token)

# Dashboard
r = client.get(f"/tools/ongoing-ctp-monitoring/results/{token}/dashboard")
check("dashboard 200", r.status_code == 200)
check("KPI: AR > 60 days", "AR &gt; 60 days" in r.text or "AR > 60 days" in r.text)
check("KPI: unapplied cash", "Unapplied cash" in r.text)
check("ageing section", "Ageing by document date" in r.text)
check("going on hold section", "Projected to go on credit hold" in r.text)
check("below-threshold form", 'name="threshold"' in r.text)
# v5.2: the bank-match section moved to the Bank Statements tool.
check("bank match section removed", "appearing on the bank statement" not in r.text)
check("customer names shown", "ACME LOGISTICS" in r.text)

# Threshold update changes the list
r1 = client.get(f"/tools/ongoing-ctp-monitoring/results/{token}/dashboard?threshold=100000")
r2 = client.get(f"/tools/ongoing-ctp-monitoring/results/{token}/dashboard?threshold=2000000")
n1 = r1.text.count("<tr>")
n2 = r2.text.count("<tr>")
check("threshold update changes listing", n2 >= n1)

# v5.2: bank-statement AR matching moved to the Bank Statements tool — the
# CtP /bank route and dashboard section were removed.

# Full results page (control summary, hold exceptions, names, automated dunning)
r = client.get(f"/tools/ongoing-ctp-monitoring/results/{token}")
check("results 200", r.status_code == 200)
check("results show counts", "8 invoices, 4 customers" in r.text)
check("control total matches", "Control total check" in r.text and "✓" in r.text)
check("summary per control type", "Summary per control type" in r.text)
check("auto dunning rows marked", "pill-auto" in r.text)
check("no manual letter actions", "Send 'Overdue Account Letter'" not in r.text)
check("hold exceptions section", "Credit-hold exceptions" in r.text)
report = re.search(r'/download/(CtP_controls_[0-9a-f]+\.xlsx)', r.text).group(1)

# Hold toggle + register
r = client.post(f"/tools/ongoing-ctp-monitoring/results/{token}/hold",
                data={"account": "1004000001", "name": "ACME LOGISTICS", "op": "hold"},
                follow_redirects=True)
check("hold toggle 200", r.status_code == 200)
check("register reflects ACME", holds.is_held("1004000001"))
r = client.get("/tools/ongoing-ctp-monitoring/holds")
check("register lists ACME", "ACME LOGISTICS" in r.text)

# Excel + email
rd = client.get(f"/download/{report}")
check("xlsx downloads", rd.status_code == 200 and rd.content[:2] == b"PK")
r = client.post("/tools/ongoing-ctp-monitoring/email",
                data={"report": report, "recipient": "finance@example.com"})
check("email page 200", r.status_code == 200 and ".eml" in r.text)

# ---- Master file flow -------------------------------------------------------
with open(MASTER, "rb") as fh:
    r = client.post("/tools/ongoing-ctp-monitoring/master",
                    files={"file": ("ar_master_sample.xlsx", fh, XLSX)},
                    follow_redirects=True)
check("master upload 200", r.status_code == 200)
check("upload page shows master on record", "Master file on record" in r.text)

r = client.get("/tools/ongoing-ctp-monitoring/master")
check("master view page 200", r.status_code == 200)
check("master lists Epsilon + segments", "EPSILON SA" in r.text and "Enterprise" in r.text)

# Re-analyze with the master applied
with open(SAMPLE, "rb") as fh:
    r = client.post("/tools/ongoing-ctp-monitoring/analyze",
                    data={"as_of": "2026-06-09", "default_rank": "60"},
                    files={"file": ("ar_breakdown_sample.xlsx", fh, XLSX)},
                    follow_redirects=False)
token2 = re.search(r"results/([0-9a-f]+)/dashboard", r.headers["location"]).group(1)
r = client.get(f"/tools/ongoing-ctp-monitoring/results/{token2}")
check("results show reconciliation section", "Master file vs transaction file" in r.text)
check("GAMMA mismatch flagged (-110,000)", "-110,000" in r.text)
check("EPSILON only-in-master flagged", "EPSILON SA" in r.text)
check("segments applied (Enterprise plan visible)", "Enterprise" in r.text)
check("hold source = master file shown", "per master file" in r.text)
check("BETA on Field plan (call at 24d)", "Call customer &amp; inform Sales Executive" in r.text)

# Cleanup
holds.release("1004000001", "ACME LOGISTICS")
check("cleanup: hold released", not holds.is_held("1004000001"))
ctp.MASTER_PATH.unlink(missing_ok=True)
for p in ctp.STORE_DIR.glob(f"{token2}*"):
    p.unlink()

print("\nALL CtP HTTP CHECKS PASSED")
