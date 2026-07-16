"""CtP analysis sections: Legal recovery, Critical customers, Duty accounts,
plus segment->plan resolution and the master "Critical Customers" column.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from testutil import smtp_guard  # noqa: E402
smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.services import ar_master, ctp_dashboard, ctp_rules  # noqa: E402
from app.tools import ongoing_ctp as ctp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "samples" / "ar_master_sample.xlsx"
TXN = ROOT / "samples" / "ar_breakdown_features_sample.xlsx"
AS_OF = date(2026, 6, 13)


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# --- 1. segment -> CtP plan resolution (richer sales channels) --------------
check("Field Sales -> field plan", ctp_rules.resolve_plan("Field Sales - Territory") == ctp_rules.PLAN_FIELD)
check("Key Account -> enterprise", ctp_rules.resolve_plan("Key Account") == ctp_rules.PLAN_ENTERPRISE)
check("Global Customer -> enterprise", ctp_rules.resolve_plan("Global Customer Solutions") == ctp_rules.PLAN_ENTERPRISE)
check("Inside Sales -> telesales", ctp_rules.resolve_plan("TAS Inside Sales") == ctp_rules.PLAN_TELESALES)
check("New Customer -> new plan", ctp_rules.resolve_plan("New Customer") == ctp_rules.PLAN_NEW)

# --- 2. master "Critical Customers" column ----------------------------------
master = ar_master.parse_master(MASTER)
check("master has critical column", master["stats"]["has_critical_column"])
check("ACME critical", master["customers"]["1004000001"]["critical"])
check("GAMMA critical", master["customers"]["1004000003"]["critical"])
check("BETA not critical", not master["customers"]["1004000002"]["critical"])
# Credit hold is read from the "Account stop/open" column (X = on hold).
check("hold column = Account stop/open", master["mapping"].get("hold") == "Account stop/open")
check("GAMMA on hold (X)", master["customers"]["1004000003"]["on_hold"])
check("ACME not on hold (blank)", not master["customers"]["1004000001"]["on_hold"])

# --- 3. analyse the feature transaction file --------------------------------
result = ctp.analyze(TXN, as_of=AS_OF, default_rank=60, master=master)
ctp.hold_compare(result["customers"])
by_key = {c["key"]: c for c in result["customers"]}
check("GAMMA flagged legal (clerk Legal)", by_key["1004000003"]["legal"])
check("GAMMA critical from master", by_key["1004000003"]["critical"])
check("GAMMA currently on hold (master)", by_key["1004000003"]["currently_held"])
check("ACME duty invoice flagged", any(i["duty"] for i in result["invoices"] if i["account"] == "1004000001"))
check("DELTA duty via reference flagged", any(i["duty"] for i in result["invoices"] if i["account"] == "1004000004"))

dash = ctp_dashboard.build(result)

# --- 4. Legal recovery section ----------------------------------------------
legal = dash["legal"]
check("legal section lists GAMMA", any(c["key"] == "1004000003" for c in legal["customers"]))
g = next(c for c in legal["customers"] if c["key"] == "1004000003")
check("legal GAMMA shows critical + hold", g["critical"] and g["currently_held"])
check("legal GAMMA over-90 populated", g["over90"] > 0)

# --- 5. Critical customer analysis ------------------------------------------
crit = dash["critical"]
crit_keys = {c["key"] for c in crit["customers"]}
check("critical section = ACME + GAMMA", crit_keys == {"1004000001", "1004000003"})
gc = next(c for c in crit["customers"] if c["key"] == "1004000003")
check("critical row has %over60 and days-to-stop",
      "pct_over60" in gc and "days_to_stop" in gc and gc["plan_label"])

# --- 6. Duty account analysis -----------------------------------------------
duty = dash["duty"]
check("two duty invoices found", duty["count"] == 2)
check("duty total > 0", duty["total"] > 0)
docs = {i["invoice_no"] for i in duty["invoices"]}
check("duty includes the D0 document id", "D0000101" in docs)

# --- 7. the dashboard page renders the new sections -------------------------
token = "fea70011"
ctp.save_result(token, result)
try:
    client = TestClient(main.app)
    page = client.get(f"/tools/ongoing-ctp-monitoring/results/{token}/dashboard")
    assert page.status_code == 200, page.status_code
    for needle in ["Sent to third-party legal", "Critical customer analysis",
                   "Duty account analysis", "GAMMA", "D0000101",
                   "Current Credit Hold status", "Critical",
                   "Payments identification"]:
        check(f"dashboard renders: {needle}", needle in page.text)
    # the two mandatory columns also on the results (controls) report
    rpage = client.get(f"/tools/ongoing-ctp-monitoring/results/{token}")
    assert rpage.status_code == 200, rpage.status_code
    check("results report shows On credit hold + Critical",
          "Current Credit Hold status" in rpage.text and "Critical" in rpage.text)
finally:
    (ctp.STORE_DIR / f"{token}.json").unlink(missing_ok=True)

# --- 8. Excel report carries both columns on customer + invoice sheets ------
import pandas as pd  # noqa: E402
hold_cmp = ctp.hold_compare(result["customers"])
out = ROOT / "data" / "outputs" / "_test_sections_report.xlsx"
out.parent.mkdir(parents=True, exist_ok=True)
from app.services import xlsx_report  # noqa: E402
xlsx_report.build_ctp_report(out, result, hold_cmp)
cust = pd.read_excel(out, sheet_name="Customer risk")
inv = pd.read_excel(out, sheet_name="Controls by invoice")
check("Excel customer sheet has both columns",
      "Current Credit Hold status" in cust.columns and "Critical customer" in cust.columns)
check("Excel invoice sheet has both columns",
      "Current Credit Hold status" in inv.columns and "Critical customer" in inv.columns)
out.unlink(missing_ok=True)

# --- 9. held-customers-paying matches carry hold + critical -----------------
from app.services import bank_statement  # noqa: E402
gamma = next(c for c in result["customers"] if c["key"] == "1004000003")
pseudo = {"lines": [{"description": gamma["customer"], "amount": 1.0,
                     "credit": 1.0, "debit": 0.0, "date": "2026-06-10", "bank": "X"}]}
matches = bank_statement.match_customers(result["customers"], pseudo)
check("bank match carries hold + critical flags",
      matches and "critical" in matches[0] and "currently_held" in matches[0])
check("bank match discloses the bank it was seen on (once)",
      matches[0].get("bank") == "X")

print("\nALL CtP SECTION TESTS PASSED")
