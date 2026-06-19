"""Orange Money statement -> per-correspondant branded receipts (ZIP) + the
name/account memory that auto-fills on the next upload.

Covers: parsing/reconciliation, exclusion of failed + C2C rows, the grouped
review model, ZIP folder structure & receipt count, and that supplied
identities persist and pre-fill on a second parse.
"""
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import customers, orange_statement  # noqa: E402
from app.tools import orange_cameroun as orange        # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "orange_statement_sample.xlsx"


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# Isolate the customers store so the test never touches real data.
_tmpdir = Path(tempfile.mkdtemp(prefix="orange_test_"))
customers._PATH = _tmpdir / "customers.json"

# --- 1. parse + reconcile ---------------------------------------------------
data = orange_statement.parse_statement(SAMPLE)
m, t = data["meta"], data["totals"]
check("account read", m["account"] == "658902134")
check("month label derived (Mai 2026)", m["month_label"] == "Mai 2026")
check("company cleaned (no code, no ' - ')",
      m["company"] == "DHL EXPRESS Cameroun, Douala")
check("4 successful collections kept", t["count"] == 4)
check("total credited = 521,774", t["credited"] == 521774)
check("3 correspondants", t["correspondant_count"] == 3)
refs = {c["reference"] for c in data["collections"]}
check("Echec B2B excluded", "BM260517.1426.C00598" not in refs)
check("outgoing C2C excluded", "PP260506.0844.D89207" not in refs)
check("XAF formatting (space thousands)", orange_statement.fmt_xaf(521774) == "521 774")

# --- 2. review model: grouped, sorted, memory-aware -------------------------
model = orange.parse_for_review(SAMPLE)
top = model["correspondants"][0]
check("grouped by correspondant, biggest first (694707993 = 280k)",
      top["correspondant"] == "694707993")
multi = next(c for c in model["correspondants"] if c["correspondant"] == "699559325")
check("repeat correspondant grouped (699559325 -> 2 receipts)", multi["count"] == 2)
check("no names remembered yet", top["name"] == "" and top["account"] == "")

# --- 3. generate the ZIP with identities -> folders + receipts --------------
identities = {
    "699559325": {"name": "ACME LOGISTICS", "account": "1004000001"},
    "696578717": {"name": "BETA TRADING", "account": "1004000002"},
}
out_zip = _tmpdir / "out.zip"
result = orange.build_zip(SAMPLE, identities, out_zip)
check("zip reports 4 receipts", result["count"] == 4)
check("bundle name", result["zip_name"] == "Collections Mai 2026 - 658902134.zip")

with zipfile.ZipFile(out_zip) as zf:
    names = zf.namelist()
check("4 PDFs in the zip", sum(n.endswith(".pdf") for n in names) == 4)
check("bundled under one parent folder",
      all(n.startswith("Collections Mai 2026 - 658902134/") for n in names))
check("a folder per correspondant (699559325)",
      any("/699559325/" in n for n in names))
check("receipt filename starts with correspondant + reference",
      any(n.endswith("/699559325/699559325_MP260501_1602_D76168.pdf") for n in names))
check("two receipts in the repeat correspondant's folder",
      sum("/699559325/" in n and n.endswith(".pdf") for n in names) == 2)

# --- 4. memory persisted + auto-fills on the NEXT upload --------------------
check("name persisted", customers.get_name(orange.TOOL_SLUG, "699559325") == "ACME LOGISTICS")
check("account persisted", customers.get_name(orange.ACCT_SLUG, "699559325") == "1004000001")
model2 = orange.parse_for_review(SAMPLE)
acme = next(c for c in model2["correspondants"] if c["correspondant"] == "699559325")
check("name auto-filled on re-upload", acme["name"] == "ACME LOGISTICS")
check("account auto-filled on re-upload", acme["account"] == "1004000001")

# --- 5. single 'N° de Compte' column uses its real index (review fix #1) -----
single = ["N°", "Date", "Heure", "Référence", "Service", "Paiement", "Statut",
          "Mode", "N° de Compte", "Wallet", "Débit", "Crédit"]
cols = orange_statement._find_columns([single])
check("single 'N° de Compte' resolves to its real index (8), not fallback 11",
      cols["correspondant"] == 8)

# --- 6. combined 'Période' cell still yields the month label (fix #2) --------
mc = orange_statement._meta([["Période :", "", "", "01/05/2026 - 31/05/2026"]])
check("combined 'Période' cell -> month label", mc["month_label"] == "Mai 2026")

# --- 7. two same/blank-reference collections never overwrite (fix #3) --------
import openpyxl  # noqa: E402
H = ["N°", "Date", "Heure", "Référence", "Service", "Paiement", "Statut",
     "Mode", "N° de Compte", "Wallet", "N° Pseudo", "N° de Compte", "Wallet",
     "Débit", "Crédit", "Compte: 658902134", "Sous-réseau"]


def _row(num, corr, credit, ref=""):
    r = [""] * 17
    r[0], r[3], r[4], r[6], r[8], r[11], r[14] = (num, ref, "Merchant Payment",
                                                  "Succès", "658902134", corr, credit)
    return r


mini = _tmpdir / "mini.xlsx"
wb = openpyxl.Workbook()
ws = wb.active
for hdr in [["Compte Orange Money :", "", "", "658902134"],
            ["Début de Période :", "", "", "01/05/2026"],
            ["Fin de Période :", "", "", "31/05/2026"], H]:
    ws.append(hdr)
ws.append(_row(1, "700000001", 1000))   # blank reference
ws.append(_row(2, "700000001", 2000))   # blank reference -> same base name
wb.save(mini)
res2 = orange.build_zip(mini, {}, _tmpdir / "mini.zip")
check("two blank-reference collections both kept", res2["count"] == 2)
with zipfile.ZipFile(_tmpdir / "mini.zip") as zf:
    n_pdf = sum(x.endswith(".pdf") for x in zf.namelist())
check("zip actually holds 2 distinct receipts (no overwrite)", n_pdf == 2)

# --- 8. blanking an identity clears the remembered value (fix #4) ------------
orange.build_zip(SAMPLE, {"694707993": {"name": "TEMP CO", "account": "9"}},
                 _tmpdir / "a.zip")
check("identity saved", customers.get_name(orange.TOOL_SLUG, "694707993") == "TEMP CO")
orange.build_zip(SAMPLE, {"694707993": {"name": "", "account": ""}}, _tmpdir / "b.zip")
check("blanking clears remembered name",
      customers.get_name(orange.TOOL_SLUG, "694707993") is None)
check("blanking clears remembered account",
      customers.get_name(orange.ACCT_SLUG, "694707993") is None)

# --- 9. rounded corners are supported on the pinned reportlab (fix #7) -------
from app.services import pdf_writer  # noqa: E402
check("reportlab ROUNDEDCORNERS feature-test is true", pdf_writer._HAS_ROUNDED)

print("\nALL ORANGE RECEIPTS TESTS PASSED")
