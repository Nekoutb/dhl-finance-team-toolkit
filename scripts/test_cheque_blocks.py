"""A bank reader can hand back a whole statement PAGE as a single row: every
date in one cell, every narration in another, every amount in a third. The
cheque register used to quote that row as the clearing line, so "Date credited"
showed a run of dates and "Amount credited" a 120-digit number (a merged digit
column parsed as 5.7e+119).

The cheque reference inside such a block IS genuine — it is the statement's own
narration — so the match must be kept. What must NOT happen is quoting a date
or an amount that belongs to the page rather than to the cheque.

Isolated to a temp data dir; the real data/ is never touched.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import bank_statement, cheques  # noqa: E402

cheques.BATCH_DIR = Path(tempfile.mkdtemp(prefix="chqblk_")) / "cheques"
cheques.BATCH_DIR.mkdir(parents=True, exist_ok=True)

_fail = 0


def check(label, cond):
    global _fail
    print(("[OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        _fail += 1


# The real shape, taken from a production GENERAL BANK statement.
BLOCK_TEXT = (
    "29/07/2026 30/07/2026 30/07/2026 30/07/2026 30/07/2026 30/07/2026 "
    "30/07/2026 30/07/2026 30/07/2026 30/07/2026 30/07/2026 30/07/2026 "
    "VERSEMENT PRODUCTION Motif : PRODUCTION REMETTANT BUINDA PAUL "
    "REM.CHQ.BD 9988802 Tireur : CA SCB Cheque N°: @ 9988802 492477 "
    "REM.CHQ.BD 8397374 Tireur : Afriland First Bank "
    "57,053 915,513 76,748 634,932,958 635,979,334 639,732,730")
BLOCK = {"bank": "GENERAL BANK", "text": BLOCK_TEXT,
         "amount": 5.705391551376749e+119, "date": BLOCK_TEXT[:120]}
REAL = {"bank": "BICEC", "text": "REM CHQ 9988802 SARL DUPONT", "date": "30/07/2026",
        "amount": 250000.0}

# === 1. The predicate ======================================================
check("a page block is not a transaction line",
      not bank_statement.is_transaction_line(BLOCK["text"], BLOCK["amount"]))
check("a normal narration is",
      bank_statement.is_transaction_line(REAL["text"], REAL["amount"]))
check("two dates in a narration is still a normal line (op. + value date)",
      bank_statement.is_transaction_line(
          "VIR DU 01/07/2026 VALEUR 02/07/2026 SARL X", 90000.0))
check("a merged amount alone condemns the line",
      not bank_statement.is_transaction_line("CHQ 1234567", 5.7e119))
check("a large but real amount is fine",
      bank_statement.is_transaction_line("VIR SALAIRES JUILLET", 900000000.0))
check("a non-numeric amount does not crash the check",
      bank_statement.is_transaction_line("CHQ 1234567", "n/a"))

# === 2. The match is KEPT — the reference in the block is real =============
apps = cheques.find_appearances("9988802", [BLOCK])
check("a cheque named inside a block still counts as found", len(apps) == 1)
a = apps[0]
check("but no date is quoted for it", a["date"] == "")
check("and no amount is quoted for it", a["amount"] is None)
check("it is flagged as not isolated", a["partial"] is True)
check("its text is centred on the reference, not on the date column",
      "9988802" in a["text"] and not a["text"].startswith("29/07/2026"))
check("the register snippet reads as a reference",
      "REM.CHQ" in cheques.ref_snippet(a, "9988802"))

# === 3. A properly isolated line always wins ===============================
both = cheques.find_appearances("9988802", [BLOCK, REAL])
check("both the block and the real line are recorded", len(both) == 2)
primary = cheques.primary_appearance(both)
check("the register discloses the REAL line, not the block",
      primary["bank"] == "BICEC" and primary["date"] == "30/07/2026"
      and primary["amount"] == 250000.0 and not primary.get("partial"))

# a real line is preferred even when the block sorts later by date
LATE_BLOCK = dict(BLOCK, date="31/12/2026 " + BLOCK_TEXT[:100])
primary = cheques.primary_appearance(
    cheques.find_appearances("9988802", [LATE_BLOCK, REAL]))
check("a later-sorting block still loses to a real line",
      primary["bank"] == "BICEC")

# === 4. Isolated lines are untouched by any of this ========================
apps = cheques.find_appearances("9988802", [REAL])
check("a normal match still carries its date and amount",
      len(apps) == 1 and apps[0]["date"] == "30/07/2026"
      and apps[0]["amount"] == 250000.0 and not apps[0].get("partial"))
check("a cheque absent from the statement is still not found",
      cheques.find_appearances("7777777", [BLOCK, REAL]) == [])
check("a number that is only PART of a longer run does not match",
      cheques.find_appearances("998880", [BLOCK]) == [])

# several blocks from the same bank collapse to one disclosed row
many = cheques.find_appearances(
    "9988802", [BLOCK, dict(BLOCK, text=BLOCK_TEXT + " x"), REAL])
check("repeated blocks are disclosed once",
      sum(1 for x in many if x.get("partial")) == 1)

# === 5. The register template must not print the block =====================
IDX = (ROOT / "app" / "templates" / "cheques" / "index.html").read_text(
    encoding="utf-8")
check("the register renders 'not isolated' for a block",
      IDX.count("not isolated") == 2 and "r.cleared.partial" in IDX)
check("the amount cell guards against a missing amount",
      "r.cleared.amount is none" in IDX)
check("the note has a style on this page",
      ".diff-warn {" in IDX)

if _fail:
    print(f"\n{_fail} CHECK(S) FAILED")
    sys.exit(1)
print("\nALL CHEQUE BLOCK TESTS PASSED")
