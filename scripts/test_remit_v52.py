"""Remittance v5.2 — clickable email link, deposit confirmation, forward,
untreated-allocation flags. Uses the gl_sample with SMTP disabled (eml mode)."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()
subprocess.run([sys.executable, str(ROOT / "scripts" / "make_gl_sample.py")],
               check=True, capture_output=True)

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.services import remittance_store  # noqa: E402

client = TestClient(main.app)
GL = ROOT / "samples" / "gl_sample.xlsx"
created_batches = []
# data/ holds REAL settings — snapshot and restore exactly at the end.
orig_settings = dict(remittance_store.load_settings())

# --- batch 1 ------------------------------------------------------------------
with open(GL, "rb") as f:
    r = client.post("/tools/remittance-portal/upload",
                    files={"file": ("gl_sample.xlsx", f)})
assert r.status_code == 200, r.status_code
batch = remittance_store.list_batches()[0]
created_batches.append(batch["id"])
# Gamma SARL: 1 invoice 2500, 1 payment 3000 → excess 500
gamma = next(c for c in batch["customers"] if "Gamma" in c["customer_name"])
token = gamma["token"]

# --- 1. link email has the greeting + clickable HTML link ---------------------
r = client.post(f"/tools/remittance-portal/send-link/{token}",
                data={"email": "gamma@example.com"}, follow_redirects=False)
assert r.status_code == 303
eml = next(main.OUTPUT_DIR.glob(f"link_{gamma['customer_key']}*.eml"), None)
assert eml, "link .eml not created"
raw = eml.read_text(encoding="utf-8", errors="ignore")
assert f"Hi Gamma SARL (Account N" in raw, "greeting missing"
assert "text/html" in raw and "<a href=" in raw, "clickable HTML link missing"
eml.unlink()
print("ok: email greeting 'Hi <name> (Account N°…)' + clickable HTML link")

# --- 2. excess without deposit confirmation is refused ------------------------
form = {"payment": ["0"], "invoice": ["0"],
        "confirmed_by": "Test User", "role": "AP Manager",
        "email": "tu@gamma.cm", "phone": "+237650111222", "note": ""}
r = client.post(f"/portal/{token}/allocate", data=form)
assert r.status_code == 400 and "deposit" in r.text.lower(), r.status_code
print("ok: excess payment without deposit tick is refused (400)")

# --- 3. with deposit confirmation it records the deposit ----------------------
form["deposit_confirm"] = "on"
r = client.post(f"/portal/{token}/allocate", data=form)
assert r.status_code == 200, r.text[:300]
assert "deposit" in r.text.lower() and "future invoices" in r.text.lower()
stmt = remittance_store.load_statement(token)
assert stmt["allocation"]["deposit_confirmed"] is True
assert stmt["allocation"]["deposit_amount"] == 500.0
print("ok: deposit confirmed and recorded (500.00 offset against future invoices)")

# --- 4. portal page carries the approval/reconciliation notice ----------------
# (fetch another pending statement)
acme = next(c for c in batch["customers"] if "ACME" in c["customer_name"])
r = client.get(f"/portal/{acme['token']}")
assert "kept on record" in r.text and "reconciliation" in r.text.lower()
print("ok: allocation screen shows the approval/record notice")

# --- 5. forward the response (settings default + per-row email) ---------------
r = client.post("/tools/remittance-portal/settings",
                data={"recipients": "finance@dhl.com",
                      "forward_default": "cash.app@dhl.com"},
                follow_redirects=False)
assert r.status_code == 303
assert remittance_store.load_settings()["forward_default"] == "cash.app@dhl.com"
page = client.get("/tools/remittance-portal/allocations")
assert 'value="cash.app@dhl.com"' in page.text, "default not prefilled"
assert "Forward" in page.text or "Make email" in page.text
r = client.post(f"/tools/remittance-portal/forward/{token}",
                data={"email": "boss@dhl.com"}, follow_redirects=False)
assert r.status_code == 303 and "message=" in r.headers["location"]
fwd = next(main.OUTPUT_DIR.glob(f"fwd_allocation_{token[:8]}.eml"), None)
assert fwd, "forward .eml not created"
raw = fwd.read_text(encoding="utf-8", errors="ignore")
assert "Gamma SARL" in raw and "Deposit confirmed" in raw
fwd.unlink()
print("ok: response forwarded (admin default prefilled + per-send address)")

# --- 6. fresh upload flags the untreated allocation ---------------------------
with open(GL, "rb") as f:
    r = client.post("/tools/remittance-portal/upload",
                    files={"file": ("gl_sample.xlsx", f)})
assert r.status_code == 200
batch2 = remittance_store.list_batches()[0]
assert batch2["id"] != batch["id"]
created_batches.append(batch2["id"])
gamma2 = next(c for c in batch2["customers"] if "Gamma" in c["customer_name"])
assert "followup" in gamma2, "untreated allocation not flagged"
assert gamma2["followup"]["amount"] == 3000.0
page = client.get(f"/tools/remittance-portal/batch/{batch2['id']}")
assert "not yet treated" in page.text
print("ok: fresh data flags Gamma SARL — allocation received but not yet treated")

# --- 7. CtP dashboard section removed; bank renamed ---------------------------
dash_tpl = (ROOT / "app" / "templates" / "ongoing" / "dashboard.html").read_text(encoding="utf-8")
assert "Open AR appearing on the bank statement" not in dash_tpl
assert 'class="drop" open' not in dash_tpl
bank_page = client.get("/tools/bank-statements")
assert "Open AR Appearing on the Bank Statements" in bank_page.text
print("ok: CtP bank section removed, drops collapsed; bank tool renamed")

# --- cleanup: delete only this test's batches (cascades statements) ----------
for b in created_batches:
    client.post(f"/tools/remittance-portal/batch/{b}/delete")
# restore the real settings exactly as they were + drop the test contact
remittance_store.save_settings(orig_settings)
remittance_store.delete_contact(gamma["customer_key"])

print("\nALL REMITTANCE v5.2 TESTS PASSED")
