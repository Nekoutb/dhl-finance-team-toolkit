"""Remittance portal logic: turn a daily GL into per-customer statements, and
record the customer's payment->invoice allocation.

Classification of each GL line into invoice vs payment:
  1. debit / credit columns if present (debit = invoice, credit = payment);
  2. else a document-type column matched against keywords;
  3. else the sign of the amount (negative = payment).
Tune FIELD_CANDIDATES / the keyword lists once a real GL export is seen.
"""
import re
from datetime import date, datetime

from ..services import excel_reader, remittance_store

TOOL_SLUG = "remittance-portal"

FIELD_CANDIDATES = {
    "customer_key": ["customer number", "customer no", "customer code", "customer id",
                     "account number", "account no", "account", "bp number", "compte"],
    "customer_name": ["customer name", "client name", "debtor", "customer", "client", "name"],
    # "reference" is mapped BEFORE doc_no so the customer-facing invoice
    # reference (column "Reference") is captured separately from the internal
    # document number.
    "reference": ["reference"],
    "doc_no": ["document number", "document no", "invoice number", "invoice no",
               "piece", "facture", "doc no", "number"],
    "doc_type": ["transaction type", "document type", "doc type", "posting type", "type"],
    "due_date": ["net due date", "due date", "net due", "due"],
    "date": ["posting date", "document date", "invoice date", "value date", "date"],
    "debit": ["debit amount", "debit", "dr"],
    "credit": ["credit amount", "credit", "cr"],
    "amount": ["amount in eur", "open amount", "amount", "balance", "montant", "value"],
    "currency": ["currency", "curr", "devise"],
}

INVOICE_KEYWORDS = ["invoice", "facture", "inv", "debit", "charge", "fee"]
PAYMENT_KEYWORDS = ["payment", "receipt", "credit", "bank", "cash", "remit",
                    "pmt", "transfer", "incoming"]


def _map_columns(header):
    used, mapping = set(), {}
    lowered = {h: h.lower() for h in header}
    for field, candidates in FIELD_CANDIDATES.items():
        for cand in candidates:
            match = next((h for h in header if h not in used and cand in lowered[h]), None)
            if match:
                mapping[field] = match
                used.add(match)
                break
    return mapping


def _to_amount(value):
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^0-9,.\-]", "", str(value))
    if s.count(",") == 1 and s.count(".") == 0:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _to_date_str(value):
    if value in (None, ""):
        return ""
    if isinstance(value, (datetime, date)):
        return value.date().isoformat() if isinstance(value, datetime) else value.isoformat()
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return s


def _classify(g):
    """Return (kind, magnitude) for a GL line."""
    debit, credit = _to_amount(g("debit")), _to_amount(g("credit"))
    if debit or credit:
        if debit and not credit:
            return "invoice", abs(debit)
        if credit and not debit:
            return "payment", abs(credit)
        net = debit - credit
        return ("invoice", net) if net >= 0 else ("payment", -net)

    amount = _to_amount(g("amount"))
    doc_type = str(g("doc_type") or "").lower()
    if doc_type:
        if any(k in doc_type for k in PAYMENT_KEYWORDS):
            return "payment", abs(amount)
        if any(k in doc_type for k in INVOICE_KEYWORDS):
            return "invoice", abs(amount)
    if amount < 0:
        return "payment", -amount
    return "invoice", amount


def build_statements_from_gl(path, source_name):
    """Parse a GL file, create a batch and one statement per customer."""
    parsed = excel_reader.read_transactions(path)
    mapping = _map_columns(parsed["header"])

    customers = {}
    for row in parsed["rows"]:
        data = row["data"]
        g = lambda f: data.get(mapping[f]) if f in mapping else None  # noqa: E731
        kind, amount = _classify(g)
        if not amount:
            continue
        key = str(g("customer_key") or g("customer_name") or "Unknown").strip()
        name = str(g("customer_name") or g("customer_key") or key).strip()
        bucket = customers.setdefault(key, {"name": name, "invoices": [], "payments": []})
        doc_no = str(g("doc_no") or "").strip()
        # The customer-facing identifier: the "Reference" column, falling back
        # to the internal document number when no reference exists.
        reference = str(g("reference") or "").strip() or doc_no
        item = {
            "reference": reference,
            "doc_no": doc_no,
            "doc_type": str(g("doc_type") or "").strip(),
            "date": _to_date_str(g("date")),
            "due_date": _to_date_str(g("due_date")),
            "amount": round(amount, 2),
            "currency": str(g("currency") or "").strip(),
        }
        bucket["invoices" if kind == "invoice" else "payments"].append(item)

    batch_id = remittance_store.new_token()[:12]
    created = remittance_store.now_iso()
    batch = {"id": batch_id, "created_at": created, "source": source_name, "customers": []}

    for key, bucket in customers.items():
        # Oldest to newest (undated lines last); ids assigned after sorting so
        # checkbox ids match the display order.
        by_age = lambda item: (item["date"] == "", item["date"])  # noqa: E731
        bucket["invoices"].sort(key=by_age)
        bucket["payments"].sort(key=by_age)
        for n, inv in enumerate(bucket["invoices"]):
            inv["id"] = n
        for n, pay in enumerate(bucket["payments"]):
            pay["id"] = n
        token = remittance_store.new_token()
        currency = next((i["currency"] for i in bucket["invoices"] + bucket["payments"]
                         if i["currency"]), "")
        statement = {
            "token": token,
            "batch_id": batch_id,
            "created_at": created,
            "customer_key": key,
            "customer_name": bucket["name"],
            "currency": currency,
            "invoices": bucket["invoices"],
            "payments": bucket["payments"],
            "status": "pending",
        }
        remittance_store.save_statement(token, statement)
        batch["customers"].append({
            "token": token,
            "customer_key": key,
            "customer_name": bucket["name"],
            "invoice_count": len(bucket["invoices"]),
            "payment_count": len(bucket["payments"]),
            "total_invoices": round(sum(i["amount"] for i in bucket["invoices"]), 2),
            "total_payments": round(sum(p["amount"] for p in bucket["payments"]), 2),
            "status": "pending",
        })

    # Highest open payments first (most cash waiting to be allocated on top).
    batch["customers"].sort(
        key=lambda c: (-c["total_payments"], c["customer_name"].lower()))
    remittance_store.save_batch(batch_id, batch)
    return batch


def apply_allocation(token, payment_ids, invoice_ids, confirmed_by, note,
                     contact=None):
    """Record the customer's allocation onto the statement; return the statement.

    ``contact`` = {role, email, phone} captured at confirmation (obligatory in
    the portal). It is stored on the allocation and remembered per customer.
    """
    statement = remittance_store.load_statement(token)
    if statement is None:
        return None
    inv_by = {i["id"]: i for i in statement["invoices"]}
    pay_by = {p["id"]: p for p in statement["payments"]}
    sel_inv = [inv_by[i] for i in invoice_ids if i in inv_by]
    sel_pay = [pay_by[p] for p in payment_ids if p in pay_by]

    total_inv = round(sum(i["amount"] for i in sel_inv), 2)
    total_pay = round(sum(p["amount"] for p in sel_pay), 2)
    difference = round(total_pay - total_inv, 2)

    contact = contact or {}
    statement["status"] = "allocated"
    statement["allocated_at"] = remittance_store.now_iso()
    statement["allocation"] = {
        "payments": sel_pay,
        "invoices": sel_inv,
        "total_payments": total_pay,
        "total_invoices": total_inv,
        "difference": difference,
        "confirmed_by": confirmed_by,
        "role": contact.get("role", ""),
        "email": contact.get("email", ""),
        "phone": contact.get("phone", ""),
        "note": note,
        "allocated_at": statement["allocated_at"],
    }
    remittance_store.save_statement(token, statement)

    # Remember the customer's contact details for future statements.
    if confirmed_by:
        remittance_store.set_contact(statement.get("customer_key"), {
            "company": statement.get("customer_name", ""),
            "name": confirmed_by,
            "role": contact.get("role", ""),
            "email": contact.get("email", ""),
            "phone": contact.get("phone", ""),
        })
    return statement
