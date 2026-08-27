"""The "Trial Balance by Account" report export must parse as-is.

Modelled on the real CM1 exports of 28 Aug 2026, which broke the variance
upload three ways at once:

1. Two metadata rows sit ABOVE the header (a one-cell title, then Amount
   Type / Business Unit / Period cells). The header detector took the
   metadata row as the header, so the account CODES were read as balances
   (a 285-billion trial balance).
2. The report lists Opening Balance before Closing Balance — header-order
   column matching handed "balance" the OPENING column, comparing two
   years of openings.
3. The two files were numerically identical, neither netted to zero, and
   neither carried a P&L account. Nothing warned.

Isolated to temp files; the real data/ is never touched.
"""
import sys
import tempfile
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import variance  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="var_tb_"))
_fail = 0


def check(label, cond):
    global _fail
    print(("[OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        _fail += 1


HDR = ["Account Code", "Account Description", "Account Type",
       "Opening Balance", "Movements Debit", "Movements Credit",
       "Movements Total", "Closing Balance"]


def report(name, period, rows):
    """A workbook in the report's exact shape: title row, metadata row with
    embedded newlines, header, data, and a trailing Total row."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Trial Balance by Account\n"])
    ws.append(["Amount Type:\nBusiness Unit:", "AMOUNT\nCM1",
               "Period From:\nPeriod To:", period, None,
               "Date:\nUser:", "8/27/2026 2:49:11 PM\nPRG-DC\\user"])
    ws.append(HDR)
    for r in rows:
        ws.append(r)
    total = sum(r[7] for r in rows)
    ws.append([None, "Total:", None, sum(r[3] for r in rows),
               None, None, 0.0, total])
    p = _tmp / name
    wb.save(p)
    return p


# Prior year: openings differ from closings — the closing MUST be the one
# read (opening would give 100/‑100/50, closing gives 400/‑250/70).
PY_ROWS = [
    ["1112101", "Purchased Software", "B", 100.0, 500.0, 200.0, 300.0, 400.0],
    ["2110001", "Share Capital", "B", -100.0, 0.0, 150.0, -150.0, -250.0],
    ["D415000022", "NESTLE-CAMEROUN", "D", 50.0, 40.0, 20.0, 20.0, 70.0],
    ["6110001", "Rent expense", "P", 0.0, 900.0, 0.0, 900.0, 900.0],
    ["7010001", "Freight revenue", "P", 0.0, 0.0, 1120.0, -1120.0, -1120.0],
]
CY_ROWS = [
    ["1112101", "Purchased Software", "B", 400.0, 100.0, 0.0, 100.0, 500.0],
    ["2110001", "Share Capital", "B", -250.0, 0.0, 100.0, -100.0, -350.0],
    ["D415000022", "NESTLE-CAMEROUN", "D", 70.0, 30.0, 10.0, 20.0, 90.0],
    ["6110001", "Rent expense", "P", 0.0, 1200.0, 0.0, 1200.0, 1200.0],
    ["7010001", "Freight revenue", "P", 0.0, 0.0, 1440.0, -1440.0, -1440.0],
]

py = variance.parse_tb(report("py.xlsx", "2025001\n2025012", PY_ROWS))
cy = variance.parse_tb(report("cy.xlsx", "2026001\n2026007", CY_ROWS))

check("the real header is found below the title and metadata rows",
      set(py) == {"1112101", "2110001", "D415000022", "6110001", "7010001"})
check("metadata never becomes an account",
      not any("amount type" in k.lower() for k in py))
check("the Total row is dropped", not any("total" in k.lower() for k in py))
check("the CLOSING balance is read — never the opening",
      py["1112101"]["balance"] == 400.0 and cy["1112101"]["balance"] == 500.0)
check("credit-side closings keep their sign",
      cy["2110001"]["balance"] == -350.0)
check("the subledger row keeps its name",
      cy["D415000022"]["name"] == "NESTLE-CAMEROUN")
check("a balanced TB nets to zero",
      abs(sum(v["balance"] for v in cy.values())) < 0.005)

# The analysis itself: rent 900 -> 1200 = +33.3%.
res = variance.build_analysis(py, [], cy, [])
exp = {r["account"]: r for r in res["expense"]}
check("the expense variance is computed on closings",
      exp["6110001"]["cy"] == 1200.0 and exp["6110001"]["py"] == 900.0
      and exp["6110001"]["pct"] == 33.3)

# --- The three warnings, exactly as the real files tripped them -------------
check("healthy inputs raise no warning", variance.tb_checks(py, cy) == [])

w = variance.tb_checks(py, py)
check("identical PY and CY trial balances are called out",
      any("NUMERICALLY IDENTICAL" in x for x in w))

lop = dict(cy)
lop.pop("7010001")               # drop revenue -> TB no longer nets to zero
w = variance.tb_checks(py, lop)
check("a TB that does not net to zero is called out, with the gap",
      any("does not balance" in x and "1,440" in x for x in w))

bs_only = {k: v for k, v in cy.items() if not k.startswith(("6", "7"))}
py_bs = {k: v for k, v in py.items() if not k.startswith(("6", "7"))}
w = variance.tb_checks(py_bs, bs_only)
check("an openings-only export (no P&L accounts) is called out",
      any("income or expense" in x for x in w))

# The upload page surfaces them.
tpl = (ROOT / "app" / "templates" / "variance" / "index.html").read_text(
    encoding="utf-8")
check("the page renders the warnings", "tb_warnings" in tpl)
check("the upload guidance names the report and the closing balance",
      "Trial Balance by" in tpl and "CLOSING balance" in tpl)

print("\n" + ("ALL VARIANCE TB-REPORT TESTS PASSED" if not _fail
              else f"{_fail} CHECK(S) FAILED"))
sys.exit(1 if _fail else 0)
