"""Adversarial verification of the CtP analyzer against the raw AR export.

Independent computation uses ONLY openpyxl — none of the app's parsing logic.
"""
import json
from datetime import date, datetime
from decimal import Decimal

import openpyxl

RAW = r"C:\Users\UltraBook 3.1\Desktop\AR DETAILS CM 0609.xlsx"
AS_OF = date(2026, 6, 9)

# ---------- Step 1: independent computation ----------
wb = openpyxl.load_workbook(RAW, read_only=True, data_only=True)
ws = wb["Sheet1"]

rows = ws.iter_rows(values_only=True)
header = [str(h).strip() if h is not None else "" for h in next(rows)]
idx = {h: i for i, h in enumerate(header)}
print("HEADER:", header)

col_amount = idx["Document Currency Value"]
col_customer = idx["Customer"]
col_name = idx["Customer Account: Name 1"]
col_due = idx["Net Due Date"]
col_docno = idx.get("Document Number")
col_doctype = idx.get("Document Type")

def is_blank(v):
    return v is None or (isinstance(v, str) and v.strip() == "")

def as_number(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if isinstance(v, str):
        s = v.strip().replace(",", "")
        if not s:
            return None
        try:
            return Decimal(s)
        except Exception:
            return None
    return None

def as_date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        s = v.strip()
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                pass
    return None

records = []  # (row_index, amount Decimal, customer, name, due, docno)
for rix, row in enumerate(rows, start=2):
    if row is None:
        continue
    amt = as_number(row[col_amount]) if col_amount < len(row) else None
    if amt is None:
        continue
    cust = row[col_customer] if col_customer < len(row) else None
    name = row[col_name] if col_name < len(row) else None
    due = row[col_due] if col_due < len(row) else None
    docno = row[col_docno] if (col_docno is not None and col_docno < len(row)) else None
    records.append((rix, amt, cust, name, due, docno))

n_numeric = len(records)
print("N_numeric =", n_numeric)

grand_sum = sum(r[1] for r in records)
# Candidate grand-total rows: Customer, Document Number, Net Due Date all blank
candidates = []
for r in records:
    rix, amt, cust, name, due, docno = r
    if is_blank(cust) and is_blank(due) and (col_docno is None or is_blank(docno)):
        # its value should equal the sum of all OTHER amounts
        others = grand_sum - amt
        if abs(others - amt) < Decimal("0.01"):
            candidates.append(r)
print("grand_total_candidates =", len(candidates))
for c in candidates:
    print("  row", c[0], "amount", c[1])

assert len(candidates) == 1, f"expected exactly 1 grand-total row, got {len(candidates)}"
total_row = candidates[0]
line_items = [r for r in records if r[0] != total_row[0]]

pos = [r for r in line_items if r[1] > 0]
neg = [r for r in line_items if r[1] < 0]
zero = [r for r in line_items if r[1] == 0]
my_total = sum(r[1] for r in line_items)

# distinct customers among line items (raw distinct values of Customer col, blanks as one bucket)
cust_values = set()
blank_cust_rows = 0
for r in line_items:
    cust = r[2]
    if is_blank(cust):
        blank_cust_rows += 1
    else:
        cust_values.add(str(cust).strip())
distinct_customers_nonblank = len(cust_values)
distinct_customers_with_blank_bucket = distinct_customers_nonblank + (1 if blank_cust_rows else 0)

overdue = Decimal("0")
overdue_count = 0
no_due_pos = 0
for r in pos:
    d = as_date(r[4])
    if d is None:
        no_due_pos += 1
        continue
    if d < AS_OF:
        overdue += r[1]
        overdue_count += 1

ind = {
    "n_numeric": n_numeric,
    "grand_total_row_amount": str(total_row[1]),
    "grand_total_row_index": total_row[0],
    "invoice_count_pos": len(pos),
    "credit_count_neg": len(neg),
    "zero_count": len(zero),
    "blank_customer_rows": blank_cust_rows,
    "distinct_customers_nonblank": distinct_customers_nonblank,
    "distinct_customers_with_blank_bucket": distinct_customers_with_blank_bucket,
    "total_all_lines": str(my_total),
    "overdue_total_pos_due_before_asof": str(overdue),
    "overdue_invoice_count": overdue_count,
    "positive_rows_without_parseable_due": no_due_pos,
}
print("\nINDEPENDENT:", json.dumps(ind, indent=2))

# ---------- Step 2: run the app analyzer ----------
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.tools import ongoing_ctp as ctp

r = ctp.analyze(RAW, as_of=date(2026, 6, 9), default_rank=60)
s = r["summary"]
print("\nAPP summary:", json.dumps(s, indent=2, default=str))
print("APP total_check:", json.dumps(r.get("total_check"), indent=2, default=str))

# ---------- Compare ----------
def close(a, b, tol=Decimal("0.01")):
    return abs(Decimal(str(a)) - Decimal(str(b))) <= tol

mismatches = []

def chk(label, app_val, ind_val, numeric=True):
    ok = close(app_val, ind_val) if numeric else (app_val == ind_val)
    print(f"{'OK ' if ok else 'MISMATCH'} {label}: app={app_val} independent={ind_val}")
    if not ok:
        mismatches.append((label, app_val, ind_val))

chk("invoice_count", s.get("invoice_count"), len(pos))
chk("credit_count", s.get("credit_count"), len(neg))
chk("total_ar", s.get("total_ar"), my_total)
chk("overdue_total", s.get("overdue_total"), overdue)

# customer_count: allow +/-1 around either blank-bucket interpretation
cc = s.get("customer_count")
if any(abs(cc - v) <= 1 for v in (distinct_customers_nonblank, distinct_customers_with_blank_bucket)):
    print(f"OK  customer_count: app={cc} independent={distinct_customers_nonblank} (nonblank) / "
          f"{distinct_customers_with_blank_bucket} (with blank bucket) — within tolerance 1")
else:
    mismatches.append(("customer_count", cc,
                       f"{distinct_customers_nonblank} nonblank / {distinct_customers_with_blank_bucket} with blank bucket"))
    print(f"MISMATCH customer_count: app={cc} independent={distinct_customers_nonblank}/"
          f"{distinct_customers_with_blank_bucket}")

tc = r.get("total_check") or {}
print("\ntotal_check detail:", tc)
if tc:
    ft = tc.get("file_total")
    if ft is not None:
        chk("total_check.file_total vs grand-total row", ft, total_row[1])
    ct = tc.get("computed_total", tc.get("line_total"))
    if ct is not None:
        chk("total_check.computed_total vs my line-item sum", ct, my_total)

print("\nMISMATCH_COUNT =", len(mismatches))
for m in mismatches:
    print("  ", m)
