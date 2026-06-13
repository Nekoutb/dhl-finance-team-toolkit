"""Certificate validity tracking — extend-on-replace, EN/FR reminders,
awaiting-response tracking (DGI mocked; emails via the .eml fallback)."""
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.services import vendors  # noqa: E402

client = TestClient(main.app)
NIU = "M012345670001Z"          # unique to this test


def _scrub():
    vendors.delete(NIU)
    for p in (ROOT / "data" / "outputs").glob(f"reminder_{NIU}_*.eml"):
        p.unlink()


def cert(niu, issue_iso, name="ACME SUPPLIES", email="ap@acme.cm"):
    return {"ok": True, "niu": niu, "issue_date": issue_iso, "reference": "",
            "name": name, "email": email, "date_mismatch": False}


_scrub()
try:
    today = date.today()
    older = (today - timedelta(days=20)).isoformat()
    newer = (today - timedelta(days=2)).isoformat()
    middle = (today - timedelta(days=10)).isoformat()

    # --- 1. first certificate is recorded + tracked --------------------------
    assert vendors.apply_certificate(cert(NIU, older)) == "created"
    v = next(x for x in vendors.all_vendors() if x["niu"] == NIU)
    ci = vendors.cert_info(v)
    assert ci["state"] in ("valid", "expiring"), ci
    assert ci["issued"] == older and ci["expires"]
    print("ok: uploaded certificate is stored and tracked (start/end computed)")

    # --- 2. per-vendor reminder EN then FR; awaiting-response flag -----------
    r = client.post("/tools/vendor-niu/remind-one",
                    data={"niu": NIU, "lang": "en"}, follow_redirects=False)
    assert r.status_code == 303 and "English" in r.headers["location"].replace("%20", " ")
    eml = ROOT / "data" / "outputs" / f"reminder_{NIU}_en.eml"
    assert eml.exists()
    raw = eml.read_text(encoding="utf-8", errors="ignore")
    assert "tax clearance certificate" in raw and "unable to process" in raw
    assert "REPLY to this email" in raw, "EN reminder must ask the vendor to reply"
    v = next(x for x in vendors.all_vendors() if x["niu"] == NIU)
    assert v.get("awaiting_update") and v.get("reminded_lang") == "en"
    assert NIU in [x["niu"] for x in vendors.reminded_awaiting()]
    print("ok: EN reminder sent (asks vendor to reply), 'awaiting update' set")

    r = client.post("/tools/vendor-niu/remind-one",
                    data={"niu": NIU, "lang": "fr"}, follow_redirects=False)
    fr = ROOT / "data" / "outputs" / f"reminder_{NIU}_fr.eml"
    assert fr.exists()
    raw = fr.read_text(encoding="utf-8", errors="ignore")
    assert "Attestation de Conformit" in raw and "paiement" in raw
    assert ("PONDRE" in raw or "pondre" in raw), "FR reminder must ask to reply"
    print("ok: FR reminder sent (asks to reply; bilingual content confirmed)")

    # --- 2b. amend the reminder email WITHOUT wiping the certificate date ----
    r = client.post("/tools/vendor-niu/email",
                    data={"niu": NIU, "email": "newcontact@beta.cm"},
                    follow_redirects=False)
    assert r.status_code == 303
    v = next(x for x in vendors.all_vendors() if x["niu"] == NIU)
    assert v["email"] == "newcontact@beta.cm", "email not amended"
    assert vendors.cert_info(v)["issued"] == older, \
        "amending the email must NOT touch the certificate date"
    r = client.post("/tools/vendor-niu/email",
                    data={"niu": NIU, "email": "not-an-email"},
                    follow_redirects=False)
    assert "valid email" in r.headers["location"].replace("+", " ").replace("%20", " ")
    print("ok: reminder email amendable (validated; cert date preserved)")

    # --- 3. an OLDER / same certificate is rejected (must extend validity) ---
    assert vendors.apply_certificate(cert(NIU, middle)) == "updated"  # middle > older, ok
    # now on file = middle; a still-older one must be refused
    assert vendors.apply_certificate(cert(NIU, older)) == "not_newer"
    v = next(x for x in vendors.all_vendors() if x["niu"] == NIU)
    assert vendors.cert_info(v)["issued"] == middle, "older cert must NOT replace"
    print("ok: a non-newer certificate is rejected, the one on file is kept")

    # --- 4. a NEWER certificate replaces + clears the awaiting flag ----------
    assert vendors.apply_certificate(cert(NIU, newer)) == "updated"
    v = next(x for x in vendors.all_vendors() if x["niu"] == NIU)
    assert vendors.cert_info(v)["issued"] == newer
    assert not v.get("awaiting_update") and v.get("responded_at")
    assert NIU not in [x["niu"] for x in vendors.reminded_awaiting()]
    print("ok: newer certificate replaces the old one and clears 'awaiting'")

    # --- 5. the tracking table renders with the required columns -------------
    page = client.get("/tools/vendor-niu")
    assert "Tax certificate validity tracking" in page.text
    for col in ("Validity start", "Validity end", "Days to expiry"):
        assert col in page.text, col
    assert 'name="lang" value="en"' in page.text and 'name="lang" value="fr"' in page.text
    print("ok: tracking table shows start/end/days-to-expiry + EN/FR remind buttons")

finally:
    _scrub()

print("\nALL CERTIFICATE-TRACKING TESTS PASSED")
