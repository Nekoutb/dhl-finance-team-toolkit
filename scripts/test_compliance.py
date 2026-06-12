"""Invoice compliance engine — rules + batch flow with mocked AI/DGI."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.services import ai_ocr, compliance, dgi  # noqa: E402


def mention(present, value=""):
    return {"present": present, "value": value}


def full_extraction(**over):
    base = {
        "supplier_name": mention(True, "SOCIETE EXEMPLE SARL"),
        "supplier_niu": mention(True, "M012345678901A"),
        "supplier_address": mention(True, "BP 123 Douala"),
        "supplier_rccm": mention(True, "RC/DLA/2020/B/123"),
        "client_name": mention(True, "DHL EXPRESS CAMEROON"),
        "client_niu": mention(True, "M098765432109B"),
        "invoice_date": mention(True, "15/05/2026"),
        "invoice_number": mention(True, "FAC-2026-001"),
        "transaction_detail": mention(True, "Prestation gardiennage mai 2026"),
        "amount_ht": mention(True, "500 000"),
        "vat_rate_and_amount": mention(True, "19,25% — 96 250"),
        "amount_ttc": mention(True, "596 250"),
        "exoneration_mention": mention(False),
        "electronic_invoicing_marker": mention(True, "QR code DGI"),
        "is_foreign_supplier": False,
        "supplier_country": "Cameroon",
        "currency": "XAF",
        "total_ttc_numeric": 596250,
        "readability": "good",
    }
    base.update(over)
    return base


ACTIVE = {"status": "active", "raison_sociale": "SOCIETE EXEMPLE SARL"}
INACTIVE = {"status": "inactive", "message": "Contribuable inactif"}

# --- rules: clean invoice passes --------------------------------------------
r = compliance.evaluate(full_extraction(), ACTIVE)
assert r["verdict"] == "PASSED", r["failed"]
assert any("100,000" in v["item"] or "100 000" in v["item"].replace(",", " ")
           for v in r["review"]), "no-cash review item missing for 596,250"
print("ok: compliant invoice PASSES, with >=100k no-cash review flag")

# --- rules: missing client NIU + RCCM fails with remediation -----------------
r = compliance.evaluate(full_extraction(
    client_niu=mention(False), supplier_rccm=mention(False)), ACTIVE)
assert r["verdict"] == "FAILED"
items = [f["item"] for f in r["failed"]]
assert any("NIU du client" in i for i in items), items
assert any("RCCM" in i for i in items), items
assert all(f["action"] for f in r["failed"]), "remediation missing"
assert all(f["basis"].startswith("Art.") for f in r["failed"])
print("ok: missing mentions FAIL with legal basis + remediation")

# --- rules: inactive supplier fails -----------------------------------------
r = compliance.evaluate(full_extraction(), INACTIVE)
assert r["verdict"] == "FAILED"
assert any("ABSENT/INACTIF" in f["item"] for f in r["failed"])
print("ok: DGI-inactive supplier FAILS (Art. 8 bis (2) tiret 4)")

# --- rules: foreign supplier — form waived ----------------------------------
r = compliance.evaluate(full_extraction(
    is_foreign_supplier=True, supplier_niu=mention(False),
    supplier_rccm=mention(False), supplier_country="France",
    electronic_invoicing_marker=mention(False)), None)
assert r["verdict"] == "PASSED", r["failed"]
assert any("étranger" in v["item"] for v in r["review"])
assert any("paradis fiscal" in v["item"] for v in r["review"])
print("ok: foreign supplier — Art. 150(5) waived, tax-haven review raised")

# --- rules: exoneration replaces VAT mention --------------------------------
r = compliance.evaluate(full_extraction(
    vat_rate_and_amount=mention(False),
    exoneration_mention=mention(True, "Exonérée")), ACTIVE)
assert r["verdict"] == "PASSED", r["failed"]
print("ok: 'Exonérée' satisfies the VAT mention")

# --- rules: billed-to-DHL identity (v5.7) ------------------------------------
COMPANY = {"niu": "M098765432109B", "legal_name": "DHL EXPRESS CAMEROON SARL"}
# matching NIU + name (extraction says 'DHL EXPRESS CAMEROON' — SARL dropped)
r = compliance.evaluate(full_extraction(), ACTIVE, company=COMPANY)
assert r["verdict"] == "PASSED", r["failed"]
assert any("NIU client = NIU DHL" in p["item"] for p in r["passed"])
assert any("raison sociale configurée" in p["item"] for p in r["passed"])
# pass reveals no extra wording around the name
assert all(p["value"] == "" for p in r["passed"]
           if "raison sociale configurée" in p["item"])
print("ok: invoice billed to DHL's NIU + legal name PASSES (clean confirm)")

# v6: spacing-only differences must still PASS (exact match, ignore spacing)
r = compliance.evaluate(full_extraction(
    client_name=mention(True, "DHL   EXPRESS  CAMEROON   SARL")),
    ACTIVE, company=COMPANY)
assert r["verdict"] == "PASSED", r["failed"]
assert any("raison sociale configurée" in p["item"] for p in r["passed"])
print("ok: extra spacing in the destinator name still PASSES")

# wrong client NIU on the invoice
r = compliance.evaluate(full_extraction(
    client_niu=mention(True, "M111111111111Z")), ACTIVE, company=COMPANY)
assert r["verdict"] == "FAILED"
assert any("NIU client ≠ NIU DHL" in f["item"] for f in r["failed"])
print("ok: wrong client NIU FAILS against the configured DHL NIU")

# wrong client name on the invoice
r = compliance.evaluate(full_extraction(
    client_name=mention(True, "SOME OTHER COMPANY LTD")), ACTIVE,
    company=COMPANY)
assert r["verdict"] == "FAILED"
assert any("raison sociale DHL" in f["item"] for f in r["failed"])
print("ok: wrong billed-to name FAILS against the configured legal name")

# nothing configured -> checks skipped entirely
r = compliance.evaluate(full_extraction(
    client_niu=mention(True, "M111111111111Z")), ACTIVE, company={})
assert r["verdict"] == "PASSED", r["failed"]
print("ok: billed-to checks skipped when company identity not configured")

# --- batch flow over HTTP with mocked extractor + DGI ------------------------
client = TestClient(main.app)
real_extract = ai_ocr.extract_invoice_compliance
real_verify = dgi.verify_niu
real_cfgd = ai_ocr.is_configured
ai_ocr.is_configured = lambda cfg: True


def fake_extract(pdf_bytes, ai_cfg):
    if b"BAD" in pdf_bytes:
        return full_extraction(client_niu=mention(False))
    return full_extraction()


ai_ocr.extract_invoice_compliance = fake_extract
dgi.verify_niu = lambda niu, **kw: dict(ACTIVE)
# the worker thread reads config for the key — patch is_configured used in route
try:
    page = client.get("/tools/invoice-compliance")
    assert page.status_code == 200 and "compliance batch" in page.text.lower() \
        or "Compliance" in page.text
    files = [("invoices", (f"inv{i}.pdf", b"%PDF-1.4 GOOD", "application/pdf"))
             for i in range(3)]
    files.append(("invoices", ("bad.pdf", b"%PDF-1.4 BAD", "application/pdf")))
    resp = client.post("/tools/invoice-compliance/upload", files=files,
                       follow_redirects=False)
    assert resp.status_code == 303, (resp.status_code, resp.text[:300])
    token = resp.headers["location"].rstrip("/").split("/")[-1]

    for _ in range(40):
        payload = compliance.load_batch(token)
        if payload and payload["status"] == "done":
            break
        time.sleep(0.25)
    assert payload["status"] == "done", "batch did not finish"
    verdicts = [i["result"]["verdict"] for i in payload["invoices"]]
    assert verdicts.count("PASSED") == 3 and verdicts.count("FAILED") == 1, verdicts
    print("ok: batch of 4 processed in background (3 PASSED, 1 FAILED)")

    page = client.get(f"/tools/invoice-compliance/batch/{token}")
    assert page.status_code == 200
    assert "PASSED" in page.text and "FAILED" in page.text
    assert "NIU du client" in page.text
    x = client.get(f"/tools/invoice-compliance/batch/{token}/export")
    assert x.status_code == 200 and len(x.content) > 3000
    print("ok: results page + Excel compliance report")
finally:
    ai_ocr.extract_invoice_compliance = real_extract
    dgi.verify_niu = real_verify
    ai_ocr.is_configured = real_cfgd
    import shutil
    shutil.rmtree(compliance.BATCH_DIR / token, ignore_errors=True)

print("\nALL COMPLIANCE TESTS PASSED")
