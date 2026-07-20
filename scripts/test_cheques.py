"""Cheque payment processing: cheque-number matching, plus an end-to-end flow
where bank statements come from the single Bank Statements section (not the
cheque tool) and the AI reader is mocked.
"""
import re
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from testutil import smtp_guard  # noqa: E402
smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.services import ai_ocr, cheques  # noqa: E402
from app.tools import bank  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
client = TestClient(main.app)

# --- 1. media-type detection -------------------------------------------------
assert ai_ocr.media_type_for("cheque.PNG") == "image/png"
assert ai_ocr.media_type_for("c.pdf") == "application/pdf"
assert ai_ocr.media_type_for("statement.xlsx") is None
print("ok: media_type_for classifies cheque scans vs unsupported files")

# --- 2. find_appearances: exact whole-number match, length guard -------------
rows = [
    {"bank": "Afriland", "text": "10/06 CHQ 0012345 ACME DEPOSIT", "amount": 1500000.0, "date": "2026-06-10"},
    {"bank": "Afriland", "text": "11/06 transfer 1234567 other", "amount": 50.0, "date": "2026-06-11"},
    {"bank": "Ecobank",  "text": "VIR 12345 BETA", "amount": 900.0, "date": "2026-06-09"},
]
m = cheques.find_appearances("0012345", rows)
assert len(m) == 1 and m[0]["bank"] == "Afriland" and m[0]["amount"] == 1500000.0, m
assert any(a["bank"] == "Ecobank" for a in cheques.find_appearances("12345", rows))
assert cheques.find_appearances("0012345", [rows[2]]) == []     # 0012345 != 12345
assert cheques.find_appearances("999", rows) == []              # too short
assert cheques.find_appearances("7654321", rows) == []          # absent
print("ok: find_appearances is an exact whole-number match with a length guard")

# --- 3. bank.all_statement_lines() aggregates the central section ------------
bank_xlsx = ROOT / "data" / "uploads" / "_test_central_bank.xlsx"
bank_xlsx.parent.mkdir(parents=True, exist_ok=True)
pd.DataFrame([
    {"Date": "2026-06-10", "Description": "CHEQUE 0012345 DEPOSIT", "Credit": 1500000},
    {"Date": "2026-06-11", "Description": "MISC TRANSFER", "Credit": 100},
]).to_excel(bank_xlsx, index=False)
# Seed the central section directly (the HTTP per-bank slot upload is covered
# by test_bank_slots.py / httptest_bank.py); this only needs a stored report so
# all_statement_lines exposes the line for cheque matching.
report = bank.build_report([(bank_xlsx, "Afriland.xlsx")], bank_slot="Afriland")
bank.save_report(report)
btoken = report["token"]
all_rows, summary = bank.all_statement_lines()
assert cheques.find_appearances("0012345", all_rows), "central bank line not found"
print("ok: Bank Statements section exposes searchable lines (all_statement_lines)")

# --- 4. end-to-end: cheque tool reads + matches against the central data -----
_orig_extract, _orig_ready = ai_ocr.extract_cheque, ai_ocr.is_configured
ai_ocr.is_configured = lambda cfg: True

def _fake_extract(file_bytes, media_type, ai_cfg):
    num = (re.search(rb"\d+", file_bytes) or [b""])[0].decode()
    return {"cheque_number": num, "customer_name": f"Customer {num[-3:]}",
            "amount": 1500000.0 if num == "0012345" else 42000.0,
            "amount_currency": "XAF", "cheque_date": "2026-06-08",
            "drawee_bank": "Standard Chartered", "readability": "good"}

ai_ocr.extract_cheque = _fake_extract
ctoken = None
try:
    # The cheque form posts ONLY cheques — no statements field exists anymore.
    files = [("cheques", ("chq_found.png", b"CHQ 0012345", "image/png")),
             ("cheques", ("chq_missing.png", b"CHQ 0099999", "image/png"))]
    r = client.post("/tools/cheque-processing/upload", files=files,
                    follow_redirects=False)
    assert r.status_code == 303, r.status_code
    # Upload lands straight back on the register (no batch page any more).
    assert r.headers["location"].startswith("/tools/cheque-processing"), \
        r.headers["location"]
    ctoken = cheques.list_recent(limit=1)[0]["token"]

    for _ in range(60):
        b = cheques.load_batch(ctoken)
        if b and b["status"] == "done":
            break
        time.sleep(0.2)
    assert b and b["status"] == "done", "batch did not finish"

    by_no = {c["result"]["cheque_number"]: c for c in b["cheques"]
             if c["status"] == "done"}
    found = by_no["0012345"]["appearances"]
    assert found, "found cheque not matched to the central bank data"
    assert found[0]["amount"] == 1500000.0 and found[0]["date"] == "2026-06-10", found
    assert found[0]["bank"], "bank name missing on the match"
    assert by_no["0099999"]["appearances"] == [], "absent cheque wrongly matched"

    home = client.get("/tools/cheque-processing")
    assert home.status_code == 200 and "0012345" in home.text
    assert "/tools/bank-statements" in home.text, "no link to the Bank Statements section"
    # the cheque upload form must NOT offer a bank-statement input
    assert 'name="statements"' not in home.text, "cheque tool still has a bank upload"
    # the batch details page is GONE (register only)
    assert "details</a>" not in home.text, "details link must be removed"
    assert client.get(f"/tools/cheque-processing/batch/{ctoken}").status_code == 404
    print("ok: cheques read + matched against central Bank Statements (no own upload)")

    # --- 5. the ELECTRONIC CHEQUE REGISTER (cumulative, ticks, references) ---
    assert found[0].get("text"), "appearance must carry the statement narration"
    rows = cheques.register_rows()
    reg = {r["cheque_number"]: r for r in rows if r["batch"] == ctoken}
    assert reg["0012345"]["found"] is True, "found cheque must tick green"
    assert reg["0099999"]["found"] is False, "absent cheque must tick red"
    assert reg["0012345"]["customer"].startswith("Customer"), reg["0012345"]
    assert reg["0012345"]["issuing_bank"] == "Standard Chartered"
    assert reg["0012345"]["amount"] == 1500000.0
    assert reg["0012345"]["uploaded"], "upload date must be on the register"
    cleared = reg["0012345"]["cleared"]
    assert cleared["bank"] and cleared["date"] == "2026-06-10" \
        and "0012345" in cleared["text"]
    assert reg["0099999"]["cleared"] is None

    home = client.get("/tools/cheque-processing")
    assert "Electronic Cheque Register" in home.text, "tool not renamed"
    assert "Cheque register" in home.text and "Reference seen on statement" in home.text
    assert ">✓<" in home.text and ">✗<" in home.text, "tick/cross markers missing"

    # register-wide refresh re-matches every batch against current bank lines
    r = client.post("/tools/cheque-processing/register/refresh",
                    follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert cheques.register_rows(), "register survived the refresh"
    print("ok: electronic cheque register — cumulative rows, ticks, references, refresh")

    # --- 6. SINGLE DISCLOSURE: duplicates collapse; latest date wins ---------
    dup_rows = [
        {"bank": "Afriland", "text": "CHQ 0012345 EXTRACT JAN 2", "amount": 1500000.0, "date": "2026-01-01"},
        {"bank": "Afriland", "text": "CHQ 0012345 EXTRACT JAN 3", "amount": 1500000.0, "date": "2026-01-01"},
    ]
    apps = cheques.find_appearances("0012345", dup_rows)
    assert len(apps) == 1, "same clearing event on two extracts must disclose once"
    multi = apps + [{"bank": "Ecobank", "amount": 200.0, "date": "2026-02-15", "text": "x 0012345"}]
    assert cheques.primary_appearance(multi)["date"] == "2026-02-15", \
        "the latest-dated appearance is the one disclosed"

    # --- 7. first-match timestamp + treated flag + ordering ------------------
    b0 = cheques.load_batch(ctoken)
    m0 = next(c for c in b0["cheques"] if (c.get("result") or {}).get("cheque_number") == "0012345")
    assert m0.get("matched_at"), "first match must be timestamped"
    first_at = m0["matched_at"]
    cheques.refresh_matches(ctoken, cheques._load_bank_rows(ctoken), b0.get("banks", []))
    b1 = cheques.load_batch(ctoken)
    m1 = next(c for c in b1["cheques"] if (c.get("result") or {}).get("cheque_number") == "0012345")
    assert m1["matched_at"] == first_at, "matched_at must survive later refreshes"

    rows = [r_ for r_ in cheques.register_rows() if r_["batch"] == ctoken]
    assert rows[0]["new"] is True and rows[0]["cheque_number"] == "0012345", \
        "newly matched cheque must be first on the register"
    home = client.get("/tools/cheque-processing")
    assert "new-match" in home.text and "Matched " in home.text, \
        "matched cheques must show the fixed-in-time Matched date"
    assert "Treated in accounting" in home.text and "Mark treated" in home.text
    assert 'id="chqdrop"' in home.text, "drag-and-drop upload zone missing"

    # mark treated (auth off locally = admin = finance-capable)
    r = client.post("/tools/cheque-processing/register/treat",
                    data={"batch": ctoken, "idx": str(m1["idx"])},
                    follow_redirects=False)
    assert r.status_code == 303, r.status_code
    rows = [r_ for r_ in cheques.register_rows() if r_["batch"] == ctoken]
    treated_row = next(r_ for r_ in rows if r_["cheque_number"] == "0012345")
    assert treated_row["treated"] and treated_row["new"] is False, \
        "treated cheque loses the new-match highlight"

    # --- 8. deletion is impossible via HTTP ----------------------------------
    r = client.post(f"/tools/cheque-processing/batch/{ctoken}/delete")
    assert r.status_code in (404, 405), "delete route must be gone"
    home = client.get("/tools/cheque-processing")
    assert "Delete batch" not in home.text
    # reference is rendered inside its bounded box (no column overlap)
    assert 'class="ref-box"' in home.text, "reference box missing"
    # whole-register Excel export
    x = client.get("/tools/cheque-processing/register/export")
    assert x.status_code == 200 and x.content[:2] == b"PK", "register export broken"
    print("ok: single disclosure, new-match highlight + treated column, no deletion")

    # --- 8b. clean reference snippet (no haphazard full narration) -----------
    long_app = {"bank": "Afriland", "amount": 1.0, "date": "2026-06-10",
                "text": "03/06 05/06 REF 991 TRX 3341 CHQ 0012345 DEPOSIT "
                        "BATCH 8873 VAL 06/06 SUITE 7788 99887766"}
    snip = cheques.ref_snippet(long_app, "0012345")
    assert "0012345" in snip and len(snip) <= 80, snip
    assert not snip.startswith("03/06"), "snippet must centre on the reference"
    rowx = next(r_ for r_ in cheques.register_rows()
                if r_["batch"] == ctoken and r_["cheque_number"] == "0012345")
    assert rowx["ref_snippet"], "register rows must carry the snippet"

    # --- 8c. daily stats + graph ---------------------------------------------
    import tempfile as _tf
    cheques.STATS_PATH = Path(_tf.mkdtemp(prefix="chqstats_")) / "stats.json"
    rows_all = cheques.register_rows()
    st = cheques.register_stats(rows_all)
    mine = [r_ for r_ in rows_all if r_["batch"] == ctoken]
    assert st["total"] >= 2 and st["unpresented"] >= 1, st
    cheques.record_daily_stats(st)
    hist = cheques.stats_history()
    assert hist and hist[-1][1]["total"] == st["total"]
    home = client.get("/tools/cheque-processing")
    assert "Today's position" in home.text and "<svg" in home.text, \
        "daily stats strip + graph missing"
    print("ok: ref snippet, daily totals (total/unpresented) + graph")

    # --- 8d. duplicate upload detection --------------------------------------
    before = {d.name for d in cheques.BATCH_DIR.iterdir() if d.is_dir()}
    r = client.post("/tools/cheque-processing/upload",
                    files=[("cheques", ("chq_again.png", b"CHQ 0012345",
                                        "image/png"))], follow_redirects=False)
    assert r.status_code == 303
    after = {d.name for d in cheques.BATCH_DIR.iterdir() if d.is_dir()}
    dtoken = (after - before).pop()
    for _ in range(80):
        db = cheques.load_batch(dtoken)
        if db and db["status"] == "done":
            break
        time.sleep(0.1)
    dup_row = next(r_ for r_ in cheques.register_rows()
                   if r_["batch"] == dtoken)
    assert dup_row["duplicate_of"], "second upload of the same cheque must be flagged"
    assert dup_row["duplicate_of"]["batch"] == ctoken
    assert dup_row["duplicate_of"]["uploaded"], "original upload date must be shown"
    st2 = cheques.register_stats(cheques.register_rows())
    assert st2["duplicates"] >= 1, st2
    assert st2["total"] == st["total"], "duplicates must not inflate the totals"
    home = client.get("/tools/cheque-processing")
    assert "DUPLICATE" in home.text and "Same cheque uploaded" in home.text
    cheques.delete_batch(dtoken)
    print("ok: duplicate uploads flagged with the original date + uploader")
finally:
    ai_ocr.extract_cheque, ai_ocr.is_configured = _orig_extract, _orig_ready
    if ctoken:
        cheques.delete_batch(ctoken)
    bank.delete_report(btoken)
    bank_xlsx.unlink(missing_ok=True)

# --- 8b. stuck "reading…" cheques can be RESUMED ------------------------------
_orig_extract2, _orig_ready2 = ai_ocr.extract_cheque, ai_ocr.is_configured
ai_ocr.is_configured = lambda cfg: True
ai_ocr.extract_cheque = _fake_extract
TOKR = "beef00123abc"
try:
    # A batch whose reader thread died: cheque stuck on "pending".
    scan_dir = cheques.BATCH_DIR / TOKR
    scan_dir.mkdir(parents=True, exist_ok=True)
    (scan_dir / "stuck.png").write_bytes(b"CHQ 0555123")
    cheques.create_batch(TOKR, [{"filename": "stuck.png", "stored": "stuck.png",
                                 "media": "image/png"}], [], [])
    assert cheques.pending_count() >= 1
    home = client.get("/tools/cheque-processing")
    assert "Resume reading" in home.text, "resume button missing while pending"

    r = client.post("/tools/cheque-processing/register/resume",
                    follow_redirects=False)
    assert r.status_code == 303, r.status_code
    for _ in range(60):
        b = cheques.load_batch(TOKR)
        if b and b["status"] == "done":
            break
        time.sleep(0.2)
    assert b and b["status"] == "done", "resume did not finish the stuck cheque"
    assert b["cheques"][0]["result"]["cheque_number"] == "0555123"
    print("ok: stuck pending cheques are resumable (register Resume button)")
finally:
    ai_ocr.extract_cheque, ai_ocr.is_configured = _orig_extract2, _orig_ready2
    cheques.delete_batch(TOKR)

# --- 9. "treated" is FINANCE-only: cheque profile denied, finance allowed ----
import json  # noqa: E402

from app.config import CONFIG_PATH  # noqa: E402
from app.services import auth  # noqa: E402

_pre_cfg = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None
TOK9 = "abc123def456"
try:
    CONFIG_PATH.write_text(json.dumps({"auth": {
        "enabled": True, "secret_key": "test-secret-treat", "secure_cookies": False,
        "users": {"cheque": auth.hash_password("nekout-test-pass"),
                  "finance": auth.hash_password("nekout-test-pass")},
        "admins": [],
        "access": {"cheque": {"cheque-processing": "modify"},
                   "finance": {"cheque-processing": "modify",
                               "ongoing-ctp-monitoring": "modify"}},
    }}), encoding="utf-8")
    cheques.create_batch(TOK9, [], [], [], uploaded_by="cheque")
    p = cheques.load_batch(TOK9)
    p["cheques"] = [{"idx": 0, "filename": "c.png", "stored": "c.png",
                     "media": "image/png", "status": "done",
                     "result": {"cheque_number": "0770001"},
                     "appearances": [{"bank": "B", "amount": 1.0,
                                      "date": "2026-01-01", "text": "0770001"}],
                     "matched_at": "2026-01-02 09:00", "error": ""}]
    cheques._save(TOK9, p)

    def _login(user):
        c = TestClient(main.app, follow_redirects=False)
        r = c.post("/login", data={"username": user,
                                   "password": "nekout-test-pass"})
        assert r.status_code == 303 and c.cookies.get(auth.COOKIE), r.status_code
        return c

    c_chq = _login("cheque")
    r = c_chq.post("/tools/cheque-processing/register/treat",
                   data={"batch": TOK9, "idx": "0"})
    assert r.status_code == 303 and "error=" in r.headers["location"], \
        "cheque profile must NOT be able to mark treated"
    assert not cheques.load_batch(TOK9)["cheques"][0].get("treated")
    # the cheque profile sees no Mark-treated control on the page
    page = c_chq.get("/tools/cheque-processing", follow_redirects=True)
    assert "Mark treated" not in page.text and "pending" in page.text

    c_fin = _login("finance")
    r = c_fin.post("/tools/cheque-processing/register/treat",
                   data={"batch": TOK9, "idx": "0"})
    assert r.status_code == 303 and "message=" in r.headers["location"]
    assert cheques.load_batch(TOK9)["cheques"][0].get("treated"), \
        "finance profile must be able to mark treated"
    print("ok: 'treated in accounting' — denied for cheque profile, allowed for finance")

    # --- 10. aliases: shown as "Uploaded by X1" on the register ---------------
    assert auth.set_alias("cheque", "X1")
    page = c_fin.get("/tools/cheque-processing", follow_redirects=True)
    assert "X1" in page.text, "alias must replace the username on the register"
    from app.config import load_config as _lc
    assert auth.get_alias(_lc()["auth"], "cheque") == "X1"
    auth.set_alias("cheque", "")            # clearing removes it
    assert auth.get_alias(_lc()["auth"], "cheque") == ""
    print("ok: uploader aliases (X1) shown on the register, admin-settable")
finally:
    cheques.delete_batch(TOK9)
    if _pre_cfg is not None:
        CONFIG_PATH.write_text(_pre_cfg, encoding="utf-8")
    elif CONFIG_PATH.exists():
        CONFIG_PATH.unlink()

print("\nALL CHEQUE PROCESSING TESTS PASSED")
