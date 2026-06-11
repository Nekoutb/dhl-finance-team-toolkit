"""Generate Mobile Money report samples: MTN-style Excel + Orange-style PDF.

Run: python scripts/make_momo_samples.py
"""
from pathlib import Path

import openpyxl
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
SAMPLES.mkdir(parents=True, exist_ok=True)

# ---- MTN Excel report (with an embedded logo image, like real exports) ------
XLS = SAMPLES / "mtn_momo_sample.xlsx"
LOGO = SAMPLES / "_mtn_logo_sample.png"

from PIL import Image as PILImage, ImageDraw  # noqa: E402

logo = PILImage.new("RGB", (180, 60), "#FFCC00")
draw = ImageDraw.Draw(logo)
draw.ellipse((6, 6, 174, 54), outline="#00304B", width=3)
draw.text((58, 22), "MTN", fill="#00304B")
logo.save(LOGO)

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Transactions"
from openpyxl.drawing.image import Image as XLImage  # noqa: E402
ws.add_image(XLImage(str(LOGO)), "A1")
ws.append(["MTN Cameroon Ltd"])
ws.append(["MTN Mobile Money - Transaction Report"])
ws.append(["Merchant:", "DHL EXPRESS CAMEROUN"])
ws.append(["Period:", "01/06/2026 - 09/06/2026"])
ws.append([])
ws.append(["Date", "Transaction ID", "From MSISDN", "Payer Name", "Amount (XAF)",
           "Fee", "Status", "External Ref"])
ROWS = [
    ["2026-06-02 10:11:01", "MM260602.1011.X1001", "237670111001", "", 250000, 500, "SUCCESSFUL", "INV-2026-0601"],
    ["2026-06-04 15:42:09", "MM260604.1542.X1002", "237671222002", "", 86000, 200, "SUCCESSFUL", "INV-2026-0617"],
    ["2026-06-05 09:05:44", "MM260605.0905.X1003", "237672333003", "", 1240000, 1500, "SUCCESSFUL", "INV-2026-0623"],
    ["2026-06-08 17:30:12", "MM260608.1730.X1004", "237670111001", "", 64000, 200, "SUCCESSFUL", "INV-2026-0640"],
]
for r in ROWS:
    ws.append(r)
wb.save(XLS)
print(f"Wrote {XLS}")

# ---- Orange PDF report ------------------------------------------------------
PDF = SAMPLES / "orange_momo_sample.pdf"
styles = getSampleStyleSheet()
doc = SimpleDocTemplate(str(PDF), pagesize=A4, topMargin=18 * mm,
                        leftMargin=14 * mm, rightMargin=14 * mm)
story = [
    Paragraph("Orange Cameroun S.A.", styles["Title"]),
    Paragraph("Orange Money — Releve de transactions", styles["Heading2"]),
    Paragraph("Compte marchand: DHL EXPRESS CAMEROUN", styles["Normal"]),
    Paragraph("Periode: 01/06/2026 - 09/06/2026", styles["Normal"]),
    Spacer(1, 8),
]
data = [["Date", "Transaction ID", "MSISDN", "Montant (XAF)", "Frais", "Statut", "Reference"]]
data += [
    ["2026-06-01 08:20:33", "MP260601.0820.A2001", "237650444111", "150000", "0", "Succes", "INV-2026-0598"],
    ["2026-06-03 11:47:21", "MP260603.1147.A2002", "237651555222", "98000", "250", "Succes", "INV-2026-0610"],
    ["2026-06-07 16:02:55", "MP260607.1602.A2003", "237650444111", "320000", "800", "Succes", "INV-2026-0633"],
]
table = Table(data, repeatRows=1)
table.setStyle(TableStyle([
    ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ff7900")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTSIZE", (0, 0), (-1, -1), 8),
]))
story.append(table)
doc.build(story)
print(f"Wrote {PDF}")
