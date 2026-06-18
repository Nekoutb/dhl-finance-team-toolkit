"""CtP hold register sourced from the AR Trial Balance file.

Proves the fix: uploading the transaction file + the customer-level AR trial
balance (with "Account stop/Open" = X) flags those customers as on credit hold
(Stop) across the analysis — no master file required. Self-manages auth (forces
it OFF for this page-render test, then restores config.json).
"""
import atexit
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import CONFIG_PATH  # noqa: E402

_pre = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None


def _restore():
    if _pre is not None:
        CONFIG_PATH.write_text(_pre, encoding="utf-8")
    elif CONFIG_PATH.exists():
        CONFIG_PATH.unlink()


atexit.register(_restore)
_cfg = json.loads(_pre) if _pre else {}
_cfg.setdefault("auth", {})["enabled"] = False         # login-free for this test
CONFIG_PATH.write_text(json.dumps(_cfg), encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402
from app import main  # noqa: E402
from app.services import ar_master  # noqa: E402
from app.tools import ongoing_ctp as ctp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TB = ROOT / "samples" / "ar_trial_balance_customers_sample.xlsx"
TXN = ROOT / "samples" / "ar_breakdown_features_sample.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# --- 1. the AR trial balance's "Account stop/Open" column is read as holds ---
m = ar_master.parse_master(TB)
# Regression: the account-number column ("Customer") must win over the name
# column ("Customer Name"); otherwise customers are keyed by name and never
# match the transaction file -> empty hold register (the bug being fixed).
check("account mapped to account-number column ('Customer'), not 'Customer Name'",
      m["mapping"].get("account") == "Customer"
      and m["mapping"].get("name") == "Customer Name")
check("customers keyed by account number (1004…)", "1004000001" in m["customers"])
check("hold column detected as 'Account Stop/Open'", m["mapping"].get("hold") == "Account Stop/Open")
check("ACME on hold (X)", m["customers"]["1004000001"]["on_hold"])
check("GAMMA on hold (X)", m["customers"]["1004000003"]["on_hold"])
check("BETA open (no X)", not m["customers"]["1004000002"]["on_hold"])
check("2 customers flagged on hold", m["stats"]["with_hold_flag"] == 2)

# --- 2. full upload route: transaction + trial balance, NO master file ------
ctp.MASTER_PATH.unlink(missing_ok=True)
client = TestClient(main.app)
with open(TXN, "rb") as ftx, open(TB, "rb") as ftb:
    r = client.post("/tools/ongoing-ctp-monitoring/analyze",
                    data={"as_of": "2026-06-13", "default_rank": "60"},
                    files=[("file", ("txn.xlsx", ftx, XLSX)),
                           ("tb_file", ("ar_tb.xlsx", ftb, XLSX))],
                    follow_redirects=False)
check("analyze -> redirect to dashboard", r.status_code == 303)
token = re.search(r"results/([0-9a-f]+)/dashboard", r.headers["location"]).group(1)

result = ctp.load_result(token)
ctp.hold_compare(result["customers"])
held = sorted(c["key"] for c in result["customers"] if c.get("currently_held"))
check("hold register NOT empty (holds came from the trial balance)", len(held) >= 1)
check("ACME + GAMMA on credit hold", "1004000001" in held and "1004000003" in held)
print(f"   customers on credit hold: {held}")

# --- 3. dashboard shows the Current Credit Hold status (Stop/Open) ----------
try:
    page = client.get(f"/tools/ongoing-ctp-monitoring/results/{token}/dashboard")
    assert page.status_code == 200, page.status_code
    check("dashboard shows 'Current Credit Hold status' column",
          "Current Credit Hold status" in page.text)
    check("dashboard shows Stop status", ">Stop<" in page.text)
    rpage = client.get(f"/tools/ongoing-ctp-monitoring/results/{token}")
    check("results report shows Current Credit Hold status",
          "Current Credit Hold status" in rpage.text and ">Stop<" in rpage.text)
finally:
    (ctp.STORE_DIR / f"{token}.json").unlink(missing_ok=True)

print("\nALL CtP HOLD-FROM-TRIAL-BALANCE TESTS PASSED")
_restore()
