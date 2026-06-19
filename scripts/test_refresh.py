"""Refresh actions re-link a stored analysis to the CURRENT related data,
without re-uploading: bank report -> latest AR; CtP -> current master; cheque
batch -> current bank statement lines.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import bank, ongoing_ctp as ctp  # noqa: E402
from app.services import cheques  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FRENCH = ROOT / "samples" / "bank_statement_french_sample.xlsx"
_tmp = Path(tempfile.mkdtemp(prefix="refresh_"))


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# === 1. Bank report re-links to the latest AR analysis ======================
bank.STORE_DIR = _tmp / "bank"
_orig_ar = bank._latest_ar_customers
bank._latest_ar_customers = lambda: ([], None)        # no AR yet -> unmatched
rep = bank.build_report([(FRENCH, "afri.xlsx")], bank_slot="Afriland")
bank.save_report(rep)
check("with no AR, ACME credit is unmatched",
      not any(m["key"] == "1004000001" for m in rep["matched"]))

# A new AR analysis now exists that includes ACME.
acme = {"key": "1004000001", "customer": "ACME LOGISTICS", "total_ar": 600000,
        "overdue": 200000, "status": "Within terms", "status_key": "good",
        "currently_held": False}
bank._latest_ar_customers = lambda: ([acme], {"token": "t", "as_of": "2026-06-13",
                                              "source": "s"})
refreshed = bank.refresh_report(rep["token"])
check("after refresh, ACME is matched to the new AR",
      any(m["key"] == "1004000001" for m in refreshed["matched"]))
check("refresh kept the same slot token (override in place)",
      refreshed["token"] == rep["token"])
bank._latest_ar_customers = _orig_ar

# === 2. CtP re-applies the current master file ==============================
result = {"customers": [{
    "key": "1004000001", "customer": "ACME LOGISTICS", "critical": False,
    "segment": "", "status_key": "good", "total_ar": 600000}]}
_orig_lm = ctp.load_master
ctp.load_master = lambda: {"customers": {"1004000001": {
    "name": "ACME LOGISTICS", "balance": 600000, "on_hold": True,
    "segment": "Enterprise", "critical": True, "plan": None}}, "stats": {}}
n = ctp.reapply_master(result)
ctp.hold_compare(result["customers"])
c0 = result["customers"][0]
check("reapply_master updated 1 customer", n == 1)
check("critical flag picked up from the current master", c0["critical"] is True)
check("segment picked up from the current master", c0["segment"] == "Enterprise")
check("master hold flows into currently_held via hold_compare", c0["currently_held"] is True)
ctp.load_master = _orig_lm

# === 3. Cheque batch re-matches to the current bank lines ===================
cheques.BATCH_DIR = _tmp / "cheques"
TOKEN = "ref0001"
cheques.create_batch(TOKEN, [], [], [])
payload = cheques.load_batch(TOKEN)
payload["cheques"] = [{"idx": 0, "filename": "c.png", "stored": "c.png",
                       "media": "image/png", "status": "done",
                       "result": {"cheque_number": "0012345"},
                       "appearances": [], "error": ""}]
cheques._save(TOKEN, payload)
check("cheque starts with no appearances", not cheques.load_batch(TOKEN)["cheques"][0]["appearances"])
rows = [{"bank": "Afriland", "text": "CHEQUE 0012345 DEPOSIT",
         "amount": 1500000.0, "date": "2026-06-10"}]
out = cheques.refresh_matches(TOKEN, rows, [{"bank": "Afriland", "lines": 1}])
check("after refresh, the cheque matches the new bank line",
      bool(out["cheques"][0]["appearances"]))
check("refresh recorded the new bank line count", out["bank_line_count"] == 1)

print("\nALL REFRESH TESTS PASSED")
