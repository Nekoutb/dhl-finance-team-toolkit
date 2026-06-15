"""Generate a customer-level AR Trial Balance sample.

This mirrors the real DHL "AR Trial balance" file: one row per customer, with
the credit-hold flag in column Q ("Account stop/Open" — "X" = on credit hold,
blank = open) plus a "Critical Customers" column. The CtP Portal reads holds +
critical from THIS file.

Run: python scripts/make_ar_tb_customers_sample.py
"""
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "ar_trial_balance_customers_sample.xlsx"
OUT.parent.mkdir(parents=True, exist_ok=True)

# 18 columns: "Account stop/Open" lands in column Q (17th), "Critical" in R.
HEADER = ["Customer Number", "Customer Name", "Customer Segment", "Country",
          "Account Owner", "Payment Terms", "Credit Limit", "Region",
          "Currency", "Last Payment Date", "DSO", "Risk Rating", "Sales Rep",
          "Branch", "Opening Balance", "Closing Balance",
          "Account stop/Open", "Critical Customers"]

# (acct, name, segment, balance, account_stop(X/blank), critical)
CUSTOMERS = [
    ("1004000001", "ACME LOGISTICS",    "Enterprise",   4500000, "X", "Yes"),
    ("1004000002", "BETA TRADING SARL", "Field Sales",  670000,  "",  ""),
    ("1004000003", "GAMMA SARL",        "Telesales",    500000,  "X", "Yes"),
    ("1004000004", "DELTA CORP",        "New Customer", 2400000, "",  ""),
    ("1004000005", "EPSILON SA",        "Telesales",    75000,   "",  ""),
]

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "AR Trial Balance"
ws.append(HEADER)
total = 0
for acct, name, seg, bal, stop, crit in CUSTOMERS:
    ws.append([acct, name, seg, "Cameroon", "Aurelie Uguebou", "15 days",
               bal * 2, "Littoral", "XAF", "2026-05-30", 45, "B", "Rep 1",
               "Douala", bal, bal, stop, crit])
    total += bal
ws.append([""] * 15 + ["TOTAL", total, "", ""])

wb.save(OUT)
held = [c[1] for c in CUSTOMERS if c[4] == "X"]
print(f"Wrote {OUT} ({len(CUSTOMERS)} customers, total {total:,})")
print(f"  Account stop/Open = X (on hold): {', '.join(held)}")
