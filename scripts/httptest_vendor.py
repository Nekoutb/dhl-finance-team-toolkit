"""Exercise the Vendor NIU verification flow (DGI calls mocked)."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.main import app
from app.services import dgi, vendors

ROOT = Path(__file__).resolve().parent.parent
client = TestClient(app)

_VJSON = ROOT / "data" / "vendors.json"
pre = _VJSON.read_text(encoding="utf-8") if _VJSON.exists() else None


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# Page loads
r = client.get("/tools/vendor-niu")
check("vendor page 200", r.status_code == 200)
check("links to DGI platform", "fiscalis.dgi.cm" in r.text)

# Paste vendors (semicolon, tab and comma separated + one junk line)
r = client.post("/tools/vendor-niu/paste", data={"vendors":
    "SOCIETE GENERALE DES TRAVAUX; M012345678901A; ANR-2026-0042\n"
    "CAMEROON SUPPLIES SARL\tP098765432109B\tANR-2026-0117\n"
    "DOUALA FREIGHT, M555666777888C\n"
    "this line has no taxpayer id\n"}, follow_redirects=True)
check("paste 200", r.status_code == 200)
check("3 added, junk skipped", "3 vendor(s) added" in r.text and "skipped" in r.text)
check("table shows pending pills", r.text.count("Not yet checked") >= 3)
check("certificate captured", "ANR-2026-0042" in r.text)

# Verify with the DGI client mocked (no real calls in tests)
def fake_verify_many(nius, delay=0.0, timeout=30):
    canned = {
        "M012345678901A": ("active", "SOCIETE GENERALE DES TRAVAUX SA", ""),
        "P098765432109B": ("inactive", "CAMEROON SUPPLIES", "Ce NIU est inactif"),
        "M555666777888C": ("not_found", "", "No taxpayer returned for this NIU"),
    }
    out = []
    for niu in nius:
        status, name, msg = canned.get(niu, ("error", "", "boom"))
        out.append({"niu": niu, "status": status, "message": msg,
                    "raison_sociale": name, "sigle": "", "cni_rc": "",
                    "activite": "COMMERCE", "regime": "REEL",
                    "centre": "CIME DOUALA",
                    "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
    return out

real_verify_many = dgi.verify_many
dgi.verify_many = fake_verify_many
try:
    r = client.post("/tools/vendor-niu/verify", data={"scope": "pending"},
                    follow_redirects=True)
finally:
    dgi.verify_many = real_verify_many

check("verify 200 with summary", r.status_code == 200 and "Checked 3 NIU(s)" in r.text)
check("ACTIVE pill shown", "ACTIVE" in r.text)
check("INACTIVE pill shown", "INACTIVE" in r.text)
check("NOT FOUND pill shown", "NOT FOUND" in r.text)
check("DGI name shown", "SOCIETE GENERALE DES TRAVAUX SA" in r.text)
check("inactive sorted first", r.text.index("INACTIVE") < r.text.index("ACTIVE"))

# Export to Excel
r = client.get("/tools/vendor-niu/export")
check("export downloads xlsx", r.status_code == 200 and r.content[:2] == b"PK")
check("export content-type", "spreadsheetml" in r.headers.get("content-type", ""))

# Remove a vendor
r = client.post("/tools/vendor-niu/delete", data={"niu": "M555666777888C"},
                follow_redirects=True)
check("delete works", "M555666777888C" not in r.text)

print("\nALL VENDOR NIU CHECKS PASSED")

# Restore registry
if pre is not None:
    _VJSON.write_text(pre, encoding="utf-8")
else:
    _VJSON.unlink(missing_ok=True)
print("registry restored")
