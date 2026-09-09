"""v11.28 — the Days-to-date filter, shipment KPIs, and Lane focus.

* Days to date cuts EVERY comparison — KPI boxes, evolution charts, months
  side by side, lanes, active customers — to the first n CALENDAR days of
  each month, so the 1st–10th of this month sits against the 1st–10th of
  the others.
* Shipments and shipments/day join the compared KPIs, and the pricing table
  carries each customer's shipment count and rate.
* Lane focus: five analyses of the customers feeding one lane, this month
  against last.

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

_tmp = Path(tempfile.mkdtemp(prefix="v1128_"))
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
       "Service Type", "Billing Type", "Orgn", "Dest",
       "Local Product Code"]


def row(period, awb, acct, name, inv, kg, w, dest):
    d = datetime.fromisoformat(inv)
    return [period, awb, acct, name, d, d, kg, w, 0, 0, 0, 0, 0, w,
            "OB", "R", "DLA", dest, "P "]


def seed(name, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HDR)
    for r in rows:
        ws.append(r)
    wb.save(_tmp / name)
    rec, cust = revenue.parse_file(_tmp / name, name)
    return revenue.store_period(rec, cust)   # the stored shape, customers in


# June: two billable weekdays (3 AWBs each — a quieter day would not count
# as billable at all). The 15th sits OUTSIDE a 10-days-to-date window.
seed("2026-06.xlsx", [
    row("2026-06", "601", "A1", "ALPHA", "2026-06-03", 50, 50000, "BRU"),
    row("2026-06", "602", "E1", "EPSILON", "2026-06-03", 50, 50000, "BRU"),
    row("2026-06", "603", "B1", "BETA", "2026-06-03", 50, 25000, "LOS"),
    row("2026-06", "604", "A1", "ALPHA", "2026-06-15", 100, 100000, "BRU"),
    row("2026-06", "605", "A1", "ALPHA", "2026-06-15", 100, 100000, "BRU"),
    row("2026-06", "606", "B1", "BETA", "2026-06-15", 10, 5000, "LOS")])
# July: EPSILON leaves the BRU lane, GAMMA and DELTA arrive, and GAMMA is
# priced well under the lane's going rate.
july = seed("2026-07.xlsx", [
    row("2026-07", "701", "A1", "ALPHA", "2026-07-06", 60, 72000, "BRU"),
    row("2026-07", "702", "G1", "GAMMA", "2026-07-06", 60, 54000, "BRU"),
    row("2026-07", "703", "G1", "GAMMA", "2026-07-06", 70, 35000, "LOS"),
    row("2026-07", "704", "D1", "DELTA", "2026-07-20", 80, 96000, "BRU"),
    row("2026-07", "705", "A1", "ALPHA", "2026-07-20", 40, 48000, "BRU"),
    row("2026-07", "706", "G1", "GAMMA", "2026-07-20", 30, 15000, "LOS")])

NOW = datetime(2026, 7, 25)      # July is the ongoing month

# === 1. The parser captures the slices everything else runs on =============
cust = july["customers"]
check("per-customer shipment counts are stored",
      cust["A1"]["shipments"] == 2 and cust["G1"]["shipments"] == 3)
check("per-customer kilos per day are stored",
      cust["A1"]["days"] == {"2026-07-06": 60.0, "2026-07-20": 40.0})
lane = july["lanes"]["OB"]["CM-BE"]
check("per-lane per-day slices are stored on top lanes",
      lane["days"]["2026-07-06"] == [126000.0, 126000.0, 120.0])
check("per-lane customers are stored on top lanes",
      lane["cust"]["D1"] == {"name": "DELTA", "net": 96000.0,
                             "weight": 96000.0, "kilos": 80.0,
                             "shipments": 1})

# === 2. The days-to-date window ============================================
data = revenue._load()
union = revenue._union_ship_days(data["periods"])
w = revenue.dtd_window(data["periods"]["2026-06"], 10, union)
check("a 10-days-to-date June is the 3rd only (the 15th is out)",
      w["kilos"] == 150.0 and w["net"] == 125000.0 and w["shipments"] == 3
      and w["days"] == 1.0)
check("the window carries the shipment rate",
      w["ships_per_day"] == 3.0)

# === 3. The dashboard under the filter =====================================
view = revenue.dashboard(now=NOW, days_to_date=10)
check("the filter is echoed back", view["dtd"] == 10)
kpis = {k["key"]: k for k in view["kpis"]}
check("five KPIs are compared — shipments and shipments/day included",
      set(kpis) == {"rev_per_day", "rev_per_shipment", "rev_per_kg",
                    "shipments", "ships_per_day"})
check("shipment KPIs are counts, not money",
      kpis["shipments"]["unit"] == "count"
      and kpis["ships_per_day"]["unit"] == "count")
check("July's first 10 days vs June's first 10 days",
      kpis["shipments"]["value"] == 3 and kpis["shipments"]["baseline"] == 3
      and kpis["shipments"]["delta_pct"] == 0.0)
check("RpK compares the two cut windows",
      round(kpis["rev_per_kg"]["value"], 2) == round(161000 / 190, 2)
      and round(kpis["rev_per_kg"]["baseline"], 2) == round(125000 / 150, 2))
check("the months table is cut too",
      next(m for m in view["months"]
           if m["period"] == "2026-07")["kilos"] == 190.0)
check("all five KPI charts render",
      len(view["graphs"]) == 5
      and {g["unit"] for g in view["graphs"]} == {"eur", "count"})
check("a projection has no place in a days-to-date view",
      view["landing"] is None)

full = revenue.dashboard(now=NOW)
check("without the filter the five KPIs still compare (full/run-rate)",
      {k["key"] for k in full["kpis"]} == set(kpis)
      and full["landing"] is not None)
check("the full months table carries shipments/day",
      next(m for m in full["months"]
           if m["period"] == "2026-06")["ships_per_day"] == 3.0)

# === 4. Lanes under the filter =============================================
lanes = revenue.lanes_for("2026-07", dtd=10)
be = next(r for r in lanes["outbound"] if r["key"] == "CM-BE")
check("lane figures are cut to the window",
      be["kilos"] == 120.0 and be["rpk"] == 126000.0 / 120.0)
check("the prior average is cut to the SAME window",
      be["prior_rpk"] == 1000.0 and be["prior_kilos"] == 100.0)
check("lane shipments are not sliced per day — unknown, never guessed",
      be["shipments"] is None)
be_full = next(r for r in revenue.lanes_for("2026-07")["outbound"]
               if r["key"] == "CM-BE")
check("unfiltered lane figures are the full month",
      be_full["kilos"] == 240.0 and be_full["rpk"] == 1125.0
      and be_full["shipments"] == 4)

# === 5. Active customers under the filter ==================================
act = revenue.active_customers(now=NOW, dtd=10)
alpha = next(r for r in act["rows"] if r["key"] == "ALPHA")
check("prior months' kilos are cut per customer",
      alpha["months"] == {"2026-06": 50.0} and alpha["avg_kilos"] == 50.0)
check("the current month is cut to the same days",
      alpha["current"] == 60.0)
act_full = revenue.active_customers(now=NOW)
alpha_f = next(r for r in act_full["rows"] if r["key"] == "ALPHA")
check("unfiltered active customers keep full months",
      alpha_f["avg_kilos"] == 250.0 and alpha_f["current"] == 100.0)

# === 6. Pricing carries shipments ==========================================
pricing = revenue.pricing_for("2026-07")
rows = {r["name"]: r for r in pricing["rows"]}
check("each pricing row carries its shipment count",
      rows["ALPHA"]["shipments"] == 2 and rows["GAMMA"]["shipments"] == 3)
check("…and shipments per billable day",
      rows["ALPHA"]["ships_per_day"] == 1.0
      and rows["GAMMA"]["ships_per_day"] == 1.5)

# === 7. Lane focus — five analyses on one lane's customers =================
lf = revenue.lane_focus("2026-07", "OB", "CM-BE")
h = lf["headline"]
check("1: headline KPIs current vs prior",
      h["current"]["kilos"] == 240.0 and h["prior"]["kilos"] == 300.0
      and h["current"]["rpk"] == 1125.0 and h["prior"]["rpk"] == 1000.0
      and h["current"]["ships_per_day"] == 2.0)
check("1: headline deltas computed",
      round(h["delta_pct"]["rpk"], 1) == 12.5
      and round(h["delta_pct"]["kilos"], 1) == -20.0)
top = lf["customers"][0]
check("2: top customers by weight with their month-on-month move",
      top["name"] == "ALPHA" and top["kilos"] == 100.0
      and top["prev_kilos"] == 250.0
      and round(top["kg_delta_pct"], 1) == -60.0)
check("3: customers who joined the lane are named",
      {c["name"] for c in lf["joined"]} == {"GAMMA", "DELTA"})
check("3: customers who left the lane are named, with what they moved",
      lf["lost"] == [{"name": "EPSILON", "kilos": 50.0}])
check("4: concentration current vs prior",
      lf["concentration"]["current"][1] == 41.7
      and lf["concentration"]["prior"][1] == 83.3
      and lf["concentration"]["current"][3] == 100.0)
gamma = next(c for c in lf["customers"] if c["name"] == "GAMMA")
check("5: price dispersion — GAMMA pays 20% under the lane's going rate",
      gamma["rpk"] == 900.0
      and round(gamma["rpk_vs_lane_pct"], 1) == -20.0)
check("a lane with no stored customer slice returns None, not a guess",
      revenue.lane_focus("2026-07", "IB", "CM-BE") is None)
check("1: the lane's RpD is in the headline (weight charge / billable days)",
      h["current"]["rpd"] == 270000.0 / 2 and h["prior"]["rpd"] == 150000.0)

# === 7b. A month with no per-day lane slice says so under the filter =======
data = revenue._load()
for svc in data["periods"]["2026-07"]["lanes"].values():
    for entry in svc.values():
        entry.pop("days", None)
revenue.STORE_PATH.write_text(__import__("json").dumps(data),
                              encoding="utf-8")
lanes_missing = revenue.lanes_for("2026-07", dtd=10)
check("all-blank lanes under the filter raise the dtd_missing flag",
      lanes_missing["dtd_missing"] is True)
check("the flag stays down when the slices exist",
      revenue.lanes_for("2026-06", dtd=10)["dtd_missing"] is False)
# restore July for the page checks below
seed("2026-07.xlsx", [
    row("2026-07", "701", "A1", "ALPHA", "2026-07-06", 60, 72000, "BRU"),
    row("2026-07", "702", "G1", "GAMMA", "2026-07-06", 60, 54000, "BRU"),
    row("2026-07", "703", "G1", "GAMMA", "2026-07-06", 70, 35000, "LOS"),
    row("2026-07", "704", "D1", "DELTA", "2026-07-20", 80, 96000, "BRU"),
    row("2026-07", "705", "A1", "ALPHA", "2026-07-20", 40, 48000, "BRU"),
    row("2026-07", "706", "G1", "GAMMA", "2026-07-20", 30, 15000, "LOS")])

# === 7c. Sources are retained; re-read heals old months without re-upload ==
import shutil
import time as _t
revenue.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
spool = revenue.UPLOAD_DIR / "rev_test1234.xlsx"
shutil.copy(_tmp / "2026-06.xlsx", spool)
revenue.ingest_async([(spool, "june re-export.xlsx")])
deadline = _t.time() + 30
while _t.time() < deadline and revenue.status().get("processing"):
    _t.sleep(0.05)
kept = dict(revenue.stored_sources())
check("the ingested file is RETAINED, one per month",
      "2026-06" in kept and kept["2026-06"].name == "ib434_2026-06.xlsx"
      and not spool.exists())
# cripple June the way a pre-v11.28 store is, then re-read from the kept file
data = revenue._load()
for c in data["periods"]["2026-06"]["customers"].values():
    c.pop("days", None)
    c.pop("shipments", None)
revenue.STORE_PATH.write_text(__import__("json").dumps(data),
                              encoding="utf-8")
done, errors = revenue.reparse_stored()
check("re-read rebuilds the month from the kept file", done == ["2026-06"]
      and errors == [])
check("…and the month has its detail back",
      revenue._load()["periods"]["2026-06"]["customers"]["A1"]["shipments"]
      == 3)

# === 8. The page ===========================================================
from testutil import smtp_guard  # noqa: E402
smtp_guard()
from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402

client = TestClient(main.app)
r = client.get("/tools/revenue-analysis?dtd=10")
check("the page accepts the filter", r.status_code == 200
      and "Days to date" in r.text and "1st–10" in r.text)
r2 = client.get("/tools/revenue-analysis?pricing=2026-07&focus=OB:CM-BE")
check("the lane focus panel renders from a lane link",
      r2.status_code == 200 and "Lane focus — CM → BE" in r2.text
      and "EPSILON" in r2.text)
check("the lane focus headline shows RpD",
      "RpD w/o fuel surcharge (EUR)" in r2.text)
check("the chosen lane is selected in the dropdown",
      'value="OB:CM-BE"\n            selected' in r2.text
      or 'value="OB:CM-BE" selected' in r2.text)
r3 = client.get("/tools/revenue-analysis?pricing=2026-07")
check("pricing table carries the new columns",
      "Shipments / day" in r3.text)
check("the Lane focus sandbox with its dropdown renders WITHOUT a click",
      "choose a lane" in r3.text and "Analyse\n        lane" in r3.text
      and 'id="lanefocus"' in r3.text)
r4 = client.get("/tools/revenue-analysis?pricing=2026-07&focus=IB:CM-BE")
check("a lane with no slice explains itself instead of vanishing",
      "no per-customer\n      lane detail on record" in r4.text
      or "no per-customer lane detail on record" in r4.text)

print("\n" + ("ALL v11.28 TESTS PASSED" if not _fail
              else f"{_fail} CHECK(S) FAILED"))
sys.exit(1 if _fail else 0)
