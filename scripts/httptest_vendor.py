"""Exercise the Vendor NIU + tax-clearance-certificate controls
(DGI calls mocked; reminder emails exercised via the .eml fallback)."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from testutil import smtp_guard
smtp_guard()

from app.main import app
from app.services import dgi, vendors

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "vendor_list_sample.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
client = TestClient(app)

# Surgical cleanup: remove ONLY this test's NIUs (runs even if a check fails,
# and clears leftovers from any earlier crashed run before we start).
TEST_NIUS = ("M012345678901A", "P098765432109B", "M555666777888C",
             "P111222333444D", "M999888777666E")


def _scrub():
    for niu in TEST_NIUS:
        vendors.delete(niu)
    for p in (ROOT / "data" / "outputs").glob("reminder_*.eml"):
        p.unlink()
    for p in (ROOT / "data" / "uploads").glob("vendors_*"):
        p.unlink()


import atexit
atexit.register(_scrub)
_scrub()


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


def fake_verify_many(nius, delay=0.0, timeout=30):
    canned = {
        "M012345678901A": ("active", "SOCIETE GENERALE DES TRAVAUX SA", ""),
        "P098765432109B": ("active", "CAMEROON SUPPLIES", ""),
        "M555666777888C": ("active", "DOUALA FREIGHT", ""),
        "P111222333444D": ("inactive", "NORTHERN LOGISTICS",
                           "Ce NIU est inactif"),
    }
    out = []
    for niu in nius:
        status, name, msg = canned.get(niu, ("error", "", "boom"))
        out.append({"niu": niu, "status": status, "message": msg,
                    "raison_sociale": name, "sigle": "", "cni_rc": "",
                    "activite": "COMMERCE", "regime": "REEL",
                    "centre": "CIME DOUALA", "checked_at": "2026-06-11 09:00"})
    return out


# Page shows the controls explanation
r = client.get("/tools/vendor-niu")
check("vendor page 200", r.status_code == 200)
check("3-month rule stated", "3 months" in r.text)
check("excel upload form present", "Upload &amp; issue report" in r.text)

# Excel upload -> auto-verified report (DGI mocked)
real = dgi.verify_many
dgi.verify_many = fake_verify_many
try:
    with open(SAMPLE, "rb") as fh:
        r = client.post("/tools/vendor-niu/upload",
                        files={"file": ("vendor_list_sample.xlsx", fh, XLSX)},
                        follow_redirects=True)
finally:
    dgi.verify_many = real
check("upload issues report", r.status_code == 200
      and "4 vendor(s) added" in r.text and "Checked 4 NIU(s)" in r.text)

# Certificate states from the dynamic dates
check("VALID cert shown", ">VALID<" in r.text)
check("EXPIRING cert shown", "EXPIRES IN" in r.text)
check("EXPIRED cert shown", ">EXPIRED<" in r.text)
check("missing date shown", ">NO DATE<" in r.text)

# Combined OK-to-pay control
check("SGT is OK to pay (active + valid)", "OK TO PAY" in r.text)
check("blocked vendors flagged", "DO NOT PAY" in r.text)
check("expired-cert reason shown", "certificate expired" in r.text)
check("inactive-NIU reason shown", "INACTIVE on the tax platform" in r.text)
sgt = next(v for v in vendors.all_vendors() if v["niu"] == "M012345678901A")
check("SGT clearance ok server-side",
      vendors.payment_clearance(sgt)["ok"] is True)
dla = next(v for v in vendors.all_vendors() if v["niu"] == "M555666777888C")
check("DOUALA blocked despite ACTIVE NIU (expired cert)",
      vendors.payment_clearance(dla)["ok"] is False)

# Inline update: give NORTHERN an email + fresh cert date (still blocked: inactive)
fresh = (date.today() - timedelta(days=5)).isoformat()
r = client.post("/tools/vendor-niu/update",
                data={"niu": "P111222333444D", "cert_date": fresh,
                      "email": "admin@northern.example"}, follow_redirects=True)
nor = next(v for v in vendors.all_vendors() if v["niu"] == "P111222333444D")
check("update saved email + cert date", nor["email"] == "admin@northern.example"
      and nor["cert_date"] == fresh)
check("NORTHERN still blocked (inactive NIU)",
      vendors.payment_clearance(nor)["ok"] is False)

# Reminders: SMTP disabled -> .eml per non-compliant vendor WITH an email.
# Expect: CAMSUP (expiring), DOUALA (expired), NORTHERN (inactive) = 3 emails.
for p in (ROOT / "data" / "outputs").glob("reminder_*.eml"):
    p.unlink()
r = client.post("/tools/vendor-niu/remind", follow_redirects=True)
emls = sorted(p.name for p in (ROOT / "data" / "outputs").glob("reminder_*.eml"))
check("3 reminder emails created", len(emls) == 3
      and "reminder_M555666777888C.eml" in emls
      and "reminder_P111222333444D.eml" in emls
      and "reminder_P098765432109B.eml" in emls)
import email as _email
msg = _email.message_from_bytes(
    (ROOT / "data" / "outputs" / "reminder_M555666777888C.eml").read_bytes())
text = msg.get_payload(decode=True).decode("utf-8", "replace")
check("reminder states the payment block",
      "will NOT be paid until the situation is regularised" in text)
check("reminder states 3-month validity",
      "three" in text and "months" in text)
check("reminder names the expired certificate",
      "certificate expired" in text)
check("reminder addressed to the vendor",
      msg["To"] == "billing@dlafreight.example")

# Excel report includes the verdict columns
r = client.get("/tools/vendor-niu/export")
check("export downloads xlsx", r.status_code == 200 and r.content[:2] == b"PK")

# Paste path still works (now with date + email tokens)
r = client.post("/tools/vendor-niu/paste", data={"vendors":
    "ZETA BUILDERS; M999888777666E; ANR-2026-0200; "
    f"{(date.today() - timedelta(days=10)).isoformat()}; zeta@builders.example"},
    follow_redirects=True)
zeta = next(v for v in vendors.all_vendors() if v["niu"] == "M999888777666E")
check("paste captures date + email", zeta["email"] == "zeta@builders.example"
      and zeta["cert_date"] == (date.today() - timedelta(days=10)).isoformat())

print("\nALL VENDOR CONTROL CHECKS PASSED")
print("test vendors scrubbed (atexit)")
