"""Variance analysis — service math + full HTTP round-trip."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()
subprocess.run([sys.executable, str(ROOT / "scripts" / "make_variance_samples.py")],
               check=True, capture_output=True)

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.services import variance  # noqa: E402

S = ROOT / "samples"

# --- service-level ----------------------------------------------------------
result = variance.build_analysis(
    variance.parse_tb(S / "variance_tb_py.xlsx"),
    variance.parse_gl(S / "variance_gl_py.xlsx"),
    variance.parse_tb(S / "variance_tb_cy.xlsx"),
    variance.parse_gl(S / "variance_gl_cy.xlsx"))

exp = {r["account"]: r for r in result["expense"]}
bal = {r["account"]: r for r in result["balance"]}

assert "701000" not in exp and "701000" not in bal, "income must be excluded"
assert set(exp) == {"611000", "622000", "641000", "616000", "628000"}, exp.keys()
assert set(bal) == {"411000", "521000"}

r = exp["611000"]
assert abs(r["pct"]) <= 2 and "flat" in r["comment"], r
print("ok: rent flagged flat (±2%)")

r = exp["622000"]
assert r["variance"] == 5_500_000, r["variance"]
assert "increase is driven by" in r["comment"] and "litigation" in r["comment"], r["comment"]
assert any("one-off" in q or "single item" in q.lower() for q in r["questions"]), r["questions"]
print("ok: professional fees driver = tax litigation counsel + question raised")

r = exp["616000"]
assert r["cy"] == 0 and "decline is explained by" in r["comment"], r
assert any("completeness" in q for q in r["questions"])
print("ok: insurance drop-to-nil explained + completeness question")

r = exp["628000"]
assert r["pct"] is None and "New this year" in r["comment"], r
assert any("budgeted" in q for q in r["questions"])
print("ok: new security line flagged with budget question")

r = bal["411000"]
assert r["pct"] == 35.6 or 35 <= r["pct"] <= 36, r["pct"]
assert any("recoverability" in q.lower() or "ageing" in q.lower()
           for q in r["questions"]), r["questions"]
print("ok: receivables growth raises ageing/recoverability question")

# --- HTTP round-trip --------------------------------------------------------
client = TestClient(main.app)
page = client.get("/tools/variance-analysis")
assert page.status_code == 200 and "Prior year" in page.text
with open(S / "variance_tb_py.xlsx", "rb") as f1, \
     open(S / "variance_gl_py.xlsx", "rb") as f2, \
     open(S / "variance_tb_cy.xlsx", "rb") as f3, \
     open(S / "variance_gl_cy.xlsx", "rb") as f4:
    resp = client.post("/tools/variance-analysis/analyze",
                       data={"label": "TEST FY"},
                       files={"py_tb": ("tb_py.xlsx", f1),
                              "py_gl": ("gl_py.xlsx", f2),
                              "cy_tb": ("tb_cy.xlsx", f3),
                              "cy_gl": ("gl_cy.xlsx", f4)})
assert resp.status_code == 200, resp.status_code
assert "Variance analysis ready" in resp.text
assert "tax litigation counsel" in resp.text.lower()
m = re.search(r"/tools/variance-analysis/export/([0-9a-f]+)", resp.text)
assert m, "export link missing"
token = m.group(1)
x = client.get(f"/tools/variance-analysis/export/{token}")
assert x.status_code == 200 and len(x.content) > 4000
print("ok: HTTP analyze + Excel export")

# cleanup this test's artifacts
for p in variance.OUT_DIR.glob(f"{token}.*"):
    p.unlink()

# --- regression: text balances with thousands separators (the proxy-error /
#     data-corruption bug) parse correctly through the full HTTP route --------
import openpyxl  # noqa: E402


def _wb(path, header, rows):
    wb = openpyxl.Workbook(); ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)


BIG = ROOT / "samples"
# Balances stored as TEXT with comma thousands — what real exports produce.
_wb(BIG / "vtxt_tb_py.xlsx", ["Account", "Account name", "Closing balance"],
    [["611000", "Rent", "12,000,000"], ["641000", "Salaries", "60,000,000"],
     ["411000", "Receivables", "45,000,000"]])
_wb(BIG / "vtxt_tb_cy.xlsx", ["Account", "Account name", "Closing balance"],
    [["611000", "Rent", "12,100,000"], ["641000", "Salaries", "63,500,000"],
     ["411000", "Receivables", "61,000,000"]])
_wb(BIG / "vtxt_gl_py.xlsx", ["Account", "Description", "Amount"],
    [["611000", "Office rent", "12,000,000"], ["641000", "Payroll", "60,000,000"],
     ["411000", "Invoices", "45,000,000"]])
_wb(BIG / "vtxt_gl_cy.xlsx", ["Account", "Description", "Amount"],
    [["611000", "Office rent", "12,100,000"],
     ["641000", "Payroll + bonus", "63,500,000"],
     ["411000", "Invoices", "61,000,000"]])

with open(BIG / "vtxt_tb_py.xlsx", "rb") as f1, \
     open(BIG / "vtxt_gl_py.xlsx", "rb") as f2, \
     open(BIG / "vtxt_tb_cy.xlsx", "rb") as f3, \
     open(BIG / "vtxt_gl_cy.xlsx", "rb") as f4:
    resp = client.post("/tools/variance-analysis/analyze",
                       data={"label": "TEXT SEP"},
                       files={"py_tb": ("a.xlsx", f1), "py_gl": ("b.xlsx", f2),
                              "cy_tb": ("c.xlsx", f3), "cy_gl": ("d.xlsx", f4)})
assert resp.status_code == 200, resp.status_code
# Salaries: 60,000,000 -> 63,500,000  (would be 60.0 -> 63.5 with the old bug)
assert "63,500,000" in resp.text and "60,000,000" in resp.text, \
    "thousands-separated text balances were not parsed correctly"
assert "Variance analysis ready" in resp.text
m2 = re.search(r"/tools/variance-analysis/export/([0-9a-f]+)", resp.text)
for p in variance.OUT_DIR.glob(f"{m2.group(1)}.*"):
    p.unlink()
for p in BIG.glob("vtxt_*.xlsx"):
    p.unlink()
print("ok: text balances with thousands separators parse correctly (no zeroing)")

print("\nALL VARIANCE TESTS PASSED")
