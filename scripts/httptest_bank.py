"""Exercise the Bank Statements collection-activity flow + the new CtP
upload features (AR transaction file + multi-master + trial balance)."""
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import openpyxl
from fastapi.testclient import TestClient

from app.config import CONFIG_PATH
from app.main import app
from app.tools import bank as bank_tool
from app.tools import ongoing_ctp as ctp

ROOT = Path(__file__).resolve().parent.parent
AR = ROOT / "samples" / "ar_breakdown_sample.xlsx"
MASTER = ROOT / "samples" / "ar_master_sample.xlsx"
TB = ROOT / "samples" / "ar_trial_balance_sample.xlsx"
BANK = ROOT / "samples" / "bank_statement_sample.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
client = TestClient(app)
ctp.MASTER_PATH.unlink(missing_ok=True)
_pre_cfg = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# ---- CtP upload page: renamed sections + multi-master + TB slot -------------
r = client.get("/tools/ongoing-ctp-monitoring")
check("upload page shows 'AR transaction file'", "AR transaction file" in r.text)
check("master input allows multiple files", 'name="master_file"' in r.text
      and "multiple" in r.text)
check("trial balance slot present", "AR trial balance" in r.text)

# Build a second tiny master in-memory (one extra customer ZETA)
wb = openpyxl.Workbook()
ws = wb.active
ws.append(["Customer Number", "Customer Name", "Customer Segment",
           "Receivable Balance", "Credit Hold Position"])
ws.append(["1004000006", "ZETA INDUSTRIES", "Enterprise", 99000, ""])
buf = io.BytesIO()
wb.save(buf)
buf.seek(0)

# Analyze with: transaction file + TWO master files + trial balance
with open(AR, "rb") as f_ar, open(MASTER, "rb") as f_m1, open(TB, "rb") as f_tb:
    r = client.post("/tools/ongoing-ctp-monitoring/analyze",
                    data={"as_of": "2026-06-09", "default_rank": "60"},
                    files=[
                        ("file", ("ar_breakdown_sample.xlsx", f_ar, XLSX)),
                        ("master_file", ("ar_master_sample.xlsx", f_m1, XLSX)),
                        ("master_file", ("master2.xlsx", buf, XLSX)),
                        ("tb_file", ("ar_trial_balance_sample.xlsx", f_tb, XLSX)),
                    ], follow_redirects=False)
check("analyze (3 file kinds) -> redirect", r.status_code == 303)
token = re.search(r"results/([0-9a-f]+)/dashboard", r.headers["location"]).group(1)

r = client.get(f"/tools/ongoing-ctp-monitoring/results/{token}")
check("FIRST analysis: TB agreement shown", "trial balance" in r.text
      and "First analysis" in r.text)
check("TB agrees (7,960,000)", "agrees to the AR trial balance" in r.text)

# Merged master: 6 customers (5 + ZETA)
m = ctp.load_master()
check("masters merged (6 customers)", m and m["stats"]["customers"] == 6)
check("merged source names both files", "master2.xlsx" in m.get("source", ""))

# ---- Bank statements tool: per-bank SLOT upload (override model) -----------
# Configure a bank slot (auth off locally -> admin); restored at cleanup.
client.post("/settings/banks", data={"banks": "Ecobank Cameroun"})
r = client.get("/tools/bank-statements")
check("bank page 200 + shows the configured slot",
      r.status_code == 200 and "Ecobank Cameroun" in r.text)
check("accepts pdf statements", ".pdf" in r.text)

with open(BANK, "rb") as f1:
    r = client.post("/tools/bank-statements/upload",
                    data={"bank": "Ecobank Cameroun"},
                    files={"file": ("bank_statement_sample.xlsx", f1, XLSX)},
                    follow_redirects=False)
check("slot upload -> redirect to that slot's report", r.status_code == 303)
btoken = re.search(r"results/([0-9a-f]+)", r.headers["location"]).group(1)
check("slot uses the bank's stable token",
      btoken == bank_tool.bank_token("Ecobank Cameroun"))

r = client.get(f"/tools/bank-statements/results/{btoken}")
check("report 200", r.status_code == 200)
check("statement labelled with the configured bank", "Ecobank Cameroun" in r.text)
check("daily collections table", "Daily collections" in r.text)

# Excel export
r = client.get(f"/tools/bank-statements/results/{btoken}/export")
check("export downloads xlsx", r.status_code == 200 and r.content[:2] == b"PK")

# Re-upload OVERRIDES — still exactly one stored report for the slot
with open(BANK, "rb") as f1:
    client.post("/tools/bank-statements/upload",
                data={"bank": "Ecobank Cameroun"},
                files={"file": ("again.xlsx", f1, XLSX)}, follow_redirects=False)
check("re-upload keeps a single report for the slot (override)",
      len(list(bank_tool.STORE_DIR.glob(f"{btoken}.json"))) == 1)

# Uploading to a bank that isn't configured is refused
with open(BANK, "rb") as f1:
    r = client.post("/tools/bank-statements/upload", data={"bank": "Ghost Bank"},
                    files={"file": ("x.xlsx", f1, XLSX)})
check("unknown bank rejected (400)", r.status_code == 400)

# Clear the slot
r = client.post(f"/tools/bank-statements/results/{btoken}/delete",
                follow_redirects=True)
check("slot cleared", r.status_code == 200
      and bank_tool.load_report(btoken) is None)

# Delete the CtP analysis through the new endpoint (exercises it too)
r = client.post(f"/tools/ongoing-ctp-monitoring/results/{token}/delete",
                follow_redirects=True)
check("ctp analysis deleted via endpoint", r.status_code == 200
      and ctp.load_result(token) is None)

print("\nALL BANK + CTP-UPLOAD CHECKS PASSED")

# Cleanup: this test's artifacts only
ctp.MASTER_PATH.unlink(missing_ok=True)
if _pre_cfg is not None:                       # restore config.json (banks)
    CONFIG_PATH.write_text(_pre_cfg, encoding="utf-8")
elif CONFIG_PATH.exists():
    CONFIG_PATH.unlink()
for p in bank_tool.STORE_DIR.glob(f"{btoken}*"):
    p.unlink()
for p in (ROOT / "data" / "uploads").glob("*"):
    p.unlink()
for p in (ROOT / "data" / "outputs").glob("CtP_controls_*"):
    p.unlink()
for p in (ROOT / "data" / "outputs").glob("Collections_*"):
    p.unlink()
print("cleaned up")
