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

# Mirrors the REAL DHL header exactly: the NAME column ("Customer Name") comes
# BEFORE the account-number column (plainly "Customer", holding 1004…). This is
# the ambiguity that broke the hold register — "customer" is a substring of
# "Customer Name", so the account field must not greedily grab the name column.
# "Account Stop/Open" lands in column Q (17th), "Critical Customers" in R.
HEADER = ["Collector ID", "Customer Name", "Customer", "Customer Segment",
          "Country", "Account Owner", "Payment Terms", "Credit Limit", "Region",
          "Currency", "Last Payment Date", "DSO", "Risk Rating", "Sales Rep",
          "Branch", "Opening Balance", "Account Stop/Open", "Critical Customers"]

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
    ws.append(["AC", name, acct, seg, "Cameroon", "Aurelie Uguebou",
               "15 days", bal * 2, "Littoral", "XAF", "2026-05-30", 45, "B",
               "Rep 1", "Douala", bal, stop, crit])
    total += bal
# Total line: identity columns blank, balance populated -> skipped as a total.
total_row = [""] * len(HEADER)
total_row[15] = total                                  # Opening Balance column
ws.append(total_row)

wb.save(OUT)
held = [c[1] for c in CUSTOMERS if c[4] == "X"]
print(f"Wrote {OUT} ({len(CUSTOMERS)} customers, total {total:,})")
print(f"  Account stop/Open = X (on hold): {', '.join(held)}")
