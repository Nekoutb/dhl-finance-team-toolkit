"""v11.16 — two portal fixes: rows that arrive already ticked (drafts,
refused submits) get the same amount-paid pre-fill as a fresh tick, and an
airwaybill typed in the search box that is on nobody's visible list is looked
up across the WHOLE Cash AR and added so it can be ticked and matched.

Isolated to a temp data dir; the real data/ is never touched.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.tools import bitcash, iro  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="v1116_"))
config.CONFIG_PATH = _tmp / "config.json"
config.invalidate_config_cache()
iro.IRO_DIR = _tmp / "iro"
iro.IRO_DIR.mkdir(parents=True, exist_ok=True)
iro.UPLOAD_DIR = _tmp / "uploads"
iro.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
iro.DEPOSITS_PATH = _tmp / "deposits.json"
bitcash.RECON_DIR = _tmp / "recons"
bitcash.RECON_DIR.mkdir(parents=True, exist_ok=True)
bitcash.FILES_DIR = _tmp / "recon_files"
bitcash.FILES_DIR.mkdir(parents=True, exist_ok=True)
bitcash.ROWS_PATH = _tmp / "rows.json"
bitcash.STORE_PATH = _tmp / "bitcash.json"

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

main.OUTPUT_DIR = _tmp / "out"
main.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
main.UPLOAD_DIR = iro.UPLOAD_DIR

client = TestClient(main.app)
_fail = 0


def check(label, cond):
    global _fail
    print(("[OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        _fail += 1


# === 0. Two operators; OTHER's AWB is what MINE will search for =============
MINE, OTHER = "4003011111", "4003022222"
MY_AWB, OTHER_AWB, MATCHED_AWB = "7010101010", "7020202020", "7030303030"
bitcash.ROWS_PATH.write_text(json.dumps({
    "bit": [{"id": 0, "amount": -125000.0, "gl_account": "1263001293",
             "assignment": "0548407000018", "posting_date": "01.07.2026",
             "reference": "CMDLA", "text": "CASH DEP", "doc_no": "410001",
             "raw": []}],
    "bit_header": [], "gen_bit": "g1", "gen_cash": "g2",
    "cash_date_col": "Doc. Date",
    "cash": [
        {"id": 0, "sap_acct": MINE, "ibs_acct": "415048444", "awb": MY_AWB,
         "assignment": MY_AWB, "reference": "R0", "amount": 100000.0,
         "customer": "ME", "doc_no": "0", "date": "01.07.2026"},
        {"id": 1, "sap_acct": OTHER, "ibs_acct": "415048445",
         "awb": OTHER_AWB, "assignment": OTHER_AWB, "reference": "R1",
         "amount": 25000.0, "customer": "COLLEAGUE", "doc_no": "1",
         "date": "01.07.2026"},
        {"id": 2, "sap_acct": OTHER, "ibs_acct": "415048445",
         "awb": MATCHED_AWB, "assignment": MATCHED_AWB, "reference": "R2",
         "amount": 5000.0, "customer": "COLLEAGUE", "doc_no": "2",
         "date": "01.07.2026"}]}, ensure_ascii=False), encoding="utf-8")

# MATCHED_AWB is already claimed in an open sandbox — invisible to search.
bitcash.save_recon({
    "token": "cafe0000cafe", "status": "open", "uploaded": "2026-08-01",
    "uploaded_by": "tester", "source": "claimed",
    "statement": {"label": "x", "date": "", "total": 5000.0, "lines": []},
    "ar_selected": [2], "bit_candidates": [], "bit_selected": None,
    "rows_gen": {"bit": "g1", "cash": "g2"}})

tok = iro.ensure_token(MINE)["token"]

# === 1. The lookup itself ===================================================
row = iro.lookup_open_awb(OTHER_AWB)
check("an AWB on another account is found in the wider Cash AR",
      row is not None and row["awb"] == OTHER_AWB
      and row["amount"] == 25000.0 and row["account"] == OTHER)
check("an already-claimed AWB stays invisible",
      iro.lookup_open_awb(MATCHED_AWB) is None)
check("too-short input finds nothing", iro.lookup_open_awb("1234") is None)
check("junk finds nothing", iro.lookup_open_awb("9999999999") is None)
check("formatting is forgiven (spaces, dashes)",
      (iro.lookup_open_awb(" 7020-202-020 ") or {}).get("awb") == OTHER_AWB)

# === 2. The endpoint ========================================================
r = client.get(f"/operator/{tok}/lookup?awb={OTHER_AWB},{MATCHED_AWB},abc")
check("lookup endpoint returns exactly the open row",
      r.status_code == 200 and [f["awb"] for f in r.json()["found"]]
      == [OTHER_AWB])
r = client.get(f"/operator/BADTOKEN00/lookup?awb={OTHER_AWB}")
check("a bad token is refused", r.status_code == 404)

# === 3. The page: pre-fill on load + search wiring ==========================
OP = (ROOT / "app" / "templates" / "operator" / "statement.html").read_text(
    encoding="utf-8")
check("rows that arrive ticked are pre-filled at load",
      '.iro-tick:checked").forEach(prefill)' in OP)
check("unmatched search terms trigger the Cash-AR lookup",
      "lookupMissing(unmatched)" in OP and "/lookup?awb=" in OP)
check("fetched rows are built with the full input set",
      "addFetchedRow" in OP and 'class="iro-cashref"' in OP
      and ">found</span>" in OP)
check("fetched rows ticked after a reload are fetched back",
      "missingRestore" in OP)

# === 4. Submitting a searched AWB ===========================================
r = client.post(f"/operator/{tok}/submit", data={
    "awb": [MY_AWB, OTHER_AWB],
    f"ref_{MY_AWB}": "DEP-1", f"ref_{OTHER_AWB}": "DEP-1",
    f"paid_{MY_AWB}": "100000", f"paid_{OTHER_AWB}": "25000",
    f"mode_{MY_AWB}": "Bank", f"mode_{OTHER_AWB}": "Bank",
    f"provider_{MY_AWB}": "BICEC", f"provider_{OTHER_AWB}": "BICEC",
    "reference": "DEP-1", "bank": "BICEC"})
check("a submission including the searched AWB is accepted",
      r.status_code == 200 and "Submission received" in r.text)
sub = iro.load_record(MINE)["submissions"][-1]
for _ in range(80):
    rec = bitcash.load_recon(sub["recon_token"]) or {}
    if rec.get("status") in ("open", "error"):
        break
    time.sleep(0.25)
check("its reconciliation opened", rec.get("status") == "open")
lines = {ln.get("reference"): ln for ln in rec["statement"]["lines"]}
sel = set(rec.get("ar_selected") or [])
check("the searched AWB carries its real Cash AR amount",
      any(abs((ln.get("amount") or 0) - 25000.0) < 0.01
          for ln in rec["statement"]["lines"]))
check("both AWBs matched their Cash AR rows — including the other account's",
      {0, 1} <= sel)
check("the BIT anchored on the declared bank total (125,000)",
      rec.get("bit_selected") == 0)

# an unknown AWB in the form is still dropped, not invented
r = client.post(f"/operator/{tok}/submit", data={
    "awb": ["9999999999"], "ref_9999999999": "DEP-X",
    "paid_9999999999": "10", "mode_9999999999": "Bank",
    "reference": "DEP-X", "bank": "BICEC"})
check("an AWB that exists nowhere is refused",
      "Tick at least one airwaybill" in r.text
      or "Submission received" not in r.text)

# === 5. Review fixes ========================================================
# a) a duplicated AWB in the form must not double the declared totals
r = client.post(f"/operator/{iro.ensure_token(OTHER)['token']}/submit", data=[
    ("awb", OTHER_AWB), ("awb", OTHER_AWB),
    (f"ref_{OTHER_AWB}", "DEP-D"), (f"paid_{OTHER_AWB}", "25000"),
    (f"mode_{OTHER_AWB}", "Bank"), (f"provider_{OTHER_AWB}", "BICEC"),
    ("reference", "DEP-D"), ("bank", "BICEC")])
check("a repeated awb field is counted once", r.status_code == 200)
if "Submission received" in r.text:
    sub2 = iro.load_record(OTHER)["submissions"][-1]
    for _ in range(80):
        rec2 = bitcash.load_recon(sub2["recon_token"]) or {}
        if rec2.get("status") in ("open", "error"):
            break
        time.sleep(0.25)
    check("its evidence carries ONE line, not two",
          len(rec2.get("statement", {}).get("lines", [])) == 1)
    bank_g = [g for g in rec2.get("mode_totals", [])
              if g["mode"] == "Bank"]
    check("its bank total is the single amount",
          bank_g and bank_g[0]["total"] == 25000.0)

# b) approval refuses an AWB already claimed by another reconciliation
_gen = {"bit": "g1", "cash": "g2"}


def _mini(token, sel):
    bitcash.save_recon({
        "token": token, "status": "open", "uploaded": "2026-08-05",
        "uploaded_by": "t", "source": "s",
        "statement": {"label": "x", "date": "", "total": 1.0, "lines": []},
        "ar_selected": sel, "bit_candidates": [0], "bit_selected": 0,
        "rows_gen": _gen})


_mini("aaaa1111aaaa", [0])
_mini("bbbb2222bbbb", [0, 1])
got = bitcash.set_status("bbbb2222bbbb", True, "t")
check("approving a recon whose AWB another open recon claims is refused",
      isinstance(got, tuple) and got[0] == "conflict"
      and MY_AWB in got[1])
got = bitcash.set_status("bbbb2222bbbb", True, "t")
check("and a retry is refused just the same",
      isinstance(got, tuple) and got[0] == "conflict")

# c) client hardening from the review
check("esc() escapes quotes for attribute contexts",
      '.replace(/"/g, "&quot;")' in OP.replace("'", '"')
      or "&quot;" in OP)
check("a debounce reset can no longer strand a search term",
      "lookupPending" in OP and "batch.forEach(function (t) { lookedUp[t] = true; })" in OP)
check("fetched rows restore what the operator typed",
      "var RESTORE = {" in OP and "RESTORE.paid[a]" in OP)
check("a late-arriving fetched row voids a shown confirmation and lands "
      "in the draft",
      "unconfirm();\n          queueDraft();" in OP)

if _fail:
    print(f"\n{_fail} CHECK(S) FAILED")
    sys.exit(1)
print("\nALL v11.16 TESTS PASSED")
