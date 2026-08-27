"""BIT / Cash AR uploads must survive BOTH SAP layouts — and must not
deadlock while doing it.

Two failures found against the real Cameroon exports on 27 Aug 2026:

1. SAP ships this extract under either the team's short field names
   (SAP Acct / Amount / Doc. No.) or the raw technical ones (Customer /
   Company Code Currency Value / Document Number). Only the short set was
   recognised, so a file in the other layout read EVERY amount as 0.00 —
   while the open-item counts, which just count rows, still looked healthy.
   The page rendered a full table of dashes and zeros.

2. ``_persist_rows`` holds ``_lock`` across its row loop and the progress
   callback re-takes it every 250 rows. With a plain Lock that self-
   deadlocks as soon as the loop outruns the callback's 0.4s write-throttle:
   the upload hangs behind its spinner forever and the SECOND file is never
   read at all.

Isolated to a temp data dir; the real data/ is never touched.
"""
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tools import bitcash  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="bclayout_"))
bitcash.STORE_PATH = _tmp / "bitcash.json"
bitcash.ROWS_PATH = _tmp / "bitcash_rows.json"
bitcash.UPLOAD_DIR = _tmp / "uploads"
bitcash.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_fail = 0


def check(label, cond):
    global _fail
    print(("[OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        _fail += 1


def book(name, header, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    p = _tmp / name
    wb.save(p)
    return p


D1 = datetime(2026, 7, 3)
D2 = datetime(2026, 7, 21)

# --- Cash AR, the team's SHORT field names (the layout that always worked) --
SHORT_HDR = ["C Code", "User Name", "MAC Code", "SAP Acct", "IBS Acct",
             "Customer Account: Name", "Mnth/Yr", "Doc. Date", "Doc. No.",
             "Doc. Type", "Assignment", "Reference", "Text", "Amount"]
SHORT_ROWS = [
    ["CM01", "SG_SCHEDULER", "SP3", "4003025705", "415004730",
     "SMIC LUXURY SHOP", "007.2026", D1, "90028505", "X5", "1763203525",
     "1763203525", "SPACM4", 56100],
    ["CM01", "SG_SCHEDULER", "SP3", "4003026287", "415021180",
     "PRO EXCELL LOGISTICS", "007.2026", D2, "90028515", "X5", "5303510785",
     "5303510785", "", 400500]]

# --- Cash AR, SAP's TECHNICAL field names (the layout that read all zeros) --
# Same two items, same money — only the header row differs.
LONG_HDR = ["Company Code", "User Name", "Attribute 6", "Customer",
            "Reference Key 3", "Customer Account: Name 1",
            "Fiscal year/period", "Document Date", "Document Number",
            "Document Type", "Assignment", "Reference", "Text",
            "Company Code Currency Value"]
LONG_ROWS = [r[:] for r in SHORT_ROWS]

# --- Cash AR, French SAP login ---------------------------------------------
FR_HDR = ["Societe", "Utilisateur", "Attribut 6", "Compte SAP",
          "Reference 3", "Compte client : nom 1", "Exercice/periode",
          "Date de piece", "Numero de piece", "Type de piece", "Affectation",
          "Reference", "Texte", "Montant"]
FR_ROWS = [r[:] for r in SHORT_ROWS]


def load(name, header, rows):
    p = book(name, header, rows)
    bitcash.record_upload("cash", p, name)
    store = bitcash.rows_store()
    return store["cash"], store


for label, name, hdr, rws in (
        ("short field names", "cash_short.xlsx", SHORT_HDR, SHORT_ROWS),
        ("technical field names", "cash_long.xlsx", LONG_HDR, LONG_ROWS),
        ("French field names", "cash_fr.xlsx", FR_HDR, FR_ROWS)):
    rows, store = load(name, hdr, rws)
    money = sum(r["amount"] for r in rows)
    check(f"Cash AR / {label}: both rows read", len(rows) == 2)
    check(f"Cash AR / {label}: amounts are real, not 0.00 "
          f"(got {money:,.0f})", money == 456600)
    check(f"Cash AR / {label}: the SAP account is mapped",
          [r["sap_acct"] for r in rows] == ["4003025705", "4003026287"])
    check(f"Cash AR / {label}: the account is NOT the customer-name column",
          all(not r["sap_acct"][0].isalpha() for r in rows))
    check(f"Cash AR / {label}: the customer name is mapped",
          rows[0]["customer"] == "SMIC LUXURY SHOP")
    check(f"Cash AR / {label}: the IBS account is mapped",
          rows[0]["ibs_acct"] == "415004730")
    check(f"Cash AR / {label}: the document number is mapped",
          rows[0]["doc_no"] == "90028505")
    check(f"Cash AR / {label}: the document date is mapped",
          rows[0]["date"].startswith("2026-07-03"))
    check(f"Cash AR / {label}: the amount column is named on record",
          bool(store["cash_amount_col"]))

# An unrecognisable amount column must SAY so rather than quietly read zero.
NO_AMT_HDR = [h if h != "Amount" else "Valeur inconnue XYZ" for h in SHORT_HDR]
rows, store = load("cash_noamt.xlsx", NO_AMT_HDR, SHORT_ROWS)
check("an unmapped amount column is recorded as empty, so the page can warn",
      store["cash_amount_col"] == "")
ag = bitcash.cash_ageing(today=datetime(2026, 8, 27).date())
check("the ageing panel is handed the amount column for that warning",
      "amount_col" in ag and ag["amount_col"] == "")

# --- BIT, both layouts ------------------------------------------------------
BIT_HDR = ["Company Code", "Fiscal year/period", "Group Account Number",
           "G/L Account", "G/L Account: Long Text", "Posting Date",
           "Document Date", "Reference", "Assignment", "Text", "Posting Key",
           "Company Code Currency Value", "Company Code Currency Key",
           "Document Number"]
BIT_ROWS = [
    ["CM01", "006.2026", "126300", "1263001293", "3P Bank Loc Curr L001 BIT",
     D1, D1, "CMSGBCM00126147", "0533348000006XAF", "PAYMENT RECEIVED", "50",
     -45161, "XAF", "1600000001"]]
BIT_FR_HDR = ["Societe", "Exercice/periode", "Compte collectif",
              "Compte general", "Compte general : texte", "Date comptable",
              "Date de piece", "Reference", "Affectation", "Texte",
              "Cle de comptabilisation", "Montant en devise societe",
              "Devise", "Numero de piece"]

for label, name, hdr in (("English", "bit_en.xlsx", BIT_HDR),
                         ("French", "bit_fr.xlsx", BIT_FR_HDR)):
    p = book(name, hdr, BIT_ROWS)
    bitcash.record_upload("bit", p, name)
    b = bitcash.rows_store()["bit"]
    check(f"BIT / {label}: the G/L account is mapped, not its long text",
          b[0]["gl_account"] == "1263001293")
    check(f"BIT / {label}: the amount is read", b[0]["amount"] == -45161)
    check(f"BIT / {label}: the posting date is read",
          b[0]["posting_date"].startswith("2026-07-03"))
    check(f"BIT / {label}: the posting key is read",
          b[0]["posting_key"] == "50")
    check(f"BIT / {label}: the text column is the real one, not the header "
          f"text", b[0]["text"] == "PAYMENT RECEIVED")

# --- The deadlock -----------------------------------------------------------
# _persist_rows holds _lock across the row loop; the real progress tracker
# takes _lock again on every tick. Reproduced directly rather than by
# generating the ~50,000 rows it takes to outrun the 0.4s write-throttle.
check("_lock is reentrant", bitcash._lock.acquire(blocking=False)
      and bitcash._lock.acquire(blocking=False))
bitcash._lock.release()
bitcash._lock.release()


def nested_tick(**_kw):
    with bitcash._lock:
        pass


done = threading.Event()


def run():
    p = book("cash_tick.xlsx", SHORT_HDR, SHORT_ROWS * 200)
    bitcash._persist_rows("cash", p, progress=nested_tick)
    done.set()


probe = threading.Thread(target=run, daemon=True)
probe.start()
check("a progress callback that re-takes the lock does not hang the ingest",
      done.wait(timeout=30))
probe.join(timeout=30)          # it must be off the row store before we upload

# The whole upload path, both files in one submit, end to end.
jobs = [("bit", book("j_bit.xlsx", BIT_HDR, BIT_ROWS), "BIT.xlsx"),
        ("cash", book("j_cash.xlsx", LONG_HDR, LONG_ROWS), "CashAR.xlsx")]
bitcash.process_uploads_async(jobs)
deadline = time.time() + 60
while time.time() < deadline and bitcash.status()["processing"]:
    time.sleep(0.05)
st = bitcash.status()
check("uploading both files finishes instead of spinning forever",
      st["processing"] is None)
check("no error was recorded", st["processing_error"] is None)
check("BOTH sides land — not just the first",
      bool(st["current"].get("bit")) and bool(st["current"].get("cash")))
final = bitcash.rows_store()
check("both row sets are stored",
      len(final["bit"]) == 1 and len(final["cash"]) == 2)
check("and the money survived the round trip",
      sum(r["amount"] for r in final["cash"]) == 456600)

print("\n" + ("ALL BIT/CASH LAYOUT TESTS PASSED" if not _fail
              else f"{_fail} CHECK(S) FAILED"))
sys.exit(1 if _fail else 0)
