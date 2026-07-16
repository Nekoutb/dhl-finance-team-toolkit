"""Quick Account Statement (one-click customer statement PDF) and
BIT & Cash AR (open-items counting + daily graph)."""
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from testutil import smtp_guard  # noqa: E402
smtp_guard()

import openpyxl  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.services import ar_master  # noqa: E402
from app.tools import bitcash, ongoing_ctp as ctp  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "samples" / "ar_master_sample.xlsx"
TXN = ROOT / "samples" / "ar_breakdown_features_sample.xlsx"
_tmp = Path(tempfile.mkdtemp(prefix="newtools_"))


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


client = TestClient(main.app, follow_redirects=False)

# === 1. Quick Account Statement ==============================================
res = ctp.analyze(TXN, as_of=date(2026, 6, 13), default_rank=60,
                  master=ar_master.parse_master(MASTER))
res["created_at"] = "2099-01-01 00:00"     # ensure it is the latest analysis
res["source"] = "features.xlsx"
ctp.save_result("aab0c5522", res)
try:
    page = client.get("/tools/quick-statement")
    check("quick statement page 200", page.status_code == 200)
    check("customer list offered", "qs-customers" in page.text
          and "1004000001" in page.text)

    r = client.get("/tools/quick-statement/generate?key=1004000001")
    check("one-click generate returns a PDF",
          r.status_code == 200 and r.content[:4] == b"%PDF")
    check("PDF is a download",
          "Statement_" in r.headers.get("content-disposition", ""))

    r = client.get("/tools/quick-statement/generate?key=nope")
    check("unknown customer -> friendly redirect",
          r.status_code == 303 and "error=" in r.headers["location"])

    # payment terms flow from the master into the analysis customers
    check("payment_term attached from the master",
          any(c.get("payment_term") for c in res["customers"]))
finally:
    (ctp.STORE_DIR / "aab0c5522.json").unlink(missing_ok=True)
print("ok: quick account statement — one click, PDF from data on file")

# === 2. BIT & Cash AR ========================================================
bitcash.STORE_PATH = _tmp / "bitcash.json"


def _wb(rows, header):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(header)
    for r_ in rows:
        ws.append(r_)
    p = _tmp / f"{header[0]}_{len(rows)}.xlsx"
    wb.save(p)
    return p

# BIT file WITH a status column: 2 open, 1 closed
bit = _wb([["T1", 100, "Open"], ["T2", 200, "Closed"], ["T3", 300, "ouvert"]],
          ["Ref", "Amount", "Status"])
# Cash AR file WITHOUT a status column: every row is an open item
cash = _wb([["C1", 10], ["C2", 20], ["C3", 30], ["C4", 40]],
           ["Item", "Amount"])

c1 = bitcash.count_items(bit)
check("status column respected (2 open of 3)",
      c1 == {"items": 3, "open_items": 2, "status_col": "Status"})
c2 = bitcash.count_items(cash)
check("no status column -> all rows open (4)",
      c2["items"] == 4 and c2["open_items"] == 4)

with open(bit, "rb") as f1, open(cash, "rb") as f2:
    r = client.post("/tools/bit-cash-ar/upload",
                    files={"bit_file": ("bit.xlsx", f1,
                                        "application/vnd.ms-excel"),
                           "cash_file": ("cash_ar.xlsx", f2,
                                         "application/vnd.ms-excel")})
check("upload both files -> redirect", r.status_code == 303)
st = bitcash.status()
check("current BIT position stored", st["current"]["bit"]["open_items"] == 2)
check("current Cash AR position stored", st["current"]["cash"]["open_items"] == 4)
check("today's history point recorded",
      st["history"] and st["history"][-1][1] == {"bit_open": 2, "cash_open": 4})

page = client.get("/tools/bit-cash-ar")
check("page shows open counts", ">2<" in page.text and ">4<" in page.text)
check("daily graph plotted", "<svg" in page.text
      and "Open BIT items" in page.text)

r = client.post("/tools/bit-cash-ar/upload")
check("no file -> friendly 400", r.status_code == 400)
print("ok: BIT & Cash AR — open items counted per day + daily graph")

print("\nALL NEW-TOOLS TESTS PASSED")
