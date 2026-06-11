"""Generate a realistic Orange Cameroun-style sample export for testing.

Run: python scripts/make_sample.py
Writes samples/orange_cameroun_sample.xlsx
"""
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "orange_cameroun_sample.xlsx"
OUT.parent.mkdir(parents=True, exist_ok=True)

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Transactions"

# Title / metadata rows above the real table (typical of telco exports).
ws.append(["Orange Cameroun S.A."])
ws.append(["Mobile Money - Releve de transactions"])
ws.append(["Compte marchand:", "DHL EXPRESS CAMEROUN"])
ws.append(["Periode:", "01/05/2026 - 31/05/2026"])
ws.append([])  # blank spacer row

# Header row.
ws.append([
    "Date", "Transaction ID", "Type", "MSISDN", "Sender Name",
    "Amount (XAF)", "Fee (XAF)", "Balance (XAF)", "Reference", "Status",
])

# Transaction rows.
rows = [
    ["2026-05-03 09:14:22", "MP260503.0914.A12345", "Cash In", "237650111222",
     "", 150000, 0, 1150000, "INV-2026-0421", "Success"],
    ["2026-05-07 16:02:10", "MP260507.1602.B67890", "Payment", "237671333444",
     "", 75000, 250, 1224750, "INV-2026-0455", "Success"],
    ["2026-05-12 11:48:55", "MP260512.1148.C24680", "Payment", "237699555666",
     "", 320000, 800, 1543950, "INV-2026-0478", "Success"],
    ["2026-05-19 14:25:30", "MP260519.1425.D13579", "Cash In", "237650111222",
     "", 50000, 0, 1593950, "INV-2026-0501", "Success"],
]
for r in rows:
    ws.append(r)

wb.save(OUT)
print(f"Wrote {OUT}")
