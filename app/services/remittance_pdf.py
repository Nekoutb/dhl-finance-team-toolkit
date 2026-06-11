"""Build the reconciliation / remittance-advice PDF from an allocated statement."""
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape as _esc

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

NAVY = colors.HexColor("#0b2545")
LIGHT = colors.HexColor("#f7f9fc")
GRID = colors.HexColor("#dddddd")
GREY = colors.HexColor("#888888")


def _money(value, currency=""):
    return f"{value:,.2f} {currency}".strip()


def _table(rows, col_widths):
    table = Table(rows, colWidths=col_widths)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, GRID),
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (-1, 0), (-1, -1), "RIGHT"),
    ]))
    return table


def build_remittance_pdf(out_path, statement, *, organization=""):
    alloc = statement["allocation"]
    currency = statement.get("currency", "")
    base = getSampleStyleSheet()
    title = ParagraphStyle("t", parent=base["Title"], fontSize=17, textColor=NAVY, spaceAfter=2)
    sub = ParagraphStyle("s", parent=base["Normal"], fontSize=10, textColor=colors.HexColor("#555"))
    h2 = ParagraphStyle("h2", parent=base["Heading2"], fontSize=11.5, textColor=NAVY,
                        spaceBefore=12, spaceAfter=5)
    small = ParagraphStyle("sm", parent=base["Normal"], fontSize=8, textColor=GREY)
    body = ParagraphStyle("b", parent=base["Normal"], fontSize=9.5)

    doc = SimpleDocTemplate(str(out_path), pagesize=A4, topMargin=20 * mm,
                            bottomMargin=18 * mm, leftMargin=18 * mm, rightMargin=18 * mm,
                            title="Payment Allocation — Remittance Advice")
    story = [Paragraph("Payment Allocation — Remittance Advice", title)]
    if organization:
        story.append(Paragraph(_esc(organization), sub))
    story.append(Spacer(1, 8))

    info = Table([
        [Paragraph("<b>Customer</b>", body), Paragraph(_esc(statement["customer_name"]), body),
         Paragraph("<b>Account</b>", body), Paragraph(_esc(statement.get("customer_key", "")), body)],
        [Paragraph("<b>Statement date</b>", body), Paragraph(_esc(statement["created_at"]), body),
         Paragraph("<b>Confirmed</b>", body), Paragraph(_esc(alloc["allocated_at"]), body)],
    ], colWidths=[28 * mm, None, 28 * mm, None])
    info.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), LIGHT),
        ("BOX", (0, 0), (-1, -1), 0.5, GRID),
        ("INNERGRID", (0, 0), (-1, -1), 0.3, GRID),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(info)

    def _ref(item):
        return item.get("reference") or item.get("doc_no", "")

    story.append(Paragraph("Payments allocated", h2))
    pay_rows = [["Date", "Reference", "Doc no", "Type", "Amount"]]
    for p in alloc["payments"]:
        pay_rows.append([p.get("date", ""), _ref(p), p.get("doc_no", ""),
                         p.get("doc_type", ""), _money(p["amount"], currency)])
    pay_rows.append(["", "", "", "Total payments",
                     _money(alloc["total_payments"], currency)])
    story.append(_table(pay_rows, [20 * mm, None, 26 * mm, 26 * mm, 30 * mm]))

    story.append(Paragraph("Invoices settled", h2))
    inv_rows = [["Date", "Invoice reference", "Doc no", "Due date", "Amount"]]
    for i in alloc["invoices"]:
        inv_rows.append([i.get("date", ""), _ref(i), i.get("doc_no", ""),
                         i.get("due_date", ""), _money(i["amount"], currency)])
    inv_rows.append(["", "", "", "Total invoices",
                     _money(alloc["total_invoices"], currency)])
    story.append(_table(inv_rows, [20 * mm, None, 26 * mm, 24 * mm, 30 * mm]))

    diff = alloc["difference"]
    if abs(diff) < 0.005:
        balance = "Fully matched — payments equal invoices settled."
    elif diff > 0:
        balance = f"Payment on account (unallocated credit): {_money(diff, currency)}."
    else:
        balance = f"Invoices not fully covered (short): {_money(-diff, currency)}."
    story.append(Spacer(1, 10))
    story.append(Paragraph(f"<b>Balance:</b> {_esc(balance)}", body))
    if alloc.get("note"):
        story.append(Paragraph(f"<b>Customer note:</b> {_esc(alloc['note'])}", body))

    story.append(Spacer(1, 14))
    confirmed_by = alloc.get("confirmed_by") or "the customer"
    who = f"Confirmed by <b>{_esc(confirmed_by)}</b>"
    if alloc.get("role"):
        who += f" ({_esc(alloc['role'])})"
    details = " · ".join(_esc(alloc[k]) for k in ("email", "phone") if alloc.get(k))
    if details:
        who += f" — {details}"
    story.append(Paragraph(
        f"{who} on {_esc(alloc['allocated_at'])} "
        "via the self-service remittance portal.", small))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · ref {statement['token'][:8]}", small))
    doc.build(story)
    return Path(out_path)
