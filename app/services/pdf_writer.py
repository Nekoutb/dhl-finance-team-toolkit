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
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

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
