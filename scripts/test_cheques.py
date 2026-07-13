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
    ctoken = re.search(r"batch/([0-9a-f]+)", r.headers["location"]).group(1)

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

    page = client.get(f"/tools/cheque-processing/batch/{ctoken}")
    assert page.status_code == 200
    assert "0012345" in page.text and "Not found" in page.text and "YES" in page.text
    assert "/tools/bank-statements" in page.text, "no link to the Bank Statements section"
    # the cheque upload form must NOT offer a bank-statement input
    home = client.get("/tools/cheque-processing")
    assert 'name="statements"' not in home.text, "cheque tool still has a bank upload"
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
    app_ = reg["0012345"]["appearances"][0]
    assert app_["bank"] and app_["date"] == "2026-06-10" and "0012345" in app_["text"]

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
finally:
    ai_ocr.extract_cheque, ai_ocr.is_configured = _orig_extract, _orig_ready
    if ctoken:
        cheques.delete_batch(ctoken)
    bank.delete_report(btoken)
    bank_xlsx.unlink(missing_ok=True)

print("\nALL CHEQUE PROCESSING TESTS PASSED")
