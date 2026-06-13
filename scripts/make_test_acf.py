"""Generate two SPECIMEN ACF certificate PDFs for testing the vendor
certificate-tracking / inbox auto-read flow. Clearly marked as test
documents (not genuine DGI certificates). Same fictitious vendor + email,
differing issue dates so the newer one replaces the older when tested.

Run: python scripts/make_test_acf.py
"""
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

OUT = Path.home() / "Downloads"
EMAIL = "nekoutboma10@yahoo.com"
NAME = "ENTREPRISE DEMO TEST"
NIU = "M500000000017T"
SIGLE = "DEMO"

# (filename, ACF reference, issue date dd/mm/yyyy, human note)
CERTS = [
    ("ACF_TEST_expired_15-05-2026.pdf", "620264999001", "15/02/2026",
     "validity expired 15/05/2026 (last month)"),
    ("ACF_TEST_valid_20-07-2026.pdf", "620264999002", "20/04/2026",
     "validity runs to 20/07/2026 (July)"),
]


def build(path, reference, issue, note):
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    y = h - 25 * mm

    def line(text, size=10, dy=6 * mm, bold=False, colour=(0, 0, 0)):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.setFillColorRGB(*colour)
        c.drawString(22 * mm, y, text)
        y -= dy

    line("REPUBLIQUE DU CAMEROUN — REPUBLIC OF CAMEROON", 9, 5 * mm)
    line("Paix - Travail - Patrie", 8, 5 * mm)
    line("MINISTERE DES FINANCES — DIRECTION GENERALE DES IMPOTS", 9, 7 * mm)
    line("*** SPECIMEN — GENERATED FOR SYSTEM TESTING, "
         "NOT A GENUINE DGI DOCUMENT ***", 9, 8 * mm, bold=True,
         colour=(0.7, 0, 0))
    line(f"Centre d'impots : DGE IFU5      Reference ACF : {reference}", 10)
    line(f"YAOUNDE : {issue}", 10, 8 * mm)
    line("ATTESTATION DE CONFORMITE FISCALE", 13, 9 * mm, bold=True)
    line(f"Le contribuable : {NAME} de NIU:", 11)
    line(NIU, 11, 8 * mm, bold=True)
    line(f"Sigle : {SIGLE}      Telephone : 237696000000", 10)
    line("Regime : REEL      Adresse E-mail :", 10)
    line(f"Ville : YAOUNDE      {EMAIL}", 10, 8 * mm)
    line("Est a jour au regard de la declaration et du paiement des impots,",
         10, 5 * mm)
    line("droits et taxes vis-a-vis de l'Administration fiscale.", 10, 8 * mm)
    line("Cette Attestation est etablie a la demande du contribuable dans le",
         10, 5 * mm)
    line("cadre exclusif de la procedure suivante : Mise a jour du dossier.",
         10, 8 * mm)
    line(f"Elle est valable pour une duree de 3 mois a compter du {issue}.",
         11, 9 * mm, bold=True)
    line("It is valid for a period of 3 month(s) from " + issue + ".", 10, 8 * mm)
    line("En foi de quoi la presente attestation lui est delivree conformement",
         9, 5 * mm)
    line("aux dispositions de l'article L 94 bis du Livre des Procedures "
         "Fiscales.", 9, 10 * mm)
    line(f"[SPECIMEN — {note}]", 9, 6 * mm, colour=(0.4, 0.4, 0.4))
    c.showPage()
    c.save()


for fname, ref, issue, note in CERTS:
    p = OUT / fname
    build(p, ref, issue, note)
    print(f"wrote {p}  ({note})")
