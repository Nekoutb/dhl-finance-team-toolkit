"""v11.22 — Revenue Analysis on the owner's stated definitions:

* revenue recognised = LCU Total − LCU Taxes (columns BD − BC);
* the per-day / per-shipment / per-kilo KPIs divide the WEIGHT CHARGE, not
  the recognised total;
* fuel surcharge measured as fuel ÷ weight charge on products D, N, P, T, Y
  only, ranking the top 30 customers;
* twelve monthly upload slots.

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

_tmp = Path(tempfile.mkdtemp(prefix="v1122_"))
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
       "Service Type", "Billing Type", "Orgn", "Dest", "Local Product Code"]


def row(period, awb, name, inv, kg, w, fuel=0, other=0, disc=0, duty=0,
        tax=0, product="P ", svc="OB", orgn="DLA", dest="PAR", btype="R",
        acct="A1"):
    # LCU Total is what the file carries: everything, taxes included.
    total = w + fuel + other - disc + duty + tax
    return {"Billing Period": period, "Air waybill": awb,
            "Bill To Account": acct, "Bill To Account Name": name,
            "Shipment Date": datetime.fromisoformat(inv),
            "Invoice Date": datetime.fromisoformat(inv),
            "Billed Weight (Kilos)": kg, "LCU Weight Charge": w,
            "LCU Fuel Surcharges": fuel, "LCU Other Charges": other,
            "LCU Discount": disc, "LCU Imp/Exp Duties & Taxes": duty,
            "LCU Taxes to Applicable Charges": tax, "LCU Total": total,
            "Service Type": svc, "Billing Type": btype, "Orgn": orgn,
            "Dest": dest, "Local Product Code": product}


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


# === 1. Revenue recognised = BD − BC ======================================
# One line: weight 1000, fuel 400, other 100, discount 50, duty 200, tax 300.
# LCU Total = 1950. Recognised = 1950 − 300 = 1650 (duty stays IN).
rec = seed("2026-03.xlsx", [
    row("2026-03", "3000000001", "ALPHA LTD", "2026-03-02", 10, 1000,
        fuel=400, other=100, disc=50, duty=200, tax=300),
    row("2026-03", "3000000002", "ALPHA LTD", "2026-03-02", 10, 1000,
        fuel=400, other=100, disc=50, duty=200, tax=300),
    row("2026-03", "3000000003", "ALPHA LTD", "2026-03-02", 10, 1000,
        fuel=400, other=100, disc=50, duty=200, tax=300)])
check("LCU Total is stored as the file writes it",
      rec["totals"]["gross"] == 3 * 1950)
check("revenue recognised = LCU Total − LCU Taxes",
      rec["totals"]["net"] == 3 * 1650)
check("duty is INSIDE the recognised revenue, tax is not",
      rec["totals"]["duty"] == 600 and rec["totals"]["tax"] == 900
      and rec["totals"]["net"] == rec["totals"]["gross"]
      - rec["totals"]["tax"])
check("the weight charge is tracked separately as the KPI base",
      rec["totals"]["weight"] == 3000)

# === 2. The KPIs divide the WEIGHT CHARGE =================================
union = revenue._union_ship_days({"2026-03": rec})
m = revenue.month_metrics(rec, union)
check("one billable day from the data", m["billable_days"] == 1.0)
check("weight charge per day, not recognised revenue per day",
      m["rev_per_day"] == 3000.0 and m["rev_per_day"] != m["net"])
check("weight charge per shipment", m["rev_per_shipment"] == 1000.0)
check("weight charge per kilo", m["rev_per_kg"] == 100.0)
check("the recognised total is still reported alongside",
      m["net"] == 4950.0 and m["weight"] == 3000.0)

# === 3. Fuel surcharge, eligible products only ============================
# P and D carry fuel; Z and 7 must be excluded from the base entirely.
seed("2026-04.xlsx", [
    # ALPHA: 1000 weight, 500 fuel  -> 50%
    row("2026-04", "4000000001", "ALPHA LTD", "2026-04-01", 10, 1000,
        fuel=500, product="P ", acct="A1"),
    # BETA: 1000 weight, 200 fuel   -> 20%
    row("2026-04", "4000000002", "BETA SARL", "2026-04-01", 10, 1000,
        fuel=200, product="D", acct="B2"),
    # GAMMA on an INELIGIBLE product — must not appear at all
    row("2026-04", "4000000003", "GAMMA & CO", "2026-04-01", 10, 5000,
        fuel=4000, product="Z ", acct="C3"),
    row("2026-04", "4000000004", "GAMMA & CO", "2026-04-01", 10, 5000,
        fuel=4000, product="7 ", acct="C3")])
fu = revenue.fuel_ranking("2026-04")
names = [r["name"] for r in fu["rows"]]
check("only fuel-bearing products form the base",
      fu["weight"] == 2000.0 and fu["fuel"] == 700.0)
check("an ineligible product's customer is not ranked",
      "GAMMA & CO" not in names and names == ["ALPHA LTD", "BETA SARL"])
check("the percentage is fuel over weight charge",
      round(fu["rows"][0]["pct"], 2) == 50.0
      and round(fu["rows"][1]["pct"], 2) == 20.0)
check("ranked highest percentage first",
      fu["rows"][0]["name"] == "ALPHA LTD")
check("the overall rate is disclosed",
      round(fu["overall"], 2) == 35.0)
check("each customer is compared to the overall in points",
      round(fu["rows"][0]["delta_pts"], 2) == 15.0
      and round(fu["rows"][1]["delta_pts"], 2) == -15.0)
check("the eligible products are named for the reader",
      fu["products"] == ["D", "N", "P", "T", "Y"])
check("the trailing spaces the file writes are tolerated",
      revenue.FUEL_PRODUCTS == {"D", "N", "P", "T", "Y"})
check("the ranking is capped at 30",
      revenue.fuel_ranking.__defaults__[0] == 30)

# a customer with fuel but NO weight charge cannot rank (infinite %)
seed("2026-05.xlsx", [
    row("2026-05", "5000000001", "ZERO CO", "2026-05-04", 0, 0, fuel=900,
        product="P "),
    row("2026-05", "5000000002", "REAL CO", "2026-05-04", 10, 1000,
        fuel=300, product="P "),
    row("2026-05", "5000000003", "REAL CO", "2026-05-04", 10, 1000,
        fuel=300, product="P ")])
fu5 = revenue.fuel_ranking("2026-05")
check("a customer with no weight charge is not ranked",
      [r["name"] for r in fu5["rows"]] == ["REAL CO"])

# === 4. Twelve monthly slots ==============================================
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

client = TestClient(main.app)
r = client.get("/tools/revenue-analysis")
check("the page renders", r.status_code == 200)
check("there is one upload slot per month of the year",
      r.text.count('name="period" value="2026-') >= 3
      and "January 2026" in r.text and "December 2026" in r.text)
check("a month on record shows its figures in its slot",
      "March 2026" in r.text and "line(s)" in r.text)
check("the fuel panel is on the page",
      "Fuel surcharge — top 30 customers" in r.text
      and "vs overall" in r.text)
check("the KPI labels say weight charge, not revenue",
      "Weight charge / day (EUR)" in r.text
      and "Weight charge / kg (EUR)" in r.text)
check("the recognised-revenue basis is stated",
      "LCU total − LCU taxes" in r.text or "LCU total" in r.text)

if _fail:
    print(f"\n{_fail} CHECK(S) FAILED")
    sys.exit(1)
print("\nALL v11.22 TESTS PASSED")
