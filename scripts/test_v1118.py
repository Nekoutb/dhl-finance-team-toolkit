"""v11.18 — Sales ▸ Revenue Analysis: IB434 ingest, net-revenue metrics,
the data-driven billable-day rule, and top-customer pricing.

Isolated to a temp data dir; the real data/ is never touched.
"""
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.tools import registry, revenue  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="v1118_"))
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


# --- synthetic IB434: only the needed columns, deliberately shuffled --------
HDR = ["Bill To Account Name", "Billing Period", "LCU Weight Charge",
       "Air waybill", "Shipment Date", "Invoice Date", "Bill To Account",
       "Billed Weight (Kilos)", "LCU Fuel Surcharges", "LCU Other Charges",
       "LCU Discount", "LCU Imp/Exp Duties & Taxes",
       "LCU Taxes to Applicable Charges", "LCU Total",
       "Service Type", "Billing Type", "Orgn", "Dest"]


def row(period, awb, acct, name, ship, inv, kg, w, f=0, o=0, d=0,
        duty=0, tax=0, svc="OB", orgn="DLA", dest="PAR",
        btype="R"):
    total = w + f + o - d + duty + tax
    return {"Billing Period": period, "Air waybill": awb,
            "Bill To Account": acct, "Bill To Account Name": name,
            "Shipment Date": datetime.fromisoformat(ship),
            "Invoice Date": datetime.fromisoformat(inv),
            "Billed Weight (Kilos)": kg, "LCU Weight Charge": w,
            "LCU Fuel Surcharges": f, "LCU Other Charges": o,
            "LCU Discount": d, "LCU Imp/Exp Duties & Taxes": duty,
            "LCU Taxes to Applicable Charges": tax, "LCU Total": total,
            "Service Type": svc, "Billing Type": btype,
            "Orgn": orgn, "Dest": dest}


def build(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Table1"
    ws.append(HDR)
    for r in rows:
        ws.append([r[h] for h in HDR])
    wb.save(path)


# June 2026 calendar: 1st = Monday. The billable-day rule runs on the
# INVOICE axis (the owner's rule is "where you don't see any billing"), so
# the fixture bills on the day it ships. Days:
#   Mon 01.06 — 3 billed shipments (+ a reversal pair that nets to zero)
#   Sat 06.06 — 3 shipments  -> half a day
#   Sun 07.06 — 3 shipments  -> never counts
#   Tue 02.06 — 2 shipments  -> under MIN_ACTIVE_ROWS, holiday-like
#   Expected billable days = 1 + 0.5 = 1.5.
JUNE = [
    # Mon — three plain shipments + a discount/tax/duty line
    row("2026-06", "1000000001", "A1", "ALPHA LTD", "2026-06-01",
        "2026-06-01", 10, 100000, f=20000, o=5000, d=5000, tax=19000,
        duty=7000),
    row("2026-06", "1000000002", "A1", "ALPHA LTD", "2026-06-01",
        "2026-06-01", 5, 50000),
    row("2026-06", "1000000003", "B2", "BETA SARL", "2026-06-01",
        "2026-06-01", 20, 80000, f=10000),
    # reversal PAIR on one AWB: bills 30k then fully reverses -> net 0,
    # kilos net 0, NOT a shipment
    row("2026-06", "1000000009", "B2", "BETA SARL", "2026-06-01",
        "2026-06-01", 4, 30000),
    row("2026-06", "1000000009", "B2", "BETA SARL", "2026-06-01",
        "2026-06-01", -4, -30000),
    # Sat (3 shipments, active -> 0.5 day)
    row("2026-06", "1000000004", "C3", "ALPHA LTD", "2026-06-06",
        "2026-06-06", 2, 20000),
    row("2026-06", "1000000005", "C3", "ALPHA LTD", "2026-06-06",
        "2026-06-06", 2, 20000),
    row("2026-06", "1000000006", "B2", "BETA SARL", "2026-06-06",
        "2026-06-06", 1, 8000),
    # Sun (active but NEVER billable)
    row("2026-06", "1000000007", "B2", "BETA SARL", "2026-06-07",
        "2026-06-07", 1, 9000),
    row("2026-06", "1000000010", "B2", "BETA SARL", "2026-06-07",
        "2026-06-07", 1, 9000),
    row("2026-06", "1000000011", "B2", "BETA SARL", "2026-06-07",
        "2026-06-07", 1, 9000),
    # quiet Tue (2 shipments, under the threshold -> not billable)
    row("2026-06", "1000000008", "A1", "ALPHA LTD", "2026-06-02",
        "2026-06-02", 1, 7000),
    row("2026-06", "1000000012", "A1", "ALPHA LTD", "2026-06-02",
        "2026-06-02", 1, 7000),
]
build(_tmp / "june.xlsx", JUNE)

rec, cust = revenue.parse_file(_tmp / "june.xlsx", "june.xlsx")
revenue.store_period(rec, cust)

# === 1. Parse ===============================================================
check("period read from the file", rec["period"] == "2026-06")
NET = (100000 + 20000 + 5000 - 5000) + 50000 + 90000 + 0 \
    + 40000 + 8000 + 27000 + 14000
check("net revenue = W+F+O−D, taxes and duties excluded",
      rec["totals"]["net"] == NET)
check("tax and duty are carried separately",
      rec["totals"]["tax"] == 19000 and rec["totals"]["duty"] == 7000)
check("kilos are SIGNED (the reversal pair nets to zero)",
      rec["kilos"] == 10 + 5 + 20 + 0 + 2 + 2 + 1 + 3 + 2)
check("a fully reversed airwaybill is not a shipment",
      rec["shipments"] == 11)          # 13 rows, 12 AWBs, 1 reversed pair

# === 2. The billable-day rule ==============================================
union = revenue._union_ship_days({"2026-06": rec})
check("Mon active = 1, Sat active = 0.5, Sun = never, quiet day = not billable",
      revenue.billable_days("2026-06", union) == 1.5)
m = revenue.month_metrics(rec, union)
check("revenue/day divides by the billable days",
      round(m["rev_per_day"], 2) == round(NET / 1.5, 2))
check("revenue/shipment and revenue/kg follow",
      round(m["rev_per_shipment"], 2) == round(NET / 11, 2)
      and round(m["rev_per_kg"], 2) == round(NET / 45, 2))

# a LATER file's shipments can prove an earlier month's day
extra = {"daily": {"2026-06-03": {"net": 1.0, "kilos": 1.0,
                                  "shipments": 5, "awbs": ["a"]}}}  # Wed
union2 = revenue._union_ship_days({"a": rec, "b": extra})
check("activity in another file's rows adds the day to the union",
      revenue.billable_days("2026-06", union2) == 2.5)

# === 3. Pricing groups by NAME and prices on the WEIGHT CHARGE =============
pt = revenue.pricing_top({"customers": cust})
names = [r["name"] for r in pt["rows"]]
check("one line per customer NAME even across several accounts",
      names.count("ALPHA LTD") == 1 and len(names) == 2)
alpha = next(r for r in pt["rows"] if r["name"] == "ALPHA LTD")
check("price/kg = weight charge ÷ kilos for the customer",
      round(alpha["per_kg"], 2) == round((100000 + 50000 + 20000 + 20000
                                          + 7000 + 7000) / 21, 2))
# hand-computed from the fixture rows: weight charges (fuel/other excluded)
# 100+50+80+30-30+20+20+8+9+9+9+7+7 = 319 thousand, over signed kilos 45
check("file average = total weight charge ÷ total kilos (hand-checked)",
      round(pt["file_avg"], 2) == round(319000 / 45, 2))
check("variance % against the file average",
      round(alpha["variance_pct"], 2) ==
      round(100 * (alpha["per_kg"] - pt["file_avg"]) / pt["file_avg"], 2))

# === 4. Replace-on-reupload + delete ========================================
# The v2 file is DIFFERENT (one extra shipment, a new customer) — the stored
# record must carry v2's numbers, not a merge of both uploads.
build(_tmp / "june2.xlsx", JUNE + [
    row("2026-06", "1000000099", "Z9", "GAMMA & CO", "2026-06-01",
        "2026-06-10", 3, 33000)])
rec2, cust2 = revenue.parse_file(_tmp / "june2.xlsx", "june-v2.xlsx")
revenue.store_period(rec2, cust2)
data = revenue._load()
check("re-uploading a month replaces it, never duplicates",
      list(data["periods"].keys()) == ["2026-06"]
      and data["periods"]["2026-06"]["source"] == "june-v2.xlsx")
stored = data["periods"]["2026-06"]
check("the replacement carries the NEW content, not a merge",
      stored["totals"]["net"] == NET + 33000
      and stored["shipments"] == 12
      and "Z9" in stored["customers"]
      and stored["customers"]["Z9"]["net"] == 33000)
check("delete removes the month", revenue.delete_period("2026-06")
      and revenue._load()["periods"] == {})

# === 5. Dashboard assembly ==================================================
build(_tmp / "july.xlsx", [
    row("2026-07", "2000000001", "A1", "ALPHA LTD", "2026-07-01",
        "2026-07-01", 10, 200000),
    row("2026-07", "2000000002", "A1", "ALPHA LTD", "2026-07-01",
        "2026-07-01", 10, 200000),
    row("2026-07", "2000000003", "B2", "BETA SARL", "2026-07-01",
        "2026-07-01", 10, 200000)])
build(_tmp / "aug.xlsx", [
    row("2026-08", "3000000001", "A1", "ALPHA LTD", "2026-08-03",
        "2026-08-04", 10, 100000),
    row("2026-08", "3000000002", "A1", "ALPHA LTD", "2026-08-03",
        "2026-08-04", 10, 100000),
    row("2026-08", "3000000003", "B2", "BETA SARL", "2026-08-03",
        "2026-08-04", 10, 100000)])
for f in ("july.xlsx", "aug.xlsx"):
    r_, c_ = revenue.parse_file(_tmp / f, f)
    revenue.store_period(r_, c_)
view = revenue.dashboard(now=datetime(2026, 8, 12))
check("months come out ascending",
      [m["period"] for m in view["months"]] == ["2026-07", "2026-08"])
check("the current calendar month is flagged ongoing",
      view["ongoing"] and view["ongoing"]["period"] == "2026-08")
check("KPI boxes compare the run-rate to the prior month, same days",
      len(view["kpis"]) == 3 and all(k["baseline"] for k in view["kpis"]))
kpi = {k["key"]: k for k in view["kpis"]}
check("revenue/day delta is computed",
      round(kpi["rev_per_day"]["value"], 0) == 300000
      and round(kpi["rev_per_day"]["baseline"], 0) == 600000
      and round(kpi["rev_per_day"]["delta_pct"], 1) == -50.0)
check("pricing defaults to the latest COMPLETE month",
      view["pricing_period"] == "2026-07")
check("the selector can still price the ongoing month",
      revenue.pricing_for("2026-08")["rows"][0]["name"] == "ALPHA LTD")

# === 6. The tool is registered and reachable ================================
check("registry knows revenue-analysis under Sales",
      registry.by_slug("revenue-analysis")["category"] == "Sales"
      and "revenue-analysis" in registry.all_slugs())
BASE = (ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")
check("the sidebar has the Sales section gated on the slug",
      ">Sales</div>" in BASE and "'revenue-analysis' in _allowed" in BASE)

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

client = TestClient(main.app)
r = client.get("/tools/revenue-analysis")
check("the dashboard page renders", r.status_code == 200
      and "Revenue Analysis" in r.text and "Months side by side" in r.text)
check("the pricing panel renders the stored customers with variances",
      "Pricing — top 10 customers" in r.text and "ALPHA LTD" in r.text
      and "%" in r.text)

# upload through the route: background ingest lands a new month
build(_tmp / "sep.xlsx", [
    row("2026-09", "4000000001", "A1", "ALPHA LTD", "2026-09-01",
        "2026-09-02", 10, 150000),
    row("2026-09", "4000000002", "A1", "ALPHA LTD", "2026-09-01",
        "2026-09-02", 10, 150000),
    row("2026-09", "4000000003", "B2", "BETA SARL", "2026-09-01",
        "2026-09-02", 10, 150000)])
with open(_tmp / "sep.xlsx", "rb") as fh:
    r = client.post("/tools/revenue-analysis/upload",
                    files={"files": ("sep.xlsx", fh.read(),
                                     "application/vnd.ms-excel")},
                    follow_redirects=False)
check("upload responds at once (303, background ingest)",
      r.status_code == 303)
for _ in range(40):
    if "2026-09" in revenue._load()["periods"]:
        break
    time.sleep(0.25)
check("the uploaded month lands in the store",
      "2026-09" in revenue._load()["periods"])

# a wrong file surfaces a readable error instead of a silent empty month
bad = openpyxl.Workbook()
bad.active.append(["Some", "Other", "File"])
bad.save(_tmp / "bad.xlsx")
with open(_tmp / "bad.xlsx", "rb") as fh:
    client.post("/tools/revenue-analysis/upload",
                files={"files": ("bad.xlsx", fh.read(),
                                 "application/vnd.ms-excel")},
                follow_redirects=False)
for _ in range(40):
    if revenue.status()["processing_error"]:
        break
    time.sleep(0.25)
err = revenue.status()["processing_error"]
check("a non-IB434 file is refused with a readable reason",
      err and "column" in err["message"])
r = client.get("/tools/revenue-analysis")
check("the error shows on the page", "column" in r.text)

r = client.post("/tools/revenue-analysis/delete", data={"period": "2026-09"},
                follow_redirects=False)
check("a month can be removed from the page", r.status_code == 303
      and "2026-09" not in revenue._load()["periods"])

# === 7. Review hardening ====================================================
# a) hostile cells cannot poison the store
check("a text 'NaN' amount counts as zero, never as NaN",
      revenue._num("NaN") == 0.0 and revenue._num("inf") == 0.0
      and revenue._num(float("nan")) == 0.0)
check("an impossible ISO-shaped date is refused",
      revenue._iso_day("2026-06-31") == "")
check("an Excel serial number date is recovered",
      revenue._iso_day(45808.0) == "2025-05-31")
check("a dd/mm/yyyy text date is recovered",
      revenue._iso_day("05/06/2026") == "2026-06-05")
check("a bad stored day cannot crash the billable-day arithmetic",
      revenue.billable_days("2026-06", {"2026-06-31": 99}) == 0.0)

# b) a negative baseline suppresses the delta instead of inverting its sign
revenue.delete_period("2026-07")     # earlier fixture must not pollute
neg = {"period": "2026-06", "totals": {"net": -100.0, "gross": -100.0,
       "tax": 0, "duty": 0, "weight": -100.0, "fuel": 0, "other": 0,
       "discount": 0}, "kilos": 1.0, "shipments": 1,
       "ship_days": {"2026-06-01": 5}, "daily_net": {}, "customers": {},
       "source": "s", "uploaded": "u", "rows": 1}
revenue.store_period(dict(neg), {})
pos = dict(neg, period="2026-08", totals=dict(neg["totals"], net=100.0),
           ship_days={"2026-08-03": 5})
revenue.store_period(dict(pos), {})
v2 = revenue.dashboard(now=datetime(2026, 8, 12))
check("a negative baseline shows no delta instead of a sign-flipped one",
      all(k["delta_pct"] is None for k in v2["kpis"]))
revenue.delete_period("2026-06")
revenue.delete_period("2026-08")

# c) the spooled upload files are removed once ingested
leftovers = list(revenue.UPLOAD_DIR.glob("rev_*"))
check("ingested upload files are cleaned from disk", leftovers == [])

# d) .xls is refused up front (the reader cannot open it)
r = client.post("/tools/revenue-analysis/upload",
                files={"files": ("old.xls", b"x", "application/vnd.ms-excel")},
                follow_redirects=False)
check(".xls is refused with a save-as hint",
      r.status_code == 303 and "xlsx" in r.headers.get("location", ""))

if _fail:
    print(f"\n{_fail} CHECK(S) FAILED")
    sys.exit(1)
print("\nALL v11.18 TESTS PASSED")
