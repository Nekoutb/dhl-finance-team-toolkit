"""Quick Account Statement — one-click customer statement from the latest
CtP analysis, produced as BOTH a DHL-branded PDF (with the credit-control
letter in French) and an Excel workbook.

Multi-account rule: a statement covers more than one account if — and only
if — the customer NAME is the same across two (or more) separate account
numbers; those same-name accounts are combined automatically, each invoice
line carrying its own account number.
"""
import re
from datetime import date, datetime
from pathlib import Path

from ..config import BASE_DIR

LOGO_PATH = BASE_DIR / "app" / "static" / "dhl_logo.png"

DHL_RED = "#D40511"
DHL_YELLOW = "#FFCC00"

# The credit-control introduction, exactly as supplied by the finance team.
INTRO_FR = [
    "Cher Client,",
    "Nous vous remercions de votre fidélité et apprécions la confiance que "
    "vous avez accordée à DHL.",
    "Cependant, suite à notre précédent rappel sur le retard de paiement de "
    "votre compte DHL, nous avons constaté que le paiement de nos services "
    "rendus est toujours en retard. Avec cette lettre, nous vous exhortons à "
    "nouveau à effectuer le paiement au plus tôt.",
    "Veuillez-vous référer à la liste ci-dessous des factures impayées pour "
    "un déblocage immédiat du paiement :",
    "Si vous avez entre temps régularisé votre situation, merci d'ignorer "
    "cette lettre.",
    "Si le montant reste impayé, nous sommes malheureusement contraints "
    "d'interrompre la facilité de crédit jusqu'à ce que le paiement intégral "
    "soit reçu. Si vous avez besoin d'une copie de facture, notre portail en "
    "ligne est accessible à l'adresse http://mybill.dhl.com, en utilisant "
    "votre numéro de compte comme indiqué dans le relevé de compte.",
    "Votre retour rapide nous aidera à mieux vous servir. Si vous avez des "
    "questions, n'hésitez pas à contacter notre équipe d'assistance par "
    "e-mail à camerouncredit@dhl.com",
]
SIGNATURE = ["Meilleures Salutations .",
             "DHL International Cameroun S.A.R.L.",
             "Service Des Comptes-Clients",
             "Email: camerouncredit@dhl.com"]


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
    # selected account first, then the other same-name accounts
    return [sel] + [c for c in same if str(c["key"]) != str(sel["key"])]


def _pdate(value):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def ageing_bucket(invoice_date, as_of):
    """Ageing based on the INVOICE date (per the team's spec)."""
    d, a = _pdate(invoice_date), _pdate(as_of)
    if not d or not a:
        return "—"
    days = (a - d).days
    for limit, label in ((30, "0-30"), (60, "31-60"), (90, "61-90"),
                         (120, "91-120"), (180, "121-180")):
        if days <= limit:
            return label
    return ">180"


def statement_data(result, key):
    """Everything both renderers need: the accounts covered, one row per open
    item (reference, issue/due dates, amount, payment terms, ageing, account
    number) and the totals."""
    accounts = accounts_for(result, key)
    if not accounts:
        return None
    keys = [str(c["key"]) for c in accounts]
    as_of = str(result.get("as_of", ""))
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
            "ageing": ageing_bucket(i.get("invoice_date"), as_of),
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
        "as_of": as_of,
        "currency": (result.get("currencies") or ["XAF"])[0],
        "rows": rows, "total": total, "overdue": overdue,
        "generated": datetime.now().strftime("%d/%m/%Y %H:%M"),
    }


# --------------------------------------------------------------------------- #
# PDF version (reportlab) — DHL letterhead + the French credit-control letter
# --------------------------------------------------------------------------- #
def build_statement_pdf(out_path, data):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (HRFlowable, Image, Paragraph,
                                    SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    from xml.sax.saxutils import escape as esc

    red = colors.HexColor(DHL_RED)
    yellow = colors.HexColor(DHL_YELLOW)
    base = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=base["Normal"], fontName="Helvetica",
                          fontSize=9.5, leading=13)
    bold = ParagraphStyle("bd", parent=body, fontName="Helvetica-Bold")
    title = ParagraphStyle("t", parent=base["Title"], fontSize=16,
                           textColor=red, spaceAfter=2, alignment=0)
    small = ParagraphStyle("sm", parent=body, fontSize=8,
                           textColor=colors.HexColor("#666666"))
    cell = ParagraphStyle("c", parent=body, fontSize=8.2, leading=10)
    cell_b = ParagraphStyle("cb", parent=cell, fontName="Helvetica-Bold")

    doc = SimpleDocTemplate(str(out_path), pagesize=A4,
                            topMargin=12 * mm, bottomMargin=14 * mm,
                            leftMargin=14 * mm, rightMargin=14 * mm,
                            title="Relevé de compte / Account statement")
    width = A4[0] - 28 * mm
    story = []

    # DHL letterhead: logo + title line, yellow rule under
    logo = Image(str(LOGO_PATH), width=52 * mm, height=13 * mm, hAlign="LEFT")
    head = Table([[logo,
                   [Paragraph("Relevé de compte — Account statement", title),
                    Paragraph(f"Au {esc(data['as_of'])} · "
                              f"{esc(data['currency'])}", body)]]],
                 colWidths=[60 * mm, None])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("LEFTPADDING", (0, 0), (0, 0), 0)]))
    story.append(head)
    story.append(HRFlowable(width="100%", thickness=3, color=yellow,
                            spaceBefore=4, spaceAfter=2))
    story.append(HRFlowable(width="100%", thickness=1.2, color=red,
                            spaceBefore=0, spaceAfter=10))

    story.append(Paragraph(f"<b>Client :</b> {esc(data['customer'])}", body))
    story.append(Paragraph(
        "<b>Compte(s) :</b> " + esc(" · ".join(data["accounts"]))
        + ("  (comptes combinés — même raison sociale)" if data["combined"]
           else ""), body))
    story.append(Spacer(1, 8))

    for para in INTRO_FR:
        story.append(Paragraph(esc(para), body))
        story.append(Spacer(1, 4))
    story.append(Spacer(1, 6))

    # Invoice table — every requested detail
    headers = ["Référence facture", "Date d'émission", "Date d'échéance",
               "Termes de paiement", "Ageing", "N° de compte",
               f"Montant ({data['currency']})"]
    tbl = [[Paragraph(f"<b>{h}</b>", cell_b) for h in headers]]
    for r in data["rows"]:
        tbl.append([
            Paragraph(esc(r["reference"]), cell),
            Paragraph(esc(r["issue_date"] or "—"), cell),
            Paragraph(esc(r["due_date"] or "—"), cell),
            Paragraph(esc(str(r["payment_terms"])), cell),
            Paragraph(esc(r["ageing"]), cell),
            Paragraph(esc(r["account"]), cell),
            Paragraph(f"{r['amount']:,.0f}", cell),
        ])
    tbl.append([Paragraph("<b>TOTAL DÛ</b>", cell_b), "", "", "", "",
                Paragraph(f"<b>dont échu : {data['overdue']:,.0f}</b>", cell_b),
                Paragraph(f"<b>{data['total']:,.0f}</b>", cell_b)])
    widths = [0.20, 0.12, 0.12, 0.14, 0.10, 0.14, 0.18]
    grid = Table(tbl, repeatRows=1, colWidths=[w * width for w in widths])
    grid.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dddddd")),
        ("BACKGROUND", (0, 0), (-1, 0), yellow),
        ("TEXTCOLOR", (0, 0), (-1, 0), red),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2),
         [colors.white, colors.HexColor("#FFF8DC")]),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#FDEBEC")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(grid)
    story.append(Spacer(1, 10))

    for line in SIGNATURE:
        story.append(Paragraph(esc(line), bold if line.startswith("DHL")
                               else body))
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        f"Généré le {esc(data['generated'])} — relevé basé sur l'analyse au "
        f"{esc(data['as_of'])}.", small))
    doc.build(story)
    return Path(out_path)


# --------------------------------------------------------------------------- #
# Excel version (xlsxwriter) — same content, workbook form
# --------------------------------------------------------------------------- #
def build_statement_xlsx(out_path, data):
    import xlsxwriter

    wb = xlsxwriter.Workbook(str(out_path))
    ws = wb.add_worksheet("Statement")
    f_title = wb.add_format({"bold": True, "font_size": 14, "font_name": "Arial",
                             "font_color": DHL_RED})
    f_body = wb.add_format({"font_name": "Arial", "font_size": 10,
                            "text_wrap": True, "valign": "top"})
    f_bold = wb.add_format({"font_name": "Arial", "font_size": 10, "bold": True})
    f_head = wb.add_format({"font_name": "Arial", "font_size": 10, "bold": True,
                            "bg_color": DHL_YELLOW, "font_color": DHL_RED,
                            "border": 1})
    f_cell = wb.add_format({"font_name": "Arial", "font_size": 10, "border": 1})
    f_num = wb.add_format({"font_name": "Arial", "font_size": 10, "border": 1,
                           "num_format": "#,##0"})
    f_tot = wb.add_format({"font_name": "Arial", "font_size": 10, "bold": True,
                           "border": 1, "num_format": "#,##0",
                           "bg_color": "#FDEBEC"})

    widths = [26, 14, 14, 18, 10, 16, 16]
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)

    ws.insert_image("A1", str(LOGO_PATH), {"x_scale": 0.35, "y_scale": 0.35})
    ws.write("A4", "Relevé de compte — Account statement", f_title)
    ws.write("A5", f"Client : {data['customer']}", f_bold)
    ws.write("A6", "Compte(s) : " + " · ".join(data["accounts"])
             + ("  (comptes combinés — même raison sociale)"
                if data["combined"] else ""), f_bold)
    ws.write("A7", f"Au {data['as_of']} · {data['currency']}", f_body)

    row = 8
    for para in INTRO_FR:
        ws.merge_range(row, 0, row, 6, para, f_body)
        ws.set_row(row, max(15, 13 * (len(para) // 110 + 1)))
        row += 1
    row += 1

    headers = ["Référence facture", "Date d'émission", "Date d'échéance",
               "Termes de paiement", "Ageing", "N° de compte",
               f"Montant ({data['currency']})"]
    header_row = row
    for c, h in enumerate(headers):
        ws.write(row, c, h, f_head)
    row += 1
    for r in data["rows"]:
        ws.write(row, 0, r["reference"], f_cell)
        ws.write(row, 1, r["issue_date"] or "—", f_cell)
        ws.write(row, 2, r["due_date"] or "—", f_cell)
        ws.write(row, 3, str(r["payment_terms"]), f_cell)
        ws.write(row, 4, r["ageing"], f_cell)
        ws.write(row, 5, r["account"], f_cell)
        ws.write_number(row, 6, r["amount"], f_num)
        row += 1
    ws.write(row, 0, "TOTAL DÛ", f_tot)
    for c in range(1, 5):
        ws.write(row, c, "", f_tot)
    ws.write(row, 5, f"dont échu : {data['overdue']:,.0f}", f_tot)
    ws.write_number(row, 6, data["total"], f_tot)
    row += 2

    for line in SIGNATURE:
        ws.write(row, 0, line, f_bold if line.startswith("DHL") else f_body)
        row += 1
    ws.write(row + 1, 0, f"Généré le {data['generated']}", f_body)

    ws.freeze_panes(header_row + 1, 0)
    wb.close()
    return Path(out_path)
