"""Generate a bank-statement sample to test name matching against open AR.

Some narrations match AR-sample customers (BETA, GAMMA, DELTA); some don't.
Run: python scripts/make_bank_sample.py
"""
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "bank_statement_sample.xlsx"
OUT.parent.mkdir(parents=True, exist_ok=True)

HEADER = ["Value Date", "Description", "Credit Amount", "Debit Amount", "Currency"]
ROWS = [
    ("2026-06-08", "VIREMENT RECU BETA TRADING SARL FACT MAI", 500000, 0),
    ("2026-06-07", "MOBILE MONEY GAMMA SARL PAIEMENT", 95000, 0),
    ("2026-06-06", "DEPOT ESPECES DELTA CORP CAMEROUN", 1200000, 0),
    ("2026-06-05", "VIR SALAIRES PERSONNEL", 0, 3400000),
    ("2026-06-05", "FRAIS BANCAIRES", 0, 25000),
    ("2026-06-04", "VIREMENT EPSILON HOLDINGS (no AR match)", 800000, 0),
]

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Statement"
ws.append(["BANK OF CENTRAL AFRICA — Statement"])
ws.append(["Account:", "DHL EXPRESS CM01"])
ws.append([])
ws.append(HEADER)
for vdate, desc, cr, dr in ROWS:
    ws.append([vdate, desc, cr, dr, "XAF"])

wb.save(OUT)
print(f"Wrote {OUT} ({len(ROWS)} statement lines)")

# ---- Second statement from a DIFFERENT bank (multi-file + bank-ID tests) ----
OUT2 = ROOT / "samples" / "bank_statement_sample2.xlsx"
ROWS2 = [
    ("2026-06-08", "TRF ACME LOGISTICS REGLEMENT FACTURES", 1500000, 0),
    ("2026-06-07", "VIREMENT OMEGA PARTNERS SARL", 300000, 0),
    ("2026-06-06", "COMMISSIONS BANCAIRES", 0, 18000),
]
wb2 = openpyxl.Workbook()
ws2 = wb2.active
ws2.title = "Statement"
ws2.append(["ECOBANK CAMEROUN S.A. — Releve de compte"])
ws2.append(["Account:", "DHL EXPRESS CM01"])
ws2.append([])
ws2.append(HEADER)
for vdate, desc, cr, dr in ROWS2:
    ws2.append([vdate, desc, cr, dr, "XAF"])
wb2.save(OUT2)
print(f"Wrote {OUT2} ({len(ROWS2)} statement lines)")

# ---- Third statement as a PDF (tests the PDF parsing path) -------------------
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OUT3 = ROOT / "samples" / "bank_statement_sample3.pdf"
styles = getSampleStyleSheet()
doc = SimpleDocTemplate(str(OUT3), pagesize=A4, topMargin=16 * mm,
                        leftMargin=14 * mm, rightMargin=14 * mm)
story = [
    Paragraph("AFRILAND FIRST BANK", styles["Title"]),
    Paragraph("Releve de compte — DHL EXPRESS CM01", styles["Normal"]),
    Paragraph("Periode: 01/06/2026 - 09/06/2026", styles["Normal"]),
    Spacer(1, 8),
]
data = [["Value Date", "Description", "Credit Amount", "Debit Amount"]]
data += [
    ["2026-06-09", "VIREMENT DELTA CORP REGLEMENT", "800000", "0"],
    ["2026-06-08", "DEPOT JUPITER VENTURES SARL", "250000", "0"],
    ["2026-06-07", "AGIOS ET FRAIS", "0", "42000"],
]
table = Table(data, repeatRows=1)
table.setStyle(TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1a5632")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
]))
story.append(table)
doc.build(story)
print(f"Wrote {OUT3} (PDF statement, 3 lines)")
