"""Bank statements: French/accented headers, "Donneur d'ordre" payer
extraction, collections-by-customer (matched to the latest AR), payments-by-
payer (independent of AR), daily totals, and the AI-OCR reading path (mocked).
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from testutil import smtp_guard  # noqa: E402
smtp_guard()

from app.services import ai_ocr, ar_master, bank_statement  # noqa: E402
from app.tools import bank, ongoing_ctp as ctp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "samples" / "ar_master_sample.xlsx"
FEATURE_TXN = ROOT / "samples" / "ar_breakdown_features_sample.xlsx"
FRENCH = ROOT / "samples" / "bank_statement_french_sample.xlsx"


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# --- 1. payer extraction (Donneur d'ordre, various spellings) ---------------
check("payer after 'Donneur d'ordre:'",
      bank_statement.extract_payer("VIR Donneur d'ordre: ACME LOGISTICS") == "ACME LOGISTICS")
check("payer after 'Donneur d'Ordre' (no colon)",
      bank_statement.extract_payer("CHQ Donneur d'Ordre GAMMA SARL") == "GAMMA SARL")
check("no payer label -> empty", bank_statement.extract_payer("FRAIS DE COMPTE") == "")

# --- 1b. date normalisation (ISO / dd-mm-yyyy / French month) ---------------
check("ISO date kept", bank_statement.normalize_date("2026-06-10") == "2026-06-10")
check("dd/mm/yyyy -> ISO", bank_statement.normalize_date("10/06/2026") == "2026-06-10")
check("French month -> ISO", bank_statement.normalize_date("10 juin 2026") == "2026-06-10")
check("unparseable date kept intact (not truncated)",
      bank_statement.normalize_date("Solde reporté") == "Solde reporté")

# --- 2. accent-insensitive headers + credit parsing on a French statement ---
parsed = bank_statement.read_bank(FRENCH)
check("Crédit column detected (accent-insensitive)", parsed["credit_col"] is not None)
credits = [ln for ln in parsed["lines"] if ln["credit"] > 0]
check("4 credit lines read", len(credits) == 4)
acme = next((ln for ln in credits if "ACME" in ln["description"]), None)
check("payer extracted clean (no trailing amount)",
      acme and acme["payer"] == "ACME LOGISTICS")

# --- 3. AR analysis on file so collections-by-customer can match ------------
master = ar_master.parse_master(MASTER)
res = ctp.analyze(FEATURE_TXN, as_of=date(2026, 6, 13), default_rank=60, master=master)
res["created_at"] = "2099-01-01 00:00"        # ensure it is the latest analysis
ctp.save_result("ba0c5511", res)
try:
    rep = bank.build_report([(FRENCH, "Afriland.xlsx")])     # xlsx -> table reader
    s = rep["summary"]
    check("4 credits, no AI needed for xlsx", s["credit_count"] == 4 and not s["ai_used"])
    check("total collected = 2,970,000", s["total_credited"] == 2970000)

    # payments-by-payer is independent of the AR (ZETA is not an AR customer)
    payers = {p["payer"].upper() for p in rep["payments_by_payer"]}
    check("payments-by-payer lists 4 payers", s["payer_count"] == 4)
    check("includes a payer absent from AR (ZETA)", any("ZETA" in p for p in payers))

    # collections-by-customer matches AR names via the payer text
    matched_keys = {m["key"] for m in rep["matched"]}
    check("ACME matched to AR via payer", "1004000001" in matched_keys)

    # daily collections: per-date credit totals
    daily = {d["date"]: d["total"] for d in rep["daily"]}
    check("daily total 10 Jun = 2,170,000", daily.get("2026-06-10") == 2170000)
    check("daily total 12 Jun = 300,000", daily.get("2026-06-12") == 300000)
    print("ok: French statement -> credits, payers, collections, daily totals")

    # --- 4. AI-OCR reading path for a PDF (reader mocked) -------------------
    _orig = ai_ocr.extract_bank_statement
    ai_ocr.extract_bank_statement = lambda b, m, c: {
        "bank_name": "SGBC", "lines": [
            {"date": "2026-06-13", "description": "VIR", "payer": "DELTA CORP",
             "credit": 2400000.0, "debit": 0.0},
            {"date": "2026-06-13", "description": "FRAIS", "payer": "",
             "credit": 0.0, "debit": 5000.0}]}
    pdf = ROOT / "data" / "uploads" / "_test_stmt.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4 dummy")
    try:
        rep2 = bank.build_report([(pdf, "SGBC.pdf")], ai_cfg={"api_key": "x"})
        check("PDF read via AI reader", rep2["summary"]["ai_used"]
              and rep2["banks"][0]["method"] == "AI reader")
        check("AI credit captured", rep2["summary"]["total_credited"] == 2400000)
        check("AI payer grouped", any("DELTA" in p["payer"].upper()
                                      for p in rep2["payments_by_payer"]))
    finally:
        ai_ocr.extract_bank_statement = _orig
        pdf.unlink(missing_ok=True)
    print("ok: PDF statement read by AI OCR (mocked) -> credits + payers")

    # --- 5. async start_report for a PDF: returns at once, fills in later ---
    import time
    _orig2 = ai_ocr.extract_bank_statement
    ai_ocr.extract_bank_statement = lambda b, m, c: {
        "bank_name": "SGBC", "lines": [
            {"date": "2026-06-13", "description": "VIR", "payer": "DELTA CORP",
             "credit": 2400000.0, "debit": 0.0}]}
    pdf2 = ROOT / "data" / "uploads" / "_test_async.pdf"
    pdf2.write_bytes(b"%PDF-1.4 dummy")
    try:
        tok = bank.start_report([(pdf2, "SGBC.pdf")], ai_cfg={"api_key": "x"})
        stub = bank.load_report(tok)
        check("start_report returns immediately with a running stub",
              stub and stub["status"] == "running")
        rep3 = None
        for _ in range(80):
            rep3 = bank.load_report(tok)
            if rep3 and rep3.get("status") == "done":
                break
            time.sleep(0.1)
        check("background read completes (status done)",
              rep3 and rep3["status"] == "done")
        check("async report captured the AI credit",
              rep3["summary"]["total_credited"] == 2400000)
        bank.delete_report(tok)
        print("ok: async PDF read — fast upload, report fills in in the background")
    finally:
        ai_ocr.extract_bank_statement = _orig2
        pdf2.unlink(missing_ok=True)
finally:
    (ctp.STORE_DIR / "ba0c5511.json").unlink(missing_ok=True)

print("\nALL BANK COLLECTIONS TESTS PASSED")
