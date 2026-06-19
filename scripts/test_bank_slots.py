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

# --- 5. HTTP: page renders a slot per configured bank ----------------------
client = TestClient(main.app, follow_redirects=False)
page = client.get("/tools/bank-statements")
check("bank page 200", page.status_code == 200)
check("shows both configured bank slots",
      "BICEC" in page.text and "Afriland First Bank" in page.text)

# --- 6. HTTP upload targets a slot and overrides ----------------------------
with open(SAMPLE, "rb") as fh:
    r = client.post("/tools/bank-statements/upload",
                    data={"bank": "Afriland First Bank"},
                    files={"file": ("afri.xlsx", fh, XLSX)})
at = bank.bank_token("Afriland First Bank")
check("upload redirects to that bank's slot report",
      r.status_code == 303 and r.headers["location"].endswith(f"/results/{at}"))
check("the slot now holds a statement", bank.slot_report("Afriland First Bank") is not None)

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
