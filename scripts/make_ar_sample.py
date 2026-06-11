"""Generate an AR breakdown sample mirroring the real SAP export layout
("AR DETAILS CM"): customer number + name columns, Document Currency Value,
Net Due Date, DZ credit lines, and a trailing control-total line where the
amount is populated but the other fields are empty.

Run: python scripts/make_ar_sample.py   (as-of date for tests: 2026-06-09)
"""
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "ar_breakdown_sample.xlsx"
OUT.parent.mkdir(parents=True, exist_ok=True)

HEADER = ["Company Code", "Customer", "Customer Account: Name 1",
          "Document Date", "Document Currency Value", "Reference",
          "Document Number", "Document Type", "Posting Date",
          "Document Currency Key", "Accounting Clerk", "Net Due Date"]

# (customer no, name, doc date, value, reference, doc no, type, due date)
# Days overdue vs 2026-06-09 chosen to hit each treatment step.
ROWS = [
    # ACME — heavily overdue -> write-off / agency / stop steps
    ("1004000001", "ACME LOGISTICS",   "2025-09-01", 1500000, "DLAR0001", "90000001", "X4", "2026-01-15"),  # 145d -> write-off
    ("1004000001", "ACME LOGISTICS",   "2026-03-01", 2200000, "DLAR0002", "90000002", "X4", "2026-04-20"),  # 50d -> agency
    ("1004000001", "ACME LOGISTICS",   "2026-04-20", 800000,  "DLAR0003", "90000003", "X5", "2026-05-12"),  # 28d -> stop credit
    # BETA — mid overdue -> call step + automated dunning
    ("1004000002", "BETA TRADING SARL", "2026-04-25", 950000, "DLAR0004", "90000004", "X4", "2026-05-16"),  # 24d -> call
    ("1004000002", "BETA TRADING SARL", "2026-05-10", 120000, "DLAR0005", "90000005", "X5", "2026-05-31"),  # 9d  -> dunning (auto)
    # GAMMA — call due but below the XAF value threshold (suppressed)
    ("1004000003", "GAMMA SARL",        "2026-04-26", 90000,  "DLAR0006", "90000006", "X4", "2026-05-16"),  # 24d -> call, suppressed
    ("1004000003", "GAMMA SARL",        "2026-05-20", 300000, "DLAR0007", "90000007", "X5", "2026-06-04"),  # 5d -> dunning (auto)
    # DELTA — within terms
    ("1004000004", "DELTA CORP",        "2026-06-01", 2500000, "DLAR0008", "90000008", "X4", "2026-06-20"),  # not due
    # Credits / payments on account (DZ, negative)
    ("1004000002", "BETA TRADING SARL", "2026-06-05", -400000, "MTN-001", "1400000001", "DZ", "2026-06-05"),
    ("1004000004", "DELTA CORP",        "2026-06-06", -100000, "MTN-002", "1400000002", "DZ", "2026-06-06"),
]

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Sheet1"
ws.append(HEADER)
total = 0
for cust, name, ddate, value, ref, docno, dtype, due in ROWS:
    clerk = "ALBERT DJONGA" if dtype == "DZ" else "Aurelie Uguebou"
    ws.append(["CM01", cust, name, ddate, value, ref, docno, dtype, ddate,
               "XAF", clerk, due])
    total += value
# Control-total line: amount + currency only, everything else empty.
ws.append(["", "", "", "", total, "", "", "", "", "XAF", "", ""])

wb.save(OUT)
print(f"Wrote {OUT} ({len(ROWS)} lines + total row, control total {total:,})")
