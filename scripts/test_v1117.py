"""v11.17 — upload-reliability audit after colleagues reported errors while
uploading:

* the CtP "refresh" on a stored analysis crashed with a 500 every time
  (dates loaded from disk are strings; _serialize demanded date objects);
* upload handlers did their disk writes and heavy parses ON the event loop,
  so one stalled disk write froze the whole worker (gunicorn SIGKILL — seen
  in production on 06.08) — all of it now runs in the threadpool.

Isolated to a temp data dir; the real data/ is never touched.
"""
import datetime as dt
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.tools import ongoing_ctp as ctp  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="v1117_"))
config.CONFIG_PATH = _tmp / "config.json"
config.invalidate_config_cache()
ctp.STORE_DIR = _tmp / "ctp"
ctp.STORE_DIR.mkdir(parents=True, exist_ok=True)
ctp.MASTER_PATH = ctp.STORE_DIR / "master.json"

_fail = 0


def check(label, cond):
    global _fail
    print(("[OK ] " if cond else "[FAIL] ") + label)
    if not cond:
        _fail += 1


# === 1. THE PRODUCTION 500: save -> load -> save must round-trip ============
RESULT = {
    "as_of": dt.date(2026, 8, 1),
    "invoices": [
        {"invoice": "INV1", "invoice_date": dt.date(2026, 7, 1),
         "due_date": dt.date(2026, 7, 31), "amount": 1000.0},
        {"invoice": "INV2", "invoice_date": None, "due_date": None,
         "amount": 50.0},
    ],
    "customers": [], "summary": {"customer_count": 0},
}
ctp.save_result("feed00000001", RESULT)
loaded = ctp.load_result("feed00000001")
check("a fresh result saves and loads",
      loaded is not None and loaded["as_of"] == "2026-08-01")

# The refresh flow: load from disk (dates are now STRINGS), then save again.
# This exact call raised AttributeError('str' has no 'isoformat') in
# production on 23.07 and 28.07 — every refresh click was a 500.
loaded["refreshed_at"] = "2026-08-06 12:00"
try:
    ctp.save_result("feed00000001", loaded)
    ok = True
except AttributeError:
    ok = False
check("re-saving a LOADED result no longer crashes (the refresh 500)", ok)
again = ctp.load_result("feed00000001")
check("dates survive the round-trip unchanged",
      again["as_of"] == "2026-08-01"
      and again["invoices"][0]["invoice_date"] == "2026-07-01"
      and again["invoices"][0]["due_date"] == "2026-07-31")
check("None dates stay None through both trips",
      again["invoices"][1]["invoice_date"] is None
      and again["invoices"][1]["due_date"] is None)
check("the refresh stamp is kept", again["refreshed_at"] == "2026-08-06 12:00")

# the helper itself, all three shapes
check("_iso: date -> ISO string",
      ctp._iso(dt.date(2026, 8, 6)) == "2026-08-06")
check("_iso: string passes through", ctp._iso("2026-08-06") == "2026-08-06")
check("_iso: empty -> None", ctp._iso(None) is None and ctp._iso("") is None)

# === 2. No upload handler blocks the event loop any more ====================
MAIN = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
check("a _spool helper writes uploads off the event loop",
      "async def _spool(" in MAIN and "run_in_threadpool(dest.write_bytes"
      in MAIN)
check("no async handler writes an upload on the loop any more",
      ".write_bytes(await" not in MAIN)

# Every heavy parser call inside an async handler must go through the
# threadpool. Walk the handlers the way the audit did.
HEAVY = re.compile(
    r"\b(ctp\.analyze|ar_master\.parse_master|remittance\.build_statements"
    r"|orange\.parse_for_review|bitcash\._read_slip_info|iro\.preread_slip"
    r"|ai_ocr\.extract\w*)\s*\(")
lines = MAIN.splitlines()
current, is_async, offenders = None, False, []
for i, ln in enumerate(lines):
    m = re.match(r"(async )?def (\w+)\(", ln)
    if m:
        current, is_async = m.group(2), bool(m.group(1))
        continue
    if current and is_async and HEAVY.search(ln):
        window = "\n".join(lines[max(0, i - 2):i + 1])
        if "run_in_threadpool" not in window:
            offenders.append(f"{current}: {ln.strip()[:60]}")
check("every heavy parse in an async handler runs in the threadpool "
      + (f"(offenders: {offenders})" if offenders else ""), not offenders)

# === 3. The whole app still imports and serves ==============================
from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

client = TestClient(main.app)
check("the app boots and serves the dashboard",
      client.get("/").status_code == 200)
r = client.post("/tools/ongoing-ctp-monitoring/results/feed00000001/refresh",
                follow_redirects=False)
check("the refresh route itself now completes (303, not 500)",
      r.status_code == 303 and "dashboard" in r.headers.get("location", ""))

if _fail:
    print(f"\n{_fail} CHECK(S) FAILED")
    sys.exit(1)
print("\nALL v11.17 TESTS PASSED")
