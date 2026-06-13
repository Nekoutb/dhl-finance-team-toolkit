"""Cheque payment processing: cheque-number matching against bank statements,
plus an end-to-end upload→read→match flow with the AI reader mocked.
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
from app.services import ai_ocr, bank_statement, cheques  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
client = TestClient(main.app)

# --- 1. media-type detection -------------------------------------------------
assert ai_ocr.media_type_for("cheque.PNG") == "image/png"
assert ai_ocr.media_type_for("c.jpeg") == "image/jpeg"
assert ai_ocr.media_type_for("c.pdf") == "application/pdf"
assert ai_ocr.media_type_for("statement.xlsx") is None
print("ok: media_type_for classifies cheque scans vs unsupported files")

# --- 2. find_appearances: whole-number match, leading zeros, length guard ----
rows = [
    {"bank": "Afriland", "text": "10/06 CHQ 0012345 ACME DEPOSIT", "amount": 1500000.0, "date": "2026-06-10"},
    {"bank": "Afriland", "text": "11/06 transfer 1234567 other", "amount": 50.0, "date": "2026-06-11"},
    {"bank": "Ecobank",  "text": "VIR 12345 BETA", "amount": 900.0, "date": "2026-06-09"},
]
m = cheques.find_appearances("0012345", rows)
assert len(m) == 1 and m[0]["bank"] == "Afriland" and m[0]["amount"] == 1500000.0, m
m2 = cheques.find_appearances("12345", rows)          # matches Ecobank's 12345
assert any(a["bank"] == "Ecobank" for a in m2), m2
assert cheques.find_appearances("999", rows) == []     # too short -> no match
assert cheques.find_appearances("7654321", rows) == []  # absent number
print("ok: find_appearances matches whole numbers, tolerates zeros, guards length")

# --- 3. detect_bank: header keyword, else file name --------------------------
assert "ECOBANK" in cheques.detect_bank(["ECOBANK CAMEROUN SA", "Statement"], "x.xlsx").upper()
assert cheques.detect_bank([], "Afriland_June.xlsx") == "Afriland_June"
print("ok: detect_bank reads the letterhead, else the file name")

# --- 4. parse_bank_files on a crafted statement ------------------------------
tmp = ROOT / "data" / "uploads" / "_test_bank_chq.xlsx"
tmp.parent.mkdir(parents=True, exist_ok=True)
pd.DataFrame([
    {"Date": "2026-06-10", "Description": "CHEQUE 0012345 ACME LOGISTICS", "Credit": 1500000},
    {"Date": "2026-06-11", "Description": "TRANSFER GAMMA", "Credit": 200000},
]).to_excel(tmp, index=False)
bank_rows, summary = cheques.parse_bank_files([(tmp, "Afriland.xlsx")])
assert summary[0]["bank"] == "Afriland" and summary[0]["lines"] == 2, summary
assert cheques.find_appearances("0012345", bank_rows), "crafted cheque not matched"
tmp.unlink(missing_ok=True)
print("ok: parse_bank_files reads a statement and exposes searchable rows")

# --- 5. end-to-end upload → read (AI mocked) → match → results table ---------
_orig_extract = ai_ocr.extract_cheque
_orig_ready = ai_ocr.is_configured
ai_ocr.is_configured = lambda cfg: True

def _fake_extract(file_bytes, media_type, ai_cfg):
    num = (re.search(rb"\d+", file_bytes) or [b""])[0].decode()
    return {"cheque_number": num, "customer_name": f"Customer {num[-3:]}",
            "amount": 1500000.0 if num == "0012345" else 42000.0,
            "amount_currency": "XAF", "cheque_date": "2026-06-08",
            "drawee_bank": "Standard Chartered", "readability": "good"}

ai_ocr.extract_cheque = _fake_extract
try:
    bank_xlsx = ROOT / "data" / "uploads" / "_e2e_bank.xlsx"
    pd.DataFrame([
        {"Date": "2026-06-10", "Description": "CHEQUE 0012345 DEPOSIT", "Credit": 1500000},
        {"Date": "2026-06-11", "Description": "MISC", "Credit": 100},
    ]).to_excel(bank_xlsx, index=False)

    files = [
        ("cheques", ("chq_found.png", b"CHQ 0012345", "image/png")),
        ("cheques", ("chq_missing.png", b"CHQ 0099999", "image/png")),
        ("statements", ("Afriland.xlsx", bank_xlsx.read_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
    ]
    r = client.post("/tools/cheque-processing/upload", files=files,
                    follow_redirects=False)
    assert r.status_code == 303, r.status_code
    token = re.search(r"batch/([0-9a-f]+)", r.headers["location"]).group(1)

    for _ in range(60):                       # wait for the background batch
        b = cheques.load_batch(token)
        if b and b["status"] == "done":
            break
        time.sleep(0.2)
    assert b and b["status"] == "done", "batch did not finish"

    by_no = {c["result"]["cheque_number"]: c for c in b["cheques"]
             if c["status"] == "done"}
    assert by_no["0012345"]["appearances"], "found cheque not matched to bank"
    app0 = by_no["0012345"]["appearances"][0]
    assert app0["bank"] == "Afriland" and app0["amount"] == 1500000.0 \
        and app0["date"] == "2026-06-10", app0
    assert by_no["0099999"]["appearances"] == [], "absent cheque wrongly matched"

    # results page renders the table with the match columns
    page = client.get(f"/tools/cheque-processing/batch/{token}")
    assert page.status_code == 200
    assert "0012345" in page.text and "Afriland" in page.text
    assert "Not found" in page.text and "YES" in page.text
    print("ok: upload-read-match end-to-end (1 found on bank, 1 not found)")

    # Excel export round-trips with the right columns
    out = cheques.BATCH_DIR / token / "cheques.xlsx"
    cheques.build_excel(out, b)
    df = pd.read_excel(out)
    for col in ["Cheque number", "Customer name", "Amount", "Cheque date",
                "On bank statement?", "Bank(s)", "Amount per statement",
                "Transaction date"]:
        assert col in df.columns, (col, df.columns.tolist())
    print("ok: Excel cheque register exports with all required columns")

    bank_xlsx.unlink(missing_ok=True)
    cheques.delete_batch(token)
finally:
    ai_ocr.extract_cheque = _orig_extract
    ai_ocr.is_configured = _orig_ready

print("\nALL CHEQUE PROCESSING TESTS PASSED")
