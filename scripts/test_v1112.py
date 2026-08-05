"""v11.12 — usability: journal downloads itself without scrolling, MyDHLPay
switched off, IRO payment methods widened, evidence genuinely optional, the
stray deposit-list upload removed, and the bank field reworded.

Isolated to a temp data dir; the real data/ is never touched.
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.tools import bitcash, iro  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="v1112_"))
config.CONFIG_PATH = _tmp / "config.json"
config.invalidate_config_cache()
iro.IRO_DIR = _tmp / "iro"
iro.IRO_DIR.mkdir(parents=True, exist_ok=True)
iro.UPLOAD_DIR = _tmp / "uploads"
iro.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
iro.DEPOSITS_PATH = _tmp / "deposits.json"
bitcash.RECON_DIR = _tmp / "recons"
bitcash.RECON_DIR.mkdir(parents=True, exist_ok=True)
bitcash.ROWS_PATH = _tmp / "rows.json"
bitcash.STORE_PATH = _tmp / "bitcash.json"

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

client = TestClient(main.app)
_fail = 0


def check(label, cond):
    global _fail
    print(("[OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        _fail += 1


IDX = (ROOT / "app" / "templates" / "bitcash" / "index.html").read_text(
    encoding="utf-8")
OP = (ROOT / "app" / "templates" / "operator" / "statement.html").read_text(
    encoding="utf-8")

# === 1. Journal result: no scrolling, and it downloads itself ==============
check("the journal redirect anchors at the download row",
      '"#journal-ready"' in (ROOT / "app" / "main.py").read_text(
          encoding="utf-8"))
check("the download row carries that anchor", 'id="journal-ready"' in IDX)
check("the page scrolls the result into view",
      "scrollIntoView" in IDX)
check("the full pack starts downloading without a click",
      'id="pack-dl"' in IDX and "document.createElement(\"iframe\")" in IDX)
check("a refresh does not download it again (guarded)",
      "sessionStorage" in IDX)
check("the journal-only link is still there to click",
      "Journal entry only (Excel)" in IDX)

# === 2. MyDHLPay / Cash Reconciliation switched off =========================
check("mydhlpay is OFF by default",
      config.DEFAULT_CONFIG["mydhlpay"]["enabled"] is False)
check("the Cash Reconciliation link is hidden unless enabled",
      "{% if cfg.mydhlpay and cfg.mydhlpay.enabled %}" in IDX)
r = client.get("/pay")
check("the public pay page refuses while off", r.status_code == 404)
r = client.post("/pay/add", data={"code": "0101ABC", "awb": "1234567890"})
check("the pay API refuses while off", r.status_code == 404)
r = client.get("/tools/bit-cash-ar/cash-recon", follow_redirects=False)
check("the Cash Reconciliation page redirects away while off",
      r.status_code == 303 and "switched+off" in
      r.headers.get("location", "").replace("%20", "+"))

# turning it back on restores everything (nothing was deleted)
config.write_config_file({"mydhlpay": {"enabled": True}})
r = client.get("/pay")
check("switching it back on restores the pay page", r.status_code == 200)
config.write_config_file({"mydhlpay": {"enabled": False}})
check("and off again", client.get("/pay").status_code == 404)

# === 3. Payment methods ====================================================
check("payment methods now cover the four rails",
      list(iro.PAYMENT_METHODS)
      == ["Bank deposit", "Cash deposit", "Mobile Money", "Credit card"])

# === 4/5/6. The IRO portal ==================================================
check("the deposit-slips-LIST upload is gone",
      "iro-deplist-input" not in OP and "deposit slips list" not in OP)
check("its JavaScript went with it",
      "iro-deplist-table" not in OP and "read-deposit-list" not in OP)
check("the deposit SLIP upload (the real evidence) is still there",
      'id="iro-slip-input"' in OP)
check("the bank field is reworded to DHL's Bank Account",
      "DHL's Bank Account" in OP and "Your bank" not in OP)
check("the free-text bank prompt matches",
      "type the DHL bank account" in OP)
check("the page says evidence is optional",
      "Attaching evidence is" in OP and "optional" in OP)

# A real submission with NO document of any kind must be accepted.
bitcash.ROWS_PATH.write_text(json.dumps({
    "bit": [], "bit_header": [], "gen_bit": "", "gen_cash": "g",
    "cash_date_col": "Doc. Date",
    "cash": [{"id": 0, "sap_acct": "4003025705", "ibs_acct": "415048444",
              "awb": "7010101010", "assignment": "7010101010",
              "reference": "R1", "amount": 9000.0, "customer": "SMIC",
              "doc_no": "1", "date": "01.07.2026"}]}, ensure_ascii=False),
    encoding="utf-8")
tok = iro.ensure_token("4003025705")["token"]
r = client.get(f"/operator/{tok}")
check("the portal renders for the operator", r.status_code == 200)
check("all four payment methods are offered on the page",
      all(m in r.text for m in iro.PAYMENT_METHODS))

r = client.post(f"/operator/{tok}/submit", data={
    "awb": "7010101010", "ref_7010101010": "DEP-NODOC",
    "reference": "DEP-NODOC", "paid_7010101010": "9000",
    "payment_method": "Mobile Money", "bank": ""})
check("references submit with NO document attached",
      r.status_code == 200 and "Submission received" in r.text)
sub = iro.load_record("4003025705")["submissions"][-1]
check("the submission is recorded and opened a sandbox",
      sub["channel"] == "portal" and sub.get("recon_token"))
check("the chosen payment method is remembered",
      iro.load_record("4003025705").get("payment_method") == "Mobile Money")

if _fail:
    print(f"\n{_fail} CHECK(S) FAILED")
    sys.exit(1)
print("\nALL v11.12 TESTS PASSED")
