"""v11.19 — Revenue Analysis upgrades: EUR display, the KPI evolution graph
(solid for complete months, dotted for the running one), the fair
same-elapsed-days comparison, top-10 outbound/inbound lanes, and the active
top-20 traders with credit-stop flags.

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
from app.services.ctp_rules import EUR_RATES  # noqa: E402
from app.tools import revenue  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="v1119_"))
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


def row(period, awb, acct, name, ship, inv, kg, w, svc="OB",
        orgn="DLA", dest="PAR"):
    return {"Billing Period": period, "Air waybill": awb,
            "Bill To Account": acct, "Bill To Account Name": name,
            "Shipment Date": datetime.fromisoformat(ship),
            "Invoice Date": datetime.fromisoformat(inv),
            "Billed Weight (Kilos)": kg, "LCU Weight Charge": w,
            "LCU Fuel Surcharges": 0, "LCU Other Charges": 0,
            "LCU Discount": 0, "LCU Imp/Exp Duties & Taxes": 0,
            "LCU Taxes to Applicable Charges": 0, "LCU Total": w,
            "Service Type": svc, "Orgn": orgn, "Dest": dest}


def build(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HDR)
    for r in rows:
        ws.append([r[h] for h in HDR])
    wb.save(path)


def seed(name, rows):
    build(_tmp / name, rows)
    rec, cust = revenue.parse_file(_tmp / name, name)
    revenue.store_period(rec, cust)
    return rec


# --- three complete months + an ongoing one --------------------------------
# May 2026: 4th = Monday, 5th = Tuesday. June: 1st = Monday. July: 1st = Wed,
# 2nd = Thu, 3rd = Fri. August: 3rd = Monday.
seed("may.xlsx", [
    row("2026-05", f"50000000{i:02d}", "A1", "ALPHA LTD", "2026-05-04",
        "2026-05-04", 10, 100000) for i in range(3)] + [
    row("2026-05", f"51000000{i:02d}", "B2", "BETA SARL", "2026-05-05",
        "2026-05-05", 10, 100000, svc="IB", orgn="LOS", dest="DLA")
    for i in range(3)])
seed("june.xlsx", [
    row("2026-06", f"60000000{i:02d}", "A1", "ALPHA LTD", "2026-06-01",
        "2026-06-01", 10, 120000) for i in range(3)] + [
    row("2026-06", f"61000000{i:02d}", "C3", "GAMMA & CO", "2026-06-01",
        "2026-06-01", 5, 60000, svc="IB", orgn="PAR", dest="DLA")
    for i in range(3)])
JULY = [
    # first billable day (Wed 01.07) — this is what the window must catch
    row("2026-07", f"70000000{i:02d}", "A1", "ALPHA LTD", "2026-07-01",
        "2026-07-01", 10, 150000) for i in range(3)] + [
    # second billable day (Thu 02.07) — must stay OUTSIDE a 1-day window
    row("2026-07", f"71000000{i:02d}", "A1", "ALPHA LTD", "2026-07-02",
        "2026-07-02", 10, 90000) for i in range(3)] + [
    row("2026-07", f"72000000{i:02d}", "B2", "BETA SARL", "2026-07-01",
        "2026-07-01", 20, 80000, svc="IB", orgn="LOS", dest="DLA")
    for i in range(3)]
seed("july.xlsx", JULY)
seed("aug.xlsx", [
    row("2026-08", f"80000000{i:02d}", "A1", "ALPHA LTD", "2026-08-03",
        "2026-08-03", 10, 100000) for i in range(3)])

NOW = datetime(2026, 8, 13)
view = revenue.dashboard(now=NOW)

# === 1. EUR ================================================================
check("the EUR rate rides along for the page",
      view["eur_rate"] == EUR_RATES["XAF"] == 655.957)

# === 2. The same-elapsed-days comparison ===================================
# August has ONE billable day (Mon 03.08). July's first billable day (Wed
# 01.07) carried 3×150,000 + 3×80,000 = 690,000 — day two's 270,000 must NOT
# be inside the window.
w = view["compare"]["window"]
check("the window walks the prior month to the same billable-day count",
      w is not None and w["days"] == 1.0 and w["through"] == "2026-07-01")
check("the window carries only the first day's revenue",
      w["net"] == 690000.0 and w["shipments"] == 6)
kpi = {k["key"]: k for k in view["kpis"]}
check("revenue/day compares like for like",
      round(kpi["rev_per_day"]["value"], 0) == 300000
      and round(kpi["rev_per_day"]["baseline"], 0) == 690000)
check("the delta follows the fair baseline",
      round(kpi["rev_per_day"]["delta_pct"], 1) ==
      round(100 * (300000 - 690000) / 690000, 1))
check("the page can say WHICH window it compared against",
      view["compare"]["prior_label"] == "July 2026")

# === 3. The KPI evolution graph ============================================
graphs = {g["key"]: g for g in view["graphs"]}
check("three KPI graphs, one per metric",
      set(graphs) == {"rev_per_day", "rev_per_shipment", "rev_per_kg"})
g = graphs["rev_per_day"]
check("every month is a point", len(g["coords"]) == 4)
check("complete months draw the SOLID line",
      g["solid_points"].count(",") == 3
      and not g["coords"][0]["ongoing"])
check("the running month closes the line DOTTED",
      g["dash_points"] and g["coords"][-1]["ongoing"])
check("dotted segment connects the last complete month to the running one",
      g["dash_points"].split()[0] == g["solid_points"].split()[-1])

# === 4. Lanes ==============================================================
lanes = view["lanes"]          # July (latest complete) by default
check("outbound and inbound are separated",
      lanes and "outbound" in lanes and "inbound" in lanes)
check("outbound lanes aggregate origin → destination",
      lanes["outbound"][0]["lane"] == "DLA → PAR"
      and lanes["outbound"][0]["net"] == 720000.0
      and lanes["outbound"][0]["shipments"] == 6)
check("inbound lanes likewise",
      lanes["inbound"][0]["lane"] == "LOS → DLA"
      and lanes["inbound"][0]["net"] == 240000.0)
# v11.20 renamed the field to RPK and added the prior-months trend
check("each lane carries its RPK",
      round(lanes["outbound"][0]["rpk"], 2) == round(720000 / 60, 2)
      and round(lanes["inbound"][0]["rpk"], 2) == round(240000 / 60, 2))

# === 5. Active customers ===================================================
act = revenue.active_customers(now=NOW)
check("the last three complete months frame the analysis",
      act["months"] == ["2026-05", "2026-06", "2026-07"]
      and act["current_period"] == "2026-08")
by_name = {r["key"]: r for r in act["rows"]}
alpha = by_name["ALPHA LTD"]
check("weight per month is disclosed per trader",
      alpha["months"] == {"2026-05": 30.0, "2026-06": 30.0, "2026-07": 60.0}
      and alpha["current"] == 30.0)
check("the average is over the three complete months",
      alpha["avg_kilos"] == 40.0)
check("a trader quiet this month shows zero current weight",
      by_name["BETA SARL"]["current"] == 0.0)
check("ranked by average weight, biggest first",
      act["rows"][0]["key"] == "ALPHA LTD")

# === 5b. Review fixes: axis, dedupe, empty window, stop-flag key ==========
# a) An AWB invoiced on TWO days inside the window counts ONCE.
build(_tmp / "dedupe.xlsx", [
    row("2026-04", "9000000001", "A1", "ALPHA LTD", "2026-04-01",
        "2026-04-01", 10, 100000),
    row("2026-04", "9000000002", "A1", "ALPHA LTD", "2026-04-01",
        "2026-04-01", 10, 100000),
    row("2026-04", "9000000003", "A1", "ALPHA LTD", "2026-04-01",
        "2026-04-01", 10, 100000),
    # the SAME airwaybill rebilled the next day — one shipment, not two
    row("2026-04", "9000000001", "A1", "ALPHA LTD", "2026-04-01",
        "2026-04-02", 1, 20000),
    row("2026-04", "9000000004", "A1", "ALPHA LTD", "2026-04-02",
        "2026-04-02", 5, 50000),
    row("2026-04", "9000000005", "A1", "ALPHA LTD", "2026-04-02",
        "2026-04-02", 5, 50000)])
rec_d, _c = revenue.parse_file(_tmp / "dedupe.xlsx", "dedupe.xlsx")
union_d = revenue._union_ship_days({"2026-04": rec_d})
win = revenue.same_days_window(rec_d, union_d, 2.0)
# 6 billing lines over 5 distinct airwaybills; net 300,000 + 120,000
check("a window counts a re-invoiced airwaybill once",
      win["shipments"] == 5 and win["net"] == 420000.0)
check("the per-day detail keeps the airwaybills for that dedupe",
      "awbs" in rec_d["daily"]["2026-04-01"])

# b) The billable-day axis is the INVOICE axis — the same one the revenue
#    sits on, so a batch-billing month cannot compare two different spans.
build(_tmp / "batch.xlsx", [
    # shipped across the week, ALL invoiced on the Friday
    row("2026-04", f"9100000{i:03d}", "A1", "ALPHA LTD",
        f"2026-04-0{1 + i % 3}", "2026-04-03", 10, 100000)
    for i in range(6)])
rec_b, _c = revenue.parse_file(_tmp / "batch.xlsx", "batch.xlsx")
union_b = revenue._union_ship_days({"2026-04": rec_b})
check("only days that actually BILLED count as billable",
      revenue.billable_days("2026-04", union_b) == 1.0)
w_b = revenue.same_days_window(rec_b, union_b, 1.0)
check("the window lands on a day that carries revenue, never an empty one",
      w_b["net"] == 600000.0 and w_b["through"] == "2026-04-03")

# c) An empty window must fall back, not present zeros as a baseline
empty = revenue.same_days_window(
    {"period": "2026-04", "daily": {"2026-04-20": {"net": 5.0, "kilos": 1.0,
                                                   "shipments": 1,
                                                   "awbs": ["x"]}}},
    {"2026-04-01": 9, "2026-04-20": 9}, 1.0)
check("a cutoff before the first billing day yields no revenue",
      empty["net"] == 0.0)

# d) The credit-stop flag reads the key CtP actually writes
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
check("stopped names are read from the CtP 'customer' key",
      'c.get("customer")' in MAIN and "stopped_names.discard" in MAIN)

# === 6. The template carries the new sections ==============================
TPL = (ROOT / "app" / "templates" / "revenue" / "index.html").read_text(
    encoding="utf-8")
check("amounts render through the EUR macro",
      "macro eur(" in TPL and "view.eur_rate" in TPL)
check("the graph draws solid and dotted polylines",
      'stroke-dasharray="6 5"' in TPL and "solid_points" in TPL)
check("the lanes panel renders both directions",
      "Lanes — top 10 outbound" in TPL and "Inbound — into Cameroon" in TPL)
check("the active-customers panel is capped at the top 20 by default",
      "act-extra" in TPL)
check("the active-customers panel flags credit stops in red",
      "STOP" in TPL and "#bd3727" in TPL and "not trading" in TPL)
check("the conversion rate is disclosed, not hardcoded",
      "Converted at the fixed rate" in TPL and "655" not in TPL)

# === 7. The page renders end-to-end with the stop flag wiring ==============
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

client = TestClient(main.app)
r = client.get("/tools/revenue-analysis")
check("the dashboard renders with all new panels",
      r.status_code == 200 and "KPI evolution" in r.text
      and "Lanes — top 10 outbound" in r.text
      and "Active customers" in r.text)
check("money on the page is in EUR",
      "EUR" in r.text and "run-rate" in r.text)

if _fail:
    print(f"\n{_fail} CHECK(S) FAILED")
    sys.exit(1)
print("\nALL v11.19 TESTS PASSED")
