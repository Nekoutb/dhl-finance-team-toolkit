"""Quick Account Statement — one-click customer statement (Excel) from the
latest AR transaction analysis on record.

Every invoice detail is pulled from that latest analysis: invoice reference,
issue date, due date, amount, the customer's payment terms (from the trial
balance, e.g. 014 = 14 days), the ageing in DAYS (today's date less the
invoice issue date) and the account number.

Multi-account rule: a statement covers more than one account if — and only
if — the customer NAME is the same across two (or more) separate account
numbers; those same-name accounts are combined automatically, each invoice
line carrying its own account number.
"""
import re
from datetime import date, datetime
from pathlib import Path


def _norm_name(name):
    return re.sub(r"\s+", " ", str(name or "").strip().upper())


def picker_entries(result):
    """One entry per account for the search box, flagging same-name siblings
    (which will be combined onto the statement automatically)."""
    customers = (result or {}).get("customers", [])
    by_name = {}
    for c in customers:
        by_name.setdefault(_norm_name(c["customer"]), []).append(c)
    entries = []
    for c in sorted(customers, key=lambda x: str(x["customer"])):
        siblings = by_name[_norm_name(c["customer"])]
        entries.append({"key": c["key"], "customer": c["customer"],
                        "total_ar": c["total_ar"],
                        "related": len(siblings) - 1})
    return entries


def accounts_for(result, key):
    """The selected account plus every other account with the SAME customer
    name (the multi-account rule). Returns [] when the key is unknown."""
    customers = (result or {}).get("customers", [])
    sel = next((c for c in customers if str(c["key"]) == str(key)), None)
    if not sel:
        return []
    name = _norm_name(sel["customer"])
    same = [c for c in customers if _norm_name(c["customer"]) == name]
    return [sel] + [c for c in same if str(c["key"]) != str(sel["key"])]


def _pdate(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def ageing_days(invoice_date, today=None):
    """Ageing in DAYS: today's date less the invoice issue date."""
    d = _pdate(invoice_date)
    if not d:
        return None
    return ((today or date.today()) - d).days


def statement_data(result, key):
    """Everything the renderer needs: the accounts covered, one row per open
    item (reference, issue/due dates, amount, payment terms from the TB,
    ageing in days, account number) and the totals — all pulled from the
    latest AR transaction analysis on record."""
    accounts = accounts_for(result, key)
    if not accounts:
        return None
    keys = [str(c["key"]) for c in accounts]
    today = date.today()
    rows = []
    for i in result.get("invoices", []):
        acct = str(i.get("account") or i.get("customer"))
        if acct not in keys:
            continue
        cust = next(c for c in accounts if str(c["key"]) == acct)
        rows.append({
            "reference": str(i.get("invoice_no") or i.get("reference") or "—"),
            "issue_date": str(i.get("invoice_date") or "")[:10],
            "due_date": str(i.get("due_date") or "")[:10],
            "amount": float(i.get("amount") or 0),
            "payment_terms": cust.get("payment_term") or "—",
            "ageing": ageing_days(i.get("invoice_date"), today),
            "account": acct,
            "kind": i.get("kind", "invoice"),
            "days_overdue": i.get("days_overdue", 0),
        })
    rows.sort(key=lambda r: (r["account"], r["issue_date"], r["reference"]))
    total = round(sum(r["amount"] for r in rows), 2)
    overdue = round(sum(r["amount"] for r in rows
                        if r["kind"] == "invoice" and r["days_overdue"] > 0), 2)
    return {
        "customer": accounts[0]["customer"],
        "accounts": [str(c["key"]) for c in accounts],
        "combined": len(accounts) > 1,
        "as_of": str(result.get("as_of", "")),
        "currency": (result.get("currencies") or ["XAF"])[0],
        "rows": rows, "total": total, "overdue": overdue,
        "generated": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


def build_statement_xlsx(out_path, data):
    """The statement as a clean Excel workbook — no logo, no narrative."""
    import xlsxwriter

    wb = xlsxwriter.Workbook(str(out_path))
    ws = wb.add_worksheet("Statement")
    f_title = wb.add_format({"bold": True, "font_size": 13,
                             "font_name": "Arial"})
    f_body = wb.add_format({"font_name": "Arial", "font_size": 10})
    f_bold = wb.add_format({"font_name": "Arial", "font_size": 10,
                            "bold": True})
    f_head = wb.add_format({"font_name": "Arial", "font_size": 10,
                            "bold": True, "bg_color": "#0b2545",
                            "font_color": "white", "border": 1})
    f_cell = wb.add_format({"font_name": "Arial", "font_size": 10,
                            "border": 1})
    f_num = wb.add_format({"font_name": "Arial", "font_size": 10, "border": 1,
                           "num_format": "#,##0"})
    f_tot = wb.add_format({"font_name": "Arial", "font_size": 10, "bold": True,
                           "border": 1, "num_format": "#,##0",
                           "bg_color": "#eef3fb"})

    widths = [26, 14, 14, 18, 12, 16, 16]
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)

    ws.write("A1", "Relevé de compte — Account statement", f_title)
    ws.write("A2", f"Client : {data['customer']}", f_bold)
    ws.write("A3", "Compte(s) : " + " · ".join(data["accounts"])
             + ("  (comptes combinés — même raison sociale)"
                if data["combined"] else ""), f_bold)
    ws.write("A4", f"Au {data['as_of']} · {data['currency']} · généré le "
                   f"{data['generated']}", f_body)

    header_row = 5
    headers = ["Référence facture", "Date d'émission", "Date d'échéance",
               "Termes de paiement", "Ageing (jours)", "N° de compte",
               f"Montant ({data['currency']})"]
    for c, h in enumerate(headers):
        ws.write(header_row, c, h, f_head)
    row = header_row + 1
    for r in data["rows"]:
        ws.write(row, 0, r["reference"], f_cell)
        ws.write(row, 1, r["issue_date"] or "—", f_cell)
        ws.write(row, 2, r["due_date"] or "—", f_cell)
        ws.write(row, 3, str(r["payment_terms"]), f_cell)
        if r["ageing"] is None:
            ws.write(row, 4, "—", f_cell)
        else:
            ws.write_number(row, 4, r["ageing"], f_cell)
        ws.write(row, 5, r["account"], f_cell)
        ws.write_number(row, 6, r["amount"], f_num)
        row += 1
    ws.write(row, 0, "TOTAL DÛ", f_tot)
    for c in range(1, 5):
        ws.write(row, c, "", f_tot)
    ws.write(row, 5, f"dont échu : {data['overdue']:,.0f}", f_tot)
    ws.write_number(row, 6, data["total"], f_tot)

    ws.freeze_panes(header_row + 1, 0)
    wb.close()
    return Path(out_path)
