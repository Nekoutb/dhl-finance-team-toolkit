"""Exercise the Orange Money web flow end-to-end via FastAPI TestClient:
upload statement -> grouped review -> generate -> ZIP of per-correspondant
branded receipts -> saved-customers directory."""
import io
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from testutil import smtp_guard
smtp_guard()

import tempfile

from app.services import customers
customers._PATH = Path(tempfile.mkdtemp(prefix="httptest_")) / "customers.json"

from app.main import app

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "orange_statement_sample.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

client = TestClient(app)


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# 1) Dashboard + upload page
r = client.get("/")
check("dashboard 200", r.status_code == 200)
check("dashboard lists Orange tool", "Orange Money" in r.text)
check("momo tool removed from dashboard", "/tools/momo" not in r.text)
r = client.get("/tools/orange-cameroun")
check("upload page 200", r.status_code == 200)

# 2) Upload the Orange statement -> grouped review
with open(SAMPLE, "rb") as fh:
    r = client.post("/tools/orange-cameroun/upload",
                    files={"file": ("orange_statement_sample.xlsx", fh, XLSX)})
check("upload -> review 200", r.status_code == 200)
check("review shows collection count", "4</strong> successful collection" in r.text)
check("review groups correspondants", "699559325" in r.text)
token = re.search(r'name="token" value="([0-9a-f]+)"', r.text).group(1)

# 3) Generate: confirm an identity for one correspondant -> ZIP
r = client.post("/tools/orange-cameroun/generate", data={
    "token": token,
    "original": "orange_statement_sample.xlsx",
    "name_699559325": "ACME LOGISTICS",
    "acct_699559325": "1004000001",
})
check("generate -> done 200", r.status_code == 200)
check("done page reports receipts", "Receipts generated" in r.text)
zip_name = re.search(r'/download/(orange_[0-9a-f]+\.zip)', r.text).group(1)

# 4) Download the ZIP and inspect it
rz = client.get(f"/download/{zip_name}?download_as=Collections.zip")
check("zip downloads", rz.status_code == 200 and rz.content[:2] == b"PK")
with zipfile.ZipFile(io.BytesIO(rz.content)) as zf:
    names = zf.namelist()
check("zip holds 4 receipts", sum(n.endswith(".pdf") for n in names) == 4)
check("per-correspondant folders", any("/699559325/" in n for n in names))

# 5) Path-traversal guard on downloads
rbad = client.get("/download/..%2f..%2fconfig.json")
check("download path-traversal blocked", rbad.status_code in (404, 400))

# 6) Saved-customers directory reflects the confirmed identity
r = client.get("/tools/orange-cameroun/customers")
check("customers page lists saved name + account",
      "ACME LOGISTICS" in r.text and "1004000001" in r.text)

# 7) The momo routes are gone
check("momo route removed (404)", client.get("/tools/momo").status_code == 404)

print("\nALL HTTP CHECKS PASSED")
