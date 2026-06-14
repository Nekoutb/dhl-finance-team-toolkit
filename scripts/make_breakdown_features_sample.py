"""Generate an AR transaction sample exercising the CtP analysis sections:

- a Legal-recovery customer (accounting clerk "Legal") — GAMMA, also Critical &
  on hold in the master;
- Critical customers (ACME + GAMMA per the master "Critical Customers" column);
- Duty invoices identified by a "D0…" id (ACME via document number, DELTA via
  the reference);
- aged items so over-60 / over-90 buckets are populated.

Pairs with ar_master_sample.xlsx. Reference as-of date: 2026-06-13.
Run: python scripts/make_breakdown_features_sample.py
"""
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "ar_breakdown_features_sample.xlsx"
OUT.parent.mkdir(parents=True, exist_ok=True)

HEADER = ["Company Code", "Customer", "Customer Account: Name 1", "Document Date",
          "Document Currency Value", "Reference", "Document Number",
          "Document Type", "Posting Date", "Document Currency Key",
          "Accounting Clerk", "Net Due Date"]

# (acct, name, doc_date, value, reference, doc_no, type, clerk, due_date)
ROWS = [
    # ACME — Critical; a normal invoice + a DUTY invoice (D0 document number).
    ("1004000001", "ACME LOGISTICS", "2026-01-15", 1500000, "DLAR0001", "90000101", "X4", "Aurelie Uguebou", "2026-02-01"),
    ("1004000001", "ACME LOGISTICS", "2026-02-01", 300000,  "DLAR0007", "D0000101", "DTY", "Aurelie Uguebou", "2026-02-08"),
    # GAMMA — with third-party Legal (clerk "Legal"); Critical + on hold in master.
    ("1004000003", "GAMMA SARL",     "2026-01-10", 500000,  "DLAR0006", "90000106", "X4", "Legal", "2026-01-25"),
    # DELTA — a DUTY invoice identified via the reference (D0…); not critical/legal.
    ("1004000004", "DELTA CORP",     "2026-05-01", 2400000, "D0000200", "90000200", "DTY", "Olga Goudji", "2026-05-16"),
    # BETA — ordinary recent invoice.
    ("1004000002", "BETA TRADING SARL", "2026-05-10", 670000, "DLAR0004", "90000004", "X4", "Olga Goudji", "2026-05-25"),
]

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "AR DETAILS CM"
ws.append(HEADER)
total = 0
for acct, name, ddate, val, ref, docno, dtype, clerk, due in ROWS:
    ws.append(["CM01", acct, name, ddate, val, ref, docno, dtype, ddate, "XAF", clerk, due])
    total += val
# Control-total line: amount populated, identity fields empty.
ws.append(["", "", "", "", total, "", "", "", "", "", "", ""])

wb.save(OUT)
print(f"Wrote {OUT} ({len(ROWS)} lines + control total {total:,})")
