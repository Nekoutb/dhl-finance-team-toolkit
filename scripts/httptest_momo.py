"""Exercise the Mobile Money (Orange/MTN) extractor: Excel + PDF inputs,
account memory, reference-named output, branding and email sending."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from testutil import smtp_guard
smtp_guard()

from app.main import app
from app.services import customers
from app.tools import momo

ROOT = Path(__file__).resolve().parent.parent
XLS = ROOT / "samples" / "mtn_momo_sample.xlsx"
PDF = ROOT / "samples" / "orange_momo_sample.pdf"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
client = TestClient(app)

_CUST = ROOT / "data" / "customers.json"
pre = _CUST.read_text(encoding="utf-8") if _CUST.exists() else None
_SET = momo.SETTINGS_PATH
pre_set = _SET.read_text(encoding="utf-8") if _SET.exists() else None


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# Settings: admin sets the default email recipient
r = client.post("/tools/momo/settings", data={"recipient": "receipts@dhl.example"},
                follow_redirects=True)
check("settings saved", r.status_code == 200 and "receipts@dhl.example" in r.text)

# ---- MTN Excel report -------------------------------------------------------
with open(XLS, "rb") as fh:
    r = client.post("/tools/momo/upload",
                    files={"file": ("mtn_momo_sample.xlsx", fh, XLSX_MIME)})
check("xlsx upload -> review", r.status_code == 200)
check("MTN provider detected", "MTN Mobile Money" in r.text)
check("report header NOT shown on review", "Merchant:" not in r.text)
check("account inputs under names", r.text.count('name="acct_') == 4)
check("recipient prefilled from settings", 'value="receipts@dhl.example"' in r.text)
token = re.search(r'name="token" value="([0-9a-f]+)"', r.text).group(1)
idxs = re.findall(r'name="selected" value="(\d+)"', r.text)

# Group extraction (3 lines) with name + account; email it (no SMTP -> .eml)
r = client.post("/tools/momo/generate", data={
    "token": token, "original": "mtn_momo_sample.xlsx",
    "selected": [idxs[0], idxs[1], idxs[3]],
    f"name_{idxs[0]}": "ACME DISTRIBUTION SARL",
    f"acct_{idxs[0]}": "CM-ACCT-7001",
    "recipient": "receipts@dhl.example", "action": "email"})
check("group generate -> done", r.status_code == 200)
check("3 transactions in receipt", "3 transaction(s)" in r.text)
pdf_name = re.search(r'/download/([^"]+\.pdf)', r.text).group(1)
check("file name starts with tx reference (X1001)",
      pdf_name.startswith("X1001"))
check("eml produced for emailing", ".eml" in r.text)
rd = client.get(f"/download/{pdf_name}")
check("receipt pdf downloads", rd.status_code == 200 and rd.content[:4] == b"%PDF")

# Name + account memory
check("name stored", customers.get_name("momo", "237670111001") == "ACME DISTRIBUTION SARL")
check("account stored", customers.get_name("momo_acct", "237670111001") == "CM-ACCT-7001")
with open(XLS, "rb") as fh:
    r = client.post("/tools/momo/upload",
                    files={"file": ("mtn_momo_sample.xlsx", fh, XLSX_MIME)})
check("name auto-fills on re-upload", r.text.count('value="ACME DISTRIBUTION SARL"') == 2)
check("account auto-fills on re-upload", r.text.count('value="CM-ACCT-7001"') == 2)

# ---- Orange PDF report ------------------------------------------------------
with open(PDF, "rb") as fh:
    r = client.post("/tools/momo/upload",
                    files={"file": ("orange_momo_sample.pdf", fh, "application/pdf")})
check("PDF upload -> review", r.status_code == 200)
check("Orange provider detected", "Orange Money" in r.text)
check("3 rows parsed from PDF table", r.text.count('class="rowpick"') == 3)
token2 = re.search(r'name="token" value="([0-9a-f]+)"', r.text).group(1)
idxs2 = re.findall(r'name="selected" value="(\d+)"', r.text)

r = client.post("/tools/momo/generate", data={
    "token": token2, "original": "orange_momo_sample.pdf",
    "selected": idxs2[1], f"name_{idxs2[1]}": "BETA SERVICES",
    "action": "download"})
check("single generate from PDF -> done", r.status_code == 200
      and "1 transaction(s)" in r.text)
pdf2 = re.search(r'/download/([^"]+\.pdf)', r.text).group(1)
check("PDF-source file name starts with reference (A2002)",
      pdf2.startswith("A2002"))
rd = client.get(f"/download/{pdf2}")
check("pdf receipt downloads", rd.status_code == 200 and rd.content[:4] == b"%PDF")

# Saved names page (with account column) + delete endpoint
r = client.get("/tools/momo/customers")
check("saved names page lists both", "ACME DISTRIBUTION SARL" in r.text
      and "BETA SERVICES" in r.text)
check("account column shown", "CM-ACCT-7001" in r.text)
r = client.post("/tools/momo/customers/delete", data={"key": "237670111001"},
                follow_redirects=True)
check("saved name + account deleted",
      customers.get_name("momo", "237670111001") is None
      and customers.get_name("momo_acct", "237670111001") is None)

# ---- Document header extraction (logos pulled from the source files) --------
from app.services import header_assets  # noqa: E402
imgs_xlsx = header_assets.extract(XLS)
check("xlsx embedded logo extracted", len(imgs_xlsx) == 1
      and imgs_xlsx[0]["bytes"][:8] == b"\x89PNG\r\n\x1a\n")
imgs_pdf = header_assets.extract(PDF)
check("pdf header strip captured as image", len(imgs_pdf) == 1
      and imgs_pdf[0]["bytes"][:8] == b"\x89PNG\r\n\x1a\n")
imgs_xls = header_assets.extract(ROOT / "samples" / "orange_cameroun_sample.xls")
check("legacy .xls degrades gracefully (no images)", imgs_xls == [])

# Group receipt is tabular + landscape-capable: just confirm it grew columns
check("group receipt pdf has content", rd.content and len(rd.content) > 2500)

print("\nALL MOMO CHECKS PASSED")

# Restore stores; drop test outputs/uploads
if pre is not None:
    _CUST.write_text(pre, encoding="utf-8")
else:
    _CUST.unlink(missing_ok=True)
if pre_set is not None:
    _SET.write_text(pre_set, encoding="utf-8")
else:
    _SET.unlink(missing_ok=True)
for pattern in ("X1001*", "A2002*"):
    for p in (ROOT / "data" / "outputs").glob(pattern):
        p.unlink()
for p in (ROOT / "data" / "uploads").glob("momo_*"):
    p.unlink()
print("cleaned up")
