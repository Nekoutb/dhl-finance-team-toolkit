"""Build PY/CY trial-balance + general-ledger samples for variance analysis.

Run: python scripts/make_variance_samples.py
"""
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"
OUT.mkdir(parents=True, exist_ok=True)

# account, name, PY balance, CY balance
TB_ROWS = [
    ("611000", "Rent expense",            12_000_000, 12_100_000),  # flat (+0.8%)
    ("622000", "Professional fees",        4_000_000,  9_500_000),  # +137%
    ("641000", "Salaries & wages",        60_000_000, 63_500_000),  # +5.8%
    ("616000", "Insurance",                2_500_000,          0),  # gone
    ("628000", "Security services",                0,  3_600_000),  # new
    ("411000", "Trade receivables",       45_000_000, 61_000_000),  # BS +35%
    ("521000", "Bank — main account",     30_000_000, 22_000_000),  # BS -27%
    ("701000", "Sales revenue",          150_000_000, 165_000_000), # income (excluded)
]

GL_PY = [
    ("611000", "Office rent Douala HQ", 1_000_000)] * 12 + [
    ("622000", "Annual statutory audit fees", 4_000_000),
    ("616000", "Annual insurance premium AXA", 2_500_000),
    ("641000", "Monthly payroll run", 5_000_000),
    ("411000", "Customer invoices issued", 45_000_000),
    ("521000", "Bank movements", 30_000_000),
]
GL_CY = [
    ("611000", "Office rent Douala HQ", 1_008_333)] * 12 + [
    ("622000", "Annual statutory audit fees", 4_200_000),
    ("622000", "Tax litigation counsel — DGI reassessment case", 5_300_000),
    ("641000", "Monthly payroll run", 5_200_000),
    ("641000", "Severance payment — restructuring", 1_100_000),
    ("628000", "New security guard contract — G4S", 3_600_000),
    ("411000", "Customer invoices issued", 61_000_000),
    ("521000", "Bank movements", 22_000_000),
]


def write_tb(path, col):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "TB"
    ws.append(["Trial balance"])
    ws.append([])
    ws.append(["Account", "Account name", "Closing balance"])
    for acct, name, py, cy in TB_ROWS:
        bal = py if col == "py" else cy
        if bal:
            ws.append([acct, name, bal])
    wb.save(path)


def write_gl(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "GL"
    ws.append(["General ledger extract"])
    ws.append([])
    ws.append(["Account", "Description", "Amount"])
    for acct, desc, amt in rows:
        ws.append([acct, desc, amt])
    wb.save(path)


write_tb(OUT / "variance_tb_py.xlsx", "py")
write_tb(OUT / "variance_tb_cy.xlsx", "cy")
write_gl(OUT / "variance_gl_py.xlsx", GL_PY)
write_gl(OUT / "variance_gl_cy.xlsx", GL_CY)
print("Wrote 4 variance samples to samples/")
