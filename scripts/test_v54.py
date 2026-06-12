"""v5.4 — brand chips & dropdowns & email-me (allocation), admin-only
settings/onboarding, welcome banner, compliance NIU/DGI fields."""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.config import CONFIG_PATH  # noqa: E402
from app.services import auth, compliance  # noqa: E402

client = TestClient(main.app)
cfg_snapshot = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None

# --- 1. allocation page: brand chips + collapsible workspaces -----------------
r = client.get("/tools/vendor-invoice-allocation")
assert r.status_code == 200
assert r.text.count("brand-chip") == 3, "three vendor logo chips expected"
assert 'aria-label="eneo"' in r.text and 'aria-label="MTN"' in r.text \
    and 'aria-label="Orange"' in r.text
assert "Open the ENEO allocation workspace" in r.text
assert "Open the MTN allocation workspace" in r.text
assert "Open the Orange allocation workspace" in r.text
print("ok: 3 brand chips + 3 collapsible entity workspaces")

# --- 2. generate then email-me (smtp off -> .eml) ------------------------------
form = {"invoice_no": "V54-TEST", "tva_rate": "19.25",
        "ht_204691644": "1000000"}
r = client.post("/tools/vendor-invoice-allocation/eno/generate", data=form)
assert r.status_code == 200
m = re.search(r"/eno/email/([0-9a-f]+)", r.text)
assert m, "email-me form missing from result"
token = m.group(1)
r = client.post(f"/tools/vendor-invoice-allocation/eno/email/{token}",
                data={"email": "me@dhl.com"})
assert r.status_code == 200 and "ready-to-send email created" in r.text
eml = main.OUTPUT_DIR / f"repartition_eno_{token[:8]}.eml"
assert eml.exists() and b"V54-TEST" in eml.read_bytes()
print("ok: répartition emailed to self (.eml fallback, Excel attached)")
eml.unlink()
from app.main import _ENO_DIR  # noqa: E402
for p in _ENO_DIR.glob(f"{token}.*"):
    p.unlink()

# --- 3. compliance result carries the taxpayer ID + DGI status ----------------
def mention(present, value=""):
    return {"present": present, "value": value}
extraction = {k: mention(True, "x") for k in
              ("supplier_name", "supplier_address", "supplier_rccm",
               "client_name", "client_niu", "invoice_date", "invoice_number",
               "transaction_detail", "amount_ht", "vat_rate_and_amount",
               "amount_ttc", "electronic_invoicing_marker")}
extraction.update(supplier_niu=mention(True, "M012345678901A"),
                  exoneration_mention=mention(False),
                  is_foreign_supplier=False, supplier_country="Cameroon",
                  currency="XAF", total_ttc_numeric=50000, readability="good")
res = compliance.evaluate(extraction, {"status": "active",
                                       "raison_sociale": "ACME SARL"})
assert res["supplier_niu"] == "M012345678901A"
assert res["dgi_status"] == "active" and res["dgi_name"] == "ACME SARL"
res2 = compliance.evaluate(dict(extraction, is_foreign_supplier=True), None)
assert res2["dgi_status"] == "n/a (foreign)"
print("ok: compliance result exposes taxpayer ID + DGI Fiscalis status")

# --- 4. admin gating + welcome banner ------------------------------------------
try:
    assert auth.add_user("admin.test", "secret77", secure_cookies=False,
                         admin=True)
    assert auth.add_user("employee.test", "secret88", admin=False)

    # employee: no Admin nav, settings sections hidden, POSTs blocked
    r = client.post("/login", data={"username": "employee.test",
                                    "password": "secret88", "next": "/"},
                    follow_redirects=False)
    assert r.status_code == 303 and "welcome=1" in r.headers["location"]
    emp_cookie = {auth.COOKIE: r.cookies.get(auth.COOKIE)}
    r = client.get("/?welcome=1", cookies=emp_cookie)
    assert "Welcome EMPLOYEE.TEST" in r.text, "welcome banner missing"
    assert "Users &amp; settings" not in r.text, "employee sees Admin nav"
    r = client.get("/settings", cookies=emp_cookie)
    assert "signed in as an" in r.text and "employee" in r.text
    assert "Onboard a new user" not in r.text
    assert "SMTP host" not in r.text and "Anthropic API key" not in r.text
    r = client.post("/settings/users/add",
                    data={"username": "x", "password": "longenough"},
                    cookies=emp_cookie, follow_redirects=False)
    assert r.status_code == 303 and "Administrator access required" in \
        r.headers["location"].replace("%20", " ")
    print("ok: employee — welcome banner, no Admin nav, settings locked")

    # admin: full settings + role pills
    r = client.post("/login", data={"username": "admin.test",
                                    "password": "secret77", "next": "/"},
                    follow_redirects=False)
    adm_cookie = {auth.COOKIE: r.cookies.get(auth.COOKIE)}
    r = client.get("/", cookies=adm_cookie)
    assert "Users &amp; settings" in r.text
    r = client.get("/settings", cookies=adm_cookie)
    assert "Onboard a new user" in r.text and "Administrator" in r.text
    assert "employee</span>" in r.text  # role pill for employee.test
    print("ok: admin — sees onboarding, settings and role pills")
finally:
    if cfg_snapshot is None:
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
    else:
        CONFIG_PATH.write_text(cfg_snapshot, encoding="utf-8")

print("\nALL v5.4 TESTS PASSED")
