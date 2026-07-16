"""Bank-statement slots: configurable banks in Settings + one override slot per
bank. Uploading a bank's statement replaces its previous one (stable per-bank
token), and the cross-tool line feed still aggregates every slot.
"""
import atexit
import json
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import CONFIG_PATH  # noqa: E402

_pre = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None


def _restore():
    if _pre is not None:
        CONFIG_PATH.write_text(_pre, encoding="utf-8")
    elif CONFIG_PATH.exists():
        CONFIG_PATH.unlink()


atexit.register(_restore)
# Two configured banks, auth off (login-free local = admin).
CONFIG_PATH.write_text(json.dumps({"banks": ["BICEC", "Afriland First Bank"]}),
                       encoding="utf-8")

from fastapi.testclient import TestClient  # noqa: E402
from app import main, config  # noqa: E402
from app.tools import bank  # noqa: E402

# Isolate stored reports from real data.
bank.STORE_DIR = Path(tempfile.mkdtemp(prefix="bankslots_")) / "bank"

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "bank_statement_french_sample.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# --- 1. stable per-bank token ----------------------------------------------
t = bank.bank_token("BICEC")
check("bank_token is 12-hex", bool(re.fullmatch(r"[0-9a-f]{12}", t)))
check("bank_token stable + case/space-insensitive", t == bank.bank_token("  bicec "))
check("different banks -> different tokens", t != bank.bank_token("Afriland First Bank"))

# --- 2. build_report keyed to a slot, tagged with the bank name -------------
rep = bank.build_report([(SAMPLE, "jan.xlsx")], token=t, bank_slot="BICEC")
check("report keyed by the slot token", rep["token"] == t)
check("report tagged with the bank slot", rep["bank_slot"] == "BICEC")
check("lines labelled with the configured bank",
      all(b["bank"] == "BICEC" for b in rep["banks"]))
bank.save_report(rep)
check("slot_report loads it back", bank.slot_report("BICEC")["token"] == t)

# --- 3. re-upload OVERRIDES (one stored file per slot) ----------------------
rep2 = bank.build_report([(SAMPLE, "feb.xlsx")], token=t, bank_slot="BICEC")
bank.save_report(rep2)
files = list(bank.STORE_DIR.glob(f"{t}.json"))
check("still exactly one stored report for the slot (overridden)", len(files) == 1)
check("slot now shows the latest source", bank.slot_report("BICEC")["source"] == "feb.xlsx")

# --- 4. cross-tool feed aggregates the slot --------------------------------
rows, summary = bank.all_statement_lines()
check("all_statement_lines includes the slot's lines", len(rows) > 0)
check("aggregation groups under the bank name",
      any(s["bank"] == "BICEC" for s in summary))

# --- 4b. PRIOR MONTHS: same bank, different months accumulate ----------------
t_jun = bank.bank_token("BICEC", "2026-06")
t_jul = bank.bank_token("BICEC", "2026-07")
check("different months -> different tokens", t_jun != t_jul != t)
rep_jun = bank.build_report([(SAMPLE, "jun.xlsx")], token=t_jun,
                            bank_slot="BICEC", period="2026-06")
bank.save_report(rep_jun)
rep_jul = bank.build_report([(SAMPLE, "jul.xlsx")], token=t_jul,
                            bank_slot="BICEC", period="2026-07")
bank.save_report(rep_jul)
months = bank.slot_reports("BICEC")
check("prior month kept alongside the new one (3 on file incl. legacy)",
      len(months) == 3)
check("months sorted newest first",
      [m.get("period", "") for m in months][:2] == ["2026-07", "2026-06"])
# same bank + same month still OVERRIDES
bank.save_report(bank.build_report([(SAMPLE, "jun2.xlsx")], token=t_jun,
                                   bank_slot="BICEC", period="2026-06"))
check("same month re-upload overrides (still 3 on file)",
      len(bank.slot_reports("BICEC")) == 3)
check("override took the new source",
      bank.slot_report("BICEC", "2026-06")["source"] == "jun2.xlsx")
rows_all, _s = bank.all_statement_lines()
check("cross-tool search sees ALL months' lines",
      len(rows_all) > len(rows))

# --- 5. HTTP: page renders a slot per configured bank ----------------------
client = TestClient(main.app, follow_redirects=False)
page = client.get("/tools/bank-statements")
check("bank page 200", page.status_code == 200)
check("shows both configured bank slots",
      "BICEC" in page.text and "Afriland First Bank" in page.text)
check("month picker on the upload form", 'type="month"' in page.text)
check("prior months listed on the bank card", "2026-06" in page.text)

# --- 6. HTTP upload targets a slot (bank + month) and overrides -------------
from datetime import datetime as _dt
with open(SAMPLE, "rb") as fh:
    r = client.post("/tools/bank-statements/upload",
                    data={"bank": "Afriland First Bank", "period": "2026-05"},
                    files={"file": ("afri.xlsx", fh, XLSX)})
at = bank.bank_token("Afriland First Bank", "2026-05")
check("upload redirects to that bank+month's report",
      r.status_code == 303 and r.headers["location"].endswith(f"/results/{at}"))
check("the slot holds the month's statement",
      bank.slot_report("Afriland First Bank", "2026-05") is not None)
# a missing/invalid month falls back to the current month
with open(SAMPLE, "rb") as fh:
    r = client.post("/tools/bank-statements/upload",
                    data={"bank": "Afriland First Bank", "period": "bogus"},
                    files={"file": ("afri2.xlsx", fh, XLSX)})
cur = bank.bank_token("Afriland First Bank", _dt.now().strftime("%Y-%m"))
check("invalid month falls back to the current month",
      r.status_code == 303 and r.headers["location"].endswith(f"/results/{cur}"))

# --- 7. uploading to a non-configured bank is refused -----------------------
with open(SAMPLE, "rb") as fh:
    r = client.post("/tools/bank-statements/upload",
                    data={"bank": "Ghost Bank"},
                    files={"file": ("x.xlsx", fh, XLSX)})
check("unknown bank rejected (400)", r.status_code == 400)

# --- 8. Settings save: trims + de-dupes the bank list -----------------------
r = client.post("/settings/banks", data={"banks": "BICEC\n  \nUBA\nbicec\nUBA"})
check("settings/banks saved (303)", r.status_code == 303)
check("bank list trimmed + de-duped (case-insensitive)",
      config.load_config()["banks"] == ["BICEC", "UBA"])

print("\nALL BANK SLOTS TESTS PASSED")
_restore()
