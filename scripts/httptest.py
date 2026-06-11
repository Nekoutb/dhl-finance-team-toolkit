"""Exercise the web flow end-to-end via FastAPI TestClient."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from testutil import smtp_guard
smtp_guard()

from app.main import app

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "orange_cameroun_sample.xlsx"

client = TestClient(app)


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# 1) Dashboard
r = client.get("/")
check("dashboard 200", r.status_code == 200)
check("dashboard lists Orange tool", "Orange Cameroun" in r.text)

# 2) Upload page
r = client.get("/tools/orange-cameroun")
check("upload page 200", r.status_code == 200)

# 3) Upload the sample file
with open(SAMPLE, "rb") as fh:
    r = client.post(
        "/tools/orange-cameroun/upload",
        files={"file": ("orange_cameroun_sample.xlsx", fh,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    )
check("upload -> review 200", r.status_code == 200)
check("review shows transactions", "transaction(s) found" in r.text)
check("review shows MSISDN key", "MSISDN" in r.text)

token = re.search(r'name="token" value="([0-9a-f]+)"', r.text).group(1)
idx = int(re.search(r'name="selected" value="(\d+)"', r.text).group(1))
print("   token:", token, "first row idx:", idx)

# 4) Generate + email (no SMTP configured -> should produce .eml)
r = client.post("/tools/orange-cameroun/generate", data={
    "token": token,
    "original": "orange_cameroun_sample.xlsx",
    "recipient": "finance@example.com",
    "action": "email",
    "selected": str(idx),
    f"name_{idx}": "Beta Trading Sarl",
})
check("generate 200", r.status_code == 200)
check("done page shows success", "Receipt generated" in r.text)
check("done page names customer", "Beta Trading Sarl" in r.text)
check("eml fallback offered", ".eml" in r.text)

pdf = re.search(r'/download/([^"]+\.pdf)', r.text).group(1)
eml = re.search(r'/download/([^"]+\.eml)', r.text).group(1)
print("   pdf:", pdf, "eml:", eml)

# 5) Downloads
rp = client.get(f"/download/{pdf}")
check("pdf downloads", rp.status_code == 200 and rp.content[:4] == b"%PDF")
re_ = client.get(f"/download/{eml}")
check("eml downloads", re_.status_code == 200 and b"Beta Trading Sarl" in re_.content)

# 6) Path-traversal guard on downloads
rbad = client.get("/download/..%2f..%2fconfig.json")
check("download path-traversal blocked", rbad.status_code in (404, 400))

# 7) Saved customers page reflects the new name
r = client.get("/tools/orange-cameroun/customers")
check("customers page lists saved name", "Beta Trading Sarl" in r.text)

print("\nALL HTTP CHECKS PASSED")
