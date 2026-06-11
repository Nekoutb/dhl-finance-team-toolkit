"""Generate a multi-customer general-ledger sample (invoices + payments).

Mirrors the real export's identifier split: "Document No" is the internal
number; "Reference" is the customer-facing invoice/payment reference (what the
portal shows to customers). Real daily GLs hold 100+ customers; this keeps a
handful for testing. Run: python scripts/make_gl_sample.py
"""
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples" / "gl_sample.xlsx"
OUT.parent.mkdir(parents=True, exist_ok=True)

HEADER = ["Customer Name", "Customer No", "Document No", "Reference",
          "Document Type", "Document Date", "Due Date", "Debit", "Credit",
          "Currency"]

# (customer, account, doc_no, reference, type, doc_date, due_date, debit, credit)
# NB: ACME's newer invoice comes FIRST in the file on purpose — the portal must
# re-sort oldest-to-newest.
ROWS = [
    ("ACME Logistics", "C1001", "90000102", "DLAR000252102", "Invoice", "2026-05-18", "2026-06-02", 1500, 0),
    ("ACME Logistics", "C1001", "90000101", "DLAR000252101", "Invoice", "2026-05-10", "2026-05-25", 3200, 0),
    ("ACME Logistics", "C1001", "14000901", "MTN-1744604901", "Payment", "2026-06-05", "", 0, 4700),
    ("Beta Trading",   "C2002", "90000201", "DLAR000252201", "Invoice", "2026-05-05", "2026-05-20", 8000, 0),
    ("Beta Trading",   "C2002", "90000202", "DLAR000252202", "Invoice", "2026-05-19", "2026-06-03", 6000, 0),
    ("Beta Trading",   "C2002", "14000902", "MTN-1744604902", "Payment", "2026-06-04", "", 0, 8000),
    ("Gamma SARL",     "C3003", "90000301", "DLAR000252301", "Invoice", "2026-05-12", "2026-05-27", 2500, 0),
    ("Gamma SARL",     "C3003", "14000903", "MTN-1744604903", "Payment", "2026-06-06", "", 0, 3000),
    ("Delta Corp",     "C4004", "90000401", "DLAR000252401", "Invoice", "2026-05-02", "2026-05-17", 30000, 0),
]

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "GL"
ws.append(["DHL Express — Customer General Ledger (daily extract)"])
ws.append(["Extraction date:", "2026-06-09"])
ws.append([])
ws.append(HEADER)
for cust, acct, doc, ref, dtype, ddate, due, debit, credit in ROWS:
    ws.append([cust, acct, doc, ref, dtype, ddate, due, debit, credit, "XAF"])

wb.save(OUT)
print(f"Wrote {OUT} ({len(ROWS)} GL lines, with Reference column)")
