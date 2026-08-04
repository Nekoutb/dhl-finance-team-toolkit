"""v11.10 — cheque register (treated rows greyed, each amount printed once),
Cash AR ageing 'Projected over 60', IRO multi-AWB search, and the shared
branch CASH accounts on every IRO statement.

Isolated to a temp data dir; the real data/ is never touched.
"""
import json
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import openpyxl  # noqa: E402

from app.services import cheques  # noqa: E402
from app.tools import bitcash, iro  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="v1110_"))
iro.IRO_DIR = _tmp / "iro"
iro.IRO_DIR.mkdir(parents=True, exist_ok=True)
iro.UPLOAD_DIR = _tmp / "uploads"
iro.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
iro.DEPOSITS_PATH = _tmp / "deposits.json"
bitcash.RECON_DIR = _tmp / "recons"
bitcash.RECON_DIR.mkdir(parents=True, exist_ok=True)
bitcash.ROWS_PATH = _tmp / "rows.json"
bitcash.STORE_PATH = _tmp / "bitcash.json"
cheques.BATCH_DIR = _tmp / "cheques"
cheques.STATS_PATH = _tmp / "cheque_stats.json"

_fail = 0


def check(label, cond):
    global _fail
    print(("[OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        _fail += 1


def _row(i, acct, awb, amount, days_old, today):
    d = (today - timedelta(days=days_old)).strftime("%d.%m.%Y")
    return {"id": i, "sap_acct": acct, "awb": awb, "assignment": awb,
            "reference": f"R{awb}", "amount": amount, "customer": "CUST",
            "doc_no": str(i), "date": d}


TODAY = date(2026, 7, 10)          # month end = 31 Jul 2026 (21 days later)

# === 1. Cash AR ageing — Projected over 60 =================================
# 55 days old today  -> 30-60 bucket now, 65 days by month end -> PROJECTED
# 20 days old today  -> 0-30 bucket now,  41 days by month end -> not yet
# 70 days old today  -> over-60 now, and still over-60 at month end
bitcash.ROWS_PATH.write_text(json.dumps({
    "bit": [], "bit_header": [], "gen_bit": "", "gen_cash": "g1",
    "cash_date_col": "Doc. Date",
    "cash": [_row(0, "4003025705", "1000000001", 100.0, 55, TODAY),
             _row(1, "4003025705", "1000000002", 50.0, 20, TODAY),
             _row(2, "4003025705", "1000000003", 30.0, 70, TODAY)],
}, ensure_ascii=False), encoding="utf-8")

ag = bitcash.cash_ageing(today=TODAY)
t = ag["totals"]
check("month end is the last day of the current month",
      ag["month_end"] == "31/07/2026")
check("today's buckets unchanged (0-30 / 30-60 / 60+)",
      t["b0"] == 50.0 and t["b31"] == 100.0 and t["b61"] == 30.0)
check("projected over 60 = everything past 60 days AT MONTH END",
      t["proj61"] == 130.0)          # the 55-day item joins the 70-day one
check("projected over 60 always includes what is already over 60",
      t["proj61"] >= t["b61"])
check("an item still under 60 at month end is excluded",
      t["proj61"] == 130.0 and 50.0 not in (t["proj61"],))
per_acct = ag["rows"][0]
check("the per-account row carries proj61 too", per_acct["proj61"] == 130.0)

# an undated item must not land in the projection
bitcash.ROWS_PATH.write_text(json.dumps({
    "bit": [], "bit_header": [], "gen_bit": "", "gen_cash": "g1",
    "cash_date_col": "",
    "cash": [{"id": 0, "sap_acct": "A", "awb": "9", "assignment": "9",
              "reference": "r", "amount": 77.0, "customer": "C",
              "doc_no": "9", "date": ""}]}, ensure_ascii=False),
    encoding="utf-8")
ag2 = bitcash.cash_ageing(today=TODAY)
check("undated items stay out of the projection",
      ag2["totals"]["undated"] == 77.0 and ag2["totals"]["proj61"] == 0.0)

# === 2. IRO statements carry the shared branch CASH accounts ===============
bitcash.ROWS_PATH.write_text(json.dumps({
    "bit": [], "bit_header": [], "gen_bit": "", "gen_cash": "g2",
    "cash_date_col": "Doc. Date",
    "cash": [_row(0, "4003025705", "2000000001", 100.0, 5, TODAY),
             _row(1, "4003026257", "2000000002", 200.0, 5, TODAY),
             _row(2, "CASHCMDLA", "2000000003", 30.0, 5, TODAY),
             _row(3, "CASHCMBUE", "2000000004", 40.0, 5, TODAY)],
}, ensure_ascii=False), encoding="utf-8")

accts = iro.operator_accounts()
by_acct = {a["account"]: a for a in accts}
check("a CASHCM account is never an operator of its own",
      not any(iro.is_cash_account(a["account"]) for a in accts)
      and set(by_acct) == {"4003025705", "4003026257"})
check("every operator statement includes BOTH cash accounts' airwaybills",
      all({r["awb"] for r in a["rows"]} >= {"2000000003", "2000000004"}
          for a in accts))
check("each operator still sees their own airwaybill",
      "2000000001" in {r["awb"] for r in by_acct["4003025705"]["rows"]}
      and "2000000002" in {r["awb"] for r in by_acct["4003026257"]["rows"]})
check("an operator does NOT see another operator's own airwaybill",
      "2000000002" not in {r["awb"] for r in by_acct["4003025705"]["rows"]})
check("cash rows are tagged with the account they were raised on",
      all(r.get("cash_account") for r in by_acct["4003025705"]["rows"]
          if r["awb"] in ("2000000003", "2000000004")))
check("totals split own vs shared pool",
      by_acct["4003025705"]["own_total"] == 100.0
      and by_acct["4003025705"]["cash_total"] == 70.0
      and by_acct["4003025705"]["total"] == 170.0
      and by_acct["4003025705"]["cash_count"] == 2)
check("account_entry() surfaces the cash rows to the portal + submit handler",
      len(iro.account_entry("4003025705")["rows"]) == 3)
check("is_cash_account matches the prefix case-insensitively, not substrings",
      iro.is_cash_account("CASHCMDLA") and iro.is_cash_account("cashcmbue")
      and not iro.is_cash_account("4003025705")
      and not iro.is_cash_account("XCASHCM01"))

# Once a cash AWB is claimed in a sandbox it leaves EVERY statement.
_gen = bitcash.rows_generation()
bitcash.save_recon({
    "token": "aaa111aaa111", "status": "open", "uploaded": "2026-07-10",
    "uploaded_by": "operator:4003025705", "source": "IRO 4003025705",
    "account": "4003025705", "payment_refs": ["DEP-1"],
    "statement": {"label": "x", "date": "", "total": 30.0,
                  "lines": [{"awb": "2000000003", "amount": 30.0,
                             "matched_ids": [2]}]},
    "ar_selected": [2], "bit_selected": None, "bit_candidates": [],
    "rows_gen": _gen, "slip": None, "extra_slips": [], "file": ""})
after = iro.operator_accounts()
check("a claimed cash airwaybill disappears from EVERY operator's statement",
      all("2000000003" not in {r["awb"] for r in a["rows"]} for a in after))
check("the unclaimed cash airwaybill is still shared",
      all("2000000004" in {r["awb"] for r in a["rows"]} for a in after))

# The statement Excel flags which rows came from a cash account.
xl = _tmp / "stmt.xlsx"
iro.build_statement_xlsx(xl, iro.account_entry("4003025705"))
ws = openpyxl.load_workbook(xl).active
refs = [ws.cell(row=r, column=2).value or "" for r in range(6, 12)]
check("the statement Excel marks the cash-account lines",
      any("[cash CASHCMBUE]" in str(v) for v in refs))

# === 3+4. Cheque register: greyed treated rows, each amount printed once ===
tpl = (ROOT / "app" / "templates" / "cheques" / "index.html").read_text(
    encoding="utf-8")
check("treated rows get the greyed-out row class",
      'tr.treated td' in tpl
      and '{% if r.treated %} class="treated"' in tpl)
check("a treated row is greyed rather than shown as a new match",
      '{% if r.treated %} class="treated"{% elif r.new %} class="new-match"'
      in tpl)
check("the credited amount is not reprinted when it equals the cheque",
      '= cheque' in tpl and 'same-as' in tpl)
check("a DIFFERING credited amount is still shown in full, flagged",
      'Differs from the cheque amount' in tpl)

# === 5. IRO portal: comma-separated multi-AWB search =======================
op = (ROOT / "app" / "templates" / "operator" / "statement.html").read_text(
    encoding="utf-8")
check("the search splits on commas into independent terms",
      'filter.value.toLowerCase().split(",")' in op)
check("a row matches when ANY term matches", "terms.some(" in op)
check("the placeholder shows the comma-separated form",
      "1005475881, 1011864556" in op)
check("cash rows are visually tagged on the portal",
      "r.cash_account" in op and "branch cash account" in op)

if _fail:
    print(f"\n{_fail} CHECK(S) FAILED")
    sys.exit(1)
print("\nALL v11.10 TESTS PASSED")
