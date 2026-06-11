"""Exercise the Vendor Invoice Allocation tool (ENEO REPARTITION)."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.main import app
from app.services import eno_allocation

ROOT = Path(__file__).resolve().parent.parent
KEY = ROOT / "samples" / "eno_allocation_key.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
client = TestClient(app)

# Snapshot + clear the stored key so the test is deterministic; restore after.
KEY_PATH = eno_allocation.KEY_PATH
pre_key = KEY_PATH.read_text(encoding="utf-8") if KEY_PATH.exists() else None
ENO_DIR = ROOT / "data" / "allocation" / "eno"
before = {p.name for p in ENO_DIR.glob("*")} if ENO_DIR.exists() else set()


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# Landing: three vendor sections present
r = client.get("/tools/vendor-invoice-allocation")
check("page 200", r.status_code == 200)
for v in ("ENEO", "MTN", "ORANGE"):
    check(f"{v} section present", v in r.text)

# Upload the allocation key
with open(KEY, "rb") as fh:
    r = client.post("/tools/vendor-invoice-allocation/eno/key",
                    files={"file": ("eno_allocation_key.xlsx", fh, XLSX)},
                    follow_redirects=True)
check("key upload -> 5 customers", "5 customer(s)" in r.text)
key = eno_allocation.load_key()
check("key parsed: 204691644 has 12 functions",
      len(key["customers"]["204691644"]["functions"]) == 12)
check("key parsed: order preserved",
      key["order"][:2] == ["204691644", "200744960"])

# Generate the repartition with the invoice HT amounts
ht = {"204691644": 1538672, "200744960": 482120, "200211025": 1236807,
      "201998424": 69562, "201824589": 124831}
data = {"invoice_no": "DHL-04-2026", "tva_rate": "19.25"}
data.update({f"ht_{k}": str(v) for k, v in ht.items()})
r = client.post("/tools/vendor-invoice-allocation/eno/generate", data=data)
check("generate 200", r.status_code == 200)
check("totals tie to invoice (TTC 4,116,500.46)", "4,116,500.46" in r.text)
check("TOTAL HT shown", "3,451,992" in r.text)
check("TVA shown (664,508.46)", "664,508.46" in r.text)
check("a function montant shown (446,214.88 = 29% x 1,538,672)",
      "446,214.88" in r.text)

# Verify the maths server-side: each customer's functions sum to its HT
computed = eno_allocation.compute(key, ht, 19.25)
for cust in computed["customers"]:
    s = round(sum(f["montant"] for f in cust["functions"]), 2)
    check(f"{cust['customer_id']} functions sum to HT",
          abs(s - cust["ht"]) <= 0.05)
check("grand total HT", computed["total_ht"] == 3451992)
check("TVA computed", computed["tva"] == 664508.46)

# Excel export downloads
token = re.search(r"/eno/export/([0-9a-f]+)", r.text).group(1)
rd = client.get(f"/tools/vendor-invoice-allocation/eno/export/{token}")
check("excel export downloads", rd.status_code == 200 and rd.content[:2] == b"PK")

# Recent list shows it
r = client.get("/tools/vendor-invoice-allocation")
check("recent allocation listed", "DHL-04-2026" in r.text)

print("\nALL ALLOCATION CHECKS PASSED")

# Cleanup: restore key, drop only this run's generated files
if pre_key is not None:
    KEY_PATH.write_text(pre_key, encoding="utf-8")
else:
    KEY_PATH.unlink(missing_ok=True)
if ENO_DIR.exists():
    for p in ENO_DIR.glob("*"):
        if p.name not in before:
            p.unlink()
for p in (ROOT / "data" / "uploads").glob("enokey_*"):
    p.unlink()
print("cleaned up")
