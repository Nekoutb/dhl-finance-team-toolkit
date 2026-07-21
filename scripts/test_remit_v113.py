"""v11.3 — remittance portal: stable per-customer links, CtP-AR-upload-driven
refresh, and oldest-first WHOLE-invoice auto-allocation (confirm-only).

Fully isolated: the remittance store is redirected to a temp directory before
`app.main` is imported, so nothing here touches the real data/remittance.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()
subprocess.run([sys.executable, str(ROOT / "scripts" / "make_gl_sample.py")],
               check=True, capture_output=True)

# Redirect the remittance store to a temp dir BEFORE importing main so every
# read/write hits tmp (the module functions reference these globals).
from app.services import remittance_store as rs  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="remit113_"))
rs._BASE = _tmp
rs._STMT = _tmp / "statements"
rs._BATCH = _tmp / "batches"
rs._SETTINGS = _tmp / "settings.json"
rs._CONTACTS = _tmp / "contacts.json"
rs._LINKS = _tmp / "links.json"

from fastapi.testclient import TestClient  # noqa: E402

import app.tools.ongoing_ctp as ctp  # noqa: E402
from app import main  # noqa: E402
from app.tools import remittance  # noqa: E402

client = TestClient(main.app)
GL = ROOT / "samples" / "gl_sample.xlsx"


def check(msg, ok):
    print(("[OK ] " if ok else "[FAIL] ") + msg)
    if not ok:
        raise SystemExit("FAILED: " + msg)


def upload():
    before = {b["id"] for b in rs.list_batches()}
    with open(GL, "rb") as f:
        r = client.post("/tools/remittance-portal/upload",
                        files={"file": ("gl_sample.xlsx", f)})
    assert r.status_code == 200, r.status_code
    after = rs.list_batches()
    new = [b for b in after if b["id"] not in before]   # avoid same-second ties
    return new[0] if new else after[0]


def tok(batch, name):
    return next(c["token"] for c in batch["customers"]
               if name in c["customer_name"])


# === 1. build + a stable token issued per customer ==========================
b1 = upload()
acme_t1, gamma_t1 = tok(b1, "ACME"), tok(b1, "Gamma")
check("a stable link token is registered per customer_key",
      rs.link_token("C1001") == acme_t1 and rs.link_token("C3003") == gamma_t1)

# === 2. auto-allocation: oldest-first, WHOLE invoices =======================
st_acme = rs.load_statement(acme_t1)
_p, i_acme, cov_a, tot_a, left_a = remittance.auto_allocation(st_acme)
check("ACME: payment 4700 settles BOTH invoices (3200+1500), no deposit",
      set(i_acme) == {i["id"] for i in st_acme["invoices"]}
      and cov_a == 4700.0 and left_a == 0.0)

st_beta = rs.load_statement(tok(b1, "Beta"))
_pb, i_beta, cov_b, _tb, _lb = remittance.auto_allocation(st_beta)
oldest_beta = sorted(st_beta["invoices"],
                     key=lambda i: (i["date"] == "", i["date"]))
check("Beta: payment 8000 settles only the OLDEST (8000); newer 6000 stays open",
      i_beta == [oldest_beta[0]["id"]] and cov_b == 8000.0)

st_gamma = rs.load_statement(gamma_t1)
_pg, i_gamma, cov_g, _tg, left_g = remittance.auto_allocation(st_gamma)
check("Gamma: payment 3000 vs invoice 2500 -> 500 leftover held as deposit",
      cov_g == 2500.0 and left_g == 500.0)

st_delta = rs.load_statement(tok(b1, "Delta"))
p_delta, i_delta, cov_d, _td, left_d = remittance.auto_allocation(st_delta)
check("Delta: no payment -> nothing settled, no deposit",
      p_delta == [] and i_delta == [] and cov_d == 0.0 and left_d == 0.0)

# === 3. portal page is READ-ONLY (no ticking) ===============================
page = client.get(f"/portal/{acme_t1}")
check("portal has no tick checkboxes (auto-allocated, read-only)",
      page.status_code == 200
      and 'name="invoice"' not in page.text
      and 'name="payment"' not in page.text
      and "Settled" in page.text)

# === 4. confirm IGNORES client-supplied ticks (server recomputes) ===========
r = client.post(f"/portal/{gamma_t1}/allocate",
                data={"invoice": ["999"], "payment": [],  # bogus / empty
                      "confirmed_by": "Jane", "role": "AP",
                      "email": "jane@gamma.cm", "phone": "+237600000000"})
check("confirm accepted", r.status_code == 200)
stg = rs.load_statement(gamma_t1)
check("server ignored the bogus ticks and applied ALL payments oldest-first",
      stg["status"] == "allocated"
      and stg["allocation"]["total_payments"] == 3000.0
      and stg["allocation"]["total_invoices"] == 2500.0
      and stg["allocation"]["deposit_amount"] == 500.0)

# a statement with no payments cannot be confirmed
r = client.post(f"/portal/{tok(b1, 'Delta')}/allocate",
                data={"confirmed_by": "X", "role": "Y",
                      "email": "x@y.cm", "phone": "+237600000000"})
check("no-payment statement is rejected on confirm",
      r.status_code == 400 and "no payments" in r.text.lower())

# === 5. STABLE link across re-upload + confirmed allocation PRESERVED =======
b2 = upload()
check("each customer keeps the SAME link across re-uploads",
      tok(b2, "Gamma") == gamma_t1 and tok(b2, "ACME") == acme_t1)
check("the refreshed link shows fresh (pending) data under the same token",
      rs.load_statement(gamma_t1)["status"] == "pending")
archived = [a for a in rs.list_allocations()
            if a.get("customer_key") == "C3003" and a.get("archived_from")]
check("Gamma's confirmed allocation is preserved (archived) after refresh",
      any(a["allocation"]["deposit_amount"] == 500.0 for a in archived))

# === 6. the CtP AR upload ALSO drives the remittance refresh ================
calls = []
_ob, _oa, _os = (remittance.build_statements_from_gl,
                 ctp.analyze, ctp.save_result)
remittance.build_statements_from_gl = \
    lambda path, name: calls.append(str(path)) or {"id": "x", "customers": []}
ctp.analyze = lambda *a, **k: {"customers": [], "summary": {}}
ctp.save_result = lambda *a, **k: None
try:
    with open(GL, "rb") as f:
        r = client.post("/tools/ongoing-ctp-monitoring/analyze",
                        files={"file": ("ar.xlsx", f)}, follow_redirects=False)
    check("uploading the AR file in the CtP portal refreshed the remittance links",
          r.status_code == 303 and len(calls) == 1)
finally:
    remittance.build_statements_from_gl = _ob
    ctp.analyze, ctp.save_result = _oa, _os

# === 7. DELETE SAFETY with stable tokens (review fix) =======================
# gamma_t1/acme_t1 now belong to b2 (b1 was overwritten in place). Deleting the
# SUPERSEDED batch b1 must NOT remove the live statements.
r = client.post(f"/tools/remittance-portal/batch/{b1['id']}/delete",
                follow_redirects=False)
check("deleting a superseded batch keeps every live link alive",
      r.status_code == 303
      and rs.load_statement(gamma_t1) is not None
      and rs.load_statement(acme_t1) is not None
      and client.get(f"/portal/{acme_t1}").status_code == 200)

# Confirm ACME on the live batch, then delete THAT batch: the pending files go,
# but the CONFIRMED allocation is archived (never lost).
client.post(f"/portal/{acme_t1}/allocate",
            data={"confirmed_by": "Al", "role": "AP",
                  "email": "al@acme.cm", "phone": "+237600000001"})
assert rs.load_statement(acme_t1)["status"] == "allocated"
r = client.post(f"/tools/remittance-portal/batch/{b2['id']}/delete",
                follow_redirects=False)
check("deleting the live batch removes its PENDING statements",
      r.status_code == 303 and rs.load_statement(gamma_t1) is None)
acme_arch = [a for a in rs.list_allocations()
             if a.get("customer_key") == "C1001" and a.get("archived_from")]
check("a CONFIRMED allocation is archived (not lost) when its batch is deleted",
      any(a["allocation"]["total_payments"] == 4700.0 for a in acme_arch))

# === 8. link registry survives concurrent writers (atomic + cross-process) ==
import threading as _thr  # noqa: E402

_errs = []


def _w(i):
    try:
        rs.set_link_token(f"CC{i:03d}", rs.new_token())
    except Exception as exc:  # noqa: BLE001
        _errs.append(exc)


_ts = [_thr.Thread(target=_w, args=(i,)) for i in range(24)]
for _t in _ts:
    _t.start()
for _t in _ts:
    _t.join()
_allk = rs.all_link_tokens()
check("concurrent set_link_token keeps every mapping (no lost updates)",
      not _errs and all(f"CC{i:03d}" in _allk for i in range(24)))

print("\nALL REMITTANCE v11.3 TESTS PASSED")
