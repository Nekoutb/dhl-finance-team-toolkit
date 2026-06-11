"""Generate synthetic "Attestation de Conformité Fiscale" PDFs mirroring the
real DGI layout, for repeatable tests (dates relative to today):
  - acf_sample_valid.pdf   : issued 20 days ago  -> VALID
  - acf_sample_expired.pdf : issued 120 days ago -> EXPIRED

Run: python scripts/make_acf_samples.py
"""
from datetime import date, timedelta
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
SAMPLES.mkdir(parents=True, exist_ok=True)

styles = getSampleStyleSheet()
body = ParagraphStyle("b", parent=styles["Normal"], fontSize=11, leading=16)
title = ParagraphStyle("t", parent=styles["Title"], fontSize=16)


def build(out, reference, name, niu, issued, email, phone):
    d = issued.strftime("%d/%m/%Y")
    doc = SimpleDocTemplate(str(out), pagesize=A4, topMargin=18 * mm,
                            leftMargin=16 * mm, rightMargin=16 * mm)
    story = [
        Table([[Paragraph(f"Réference ACF : <b>{reference}</b>", body),
                Paragraph(f"YAOUNDE : <b>{d}</b>", body)]],
              colWidths=[None, 60 * mm]),
        Spacer(1, 10),
        Paragraph("ATTESTATION DE CONFORMITE FISCALE", title),
        Spacer(1, 14),
        Paragraph(f"Le contribuable : <b>{name}</b> de NIU:", body),
        Paragraph(f"<b>{niu}</b>", body),
        Spacer(1, 8),
        Paragraph("Sigle : null", body),
        Paragraph(f"Téléphone : {phone}", body),
        Paragraph("Regime : IMPOT GENERAL SYNTHETIQUE", body),
        Paragraph(f"Adresse E-mail : {email}", body),
        Paragraph("Ville : YAOUNDE", body),
        Spacer(1, 10),
        Paragraph("Est à jour au regard de la déclaration et du paiement des "
                  "impôts, droits et taxes vis-à-vis de l'Administration "
                  "fiscale.", body),
        Paragraph("Cette Attestation est établie à la demande du contribuable "
                  "dans le cadre exclusif de la procédure suivante : "
                  "Mise à jour du dossier fiscal.", body),
        Paragraph(f"Elle est valable pour une durée de 3 mois à compter du "
                  f"<b>{d}</b>.", body),
        Paragraph("En foi de quoi la présente attestation lui est délivrée "
                  "conformément aux dispositions de l'article L 94 bis du "
                  "Livre des Procédures Fiscales (LPF), pour servir et valoir "
                  "ce que de droit.", body),
    ]
    doc.build(story)
    print(f"Wrote {out} (issued {d})")


today = date.today()
build(SAMPLES / "acf_sample_valid.pdf", "620264000001",
      "ZETA BUILDERS SARL", "M111222333444A",
      today - timedelta(days=20), "compta@zeta.example", "237699000001")
build(SAMPLES / "acf_sample_expired.pdf", "620264000002",
      "OLD STONE TRADING", "P555666777888B",
      today - timedelta(days=120), "finance@oldstone.example", "237699000002")
