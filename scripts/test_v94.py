"""v9.4 — cheque↔AR columns, CtP daily stats + graphs, dashboard KPIs,
Orange Money AR columns + upload history, Account Stop.

All stores are isolated to a temp dir; the CtP store itself is pointed at the
temp dir so the fabricated analysis is the only one the app sees."""
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from testutil import smtp_guard  # noqa: E402
smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.services import auth, cheques, customers  # noqa: E402
from app.tools import account_stop, bank, bitcash  # noqa: E402
from app.tools import ongoing_ctp as ctp  # noqa: E402
from app.tools import orange_cameroun as orange  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "orange_statement_sample.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_tmp = Path(tempfile.mkdtemp(prefix="v94_"))

# Hermetic stores — nothing in data/ is read or written by this test.
ctp.STORE_DIR = _tmp / "ctp"
ctp.STATS_PATH = _tmp / "ctp_daily_stats.json"
cheques.BATCH_DIR = _tmp / "cheq_batches"
cheques.STATS_PATH = _tmp / "cheque_daily_stats.json"
bank.STORE_DIR = _tmp / "bank"
orange.HISTORY_DIR = _tmp / "orange"
customers._PATH = _tmp / "customers.json"
bitcash.RECON_DIR = _tmp / "recons"
bitcash.STORE_PATH = _tmp / "bitcash.json"
bitcash.ROWS_PATH = _tmp / "bitcash_rows.json"
bitcash.FILES_DIR = _tmp / "bitfiles"
bitcash.UPLOAD_DIR = _tmp / "bit_uploads"
bitcash.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


client = TestClient(main.app, follow_redirects=False)

# === 1. CtP daily stats maths ================================================
tiny = {"customers": [], "summary": {"total_ar": 150.0},
        "invoices": [
            {"kind": "invoice", "amount": 100.0, "days_overdue": 70},
            {"kind": "invoice", "amount": 100.0, "days_overdue": 10},
            {"kind": "credit", "amount": -50.0, "days_overdue": 0}]}
s = ctp.compute_daily_stats(tiny)
check("pct>60 = 50.0 (credits excluded from the base)",
      s["pct_over_60"] == 50.0 and s["pct_over_90"] == 0.0)
check("empty result -> 0.0 not division error",
      ctp.compute_daily_stats({"customers": [], "invoices": [],
                               "summary": {}})["pct_over_60"] == 0.0)

hist = ctp.record_daily_stats(tiny, day="2026-07-01")
check("day override recorded", "2026-07-01" in hist)
for i in range(200):                       # prune keeps ~180 days
    hist = ctp.record_daily_stats(tiny, day=f"2027-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}")
check("history pruned to 180 days", len(hist) <= 180)
ctp.STATS_PATH.unlink()                    # clean slate for the page tests

# === 2. Account Stop empty state (no analysis on file yet) ==================
r = client.get("/tools/account-stop")
check("account-stop page 200 (empty)", r.status_code == 200)
check("no-analysis empty state shown", "No AR analysis on file yet" in r.text)

# === 3. Fabricated latest analysis (ACME stopped, BETA ok) ==================
RESULT = {
    "as_of": date(2026, 7, 16), "mapping": {}, "header": [],
    "default_rank": 60,
    "invoices": [
        {"kind": "invoice", "customer": "ACME LOGISTICS SARL",
         "account": "1004000001", "invoice_no": "INV-1", "amount": 300000.0,
         "days_overdue": 75, "invoice_date": None, "due_date": None},
        {"kind": "invoice", "customer": "ACME LOGISTICS SARL",
         "account": "1004000001", "invoice_no": "INV-2", "amount": 100000.0,
         "days_overdue": 95, "invoice_date": None, "due_date": None},
        {"kind": "invoice", "customer": "BETA TRADING SA",
         "account": "1004000002", "invoice_no": "INV-3", "amount": 100000.0,
         "days_overdue": 5, "invoice_date": None, "due_date": None},
        {"kind": "credit", "customer": "BETA TRADING SA",
         "account": "1004000002", "invoice_no": "CR-1", "amount": -50000.0,
         "days_overdue": 0, "invoice_date": None, "due_date": None},
    ],
    "customers": [
        {"key": "1004000001", "customer": "ACME LOGISTICS SARL",
         "total_ar": 400000.0, "overdue": 400000.0, "gross_ar": 400000.0,
         "status_key": "stop", "status": "Stop credit", "master_hold": True},
        {"key": "1004000002", "customer": "BETA TRADING SA",
         "total_ar": 50000.0, "overdue": 0.0, "gross_ar": 100000.0,
         "status_key": "ok", "status": "Within terms", "master_hold": False},
    ],
    "controls_summary": [],
    "summary": {"total_ar": 450000.0, "overdue_total": 400000.0,
                "credits_total": -50000.0, "actions_due": 0,
                "invoice_count": 4, "customer_count": 2},
    "total_check": None, "tb_check": None, "reconciliation": None,
    "master_used": False, "master_stats": None, "unmapped": [],
    "currencies": ["XAF"],
    "source": "ar_test_v94.xlsx", "created_at": "2026-07-17 08:00",
}
ctp.save_result("94feedbeefab", RESULT)
s = ctp.compute_daily_stats(json.loads(json.dumps(RESULT, default=str)))
check("held honours master_hold (1 of 2)", s["held"] == 1)
check("pct>60 = 80.0 / pct>90 = 20.0",
      s["pct_over_60"] == 80.0 and s["pct_over_90"] == 20.0)
ctp.record_daily_stats(RESULT)

r = client.get("/tools/ongoing-ctp-monitoring")
check("CtP portal shows the held-accounts daily graph",
      r.status_code == 200 and "Accounts on credit hold" in r.text)

# === 4. Cheque register AR columns ==========================================
bdir = cheques.BATCH_DIR / "aa11bb22cc33"
bdir.mkdir(parents=True)
(bdir / "results.json").write_text(json.dumps({
    "status": "done", "uploaded_by": "tester",
    "created_at": "2026-07-17 08:30", "banks": [], "bank_line_count": 0,
    "cheques": [
        {"idx": 0, "filename": "acme.png", "stored": "000.png",
         "media": "image/png", "status": "done",
         "result": {"cheque_number": "5550001",
                    "customer_name": "ACME LOGISTICS SARL",
                    "amount": 150000.0, "amount_currency": "XAF",
                    "cheque_date": "2026-07-01", "drawee_bank": "SCB",
                    "readability": "good"},
         "appearances": [{"bank": "SGBC", "amount": 150000.0,
                          "date": "2026-07-10",
                          "text": "REMISE CHQ 5550001 ACME"}], "error": ""},
        {"idx": 1, "filename": "who.png", "stored": "001.png",
         "media": "image/png", "status": "done",
         "result": {"cheque_number": "5550002",
                    "customer_name": "TOTALLY UNKNOWN ZZZQX",
                    "amount": 42000.0, "amount_currency": "XAF",
                    "cheque_date": "2026-07-02", "drawee_bank": "UBA",
                    "readability": "good"},
         "appearances": [], "error": ""},
    ]}, ensure_ascii=False), encoding="utf-8")

r = client.get("/tools/cheque-processing")
check("register shows the AR columns", r.status_code == 200
      and "AR customer (latest analysis)" in r.text
      and "Overdue balance" in r.text)
check("ACME matched to its AR account + balances",
      "1004000001" in r.text and "400,000" in r.text)
check("unknown name shows — (no match)", ">TOTALLY UNKNOWN ZZZQX<" in r.text)

rx = client.get("/tools/cheque-processing/register/export")
check("register Excel downloads", rx.status_code == 200)
import io  # noqa: E402

import openpyxl  # noqa: E402
wsx = openpyxl.load_workbook(io.BytesIO(rx.content)).active
head = [c.value for c in wsx[1]]
check("Excel carries the 4 AR columns in place",
      head[4:9] == ["Client name", "AR customer (latest analysis)",
                    "AR account", "AR balance", "Overdue balance"]
      and "Scan file" in head and head.index("Scan file") == 11)
acme_row = next(row for row in wsx.iter_rows(min_row=2, values_only=True)
                if row[4] == "ACME LOGISTICS SARL")
check("Excel AR values populated (account + balances)",
      acme_row[6] == "1004000001" and acme_row[7] == 400000
      and acme_row[8] == 400000)

# === 5. Dashboard ============================================================
r = client.get("/")
check("dashboard shows the unpresented-cheques KPI",
      r.status_code == 200 and "Unpresented cheques" in r.text)
check("dashboard shows the ageing trend", "&gt; 60 days %" in r.text
      or "> 60 days %" in r.text)
check("dashboard shows the trends section",
      "Receivables ageing" in r.text and "open items per day" in r.text)

# === 6. Orange Money — AR columns, history, comparison ======================
customers.set_name(orange.TOOL_SLUG, "699559325", "ACME LOGISTICS SARL")
customers.set_name(orange.ACCT_SLUG, "699559325", "1004000001")

with open(SAMPLE, "rb") as fh:
    r = client.post("/tools/orange-cameroun/upload",
                    files={"file": ("orange_mai.xlsx", fh, XLSX)})
check("review page renders", r.status_code == 200)
check("review shows the AR-ledger columns",
      "Account per AR ledger" in r.text and "Balance per AR ledger" in r.text)
check("saved account joined to the AR ledger (400 000 balance)",
      "400 000" in r.text)
m = re.search(r'name="htoken" value="([0-9a-f]{12})"', r.text)
check("history token embedded for generate", bool(m))
htoken = m.group(1)
check("upload summary persisted", (orange.HISTORY_DIR / f"{htoken}.json").exists())

with open(SAMPLE, "rb") as fh:
    client.post("/tools/orange-cameroun/upload",
                files={"file": ("orange_mai_again.xlsx", fh, XLSX)})
check("re-upload of the same month replaces (stable token)",
      len(list(orange.HISTORY_DIR.glob("*.json"))) == 1)

u = orange.mark_generated(htoken, {"699559325": {
    "name": "ACME LOGISTICS SARL", "account": "1004000001"}})
check("generated stamp + identities stored on the history entry",
      u and u["generated_at"]
      and any(c["account"] == "1004000001" for c in u["correspondants"]))

r = client.get("/tools/orange-cameroun")
check("previous uploads listed on the tool page",
      "Previous uploads" in r.text and "Compare vs AR" in r.text)

r = client.get(f"/tools/orange-cameroun/history/{htoken}")
check("history comparison page renders", r.status_code == 200
      and "Balance per latest AR TB" in r.text and "400 000" in r.text
      and "1004000001" in r.text)
r = client.get("/tools/orange-cameroun/history/ffffffffffff")
check("unknown history token -> friendly redirect",
      r.status_code == 303 and "error=" in r.headers["location"])

# === 7. Account Stop =========================================================
check("account-stop registered", any(t["slug"] == "account-stop"
                                     for t in __import__("app.tools.registry",
                                                          fromlist=["TOOLS"]).TOOLS))
bank.STORE_DIR.mkdir(parents=True, exist_ok=True)
(bank.STORE_DIR / "beefcafe0001.json").write_text(json.dumps({
    "token": "beefcafe0001", "created_at": "2026-07-15 10:00",
    "unmatched": [
        {"payer": "ACME LOGISTICS", "description": "VIR ACME LOGISTICS SARL",
         "credit": 250000.0, "debit": 0.0, "date": "2026-07-14",
         "text": "VIR ACME LOGISTICS SARL REF 778", "bank": "SGBC"},
        {"payer": "", "description": "FRAIS TENUE DE COMPTE",
         "credit": 0.0, "debit": 5000.0, "date": "2026-07-14",
         "text": "FRAIS", "bank": "SGBC"}],
    "matched": []}, ensure_ascii=False), encoding="utf-8")

lines = bank.all_credit_lines()
check("all_credit_lines keeps credits only (payer preserved)",
      len(lines) == 1 and lines[0]["payer"] == "ACME LOGISTICS"
      and lines[0]["credit"] == 250000.0)

view = account_stop.build_view()
check("one stopped account in the view",
      view["counts"]["stopped"] == 1
      and view["accounts"][0]["key"] == "1004000001")
a = view["accounts"][0]
check("bank payment traced", a["bank"]
      and a["bank"][0]["amount"] == 250000.0 and a["bank"][0]["bank"] == "SGBC")
check("cheque payment traced (cleared bank)", a["cheques"]
      and a["cheques"][0]["bank"] == "SGBC"
      and a["cheques"][0]["amount"] == 150000.0)
check("mobile money traced from the orange upload", a["momo"]
      and a["momo"][0]["operator"] == "Orange Money"
      and all(p["amount"] > 0 for p in a["momo"]))
momo_before = len(a["momo"])
view2 = account_stop.build_view()
check("momo payments deduped across re-reads",
      len(view2["accounts"][0]["momo"]) == momo_before)

r = client.get("/tools/account-stop")
check("account-stop page renders the three groups", r.status_code == 200
      and "Bank payments" in r.text and "Cheque payments" in r.text
      and "Mobile money" in r.text)
check("Account Stop present in the sidebar menu",
      'href="/tools/account-stop"' in r.text
      and 'nav-label">Account Stop' in r.text)
check("stopped account row rendered with payments",
      "1004000001" in r.text and "ACME LOGISTICS" in r.text
      and "Orange Money" in r.text and "250,000" in r.text)

# === 8. Access rights cover the new slug =====================================
acfg = {"enabled": True, "users": {"a": "x", "e": "x"}, "admins": ["a"],
        "access": {"e": {"cheque-processing": "read"}}}
check("non-admin without grant -> none",
      auth.area_level(acfg, "e", "account-stop") == "none")
acfg["access"]["e"]["account-stop"] = "read"
check("non-admin with grant -> read; admin always modify",
      auth.area_level(acfg, "e", "account-stop") == "read"
      and auth.area_level(acfg, "a", "account-stop") == "modify")

# === 9. BIT & Cash AR banner — no age bracket ================================
r = client.get("/tools/bit-cash-ar")
check("banner carries no '(today)' bracket",
      r.status_code == 200 and "(today)" not in r.text
      and "day old" not in r.text)

# === 10. BIT recon: arithmetic total, ±1000 window, manual plug =============
AWBS = [("4095823454", 53639.0), ("3751239391", 53639.0),
        ("6057894780", 53639.0), ("1616021735", 53639.0),
        ("1107827641", 56100.0), ("8457401400", 59600.0),
        ("8457378215", 73800.0), ("7177996162", 119515.0)]
FAKE_ROWS = {
    "bit_header": [],
    "bit": [{"id": 0, "amount": -523500.0, "gl_account": "1263001293",
             "assignment": "0548407000018", "posting_date": "07.07.2026",
             "reference": "CMBUE", "text": "CASH DEPOSIT BUEA",
             "doc_no": "410001", "raw": []},
            {"id": 1, "amount": -1000000.0, "gl_account": "1263001293",
             "assignment": "0548407000019", "posting_date": "07.07.2026",
             "reference": "X", "text": "OTHER", "doc_no": "410002",
             "raw": []}],
    "cash": [{"id": i, "awb": awb, "reference": awb, "amount": amt,
              "sap_acct": "4003025929", "assignment": awb,
              "customer": "DHL SERVICE POINT BUEA", "doc_no": f"90000{i}"}
             for i, (awb, amt) in enumerate(AWBS)],
}
bitcash.rows_store = lambda: FAKE_ROWS

st = {"label": "DHL BUEA CASHCMBUE", "date": "07.07.2026",
      "total": 523500.0,        # the (wrong) figure a document might state
      "lines": [{"reference": awb, "amount": amt, "description": ""}
                for awb, amt in AWBS]}
lines, ar_sel, cands, bit_sel = bitcash.automatch(st)
check("evidence total recomputed arithmetically (523,571)",
      st["total"] == 523571.0 and st["stated_total"] == 523500.0)
check("BIT candidate found within the window (523,500) and auto-selected",
      cands == [0] and bit_sel == 0)
check("all 8 AWBs matched to the Cash AR",
      len(ar_sel) == 8 and all(ln["matched_ids"] for ln in lines))
check("margin is a constant of 1000", bitcash.BIT_MATCH_MARGIN == 1000.0)

near = {"total": 0, "lines": [{"reference": "999", "amount": 524000.0,
                               "description": ""}]}
_l, _a, near_cands, near_sel = bitcash.automatch(near)
check("a 500-off amount is now a candidate (524,000 finds 523,500)",
      near_cands == [0] and near_sel == 0)

far = {"total": 0, "lines": [{"reference": "999", "amount": 525000.0,
                              "description": ""}]}
_l, _a, far_cands, _s = bitcash.automatch(far)
check("amounts beyond ±1000 are not candidates (525,000 vs 523,500)",
      far_cands == [])

st["lines"] = lines
rec = {"token": "feedbead0001", "status": "open",
       "uploaded": "2026-07-17 10:00", "uploaded_by": "tester",
       "source": "DHL BUEA CASHCMBUE 07.07.26.xlsx", "statement": st,
       "ar_selected": ar_sel, "bit_candidates": cands, "bit_selected": bit_sel,
       "bit_duplicate": False, "error": ""}
bitcash.save_recon(rec)
view = bitcash.recon_view(rec)
check("difference disclosed (71 = 523,571 - 523,500)",
      view["ar_total"] == 523571.0 and view["bit_amount"] == 523500.0
      and view["difference"] == 71.0 and view["residual"] == 71.0)
check("candidate carries its delta vs the evidences (-71)",
      view["bit_candidates"][0]["delta"] == -71.0)

r = client.get("/tools/bit-cash-ar/recon/feedbead0001")
check("sandbox narrative discloses the arithmetic total",
      r.status_code == 200 and "Total of evidences is XAF" in r.text
      and "523,571" in r.text)
check("sandbox shows the manual-plug section", "Manual plug" in r.text)

r = client.post("/tools/bit-cash-ar/recon/feedbead0001/plug",
                data={"amount": "71", "account": "", "note": ""})
check("plug without a G/L account is refused",
      r.status_code == 303 and "error=" in r.headers["location"])
r = client.post("/tools/bit-cash-ar/recon/feedbead0001/plug",
                data={"amount": "71", "account": "471000",
                      "note": "BANKED SHORT"})
check("plug saved via the route", r.status_code == 303
      and "Manual+plug+saved" in r.headers["location"].replace("%20", "+"))
view = bitcash.recon_view(bitcash.load_recon("feedbead0001"))
check("residual is 0 after the plug",
      view["plug"]["amount"] == 71.0 and view["residual"] == 0.0)

bitcash.set_status("feedbead0001", True, "tester")
je = bitcash.build_journal(_tmp / "je_v95.xlsx")
check("journal built — 8 AWB pairs + balanced plug pair = 18 lines",
      je and je["count"] == 1 and je["lines"] == 18
      and je["tokens"] == ["feedbead0001"])
wj = openpyxl.load_workbook(_tmp / "je_v95.xlsx")
wsj = next(ws for ws in wj.worksheets if ws.title.startswith("CM01_"))
jrows = [row for row in wsj.iter_rows(min_row=4, max_row=24,
                                      values_only=True)
         if row[0] is not None]
check("bank side split per AWB (first pair: 40 + 15, 53,639, AWB in Doc.Nr)",
      jrows[0][9] == 1263001293 and jrows[0][10] == 40
      and jrows[0][11] == 53639.0 and jrows[0][6] == "4095823454"
      and jrows[1][9] == 4003025929 and jrows[1][10] == 15
      and jrows[1][11] == 53639.0 and jrows[1][6] == "4095823454")
plug_pair = [r for r in jrows if r[11] == 71.0]
check("plug booked as a balanced pair (BIT G/L key 50 + 471000 key 40)",
      len(plug_pair) == 2
      and plug_pair[0][9] == 1263001293 and plug_pair[0][10] == 50
      and plug_pair[1][9] == 471000 and plug_pair[1][10] == 40
      and plug_pair[1][13] == "BANKED SHORT")
sum40 = sum(r[11] for r in jrows if r[10] == 40)
sum50 = sum(r[11] for r in jrows if r[10] == 50)
sum15 = sum(r[11] for r in jrows if r[10] == 15)
check("workbook CHECK stays balanced (+40s -50s -15s = 0)",
      round(sum40 - sum50 - sum15, 2) == 0.0)
check("BIT G/L nets to the actually banked amount (523,500)",
      round(sum(r[11] if r[10] == 40 else -r[11]
                for r in jrows if r[9] == 1263001293), 2) == 523500.0)

# === 11. v9.6 — evidence reader fallback + async BIT/Cash AR uploads ========
import time  # noqa: E402


def _buea_like(path):
    """Replica of the DHL BUEA layout that broke the reader: decorative rows,
    an Excel table's phantom 'Column1…' header ABOVE the real header, a #REF!
    remnant and a TEXT total cell."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["", "", "", "", "", "", "", "#REF!"])
    ws.append(["", "", "DHL BUEA CASH RECORD SHEET.", "", "", "", "", ""])
    ws.append(["", "", "Column1", "Column2", "   ", "Column4", "Column5", ""])
    ws.append(["", "", "Date", "Waybill", "Weight (kg, gram)", "Country",
               "Amount (Xaf)", ""])
    ws.append(["", "", "07.07.2026", 4095823454, "0.5 KG", "CANADA", 53639, ""])
    ws.append(["", "", "07.07.2026", 1107827641, "0.5 KG", "FRANCE", 56100, ""])
    ws.append(["", "", "07.07.2026", 7177996162, "0.5 KG", "SURINAME",
               119515, ""])
    ws.append(["", "", "", "", "", "", "TOTAL: 229.254", ""])
    wb.save(path)
    return path


st6 = bitcash._read_excel_statement(_buea_like(_tmp / "buea_like.xlsx"))
check("BUEA-style layout read via the row-scan fallback (3 AWBs, 229,254)",
      st6["total"] == 229254.0 and len(st6["lines"]) == 3
      and st6["lines"][0]["reference"] == "4095823454")

bad = _tmp / "no_columns.xlsx"
wbx = openpyxl.Workbook()
wbx.active.append(["Foo", "Bar"])
wbx.active.append(["x", "y"])
wbx.save(bad)
try:
    bitcash._read_excel_statement(bad)
    check("unreadable evidence raises a clear error", False)
except ValueError as exc:
    check("unreadable evidence raises a clear error",
          "Waybill / AWB / Reference" in str(exc))


def _wait_bitcash(timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not bitcash.status().get("processing"):
            return
        time.sleep(0.1)
    raise SystemExit("background BIT/Cash AR processing never finished")


# Processing banner branches (deterministic — flag written directly).
bitcash._save({"current": {}, "history": {},
               "processing": {"started": "2026-07-17 12:00",
                              "beat": time.time(),
                              "files": [{"kind": "bit", "label": "BIT",
                                         "source": "big.xlsx"}],
                              "stage": "storing the rows", "current": "BIT",
                              "pct": 62, "rows_done": 1500,
                              "rows_total": 2400, "eta_seconds": 95}})
r = client.get("/tools/bit-cash-ar")
flat = " ".join(r.text.split())
check("live tracker shown: %, rows and time left",
      "Ingesting" in flat and "location.replace" in r.text
      and "62%" in flat and "1,500 of 2,400 row(s)" in flat
      and "1 min 35 s left" in flat)
# A stale flag (no heartbeat for >15 min — server restarted) self-clears.
bitcash._save({"current": {}, "history": {},
               "processing": {"started": "2026-07-17 12:00", "beat": 1.0,
                              "files": [], "stage": "queued", "pct": 0}})
st_stale = bitcash.status()
check("stale processing flag cleared into an error",
      not st_stale.get("processing")
      and "interrupted" in (st_stale.get("processing_error") or {})
      .get("message", ""))
bitcash._save({"current": {}, "history": {},
               "processing_error": {"at": "2026-07-17 12:01",
                                    "message": "Could not read big.xlsx"}})
r = client.get("/tools/bit-cash-ar")
check("failed upload surfaced on the page",
      "The last upload failed" in r.text and "Could not read big.xlsx" in r.text)
bitcash._save({"current": {}, "history": {}})

# Real async round-trip: instant 303, counts land in the background.
wb_bit = openpyxl.Workbook()
wb_bit.active.append(["Ref", "Amount", "Status"])
wb_bit.active.append(["T1", 100, "Open"])
wb_bit.active.append(["T2", 200, "Closed"])
bit_path = _tmp / "bit_async.xlsx"
wb_bit.save(bit_path)
with open(bit_path, "rb") as fh:
    r = client.post("/tools/bit-cash-ar/upload",
                    files={"bit_file": ("bit_async.xlsx", fh, XLSX)})
check("upload responds instantly with a redirect", r.status_code == 303
      and "received" in r.headers["location"])
_wait_bitcash()
st7 = bitcash.status()
check("counts recorded by the background parser",
      st7["current"]["bit"]["open_items"] == 1
      and not st7.get("processing") and not st7.get("processing_error"))

# Corrupt file -> background error recorded, nothing crashes.
junk = _tmp / "junk.xlsx"
junk.write_bytes(b"this is not a workbook")
bitcash.process_uploads_async([("bit", junk, "junk.xlsx")])
_wait_bitcash()
check("corrupt upload lands as a processing error",
      (bitcash.status().get("processing_error") or {}).get("message", "")
      .startswith("Could not read junk.xlsx"))

# === 12. v9.9 — JE pack, variance email, deposit slip, progress, replace ====
import zipfile  # noqa: E402
from io import BytesIO  # noqa: E402
from urllib.parse import parse_qs, urlencode, urlparse  # noqa: E402

# --- pack: advice + slip + evidences Excel + journal, zipped
bitcash.FILES_DIR.mkdir(parents=True, exist_ok=True)
(bitcash.FILES_DIR / "advice_feedbead0001.pdf").write_bytes(b"%PDF-1.4 advice")
rec1 = bitcash.load_recon("feedbead0001")
rec1["file"] = "advice_feedbead0001.pdf"
bitcash.save_recon(rec1)
(bitcash.FILES_DIR / "slip_feedbead0001.pdf").write_bytes(b"%PDF-1.4 slip")
check("deposit slip recorded on the sandbox",
      bitcash.set_slip("feedbead0001", "slip_feedbead0001.pdf",
                       "BUEA deposit slip.pdf") is not None)

pack = _tmp / "pack.zip"
bitcash.build_pack(pack, _tmp / "je_v95.xlsx",
                   "CM01_PC TEMPLATE_17.07.26_PC.xlsx", ["feedbead0001"])
with zipfile.ZipFile(pack) as zf:
    names = zf.namelist()
    check("pack carries the journal at the root",
          "CM01_PC TEMPLATE_17.07.26_PC.xlsx" in names)
    check("pack carries the advice copy, the slip and the evidences Excel",
          any("Payment advice - " in n for n in names)
          and any("Deposit slip - " in n for n in names)
          and any(n.endswith("Evidences_feedbead0001.xlsx") for n in names))
    ev_name = next(n for n in names if n.endswith(".xlsx") and "Evidences" in n)
    wse = openpyxl.load_workbook(BytesIO(zf.read(ev_name)))["Evidences"]
    ev_text = " ".join(str(c.value) for row in wse.iter_rows()
                       for c in row if c.value is not None)
    check("evidences Excel lists the AWBs, the total and the plug",
          "4095823454" in ev_text and "TOTAL OF EVIDENCES" in ev_text
          and "523571" in ev_text.replace(",", "").replace(".0", "")
          and "471000" in ev_text)

# --- journal route now redirects with journal + pack downloads
r = client.post("/tools/bit-cash-ar/journal")
check("journal route -> redirect with journal + pack params",
      r.status_code == 303 and "journal=" in r.headers["location"]
      and "pack=" in r.headers["location"])
qs2 = parse_qs(urlparse(r.headers["location"]).query)
page = client.get("/tools/bit-cash-ar?" + urlencode(
    {"journal": qs2["journal"][0], "jname": qs2["jname"][0],
     "pack": qs2["pack"][0]}, doseq=True))
check("home page offers the pack + journal downloads",
      "Full pack" in page.text and "Journal entry only" in page.text)
(main.OUTPUT_DIR / qs2["journal"][0]).unlink(missing_ok=True)
(main.OUTPUT_DIR / qs2["pack"][0]).unlink(missing_ok=True)

# --- variance email: excess payment far above the 5,000 threshold
st2 = dict(st, lines=[dict(ln) for ln in st["lines"]])
rec2 = {"token": "feedbead0002", "status": "open",
        "uploaded": "2026-07-17 11:00", "uploaded_by": "tester",
        "source": "EXCESS CASE.xlsx", "statement": st2,
        "ar_selected": list(range(8)), "bit_candidates": [0, 1],
        "bit_selected": 1, "bit_duplicate": False, "error": "",
        "file": ""}
bitcash.save_recon(rec2)
view2 = bitcash.recon_view(rec2)
check("excess payment above threshold flags the email trigger",
      view2["difference"] == -476429.0 and view2["needs_email"])
page = client.get("/tools/bit-cash-ar/recon/feedbead0002")
check("sandbox shows the variance clarification prompt",
      "Variance clarification" in page.text
      and "excess payment" in page.text
      and "5,000" in page.text and "account holder" in page.text)
r = client.post("/tools/bit-cash-ar/recon/feedbead0002/email",
                data={"recipient": "client@example.com",
                      "subject": "Clarification",
                      "body": "An excess payment of 476,429 XAF was received."})
check("email route falls back to a ready-to-send .eml (SMTP off in tests)",
      r.status_code == 303 and "ready-to-send" in
      r.headers["location"].replace("+", " ").replace("%20", " "))
ve = (bitcash.load_recon("feedbead0002") or {}).get("variance_email") or {}
check("variance email recorded on the sandbox (eml mode)",
      ve.get("mode") == "eml" and ve.get("to") == "client@example.com"
      and (main.OUTPUT_DIR / ve.get("eml", "x")).exists())
page = client.get("/tools/bit-cash-ar/recon/feedbead0002")
check("sandbox discloses the prepared clarification",
      "Clarification" in page.text and "prepared" in page.text)
(main.OUTPUT_DIR / ve["eml"]).unlink(missing_ok=True)

# --- slip route + evidence download
r = client.post("/tools/bit-cash-ar/recon/feedbead0002/slip",
                files={"slip": ("Deposit BUEA.pdf", b"%PDF-1.4 slip2",
                                "application/pdf")})
check("slip upload attaches to the sandbox", r.status_code == 303
      and (bitcash.FILES_DIR / "slip_feedbead0002.pdf").exists())
r = client.get("/tools/bit-cash-ar/recon/feedbead0002/evidence/slip")
check("slip is viewable via the evidence route",
      r.status_code == 200 and r.content.startswith(b"%PDF"))
r = client.get("/tools/bit-cash-ar/recon/feedbead0002/evidence/advice",
               follow_redirects=False)
check("missing advice degrades to a friendly redirect",
      r.status_code == 303 and "error=" in r.headers["location"])

# --- single-parse progress ticks + replace cleanup + history continuity
data_bc = bitcash._load()
data_bc["history"]["2000-01-01"] = {"bit_open": 5}
bitcash._save(data_bc)

wbp = openpyxl.Workbook()
wbp.active.append(["Ref", "Amount", "Status"])
for i in range(3):
    wbp.active.append([f"P{i}", 100 + i, "Open"])
p1 = bitcash.UPLOAD_DIR / "bitcash_bit_first.xlsx"
wbp.save(p1)
ticks = []
bitcash.record_upload("bit", p1, "first.xlsx",
                      progress=lambda **kw: ticks.append(kw))
check("progress ticks cover the stages up to 100%",
      any(t.get("stage") == "reading the workbook" for t in ticks)
      and any(t.get("pct") == 100 for t in ticks)
      and any(t.get("rows_total") == 3 for t in ticks))
check("current position remembers the stored file",
      bitcash.status()["current"]["bit"]["stored"] == "bitcash_bit_first.xlsx")

wbp.save(bitcash.UPLOAD_DIR / "bitcash_bit_second.xlsx")
bitcash.record_upload("bit", bitcash.UPLOAD_DIR / "bitcash_bit_second.xlsx",
                      "second.xlsx")
check("replaced upload file cleaned up, new one is the reference",
      not p1.exists()
      and bitcash.status()["current"]["bit"]["stored"]
      == "bitcash_bit_second.xlsx")
hist_days = dict(bitcash.status()["history"])
check("daily graph keeps prior-day figures after replacement",
      "2000-01-01" in hist_days and hist_days["2000-01-01"]["bit_open"] == 5)

# === 13. v10.0 — reconciliations fixed in time + slip-anchored matching =====
# Simulate a file replacement: the row store gets NEW generation stamps.
FAKE_ROWS["gen_bit"] = "genB1"
FAKE_ROWS["gen_cash"] = "genC1"

# (a) The approved sandbox is FROZEN: even mutating the live rows (as a
# replaced file would) cannot change its figures — the exact bug reported.
view1 = bitcash.recon_view(bitcash.load_recon("feedbead0001"))
check("approved sandbox reads from the frozen snapshot",
      view1["frozen"] and not view1["stale"] and view1["ar_total"] == 523571.0)
_saved_amounts = [r["amount"] for r in FAKE_ROWS["cash"]]
for r_ in FAKE_ROWS["cash"]:
    r_["amount"] = 999999.0
view1b = bitcash.recon_view(bitcash.load_recon("feedbead0001"))
check("replacing the live rows can NOT change an approved reconciliation",
      view1b["ar_total"] == 523571.0 and view1b["bit_amount"] == 523500.0)
for r_, amt in zip(FAKE_ROWS["cash"], _saved_amounts):
    r_["amount"] = amt

# (b) An OPEN sandbox matched against the OLD files reads as stale — it
# resolves nothing instead of silently mixing sources.
st3 = {"label": "STALE CASE", "date": "", "total": 523571.0,
       "lines": [{"reference": a, "amount": amt, "description": "",
                  "matched_ids": []} for a, amt in AWBS]}
rec3 = {"token": "feedbead0003", "status": "open",
        "uploaded": "2026-07-17 09:00", "uploaded_by": "tester",
        "source": "OLD FILES.xlsx", "statement": st3,
        "rows_gen": {"bit": "OLDGEN", "cash": "OLDGEN"},
        "ar_selected": [0, 1, 2], "bit_candidates": [0], "bit_selected": 0,
        "bit_duplicate": False, "error": "", "file": ""}
bitcash.save_recon(rec3)
view3 = bitcash.recon_view(rec3)
check("stale sandbox resolves nothing (no mixed-up rows)",
      view3["stale"] and view3["ar_rows"] == []
      and view3["bit_candidates"] == [] and view3["ar_total"] == 0)
page = client.get("/tools/bit-cash-ar/recon/feedbead0003")
check("stale sandbox shows the replacement warning + re-run button",
      "files on record were replaced" in page.text
      and "Re-run" in page.text)
check("approval refused while stale",
      bitcash.set_status("feedbead0003", True, "t") == "stale")
check("selection refused while stale",
      bitcash.set_selection("feedbead0003", ["0"], "0") == "stale")

# (c) One click re-matches against the CURRENT files and heals the sandbox.
rec3b = bitcash.rematch("feedbead0003")
check("re-match rebuilds selections against the current generation",
      rec3b and rec3b["rows_gen"] == {"bit": "genB1", "cash": "genC1"}
      and rec3b["ar_selected"] and rec3b["bit_selected"] == 0
      and not bitcash.recon_view(rec3b)["stale"])

# (d) A legacy APPROVED sandbox without a frozen snapshot whose files were
# replaced is SKIPPED by the journal, never written from wrong rows.
rec4 = dict(rec3, token="feedbead0004", status="approved",
            rows_gen={"bit": "OLDGEN", "cash": "OLDGEN"},
            approved_by="t", approved_at="2026-07-17 09:30")
rec4.pop("frozen", None)
bitcash.save_recon(rec4)
je2 = bitcash.build_journal(_tmp / "je_v100.xlsx")
check("journal writes frozen sandboxes and SKIPS stale legacy ones",
      je2 and "feedbead0001" in je2["tokens"]
      and "feedbead0004" not in je2["tokens"] and je2["skipped"] >= 1)

# (e) The deposit slip's banked total anchors the BIT search.
st5 = {"label": "SLIP CASE", "total": 999999.0,
       "lines": [{"reference": "5550001112", "amount": 999999.0,
                  "description": ""}]}
_l5, _a5, cands5, sel5 = bitcash.automatch(st5, slip_total=523500.0)
check("slip total wins the BIT selection over the evidences total",
      sel5 == 0 and 0 in cands5)

# (f) Slip uploaded TOGETHER with the statement drives the whole flow.
wbs = openpyxl.Workbook()
wbs.active.append(["AWB", "Amount"])
wbs.active.append(["4095823454", 53639])
stmt_path = _tmp / "slipflow_stmt.xlsx"
wbs.save(stmt_path)
wsl = openpyxl.Workbook()
wsl.active.append(["Ref", "Amount"])
wsl.active.append(["1", 523500])
slip_path = _tmp / "slipflow_slip.xlsx"
wsl.save(slip_path)
with open(stmt_path, "rb") as f1, open(slip_path, "rb") as f2:
    r = client.post("/tools/bit-cash-ar/recon/upload",
                    files={"statement": ("advice.xlsx", f1, XLSX),
                           "deposit_slip": ("slip.xlsx", f2, XLSX)})
check("statement + slip upload accepted", r.status_code == 303)
tok5 = r.headers["location"].rstrip("/").split("/")[-1]
for _ in range(100):
    rec5 = bitcash.load_recon(tok5)
    if rec5 and rec5["status"] in ("open", "error"):
        break
    time.sleep(0.1)
check("slip stored with the sandbox and its banked total read",
      rec5["status"] == "open"
      and (rec5.get("slip") or {}).get("name") == f"slip_{tok5}.xlsx"
      and rec5.get("slip_total") == 523500.0)
check("BIT auto-selected from the slip's banked amount",
      rec5.get("bit_selected") == 0)
page = client.get(f"/tools/bit-cash-ar/recon/{tok5}")
flat5 = " ".join(page.text.split())
check("sandbox narrative discloses the deposit-slip total",
      "Deposit slip total: 523,500" in flat5)

print("\nALL v9.4—v10.0 TESTS PASSED")
