"""Generate an AR customer MASTER file sample.

Pairs with ar_breakdown_sample.xlsx. Engineered cases:
- ACME    Enterprise,   balance matches,    NOT on hold (policy will demand hold)
- BETA    Field Sales,  balance matches,    not on hold
- GAMMA   Telesales,    balance MISMATCH (500,000 vs 390,000 in transactions),
                        ON HOLD (policy says ok -> review for release)
- DELTA   New Customer, balance matches,    not on hold
- EPSILON only in the master (no transaction lines) with a balance
Plus a total line (balance populated, identity fields empty) to be excluded.

Run: python scripts/make_master_sample.py
"""
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "ar_master_sample.xlsx"
OUT.parent.mkdir(parents=True, exist_ok=True)

HEADER = ["Customer Number", "Customer Name", "Customer Segment",
          "Receivable Balance", "Credit Hold Position", "Payment Terms",
          "Country", "Account Owner"]

ROWS = [
    ("1004000001", "ACME LOGISTICS",    "Enterprise",   4500000, "",  "15 days", "Cameroon", "Aurelie Uguebou"),
    ("1004000002", "BETA TRADING SARL", "Field Sales",  670000,  "",  "15 days", "Cameroon", "Olga Goudji"),
    ("1004000003", "GAMMA SARL",        "Telesales",    500000,  "X", "15 days", "Cameroon", "ALBERT DJONGA"),
    ("1004000004", "DELTA CORP",        "New Customer", 2400000, "",  "15 days", "Cameroon", "Aurelie Uguebou"),
    ("1004000005", "EPSILON SA",        "Telesales",    75000,   "",  "15 days", "Cameroon", "Olga Goudji"),
]

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Master"
ws.append(HEADER)
total = 0
for row in ROWS:
    ws.append(list(row))
    total += row[3]
# Total line: balance + nothing identifying.
ws.append(["", "", "", total, "", "", "", ""])

wb.save(OUT)
print(f"Wrote {OUT} ({len(ROWS)} customers + total row, total {total:,})")
