"""Render selected transaction line(s) into a clean PDF receipt (reportlab)."""
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from .orange_statement import fmt_xaf

NAVY = colors.HexColor("#0b2545")
LIGHT = colors.HexColor("#f7f9fc")
ACCENT_BG = colors.HexColor("#eef3fb")
ACCENT_BORDER = colors.HexColor("#c3d4ef")
GREY = colors.HexColor("#888888")
GRID = colors.HexColor("#dddddd")


def _esc(value):
    return _xml_escape(str(value)) if value is not None else ""


def _fmt(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _header_image_flowables(header_images, max_width):
    """The document's own header (logos etc.) scaled to the page width."""
    flowables = []
    for img in header_images or []:
        try:
            reader = ImageReader(BytesIO(img["bytes"]))
            iw, ih = reader.getSize()
            width = min(max_width, iw * 0.75)   # px -> pt-ish, capped to page
            height = ih * (width / iw)
            if height > 70 * mm:                 # keep the header compact
                height = 70 * mm
                width = iw * (height / ih)
            flowables.append(Image(BytesIO(img["bytes"]),
                                   width=width, height=height, hAlign="LEFT"))
            flowables.append(Spacer(1, 4))
        except Exception:  # noqa: BLE001 - a bad image must not kill the receipt
            continue
    return flowables


def build_pdf(out_path, *, company, document_title, metadata, transactions,
              customer_name=None, customer_account=None, brand=None,
              header_images=None, layout="blocks"):
    """Write a PDF to ``out_path`` and return the path.

    ``transactions`` is a list of dicts (column name -> value); each becomes a
    label/value table. A header block (with the provider's logo mark when
    ``brand`` is given), optional customer banner, the source file's report
    header rows and a generated-on footer surround them.

    ``brand`` = {"bg": "#FF7900", "fg": "#FFFFFF", "word": "orange"} renders a
    logo block like the provider's mark at the top of the receipt.
    """
    base = getSampleStyleSheet()
    title_style = ParagraphStyle("t", parent=base["Title"], fontSize=18,
                                 textColor=NAVY, spaceAfter=2)
    sub_style = ParagraphStyle("s", parent=base["Normal"], fontSize=10,
                               textColor=colors.HexColor("#555555"))
    h2_style = ParagraphStyle("h2", parent=base["Heading2"], fontSize=12,
                              textColor=NAVY, spaceBefore=10, spaceAfter=6)
    small = ParagraphStyle("sm", parent=base["Normal"], fontSize=8, textColor=GREY)
    label = ParagraphStyle("lbl", parent=base["Normal"], fontSize=9,
                           textColor=colors.HexColor("#333333"))
    val = ParagraphStyle("val", parent=base["Normal"], fontSize=9)

    # Tabular layout with many columns reads better in landscape.
    columns = list(transactions[0].keys()) if transactions else []
    page = A4
    if layout == "table" and len(columns) > 6:
        page = landscape(A4)
    doc = SimpleDocTemplate(
        str(out_path), pagesize=page,
        topMargin=16 * mm, bottomMargin=16 * mm,
        leftMargin=14 * mm, rightMargin=14 * mm,
        title=document_title,
    )
    body_width = page[0] - 28 * mm

    story = []
    # The document's OWN header (logos etc.) comes first when available;
    # the drawn brand mark is only a fallback.
    doc_header = _header_image_flowables(header_images, body_width)
    if doc_header:
        story.extend(doc_header)
        story.append(Paragraph(_esc(document_title), title_style))
        if company:
            story.append(Paragraph(_esc(company), sub_style))
        story.append(Spacer(1, 8))
    elif brand:
        logo_style = ParagraphStyle(
            "logo", parent=base["Normal"], fontSize=13, leading=15,
            textColor=colors.HexColor(brand.get("fg", "#FFFFFF")),
            alignment=1, fontName="Helvetica-Bold")
        logo_cell = Paragraph(_esc(brand.get("word", "")), logo_style)
        head = Table(
            [[logo_cell,
              [Paragraph(_esc(document_title), title_style),
               Paragraph(_esc(company or ""), sub_style)]]],
            colWidths=[24 * mm, None], rowHeights=[20 * mm],
        )
        head.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), colors.HexColor(brand.get("bg", "#FF7900"))),
            ("VALIGN", (0, 0), (0, 0), "MIDDLE"),
            ("VALIGN", (1, 0), (1, 0), "MIDDLE"),
            ("LEFTPADDING", (1, 0), (1, 0), 10),
        ]))
        story.append(head)
        story.append(Spacer(1, 10))
    else:
        story.append(Paragraph(_esc(document_title), title_style))
        if company:
            story.append(Paragraph(_esc(company), sub_style))
        story.append(Spacer(1, 8))

    if customer_name or customer_account:
        rows = []
        if customer_name:
            rows.append([Paragraph("<b>Customer</b>", label),
                         Paragraph(_esc(customer_name), val)])
        if customer_account:
            rows.append([Paragraph("<b>Account number</b>", label),
                         Paragraph(_esc(customer_account), val)])
        banner = Table(rows, colWidths=[35 * mm, None])
        banner.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), ACCENT_BG),
            ("BOX", (0, 0), (-1, -1), 0.5, ACCENT_BORDER),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        story.append(banner)
        story.append(Spacer(1, 8))

    if metadata:
        story.append(Paragraph("<br/>".join(_esc(m) for m in metadata), small))
        story.append(Spacer(1, 10))

    if layout == "table" and transactions:
        # One row per transaction — fits many selections on a page.
        cell = ParagraphStyle("cell", parent=base["Normal"], fontSize=7.6,
                              leading=9.5)
        head_cell = ParagraphStyle("hcell", parent=cell,
                                   textColor=colors.white,
                                   fontName="Helvetica-Bold")
        rows = [[Paragraph(_esc(c), head_cell) for c in columns]]
        for tx in transactions:
            rows.append([Paragraph(_esc(_fmt(tx.get(c, ""))), cell)
                         for c in columns])
        grid = Table(rows, repeatRows=1, colWidths=[body_width / len(columns)] * len(columns))
        grid.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, GRID),
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, LIGHT]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(grid)
        story.append(Spacer(1, 6))
        story.append(Paragraph(
            f"{len(transactions)} transaction(s) selected", small))
    else:
        multiple = len(transactions) > 1
        for n, tx in enumerate(transactions, 1):
            if multiple:
                story.append(Paragraph(f"Transaction {n}", h2_style))
            data = [
                [Paragraph(f"<b>{_esc(col)}</b>", label),
                 Paragraph(_esc(_fmt(value)), val)]
                for col, value in tx.items()
            ]
            table = Table(data, colWidths=[60 * mm, None])
            table.setStyle(TableStyle([
                ("GRID", (0, 0), (-1, -1), 0.4, GRID),
                ("BACKGROUND", (0, 0), (0, -1), LIGHT),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]))
            story.append(table)
            story.append(Spacer(1, 8))

    story.append(Spacer(1, 12))
    story.append(Paragraph(
        f"Generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", small))
    doc.build(story)
    return Path(out_path)


# --- Orange Money branded receipt -------------------------------------------
_OM_ORANGE = colors.HexColor("#FF7900")
_OM_BLACK = colors.HexColor("#111418")
_OM_AMOUNT = colors.HexColor("#C2410C")
_OM_GREEN_BG = colors.HexColor("#E6F5EC")
_OM_GREEN_FG = colors.HexColor("#1C7A4D")


# ROUNDEDCORNERS is only handled by newer reportlab. TableStyle() does not
# validate command names (an unknown one would instead blow up later in
# Table.setStyle), so feature-test the Table handler up front and only emit the
# command when it is actually supported — otherwise degrade to square corners.
from reportlab.platypus.tables import Table as _RLTable  # noqa: E402
_HAS_ROUNDED = hasattr(_RLTable, "_setCornerRadii")


def _rounded(style_cmds, radius=8):
    """Add rounded corners when this reportlab supports them, else square."""
    if _HAS_ROUNDED:
        return TableStyle(style_cmds + [("ROUNDEDCORNERS", [radius] * 4)])
    return TableStyle(style_cmds)


def build_orange_receipt(out_path, *, meta, tx, customer=None):
    """One Orange-Money-branded receipt for a single successful collection.

    ``meta``  = parsed statement metadata (application, reseau, account,
                company, report_type, month_label).
    ``tx``    = {date, time, reference, correspondant, credit, status}.
    ``customer`` = {"name": .., "account": ..} or None — when present the
                receipt also shows the client name and AR account number.
    """
    base = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        topMargin=14 * mm, bottomMargin=16 * mm,
        leftMargin=16 * mm, rightMargin=16 * mm,
        title="Reçu de transaction",
    )
    body_width = A4[0] - 32 * mm
    story = []

    # --- black header bar with the inset orange logo block ------------------
    logo_style = ParagraphStyle("omlogo", parent=base["Normal"],
                                fontName="Helvetica-Bold", fontSize=12,
                                textColor=colors.white, alignment=1, leading=14)
    title_w = ParagraphStyle("omtitle", parent=base["Title"],
                             fontName="Helvetica-Bold", fontSize=17,
                             textColor=colors.white, spaceAfter=2, leading=20)
    sub_o = ParagraphStyle("omsub", parent=base["Normal"], fontSize=9.5,
                           textColor=_OM_ORANGE, leading=12)

    logo = Table([[Paragraph("orange", logo_style)]],
                 colWidths=[20 * mm], rowHeights=[13 * mm])
    logo.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _OM_ORANGE),
                              ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                              ("ALIGN", (0, 0), (-1, -1), "CENTER")]))
    title_cell = [Paragraph("Relevé de vos opérations", title_w),
                  Paragraph(f"Reçu de transaction — {_esc(meta.get('month_label', ''))}",
                            sub_o)]
    header = Table([[logo, title_cell]], colWidths=[34 * mm, None],
                   rowHeights=[22 * mm])
    header.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), _OM_BLACK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (0, 0), 7), ("RIGHTPADDING", (0, 0), (0, 0), 7),
        ("LEFTPADDING", (1, 0), (1, 0), 12),
    ]))
    story.append(header)
    story.append(HRFlowable(width="100%", thickness=4, color=_OM_ORANGE,
                            spaceBefore=0, spaceAfter=16))

    # --- statement info block -----------------------------------------------
    label_b = ParagraphStyle("omlb", parent=base["Normal"],
                             fontName="Helvetica-Bold", fontSize=9.5,
                             textColor=colors.HexColor("#222222"))
    val_n = ParagraphStyle("omvn", parent=base["Normal"], fontSize=9.5,
                           textColor=colors.HexColor("#333333"))
    account = meta.get("account", "")
    company = meta.get("company", "")
    compte_val = f"{account} — {company}" if company else account
    info = Table([
        [Paragraph("Application :", label_b),
         Paragraph(_esc(meta.get("application", "Orange Money")), val_n)],
        [Paragraph("Réseau :", label_b),
         Paragraph(_esc(meta.get("reseau", "Cameroun")), val_n)],
        [Paragraph("Compte Orange Money :", label_b),
         Paragraph(_esc(compte_val), val_n)],
        [Paragraph("Type de rapport :", label_b),
         Paragraph(_esc(meta.get("report_type", "Rapport mensuel")), val_n)],
    ], colWidths=[42 * mm, None])
    info.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                              ("TOPPADDING", (0, 0), (-1, -1), 2),
                              ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                              ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(info)
    story.append(Spacer(1, 16))

    # --- boxed transaction details ------------------------------------------
    box_head = ParagraphStyle("ombh", parent=base["Normal"],
                              fontName="Helvetica-Bold", fontSize=12,
                              textColor=_OM_ORANGE)
    dlabel = ParagraphStyle("omdl", parent=base["Normal"], fontSize=9.5,
                            textColor=colors.HexColor("#444444"))
    dval = ParagraphStyle("omdv", parent=base["Normal"],
                          fontName="Helvetica-Bold", fontSize=10,
                          textColor=colors.HexColor("#1a1a1a"))
    amount_style = ParagraphStyle("omamt", parent=base["Normal"],
                                  fontName="Helvetica-Bold", fontSize=15,
                                  textColor=_OM_AMOUNT)
    badge_style = ParagraphStyle("ombdg", parent=base["Normal"],
                                 fontName="Helvetica-Bold", fontSize=9.5,
                                 textColor=_OM_GREEN_FG, alignment=1)
    badge = Table([[Paragraph("Succès", badge_style)]],
                  colWidths=[22 * mm], rowHeights=[7 * mm])
    badge.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), _OM_GREEN_BG),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                               ("ALIGN", (0, 0), (-1, -1), "CENTER")]))

    rows = [
        [Paragraph("Date", dlabel), Paragraph(_esc(tx.get("date", "")), dval)],
        [Paragraph("Heure", dlabel), Paragraph(_esc(tx.get("time", "")), dval)],
        [Paragraph("Référence", dlabel),
         Paragraph(_esc(tx.get("reference", "")), dval)],
        [Paragraph("Correspondant", dlabel),
         Paragraph(_esc(tx.get("correspondant", "")), dval)],
    ]
    if customer and customer.get("name"):
        rows.append([Paragraph("Client", dlabel),
                     Paragraph(_esc(customer["name"]), dval)])
    if customer and customer.get("account"):
        rows.append([Paragraph("Compte client", dlabel),
                     Paragraph(_esc(customer["account"]), dval)])
    rows.append([Paragraph("Statut", dlabel), badge])
    rows.append([Paragraph("Montant crédité (XAF)", dlabel),
                 Paragraph(fmt_xaf(tx.get("credit", 0)), amount_style)])
    details = Table(rows, colWidths=[48 * mm, None])
    details.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                 ("TOPPADDING", (0, 0), (-1, -1), 6),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 0)]))

    inner = [Paragraph("Détails de la transaction", box_head),
             HRFlowable(width="100%", thickness=0.8, color=_OM_ORANGE,
                        spaceBefore=6, spaceAfter=8),
             details]
    box = Table([[inner]], colWidths=[body_width])
    box.setStyle(_rounded([
        ("BOX", (0, 0), (-1, -1), 1.2, _OM_ORANGE),
        ("TOPPADDING", (0, 0), (-1, -1), 12),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
        ("LEFTPADDING", (0, 0), (-1, -1), 16),
        ("RIGHTPADDING", (0, 0), (-1, -1), 16),
    ]))
    story.append(box)

    foot = ParagraphStyle("omft", parent=base["Normal"], fontSize=8,
                          textColor=GREY, alignment=1,
                          fontName="Helvetica-Oblique")
    story.append(Spacer(1, 24))
    story.append(Paragraph(
        "Document généré à partir du Relevé mensuel Orange Money — "
        f"Compte {_esc(account)}. Collections (crédits) uniquement.", foot))
    doc.build(story)
    return Path(out_path)
