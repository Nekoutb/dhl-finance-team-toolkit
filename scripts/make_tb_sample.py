"""Generate an AR trial balance sample agreeing to ar_breakdown_sample.xlsx
(net total 7,960,000) — three AR control accounts plus a grand-total line.

Run: python scripts/make_tb_sample.py
"""
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "ar_trial_balance_sample.xlsx"
OUT.parent.mkdir(parents=True, exist_ok=True)

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "TB"
ws.append(["AR Trial Balance — as at 09/06/2026"])
ws.append([])
ws.append(["Account", "Account name", "Closing Balance"])
ROWS = [
    ("411100", "Trade receivables — domestic", 5460000),
    ("411200", "Trade receivables — government", 2100000),
    ("411900", "Trade receivables — sundry", 400000),
]
total = 0
for acct, name, bal in ROWS:
    ws.append([acct, name, bal])
    total += bal
ws.append(["", "TOTAL", total])
wb.save(OUT)
print(f"Wrote {OUT} (3 accounts, total {total:,})")
