"""v11.20 — Revenue Analysis refinements: top-60 collapsible active
customers, credit-stop matching that survives the register's truncated
names, a like-for-like KPI chart, the month-landing projection, and lane
RPK against the prior three months.

Isolated to a temp data dir; the real data/ is never touched.
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.tools import revenue  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="v1120_"))
config.CONFIG_PATH = _tmp / "config.json"
config.invalidate_config_cache()
revenue.STORE_PATH = _tmp / "revenue" / "store.json"
revenue.UPLOAD_DIR = _tmp / "revenue" / "uploads"

_fail = 0


def check(label, cond):
    global _fail
    print(("[OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        _fail += 1


HDR = ["Billing Period", "Air waybill", "Bill To Account",
       "Bill To Account Name", "Shipment Date", "Invoice Date",
       "Billed Weight (Kilos)", "LCU Weight Charge", "LCU Fuel Surcharges",
       "LCU Other Charges", "LCU Discount", "LCU Imp/Exp Duties & Taxes",
       "LCU Taxes to Applicable Charges", "LCU Total",
       "Service Type", "Orgn", "Dest"]


def row(period, awb, acct, name, inv, kg, w, svc="OB", orgn="DLA",
        dest="PAR"):
    return {"Billing Period": period, "Air waybill": awb,
            "Bill To Account": acct, "Bill To Account Name": name,
            "Shipment Date": datetime.fromisoformat(inv),
            "Invoice Date": datetime.fromisoformat(inv),
            "Billed Weight (Kilos)": kg, "LCU Weight Charge": w,
            "LCU Fuel Surcharges": 0, "LCU Other Charges": 0,
            "LCU Discount": 0, "LCU Imp/Exp Duties & Taxes": 0,
            "LCU Taxes to Applicable Charges": 0, "LCU Total": w,
            "Service Type": svc, "Orgn": orgn, "Dest": dest}


def seed(name, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HDR)
    for r in rows:
        ws.append([r[h] for h in HDR])
    wb.save(_tmp / name)
    rec, cust = revenue.parse_file(_tmp / name, name)
    revenue.store_period(rec, cust)
    return rec


# === 1. Credit-stop matching against a TRUNCATED register =================
# The register holds the AR name, which is frequently cut short. The finance
# lead spotted a top trader missed for exactly this reason.
STOP = {"SOCIETE ANONYME DES BOISSONS DU", "CHOCOCAM", "STBC SARL",
        "ETS FAKA"}
check("a truncated register entry still matches, flagged as LIKELY",
      revenue.match_stopped("SOCIETE ANONYME DES BOISSONS DU CAMEROUN", STOP)
      == ("likely", "SOCIETE ANONYME DES BOISSONS DU"))
check("an exact name is reported as exact",
      revenue.match_stopped("CHOCOCAM", STOP) == ("exact", "CHOCOCAM"))
check("a merely SIMILAR name is NOT flagged (RGSTTC is not STBC)",
      revenue.match_stopped("RGSTTC SARL", STOP)[0] == "")
check("a short shared prefix cannot flag a customer (ETS …)",
      revenue.match_stopped("ETS FAKA AKWA", STOP)[0] == "")
check("an unrelated name is clean",
      revenue.match_stopped("CENTRE PASTEUR", STOP)[0] == "")
check("blank input is clean", revenue.match_stopped("", STOP)[0] == "")

# === 2. Like-for-like KPI series ==========================================
# May/June/July complete, August running. Each month bills on its 1st-3rd;
# August has reached only its first billing day.
for p, day1, day2 in (("2026-05", "2026-05-04", "2026-05-05"),
                      ("2026-06", "2026-06-01", "2026-06-02"),
                      ("2026-07", "2026-07-01", "2026-07-02")):
    seed(f"{p}.xlsx",
         [row(p, f"{p[-2:]}1{i:07d}", "A1", "ALPHA LTD", day1, 10, 100000)
          for i in range(3)] +
         [row(p, f"{p[-2:]}2{i:07d}", "A1", "ALPHA LTD", day2, 10, 900000)
          for i in range(3)])
seed("2026-08.xlsx",
     [row("2026-08", f"081{i:07d}", "A1", "ALPHA LTD", "2026-08-03", 10,
          50000) for i in range(3)])

NOW = datetime(2026, 8, 13)
view = revenue.dashboard(now=NOW)
check("the chart is cut to the running month's billable days",
      view["lfl_days"] == 1.0 and not view["lfl_partial"])
g = {x["key"]: x for x in view["graphs"]}["rev_per_day"]
pts = {c["label"]: c["v"] for c in g["coords"]}
check("each complete month contributes only its FIRST day, not the month",
      pts["May 2026"] == 300000.0 and pts["June 2026"] == 300000.0
      and pts["July 2026"] == 300000.0)
check("the running month is the last point and is flagged in progress",
      g["coords"][-1]["label"] == "August 2026"
      and g["coords"][-1]["ongoing"] and g["coords"][-1]["v"] == 150000.0)
check("without the clipping the months would have looked 4x bigger",
      view["months"][0]["net"] == 3000000.0)

# === 3. The landing projection ============================================
L = view["landing"]
check("a landing column is produced for the running month",
      L and L["label"] == "August 2026 landing")
check("it carries the running rate over the typical completed-month days",
      L["elapsed_days"] == 1.0 and L["typical_days"] == 2.0
      and L["net"] == 300000.0)
check("shipments and kilos are projected on the same rate",
      L["shipments"] == 6.0 and L["kilos"] == 60.0)
check("the per-day rate itself is unchanged by the projection",
      L["rev_per_day"] == view["ongoing"]["rev_per_day"])

# === 4. Lane RPK vs the prior three months ================================
seed("lanes-05.xlsx", [
    row("2026-05", "5900000001", "A1", "ALPHA LTD", "2026-05-04", 10, 100000,
        orgn="DLA", dest="BRU"),
    row("2026-05", "5900000002", "A1", "ALPHA LTD", "2026-05-04", 10, 100000,
        orgn="DLA", dest="LOS"),
    row("2026-05", "5900000003", "A1", "ALPHA LTD", "2026-05-04", 10, 100000,
        orgn="DLA", dest="ACC")])
seed("lanes-06.xlsx", [
    # BRU doubles its RPK, LOS holds, ACC halves, GVA is brand new
    row("2026-06", "6900000001", "A1", "ALPHA LTD", "2026-06-01", 10, 200000,
        orgn="DLA", dest="BRU"),
    row("2026-06", "6900000002", "A1", "ALPHA LTD", "2026-06-01", 10, 100000,
        orgn="DLA", dest="LOS"),
    row("2026-06", "6900000003", "A1", "ALPHA LTD", "2026-06-01", 10, 50000,
        orgn="DLA", dest="ACC"),
    row("2026-06", "6900000004", "A1", "ALPHA LTD", "2026-06-01", 10, 300000,
        orgn="DLA", dest="GVA")])
lanes = revenue.lanes_for("2026-06")
by_lane = {r["lane"]: r for r in lanes["outbound"]}
check("the lane RPK is net revenue over kilos",
      by_lane["DLA → BRU"]["rpk"] == 20000.0)
check("a lane whose RPK rose is marked up",
      by_lane["DLA → BRU"]["trend"] == "up"
      and round(by_lane["DLA → BRU"]["delta_pct"]) == 100)
check("a lane whose RPK held is marked flat",
      by_lane["DLA → LOS"]["trend"] == "flat"
      and by_lane["DLA → LOS"]["delta_pct"] == 0.0)
check("a lane whose RPK fell is marked down",
      by_lane["DLA → ACC"]["trend"] == "down"
      and round(by_lane["DLA → ACC"]["delta_pct"]) == -50)
check("a lane with no history is marked new, never compared to nothing",
      by_lane["DLA → GVA"]["trend"] == "new"
      and by_lane["DLA → GVA"]["delta_pct"] is None)
check("the prior months used are disclosed",
      lanes["prior_months"] == ["2026-05"])
check("enough lanes are kept that a lower-ranked lane is not lost",
      revenue.MAX_LANES >= 400)

# === 5. Active customers reach 60 =========================================
check("the default depth is 60 traders", revenue.active_customers.__defaults__[0]
      == 60)

# === 6. Template wiring ===================================================
TPL = (ROOT / "app" / "templates" / "revenue" / "index.html").read_text(
    encoding="utf-8")
check("the lane column is labelled RPK",
      "RPK (EUR)" in TPL and "Rev / kg (EUR)" not in TPL)
check("the trend renders as coloured arrows",
      "▲" in TPL and "▼" in TPL and "▶" in TPL)
check("green up, red down, amber flat",
      "#1b7f4b" in TPL and "#bd3727" in TPL and "#b06f00" in TPL)
check("the active list rolls up to 20 and reveals the rest",
      "act-extra" in TPL and "Reveal all" in TPL and "Roll up" in TPL)
check("a likely stop match is shown as a question, not a fact",
      "STOP\n                CREDIT?" in TPL or "CREDIT?" in TPL)
check("the landing column is marked as a projection",
      "landing" in TPL and "A projection, not a result" in TPL)
check("the chart says it is like for like",
      "like for like" in TPL)

# === 7. The page renders ==================================================
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

client = TestClient(main.app)
r = client.get("/tools/revenue-analysis")
check("the dashboard renders with every panel",
      r.status_code == 200 and "RPK (EUR)" in r.text
      and "landing" in r.text and "Reveal all" in r.text)

if _fail:
    print(f"\n{_fail} CHECK(S) FAILED")
    sys.exit(1)
print("\nALL v11.20 TESTS PASSED")
