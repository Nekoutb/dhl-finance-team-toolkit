"""ENEO vendor-invoice allocation — the "REPARTITION ENEO" sheet.

Each customer's electricity cost (the HT amount from the ENEO invoice) is
split across internal *functions* by a standing % allocation key. This module
parses/stores that key (from a Book1-style Excel), applies it to the HT
figures entered for a given invoice, and rebuilds the REPARTITION sheet for
on-screen view and Excel download.

Standard Cameroon VAT on electricity is 19.25% (the example invoice's
664,508 / 3,451,992 = 19.25%); the rate is editable per invoice.
"""
import json
import re
from threading import Lock

from ..config import DATA_DIR
from . import excel_reader

KEY_PATH = DATA_DIR / "allocation" / "eno_key.json"
DEFAULT_TVA_RATE = 19.25
_lock = Lock()


def _norm_id(value):
    s = str(value if value is not None else "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def parse_key_workbook(path):
    """Parse a REPARTITION-style Excel into the allocation key.

    Layout (Book1): NUMERO FACTURE | refclient CMS | FONCTIONS | % | MONTANT.
    The refclient cell starts a customer group; function rows carry a code and
    a %; the per-customer subtotal row (% = 1, no function) and the trailing
    TOTAL HT / TVA / TOTAL rows are ignored — only function + % are the key.
    """
    parsed = excel_reader.read_transactions(path)
    header = parsed["header"]
    # Map columns by position from the known layout, tolerant of header text.
    low = [str(h).lower() for h in header]

    def col(*frags, default=None):
        for i, h in enumerate(low):
            if any(f in h for f in frags):
                return header[i]
        return header[default] if default is not None and default < len(header) else None

    c_ref = col("refclient", "cms custom", "client") or (header[1] if len(header) > 1 else None)
    c_fn = col("fonction", "function") or (header[2] if len(header) > 2 else None)
    c_pct = col("%", "pourc") or (header[3] if len(header) > 3 else None)
    c_num = col("numero facture", "numéro facture", "facture") or (header[0] if header else None)

    key, order, current = {}, [], None
    for row in parsed["rows"]:
        d = row["data"]
        num = str(d.get(c_num) or "").strip() if c_num else ""
        if num and ("TOTAL" in num.upper() or "TVA" in num.upper()):
            break
        ref = _norm_id(d.get(c_ref)) if c_ref else ""
        if ref:
            current = ref
            if current not in key:
                key[current] = {"functions": []}
                order.append(current)
        fn = str(d.get(c_fn) or "").strip() if c_fn else ""
        pct_raw = d.get(c_pct) if c_pct else None
        if current and fn and pct_raw not in (None, ""):
            try:
                pct = float(str(pct_raw).replace(",", "."))
            except ValueError:
                continue
            key[current]["functions"].append({"function": fn, "pct": pct})
    return {"customers": key, "order": order}


def save_key(parsed, source=""):
    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(parsed)
    payload["source"] = source
    with _lock:
        KEY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    return payload


def load_key():
    if not KEY_PATH.exists():
        return None
    try:
        return json.loads(KEY_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def compute(key, ht_by_customer, tva_rate=DEFAULT_TVA_RATE):
    """Build the repartition from the key + HT per customer.

    ht_by_customer: {customer_id: ht_amount}. Returns customers (each with
    function rows function/pct/montant + subtotal) and totals (ht/tva/ttc).
    Rounding: function montants to 2dp; the subtotal is the true HT so the
    sheet always ties to the invoice.
    """
    customers, total_ht = [], 0.0
    order = key.get("order") or list(key.get("customers", {}).keys())
    for cid in order:
        ht = round(float(ht_by_customer.get(cid, 0) or 0), 2)
        funcs = key["customers"].get(cid, {}).get("functions", [])
        rows = [{"function": f["function"], "pct": f["pct"],
                 "montant": round(f["pct"] * ht, 2)} for f in funcs]
        customers.append({
            "customer_id": cid,
            "functions": rows,
            "pct_total": round(sum(f["pct"] for f in funcs), 4),
            "ht": ht,
        })
        total_ht += ht
    total_ht = round(total_ht, 2)
    tva = round(total_ht * tva_rate / 100, 2)
    return {
        "customers": customers,
        "tva_rate": tva_rate,
        "total_ht": total_ht,
        "tva": tva,
        "total_ttc": round(total_ht + tva, 2),
    }


def build_excel(out_path, computed, invoice_no=""):
    """Write the REPARTITION ENEO workbook replicating the Book1 layout."""
    import xlsxwriter

    wb = xlsxwriter.Workbook(str(out_path))
    ws = wb.add_worksheet("REPARTITION ENEO")
    title = wb.add_format({"bold": True, "font_size": 13})
    head = wb.add_format({"bold": True, "bg_color": "#0b2545",
                          "font_color": "white", "border": 1})
    cell = wb.add_format({"border": 1})
    pctf = wb.add_format({"border": 1, "num_format": "0%"})
    money = wb.add_format({"border": 1, "num_format": "#,##0.00"})
    boldmoney = wb.add_format({"border": 1, "num_format": "#,##0.00", "bold": True})
    boldcell = wb.add_format({"border": 1, "bold": True})

    ws.set_column(0, 0, 16)
    ws.set_column(1, 2, 22)
    ws.set_column(3, 3, 8)
    ws.set_column(4, 4, 16)
    ws.write(0, 1, "REPARTITION ENEO", title)
    for c, label in enumerate(["NUMERO FACTURE", "refclient CMS /CMS Customer",
                               "FONCTIONS", "%", "MONTANT"]):
        ws.write(1, c, label, head)

    r = 2
    for n, cust in enumerate(computed["customers"]):
        first = r
        for f in cust["functions"]:
            ws.write(r, 0, invoice_no if (n == 0 and r == first) else "", cell)
            ws.write(r, 1, cust["customer_id"] if r == first else "", cell)
            ws.write(r, 2, f["function"], cell)
            ws.write_number(r, 3, f["pct"], pctf)
            ws.write_number(r, 4, f["montant"], money)
            r += 1
        # per-customer subtotal (100% of that customer's HT)
        ws.write(r, 0, "", cell)
        ws.write(r, 1, "" if cust["functions"] else cust["customer_id"], cell)
        ws.write(r, 2, "", cell)
        ws.write_number(r, 3, 1, pctf)
        ws.write_number(r, 4, cust["ht"], boldmoney)
        r += 1

    ws.write(r, 0, "TOTAL HT", boldcell)
    ws.write_number(r, 4, computed["total_ht"], boldmoney); r += 1
    ws.write(r, 0, f"TVA ({computed['tva_rate']:g}%)", boldcell)
    ws.write_number(r, 4, computed["tva"], boldmoney); r += 1
    ws.write(r, 1, "TOTAL", boldcell)
    ws.write_number(r, 4, computed["total_ttc"], boldmoney)
    wb.close()
    return out_path
