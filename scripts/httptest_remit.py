"""Exercise the remittance portal web flow (finance upload + customer allocate)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from testutil import smtp_guard
smtp_guard()

from app.main import app
from app.services import remittance_store

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "gl_sample.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
client = TestClient(app)

# Snapshot settings/contacts so the test leaves no trace.
_SET = ROOT / "data" / "remittance" / "settings.json"
_CON = ROOT / "data" / "remittance" / "contacts.json"
pre_set = _SET.read_text(encoding="utf-8") if _SET.exists() else None
pre_con = _CON.read_text(encoding="utf-8") if _CON.exists() else None


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# Admin: configure recipient emails in settings
r = client.post("/tools/remittance-portal/settings",
                data={"recipients": "credit.cm@example.com\nfinance.cm@example.com\nnot-an-email"},
                follow_redirects=True)
check("settings save 200", r.status_code == 200)
check("settings keep 2 valid emails",
      remittance_store.load_settings()["recipients"] ==
      ["credit.cm@example.com", "finance.cm@example.com"])

# Finance: upload the GL
with open(SAMPLE, "rb") as fh:
    r = client.post("/tools/remittance-portal/upload",
                    files={"file": ("gl_sample.xlsx", fh, XLSX)})
check("upload -> batch 200", r.status_code == 200)
check("batch shows names AND accounts", "ACME Logistics" in r.text and "C1001" in r.text)
check("copy buttons present", "copy-btn" in r.text)
check("send-link form present", "send-link" in r.text)

# Sorted by open payments desc: Beta (8,000) before ACME (4,700) before Gamma (3,000)
order = [r.text.index(n) for n in ("Beta Trading", "ACME Logistics", "Gamma SARL")]
check("sorted by highest open payments", order == sorted(order))

batch_id = re.search(r"Batch ([0-9a-f]+)", r.text).group(1)
batch = remittance_store.load_batch(batch_id)
acme = next(c for c in batch["customers"] if c["customer_name"] == "ACME Logistics")
token = acme["token"]
print("   batch:", batch_id, "ACME token:", token[:8])

# Customer: statement page has the obligatory contact fields
r = client.get(f"/portal/{token}")
check("portal statement 200", r.status_code == 200)
check("contact fields present", all(f'name="{f}"' in r.text
      for f in ("confirmed_by", "role", "email", "phone")))
check("select-all controls present", 'id="allpay"' in r.text
      and 'id="allinv"' in r.text and 'id="selectAll"' in r.text)
check("customer-facing references shown (not internal doc nos)",
      "DLAR000252101" in r.text and "MTN-1744604901" in r.text)
# The file lists ACME's 2026-05-18 invoice first; display must be oldest first.
check("invoices sorted oldest to newest",
      r.text.index("2026-05-10") < r.text.index("2026-05-18"))

# Missing contact details -> rejected
r = client.post(f"/portal/{token}/allocate", data={
    "payment": "0", "invoice": ["0", "1"],
    "confirmed_by": "J. Doe", "role": "", "email": "", "phone": ""})
check("incomplete contact rejected (400)", r.status_code == 400 and "Please provide" in r.text)

# Valid confirmation with full contact details
CONFIRMER = f"Test Confirmer {token[:6]}"
r = client.post(f"/portal/{token}/allocate", data={
    "payment": "0", "invoice": ["0", "1"],
    "confirmed_by": CONFIRMER, "role": "Finance Manager",
    "email": "jane@acme.example", "phone": "+237650111222",
    "note": "May invoices"})
check("allocate -> done 200", r.status_code == 200)
check("done shows matched totals", "4,700" in r.text)

# Contact remembered
contact = remittance_store.get_contact("C1001")
check("contact stored in memory", contact and contact["email"] == "jane@acme.example"
      and contact["role"] == "Finance Manager" and contact["phone"] == "+237650111222")

# Contacts page lists it
r = client.get("/tools/remittance-portal/contacts")
check("contacts page lists contact", CONFIRMER in r.text and "jane@acme.example" in r.text)

# Reconciliation eml addressed to BOTH configured recipients
pdf = None
r = client.get(f"/portal/{token}")
pdf = re.search(r'/download/(Remittance_[^"\']+\.pdf)', r.text).group(1)
eml_name = pdf.replace(".pdf", ".eml")
re_ = client.get(f"/download/{eml_name}")
check("reconciliation eml exists", re_.status_code == 200)
check("eml addressed to settings recipients",
      b"credit.cm@example.com" in re_.content and b"finance.cm@example.com" in re_.content)
check("eml notes confirmer contact", b"Finance Manager" in re_.content)

# Batch page now prefills ACME's email and shows Allocated
r = client.get(f"/tools/remittance-portal/batch/{batch_id}")
check("batch prefills stored email", 'value="jane@acme.example"' in r.text)
check("batch shows Allocated pill", "Allocated" in r.text)

# Send-link for a pending customer (Beta) -> no SMTP -> .eml created + message
beta = next(c for c in batch["customers"] if c["customer_name"] == "Beta Trading")
r = client.post(f"/tools/remittance-portal/send-link/{beta['token']}",
                data={"email": "pay@beta.example"}, follow_redirects=True)
check("send-link 200 with message", r.status_code == 200 and "ready-to-send" in r.text)
link_eml = ROOT / "data" / "outputs" / f"link_{beta['customer_key']}.eml"
check("link eml created with portal url",
      link_eml.exists() and b"/portal/" in link_eml.read_bytes())

# PDF downloads
rp = client.get(f"/download/{pdf}")
check("pdf downloads", rp.status_code == 200 and rp.content[:4] == b"%PDF")

# Delete the batch through the new endpoint (statements go with it)
r = client.post(f"/tools/remittance-portal/batch/{batch_id}/delete",
                follow_redirects=True)
check("batch deleted via endpoint", r.status_code == 200
      and remittance_store.load_batch(batch_id) is None
      and remittance_store.load_statement(token) is None)

print("\nALL REMITTANCE HTTP CHECKS PASSED")

# ---- restore settings/contacts, drop test artifacts -------------------------
if pre_set is not None:
    _SET.write_text(pre_set, encoding="utf-8")
else:
    _SET.unlink(missing_ok=True)
if pre_con is not None:
    _CON.write_text(pre_con, encoding="utf-8")
else:
    _CON.unlink(missing_ok=True)
(ROOT / "data" / "remittance" / "batches" / f"{batch_id}.json").unlink(missing_ok=True)
for c in batch["customers"]:
    (ROOT / "data" / "remittance" / "statements" / f"{c['token']}.json").unlink(missing_ok=True)
print("test artifacts cleaned")
