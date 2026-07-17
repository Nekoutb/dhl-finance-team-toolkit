"""ENEO AI-read flow — mocked extractor, full HTTP round-trips."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.services import ai_ocr  # noqa: E402

client = TestClient(main.app)
FAKE_PDF = b"%PDF-1.4 fake scanned invoice"

# --- 1. Allocation page renders with the two-step UI -----------------------
r = client.get("/tools/vendor-invoice-allocation")
assert r.status_code == 200, r.status_code
assert "Step 2" in r.text and "refclient CMS" in r.text
print("ok: allocation page renders with Step 1/Step 2 UI")

# --- 2. Extract with no API key -> clear error ------------------------------
r = client.post("/tools/vendor-invoice-allocation/eno/extract",
                files={"invoice_pdf": ("inv.pdf", FAKE_PDF, "application/pdf")})
assert r.status_code == 200
assert "Document reading failed" in r.text
print("ok: missing key produces a clear error, not a 500")

# --- 3. Mocked AI read -> amounts prefilled ---------------------------------
real = ai_ocr.extract_eneo_invoice
def fake_extract(pdf_bytes, refs, ai_cfg):  # noqa: ANN001
    return {"invoice_no": "TEST-04-2026", "period": "AVRIL 2026",
            "lines": {"204691644": 1538672.0, "200744960": 482120.0,
                      "200211025": 1236807.0, "201998424": 69562.0,
                      "201824589": 124831.0},
            "total_ht": 3451992.0, "total_ttc": 4116500.46}
ai_ocr.extract_eneo_invoice = fake_extract
try:
    r = client.post("/tools/vendor-invoice-allocation/eno/extract",
                    files={"invoice_pdf": ("inv.pdf", FAKE_PDF, "application/pdf")})
    assert r.status_code == 200, r.status_code
    assert "Invoice read" in r.text, "prefill banner missing"
    assert "1538672" in r.text and "124831" in r.text, "prefilled values missing"
    assert "TEST-04-2026" in r.text
    # the staged-pdf token must be embedded for generate to pick up
    import re as _re
    m = _re.search(r'name="pdf_token" value="([0-9a-f]{12})"', r.text)
    assert m, "pdf_token not embedded"
    pdf_token = m.group(1)
    print("ok: AI extract prefills the per-CMS amounts + stages the PDF")

    # --- 4. Generate from prefill -> totals tie + evidence PDF attached -----
    form = {"invoice_no": "TEST-04-2026", "tva_rate": "19.25",
            "pdf_token": pdf_token,
            "ht_204691644": "1538672", "ht_200744960": "482120",
            "ht_200211025": "1236807", "ht_201998424": "69562",
            "ht_201824589": "124831"}
    r = client.post("/tools/vendor-invoice-allocation/eno/generate", data=form)
    assert r.status_code == 200
    assert "4,116,500" in r.text, "TTC total does not tie"
    m = _re.search(r"/eno/export/([0-9a-f]+)", r.text)
    assert m, "export link missing"
    token = m.group(1)
    from app.main import _ENO_DIR
    assert (_ENO_DIR / f"{token}.pdf").exists(), "staged PDF not attached"
    print("ok: generate ties to TTC 4,116,500 and attaches the staged PDF")
finally:
    ai_ocr.extract_eneo_invoice = real
    # scrub only this test's artifacts
    for p in list(_ENO_DIR.glob(f"{token}.*")):
        p.unlink()
    staged = Path(main.UPLOAD_DIR) / f"enoinv_{pdf_token}.pdf"
    if staged.exists():
        staged.unlink()

# --- 5. Settings page shows the document-reading section --------------------
r = client.get("/settings")
assert r.status_code == 200
assert "Document reading" in r.text and "AI document reading" not in r.text
print("ok: Settings shows the document-reading section (no AI mention)")

print("\nALL ENEO-AI TESTS PASSED")
