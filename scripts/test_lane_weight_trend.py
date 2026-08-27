"""A lane's BILLED WEIGHT is compared to its own prior-3-month average,
alongside its RPK.

RPK on its own misleads: a lane whose price per kilo jumps 30% while its
volume halves is a lane being lost, not a lane being repriced. The lanes
table therefore trends both figures over the same three prior months.

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

_tmp = Path(tempfile.mkdtemp(prefix="lane_kg_"))
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


def row(period, awb, inv, kg, w, dest="PAR", svc="OB", orgn="DLA"):
    return {"Billing Period": period, "Air waybill": awb,
            "Bill To Account": "A1", "Bill To Account Name": "ALPHA LTD",
            "Shipment Date": datetime.fromisoformat(inv),
            "Invoice Date": datetime.fromisoformat(inv),
            "Billed Weight (Kilos)": kg, "LCU Weight Charge": w,
            "LCU Fuel Surcharges": 0, "LCU Other Charges": 0,
            "LCU Discount": 0, "LCU Imp/Exp Duties & Taxes": 0,
            "LCU Taxes to Applicable Charges": 0, "LCU Total": w,
            "Service Type": svc, "Billing Type": "R",
            "Orgn": orgn, "Dest": dest, "Local Product Code": "P "}


def seed(name, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(HDR)
    for r in rows:
        ws.append([r[h] for h in HDR])
    wb.save(_tmp / name)
    rec, cust = revenue.parse_file(_tmp / name, name)
    revenue.store_period(rec, cust)


# Three prior months, then the month under test. The price per kilo is held
# at 1,000 everywhere in the prior months, so any RPK movement in July comes
# from July alone. Weight per lane across Apr/May/Jun:
#   BRU  100 / 100 / 100 kg -> average 100
#   LOS  100 / 200 / 300 kg -> average 200 (a ramp, not a repeat)
#   ACC  100 / 100 / 100 kg -> average 100
for period, day, los_kg in (("2026-04", "2026-04-06", 100),
                            ("2026-05", "2026-05-06", 200),
                            ("2026-06", "2026-06-06", 300)):
    mm = period[5:]
    seed(f"prior-{period}.xlsx", [
        row(period, f"1{mm}00001", day, 100, 100 * 1000, dest="BRU"),
        row(period, f"1{mm}00002", day, los_kg, los_kg * 1000, dest="LOS"),
        row(period, f"1{mm}00003", day, 100, 100 * 1000, dest="ACC")])

seed("2026-07.xlsx", [
    # BRU: weight doubles, price per kilo unchanged at 1,000/kg
    row("2026-07", "3070000001", "2026-07-06", 200, 200 * 1000, dest="BRU"),
    # LOS: weight collapses to a quarter of its 200 kg average while the
    # price per kilo JUMPS 50% — the case RPK alone reads as good news
    row("2026-07", "3070000002", "2026-07-06", 50, 50 * 1500, dest="LOS"),
    # ACC: weight holds inside the ±5% band
    row("2026-07", "3070000003", "2026-07-06", 102, 102 * 1000, dest="ACC"),
    # GVA: brand new, nothing to compare against
    row("2026-07", "3070000004", "2026-07-06", 40, 40 * 1000, dest="GVA")])

lanes = revenue.lanes_for("2026-07")
by_lane = {r["lane"]: r for r in lanes["outbound"]}
check("the prior three months are the ones used",
      lanes["prior_months"] == ["2026-04", "2026-05", "2026-06"])

be = by_lane["CM → BE"]
check("prior weight is the lane's own 3-month average",
      be["prior_kilos"] == 100.0)
check("weight doubled reads as up +100%",
      be["kg_trend"] == "up" and round(be["kg_delta_pct"]) == 100)
check("a flat price is still reported flat next to it",
      be["trend"] == "flat")

ng = by_lane["CM → NG"]
check("a ramped lane averages its three months (100/200/300 -> 200)",
      ng["prior_kilos"] == 200.0)
check("weight down to a quarter reads as down -75%",
      ng["kg_trend"] == "down" and round(ng["kg_delta_pct"]) == -75)
check("...while its RPK is simultaneously up +50% — the two disagree, "
      "which is the whole point",
      ng["trend"] == "up" and round(ng["delta_pct"]) == 50)

gh = by_lane["CM → GH"]
check("weight within ±5% reads as flat, not as movement",
      gh["kg_trend"] == "flat" and round(gh["kg_delta_pct"]) == 2)

ch = by_lane["CM → CH"]
check("a lane with no history is 'new' on weight too, never compared "
      "against nothing",
      ch["kg_trend"] == "new" and ch["kg_delta_pct"] is None
      and ch["prior_kilos"] is None)

check("both comparisons share one month count",
      be["prior_n"] == 3 and ch["prior_n"] == 0)
check("the existing RPK keys are untouched",
      set(("rpk", "prior_rpk", "delta_pct", "trend")) <= set(be))

# The page must actually render the new column.
html = (ROOT / "app" / "templates" / "revenue" / "index.html").read_text(
    encoding="utf-8")
check("the lanes table has a Weight billed column",
      "Weight billed (kg)" in html)
check("both figures render through the shared trend cell",
      'trend_cell("Weight billed"' in html and 'trend_cell("RPK"' in html)
check("the empty-table colspan still covers every column",
      'colspan="8"' in html)

print("\n" + ("ALL LANE WEIGHT-TREND TESTS PASSED" if not _fail
              else f"{_fail} CHECK(S) FAILED"))
sys.exit(1 if _fail else 0)
