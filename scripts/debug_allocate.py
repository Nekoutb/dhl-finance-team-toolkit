"""Reproduce the exporter's allocate POST and inspect what the server received."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.main import app
from app.services import remittance_store

ROOT = Path(__file__).resolve().parent.parent
GL_SAMPLE = ROOT / "samples" / "gl_sample.xlsx"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

client = TestClient(app)

r = client.post("/tools/remittance-portal/upload",
                files={"file": (GL_SAMPLE.name, GL_SAMPLE.open("rb"), XLSX_MIME)})
batch_id = re.search(r"Batch ([0-9a-f]+)", r.text).group(1)
batch = remittance_store.load_batch(batch_id)
cust = next(c for c in batch["customers"] if c["customer_name"] == "ACME Logistics")
token = cust["token"]
print("token:", token[:8])

stmt = remittance_store.load_statement(token)
print("payments ids:", [p["id"] for p in stmt["payments"]])
print("invoices ids:", [i["id"] for i in stmt["invoices"]])

# GET statement first (exporter does this)
client.get(f"/portal/{token}")

# EXACT exporter-style POST
r = client.post(f"/portal/{token}/allocate", data=[
    ("payment", "0"), ("invoice", "0"), ("invoice", "1"),
    ("confirmed_by", "J. Doe (ACME Logistics)"), ("note", "May invoices"),
])
print("allocate status:", r.status_code)

after = remittance_store.load_statement(token)
alloc = after.get("allocation", {})
print("total_payments:", alloc.get("total_payments"))
print("total_invoices:", alloc.get("total_invoices"))
print("confirmed_by:", repr(alloc.get("confirmed_by")))
print("has 4,700 in response:", "4,700" in r.text)

# cleanup
remit = ROOT / "data" / "remittance"
(remit / "batches" / f"{batch_id}.json").unlink(missing_ok=True)
for c in batch["customers"]:
    (remit / "statements" / f"{c['token']}.json").unlink(missing_ok=True)
for p in (ROOT / "data" / "outputs").glob(f"*{token[:8]}*"):
    p.unlink(missing_ok=True)
for p in (ROOT / "data" / "uploads").glob("gl_*.xlsx"):
    p.unlink(missing_ok=True)
