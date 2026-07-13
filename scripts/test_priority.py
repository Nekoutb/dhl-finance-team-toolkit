"""Priority customers (v5.5) — list management + ⭐ filter on analyses."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()
for maker in ("make_sample.py", "make_master_sample.py"):
    subprocess.run([sys.executable, str(ROOT / "scripts" / maker)],
                   check=True, capture_output=True)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.tools import ongoing_ctp as ctp  # noqa: E402

client = TestClient(app)
SAMPLE = ROOT / "samples" / "ar_breakdown_sample.xlsx"
MASTER = ROOT / "samples" / "ar_master_sample.xlsx"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# data/ctp/priority.json is REAL user data — snapshot & restore exactly.
pri_snapshot = (ctp.PRIORITY_PATH.read_text(encoding="utf-8")
                if ctp.PRIORITY_PATH.exists() else None)
created_token = None
try:
    # --- master + analysis on file ------------------------------------------
    with open(MASTER, "rb") as fh:
        r = client.post("/tools/ongoing-ctp-monitoring/master",
                        files={"file": ("ar_master_sample.xlsx", fh, XLSX)},
                        follow_redirects=False)
    with open(SAMPLE, "rb") as fh:
        r = client.post("/tools/ongoing-ctp-monitoring/analyze",
                        data={"as_of": "2026-06-09", "default_rank": "60"},
                        files={"file": ("ar_breakdown_sample.xlsx", fh, XLSX)},
                        follow_redirects=False)
    assert r.status_code == 303
    created_token = re.search(r"results/([0-9a-f]+)/dashboard",
                              r.headers["location"]).group(1)

    # --- 1. bulk paste: account + name + unknown line ------------------------
    r = client.post("/tools/ongoing-ctp-monitoring/priority/bulk",
                    data={"keys": "1004000001\nBETA TRADING SARL\nMYSTERY LTD"},
                    follow_redirects=False)
    assert r.status_code == 303
    keys = ctp.priority_set()
    assert "1004000001" in keys, keys                      # account resolved
    assert "1004000002" in keys, keys                      # name -> account key
    assert "MYSTERY LTD" in keys, keys                     # kept as typed
    print("ok: bulk paste resolves accounts and names (3 customers)")

    # --- 2. master page shows stars + count + filter --------------------------
    r = client.get("/tools/ongoing-ctp-monitoring/master")
    assert "Priority customers" in r.text
    assert r.text.count("★") >= 2, "filled stars missing"
    r = client.get("/tools/ongoing-ctp-monitoring/master?priority=1")
    assert "ACME LOGISTICS" in r.text and "BETA TRADING" in r.text
    assert "GAMMA SARL" not in r.text, "non-priority shown in filtered view"
    print("ok: master page stars + priority-only view")

    # --- 3. toggle route ------------------------------------------------------
    r = client.post("/tools/ongoing-ctp-monitoring/priority/toggle",
                    data={"key": "1004000003"}, follow_redirects=False)
    assert "1004000003" in ctp.priority_set()
    client.post("/tools/ongoing-ctp-monitoring/priority/toggle",
                data={"key": "1004000003"}, follow_redirects=False)
    assert "1004000003" not in ctp.priority_set()
    print("ok: star toggle adds and removes a customer")

    # --- 4. dashboard filter recomputes every section -------------------------
    full = client.get(f"/tools/ongoing-ctp-monitoring/results/{created_token}/dashboard")
    pri = client.get(f"/tools/ongoing-ctp-monitoring/results/{created_token}"
                     "/dashboard?priority=1")
    assert pri.status_code == 200
    assert "priority customers only" in pri.text
    m_full = re.search(r"(\d+) invoices", full.text)
    m_pri = re.search(r"(\d+) invoices", pri.text)
    assert m_pri and m_full and int(m_pri.group(1)) < int(m_full.group(1)), \
        (m_full and m_full.group(0), m_pri and m_pri.group(0))
    assert "GAMMA" not in pri.text.replace("GAMMA SARL — possible", ""), \
        "non-priority customer leaked into the filtered dashboard"
    print("ok: dashboard priority filter — sections recomputed")

    # --- 5. results page filter + priority Excel report -----------------------
    r = client.get(f"/tools/ongoing-ctp-monitoring/results/{created_token}"
                   "?priority=1")
    assert r.status_code == 200
    assert "priority customer(s) found in" in r.text
    assert "_priority.xlsx" in r.text
    print("ok: results page priority filter + dedicated priority Excel report")

    # --- 6. priority view discloses ALL customers — no further filter/cap -----
    assert "All priority customers" in pri.text, \
        "complete priority listing section missing"
    assert "no further filtering or sorting applied" in pri.text
    assert "Showing top" not in pri.text, \
        "priority view must not truncate any table (top-N cap leaked in)"
    assert "All priority customers" not in full.text, \
        "complete listing must only appear on the priority view"
    print("ok: priority view lists every priority customer — no caps")

    # --- 7. projected top-offenders section renders on both views -------------
    for page in (full, pri):
        assert "Projected top offenders at month-end" in page.text
        assert "Projected over 60 days" in page.text
        assert "Projected over 90 days" in page.text
    print("ok: projected month-end top-offenders section on the dashboard")

finally:
    if pri_snapshot is None:
        ctp.PRIORITY_PATH.unlink(missing_ok=True)
    else:
        ctp.PRIORITY_PATH.write_text(pri_snapshot, encoding="utf-8")
    if created_token:
        ctp.delete_result(created_token)

print("\nALL PRIORITY TESTS PASSED")
