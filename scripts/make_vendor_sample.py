"""Generate an Excel vendor list sample for the NIU + certificate controls.

Dates are relative to today so the demo always shows all certificate states:
  - SGT     : issued 30 days ago  -> VALID
  - CAMSUP  : issued 85 days ago  -> EXPIRING (3-month validity, <=14d left)
  - DOUALA  : issued 150 days ago -> EXPIRED
  - NORTHERN: no issue date       -> NO DATE

Run: python scripts/make_vendor_sample.py
"""
from datetime import date, timedelta
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "vendor_list_sample.xlsx"
OUT.parent.mkdir(parents=True, exist_ok=True)

today = date.today()
ROWS = [
    ("SOCIETE GENERALE DES TRAVAUX", "M012345678901A", "ANR-2026-0042",
     (today - timedelta(days=30)).isoformat(), "compta@sgt-cm.example"),
    ("CAMEROON SUPPLIES SARL", "P098765432109B", "ANR-2026-0117",
     (today - timedelta(days=85)).isoformat(), "finance@camsup.example"),
    ("DOUALA FREIGHT SERVICES", "M555666777888C", "ANR-2025-0903",
     (today - timedelta(days=150)).isoformat(), "billing@dlafreight.example"),
    ("NORTHERN LOGISTICS CO", "P111222333444D", "", "", ""),
]

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Vendors"
ws.append(["Vendor Name", "Taxpayer ID (NIU)", "Tax Clearance Certificate",
           "Certificate Issue Date", "Email"])
for row in ROWS:
    ws.append(list(row))
wb.save(OUT)
print(f"Wrote {OUT} ({len(ROWS)} vendors, dates relative to {today})")
