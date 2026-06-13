"""CtP Portal: the tool rename + credit-stop status carried into all reports.

Checks:
  * credit_stop_status() maps every case correctly,
  * hold_compare() enriches every customer with credit_stop / credit_stop_key,
  * the dashboard build carries it onto the reduced offender + unapplied rows,
  * the on-screen results + dashboard pages render a "Credit stop" column,
  * the Excel report has a "Credit stop status" column on the customer and
    invoice sheets,
  * the tool now reads "CtP Portal" (not "Monitoring") in the UI.
"""
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from testutil import smtp_guard  # noqa: E402
smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.services import ctp_rules, ctp_dashboard, xlsx_report  # noqa: E402
from app.tools import ongoing_ctp as ctp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "ar_breakdown_sample.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
client = TestClient(app)
ctp.MASTER_PATH.unlink(missing_ok=True)

# --- 1. credit_stop_status() mapping -----------------------------------------
assert ctp_rules.credit_stop_status("stop", True)[0] == "stopped"
assert ctp_rules.credit_stop_status("ok", True)[0] == "stopped"      # held wins
assert ctp_rules.credit_stop_status("stop", False) == ("stop", "Stop credit (due)")
assert ctp_rules.credit_stop_status("hold", False)[0] == "hold"
assert ctp_rules.credit_stop_status("watch", False)[0] == "watch"
assert ctp_rules.credit_stop_status("ok", False) == ("ok", "Active — no stop")
print("ok: credit_stop_status() maps held/stop/hold/watch/ok correctly")

# --- 2. tool rename ----------------------------------------------------------
r = client.get("/tools/ongoing-ctp-monitoring")
assert r.status_code == 200
assert "CtP Portal" in r.text, "renamed title missing"
assert "<title>CtP Portal" in r.text
print("ok: tool renamed to 'CtP Portal' (slug/URL unchanged)")

# --- 3. analyse, then check enrichment + reports -----------------------------
with open(SAMPLE, "rb") as fh:
    r = client.post("/tools/ongoing-ctp-monitoring/analyze",
                    data={"as_of": "2026-06-09", "default_rank": "60"},
                    files={"file": ("ar_breakdown_sample.xlsx", fh, XLSX)},
                    follow_redirects=False)
token = re.search(r"results/([0-9a-f]+)/dashboard", r.headers["location"]).group(1)

result = ctp.load_result(token)
hold_cmp = ctp.hold_compare(result["customers"])
for c in result["customers"]:
    assert c.get("credit_stop"), f"{c['key']} missing credit_stop label"
    assert c.get("credit_stop_key") in ctp_rules.CREDIT_STOP_LABELS, c.get("credit_stop_key")
print(f"ok: hold_compare enriched all {len(result['customers'])} customers")

dash = ctp_dashboard.build(result)
for row in dash["over60_customers"] + dash["over90_customers"] + dash["unapplied"]["customers"]:
    assert "credit_stop_key" in row, "offender/unapplied row missing credit_stop_key"
print("ok: dashboard offender + unapplied rows carry the credit-stop status")

# --- 4. on-screen reports show a Credit stop column --------------------------
r = client.get(f"/tools/ongoing-ctp-monitoring/results/{token}")
assert "Credit stop status" in r.text, "results customer table missing column"
assert r.text.count("Credit stop") >= 2, "invoice table credit-stop column missing"
r = client.get(f"/tools/ongoing-ctp-monitoring/results/{token}/dashboard")
assert "Credit stop" in r.text, "dashboard missing credit-stop column"
print("ok: results + dashboard pages render the Credit stop column")

# --- 5. Excel report has the column on customer + invoice sheets -------------
out = ROOT / "data" / "outputs" / "test_credit_stop_report.xlsx"
out.parent.mkdir(parents=True, exist_ok=True)
xlsx_report.build_ctp_report(out, result, hold_cmp)
cust = pd.read_excel(out, sheet_name="Customer risk")
inv = pd.read_excel(out, sheet_name="Controls by invoice")
assert "Credit stop status" in cust.columns, cust.columns.tolist()
assert "Credit stop status" in inv.columns, inv.columns.tolist()
assert cust["Credit stop status"].astype(str).str.len().gt(0).all(), "blank credit-stop cells"
out.unlink(missing_ok=True)
print("ok: Excel report carries 'Credit stop status' on customer + invoice sheets")

print("\nALL CtP CREDIT-STOP / RENAME TESTS PASSED")
