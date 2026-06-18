"""A realistic French bank statement sample for the Bank Statements tool:
accented headers (Date opération / Libellé / Débit / Crédit) and payers printed
after "Donneur d'ordre" — including one payer (ZETA) absent from the AR.
Run: python scripts/make_bank_french_sample.py
"""
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "bank_statement_french_sample.xlsx"
OUT.parent.mkdir(parents=True, exist_ok=True)

HEADER = ["Date opération", "Libellé", "Débit", "Crédit"]
ROWS = [
    ("2026-06-10", "VIR RECU - Donneur d'ordre: ACME LOGISTICS", "", 1500000),
    ("2026-06-10", "VIREMENT Donneur d'ordre BETA TRADING SARL", "", 670000),
    ("2026-06-11", "REGLEMENT CHQ Donneur d'Ordre: GAMMA SARL", "", 500000),
    ("2026-06-11", "FRAIS DE TENUE DE COMPTE", 25000, ""),
    ("2026-06-12", "VIR RECU - Donneur d'ordre: ZETA TRADING", "", 300000),
]

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Relevé"
ws.append(["RELEVE DE COMPTE - AFRILAND FIRST BANK", "", "", ""])
ws.append(HEADER)
for r in ROWS:
    ws.append(list(r))
wb.save(OUT)
print(f"Wrote {OUT} ({len(ROWS)} lines)")
