"""Export the site as standalone HTML into preview/ for offline review.

Every screen renders from the real app (so the preview stays faithful), then
internal links are rewritten to relative .html files and the stylesheet is
inlined so each file is fully self-contained. Form/download actions are made
inert ('#') because there's no server behind a double-clicked file — this is a
visual/flow draft, not the working app (run the .bat launcher for that).

A "preview navigation" bar is injected at the top of each page so you can jump
between all screens, including ones normally reached mid-flow (review/results/
confirmation pages).

Run: python scripts/export_static.py
"""
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.main import app
from app.services import remittance_store

ROOT = Path(__file__).resolve().parent.parent
PREVIEW = ROOT / "preview"
OUTPUTS = ROOT / "data" / "outputs"
UPLOADS = ROOT / "data" / "uploads"
SAMPLE = ROOT / "samples" / "orange_cameroun_sample.xlsx"
AR_SAMPLE = ROOT / "samples" / "ar_breakdown_sample.xlsx"
MASTER_SAMPLE = ROOT / "samples" / "ar_master_sample.xlsx"
BANK_SAMPLE = ROOT / "samples" / "bank_statement_sample.xlsx"
GL_SAMPLE = ROOT / "samples" / "gl_sample.xlsx"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

client = TestClient(app)

# Plain string rewrites. Order matters: most-specific paths first.
REWRITES = [
    ('action="/tools/orange-cameroun/generate"', 'action="#"'),
    ('action="/tools/orange-cameroun/upload"', 'action="#"'),
    ('action="/tools/ongoing-ctp-monitoring/analyze"', 'action="#"'),
    ('action="/tools/ongoing-ctp-monitoring/email"', 'action="#"'),
    ('action="/tools/ongoing-ctp-monitoring/holds/update"', 'action="#"'),
    ('action="/tools/ongoing-ctp-monitoring/master/delete"', 'action="#"'),
    ('action="/tools/ongoing-ctp-monitoring/master"', 'action="#"'),
    ('action="/tools/remittance-portal/contacts/delete"', 'action="#"'),
    ('action="/tools/orange-cameroun/customers/delete"', 'action="#"'),
    ('action="/tools/momo/customers/delete"', 'action="#"'),
    ('action="/tools/remittance-portal/upload"', 'action="#"'),
    ('action="/tools/remittance-portal/settings"', 'action="#"'),
    ('action="/settings/smtp/test"', 'action="#"'),
    ('action="/settings/smtp"', 'action="#"'),
    ('href="/settings"', 'href="app-settings.html"'),
    ('action="/tools/bank-statements/upload"', 'action="#"'),
    ('/tools/bank-statements', 'bank-statements.html'),
    ('action="/tools/vendor-invoice-allocation/eno/key"', 'action="#"'),
    ('action="/tools/vendor-invoice-allocation/eno/generate"', 'action="#"'),
    ('/tools/vendor-invoice-allocation', 'vendor-invoice-allocation.html'),
    ('action="/tools/momo/upload"', 'action="#"'),
    ('action="/tools/momo/generate"', 'action="#"'),
    ('action="/tools/momo/settings"', 'action="#"'),
    ('/tools/momo/customers', 'momo-customers.html'),
    ('/tools/momo', 'momo.html'),
    ('action="/tools/vendor-niu/certificates"', 'action="#"'),
    ('action="/tools/vendor-niu/paste"', 'action="#"'),
    ('action="/tools/vendor-niu/upload"', 'action="#"'),
    ('action="/tools/vendor-niu/update"', 'action="#"'),
    ('action="/tools/vendor-niu/remind"', 'action="#"'),
    ('action="/tools/vendor-niu/verify"', 'action="#"'),
    ('action="/tools/vendor-niu/delete"', 'action="#"'),
    ('href="/tools/vendor-niu/export"', 'href="#"'),
    ('/tools/vendor-niu', 'vendor-niu.html'),
    ('/tools/orange-cameroun/customers', 'orange-customers.html'),
    ('/tools/orange-cameroun', 'orange-cameroun.html'),
    ('/tools/ongoing-ctp-monitoring/analyze', '#'),
    ('/tools/ongoing-ctp-monitoring/email', '#'),
    ('/tools/ongoing-ctp-monitoring/holds', 'ongoing-holds.html'),
    ('/tools/ongoing-ctp-monitoring/master', 'ongoing-master.html'),
    ('/tools/ongoing-ctp-monitoring', 'ongoing-ctp.html'),
    ('/tools/remittance-portal/allocations', 'remittance-allocations.html'),
    ('/tools/remittance-portal/settings', 'remittance-settings.html'),
    ('/tools/remittance-portal/contacts', 'remittance-contacts.html'),
    ('/tools/remittance-portal', 'remittance-portal.html'),
    ('href="/download/', 'href="#'),
    ('/download/', '#'),
    ('href="/"', 'href="index.html"'),
]

# Regex rewrites for links that embed generated tokens / batch ids.
REGEX_REWRITES = [
    (re.compile(r'action="/tools/[a-z-]+/(results|batch)/[0-9a-f]+/delete"'),
     'action="#"'),
    (re.compile(r'href="/tools/vendor-niu/certificate/[PM]\d{12}[A-Z]"'),
     'href="#"'),
    (re.compile(r'href="/tools/bank-statements/results/[0-9a-f]+/export"'),
     'href="#"'),
    (re.compile(r'href="/tools/bank-statements/results/[0-9a-f]+"'),
     'href="bank-report.html"'),
    (re.compile(r'(href|action)="/tools/ongoing-ctp-monitoring/results/[0-9a-f]+/dashboard"'),
     r'\1="ongoing-dashboard.html"'),
    (re.compile(r'action="/tools/ongoing-ctp-monitoring/results/[0-9a-f]+/bank"'),
     'action="#"'),
    (re.compile(r'action="/tools/ongoing-ctp-monitoring/results/[0-9a-f]+/hold"'),
     'action="#"'),
    (re.compile(r'href="/tools/ongoing-ctp-monitoring/results/[0-9a-f]+"'),
     'href="ongoing-results.html"'),
    (re.compile(r'action="/portal/[0-9a-f]+/allocate"'), 'action="#"'),
    (re.compile(r'action="/tools/remittance-portal/send-link/[0-9a-f]+"'),
     'action="#"'),
    (re.compile(r'href="/portal/[0-9a-f]+"'), 'href="portal-statement.html"'),
    (re.compile(r'href="/tools/remittance-portal/batch/[0-9a-f]+"'),
     'href="remittance-batch.html"'),
    # The copyable link shown to finance — replace the test token with a
    # readable placeholder so the preview explains itself.
    (re.compile(r'https?://testserver/portal/[0-9a-f]+'),
     'http://YOUR-HOST:8801/portal/UNIQUE-CUSTOMER-TOKEN'),
]

# Inline the stylesheet + app JS so each exported page is fully
# self-contained — it stays styled and interactive even opened on its own.
JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
SCRIPT_TAG = '<script src="/static/app.js" defer></script>'
CSS = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")
CSS += """
/* Preview-only navigation bar */
.preview-nav { background: #fff8e6; border: 1px solid #f0d9a0; border-radius: 10px;
  padding: 10px 14px; margin-bottom: 18px; font-size: 12.5px; line-height: 2; }
.preview-nav strong { color: #8a6100; margin-right: 6px; }
.preview-nav a { margin-right: 10px; white-space: nowrap; }
"""
LINK_TAG = '<link rel="stylesheet" href="/static/styles.css">'

PREVIEW_NAV = """
<div class="preview-nav"><strong>Preview navigation:</strong>
<a href="index.html">Dashboard</a> ·
Orange: <a href="orange-cameroun.html">Upload</a>
<a href="orange-review.html">Review</a>
<a href="orange-done.html">Done</a>
<a href="orange-customers.html">Saved names</a> ·
MoMo: <a href="momo.html">Upload</a>
<a href="momo-review.html">Review</a>
<a href="vendor-invoice-allocation.html">Invoice allocation</a> ·
CtP: <a href="ongoing-ctp.html">Upload</a>
<a href="ongoing-dashboard.html">Dashboard</a>
<a href="ongoing-results.html">Results</a>
<a href="ongoing-master.html">Master file</a>
<a href="ongoing-holds.html">Hold register</a>
<a href="ongoing-emailed.html">Emailed</a> ·
Bank: <a href="bank-statements.html">Upload</a>
<a href="bank-report.html">Report</a> ·
Remittance: <a href="remittance-portal.html">Upload GL</a>
<a href="remittance-batch.html">Customer links</a>
<a href="remittance-allocations.html">Allocations file</a>
<a href="remittance-contacts.html">Contacts</a>
<a href="remittance-settings.html">Settings</a> ·
Customer portal: <a href="portal-statement.html">Statement</a>
<a href="portal-done.html">Confirmation</a> ·
<a href="vendor-niu.html">Vendor NIU</a>
</div>
"""


def rewrite(html):
    html = html.replace(LINK_TAG, f"<style>\n{CSS}\n</style>")
    html = html.replace(SCRIPT_TAG, f"<script defer>\n{JS}\n</script>")
    for pattern, repl in REGEX_REWRITES:
        html = pattern.sub(repl, html)
    for find, repl in REWRITES:
        html = html.replace(find, repl)
    html = html.replace('<main class="container">',
                        f'<main class="container">{PREVIEW_NAV}', 1)
    return html


def write_page(name, html):
    (PREVIEW / name).write_text(rewrite(html), encoding="utf-8")
    print(f"  wrote preview/{name}")


def get(url, **kw):
    r = client.get(url, **kw)
    assert r.status_code == 200, f"{url} -> {r.status_code}"
    return r.text


def post(url, **kw):
    r = client.post(url, **kw)
    assert r.status_code == 200, f"{url} -> {r.status_code}"
    return r.text


def main():
    if PREVIEW.exists():
        shutil.rmtree(PREVIEW)
    PREVIEW.mkdir(parents=True)

    # Snapshot runtime state so we can clean up everything this export creates.
    pre_outputs = {p.name for p in OUTPUTS.glob("*")} if OUTPUTS.exists() else set()
    pre_uploads = {p.name for p in UPLOADS.glob("*")} if UPLOADS.exists() else set()
    customers_file = ROOT / "data" / "customers.json"
    pre_customers = customers_file.read_text(encoding="utf-8") \
        if customers_file.exists() else None

    print("Exporting static preview...")

    # ---- Dashboard + app settings -------------------------------------------
    write_page("index.html", get("/"))
    write_page("app-settings.html", get("/settings"))

    # ---- Orange Cameroun flow ---------------------------------------------
    write_page("orange-cameroun.html", get("/tools/orange-cameroun"))

    review_html = post("/tools/orange-cameroun/upload",
                       files={"file": (SAMPLE.name, SAMPLE.open("rb"), XLSX_MIME)})
    write_page("orange-review.html", review_html)

    token = re.search(r'name="token" value="([0-9a-f]+)"', review_html).group(1)
    idx = re.search(r'name="selected" value="(\d+)"', review_html).group(1)
    done_html = post("/tools/orange-cameroun/generate", data={
        "token": token, "original": SAMPLE.name, "recipient": "",
        "action": "email", "selected": idx, f"name_{idx}": "ACME Logistics Sarl",
    })
    write_page("orange-done.html", done_html)
    write_page("orange-customers.html", get("/tools/orange-cameroun/customers"))

    # ---- Mobile Money (Orange & MTN) flow -----------------------------------
    MOMO_SAMPLE = ROOT / "samples" / "mtn_momo_sample.xlsx"
    write_page("momo.html", get("/tools/momo"))
    write_page("momo-review.html",
               post("/tools/momo/upload",
                    files={"file": (MOMO_SAMPLE.name, MOMO_SAMPLE.open("rb"),
                                    XLSX_MIME)}))

    # ---- Ongoing CtP Monitoring flow ---------------------------------------
    write_page("ongoing-ctp.html", get("/tools/ongoing-ctp-monitoring"))

    # Seed a demo hold (a customer the policy does NOT require on hold) so the
    # preview shows the "review for release" exception too; restored later.
    from app.services import holds as _holds
    from app.tools import ongoing_ctp as _ctp
    holds_file = ROOT / "data" / "credit_holds.json"
    pre_holds = holds_file.read_text(encoding="utf-8") if holds_file.exists() else None
    _holds.set_hold("1004000004", "DELTA CORP", note="demo — preview only")

    # Seed the sample MASTER file so the preview shows segments, the master
    # page, and the reconciliation section; restored later.
    pre_master = _ctp.MASTER_PATH.read_text(encoding="utf-8") \
        if _ctp.MASTER_PATH.exists() else None
    client.post("/tools/ongoing-ctp-monitoring/master",
                files={"file": (MASTER_SAMPLE.name, MASTER_SAMPLE.open("rb"),
                                XLSX_MIME)}, follow_redirects=False)
    write_page("ongoing-master.html", get("/tools/ongoing-ctp-monitoring/master"))

    # Analyze now redirects to the dashboard; capture the token from Location.
    rr = client.post("/tools/ongoing-ctp-monitoring/analyze",
                     data={"as_of": "2026-06-09", "default_rank": "60"},
                     files={"file": (AR_SAMPLE.name, AR_SAMPLE.open("rb"), XLSX_MIME)},
                     follow_redirects=False)
    ctp_token = re.search(r"results/([0-9a-f]+)/dashboard", rr.headers["location"]).group(1)

    # Bank match so the dashboard preview shows the matching section populated.
    client.post(f"/tools/ongoing-ctp-monitoring/results/{ctp_token}/bank",
                files={"file": (BANK_SAMPLE.name, BANK_SAMPLE.open("rb"), XLSX_MIME)},
                follow_redirects=False)

    write_page("ongoing-dashboard.html",
               get(f"/tools/ongoing-ctp-monitoring/results/{ctp_token}/dashboard"))
    write_page("ongoing-results.html",
               get(f"/tools/ongoing-ctp-monitoring/results/{ctp_token}"))
    write_page("ongoing-holds.html", get("/tools/ongoing-ctp-monitoring/holds"))

    results_html = get(f"/tools/ongoing-ctp-monitoring/results/{ctp_token}")
    report = re.search(r'/download/(CtP_controls_[0-9a-f]+\.xlsx)', results_html).group(1)
    write_page("ongoing-emailed.html",
               post("/tools/ongoing-ctp-monitoring/email",
                    data={"report": report, "recipient": ""}))

    # ---- Bank statements (uses the CtP analysis above for the AR link) -----
    rr = client.post("/tools/bank-statements/upload",
                     files={"file": (BANK_SAMPLE.name, BANK_SAMPLE.open("rb"),
                                     XLSX_MIME)}, follow_redirects=False)
    bank_token = re.search(r"results/([0-9a-f]+)", rr.headers["location"]).group(1)
    write_page("bank-report.html",
               get(f"/tools/bank-statements/results/{bank_token}"))
    write_page("bank-statements.html", get("/tools/bank-statements"))
    write_page("vendor-invoice-allocation.html",
               get("/tools/vendor-invoice-allocation"))
    from app.tools import bank as _bank
    (_bank.STORE_DIR / f"{bank_token}.json").unlink(missing_ok=True)

    # Restore hold register + master file; drop the stored analysis + bank matches.
    if pre_holds is not None:
        holds_file.write_text(pre_holds, encoding="utf-8")
    elif holds_file.exists():
        holds_file.unlink()
    if pre_master is not None:
        _ctp.MASTER_PATH.write_text(pre_master, encoding="utf-8")
    else:
        _ctp.MASTER_PATH.unlink(missing_ok=True)
    (ROOT / "data" / "ctp" / f"{ctp_token}.json").unlink(missing_ok=True)
    (ROOT / "data" / "ctp" / f"{ctp_token}_bank.json").unlink(missing_ok=True)

    # ---- Remittance & Allocation Portal flow -------------------------------
    # Snapshot settings + contacts; seed demo recipients so the settings page
    # previews populated. Restored in the cleanup section.
    rem_set = ROOT / "data" / "remittance" / "settings.json"
    rem_con = ROOT / "data" / "remittance" / "contacts.json"
    pre_rem_set = rem_set.read_text(encoding="utf-8") if rem_set.exists() else None
    pre_rem_con = rem_con.read_text(encoding="utf-8") if rem_con.exists() else None
    client.post("/tools/remittance-portal/settings",
                data={"recipients": "credit.controller@example.com\nfinance.cm@example.com"},
                follow_redirects=False)
    write_page("remittance-settings.html",
               get("/tools/remittance-portal/settings"))

    batch_html = post("/tools/remittance-portal/upload",
                      files={"file": (GL_SAMPLE.name, GL_SAMPLE.open("rb"), XLSX_MIME)})
    batch_id = re.search(r"Batch ([0-9a-f]+)", batch_html).group(1)
    batch = remittance_store.load_batch(batch_id)
    cust = next(c for c in batch["customers"]
                if c["customer_name"] == "ACME Logistics")

    # Render the upload page AFTER the batch exists so the "Recent uploads"
    # table appears (and links through to the customer-links page).
    write_page("remittance-portal.html", get("/tools/remittance-portal"))

    # Customer statement (pending) — what the customer sees on their link.
    write_page("portal-statement.html", get(f"/portal/{cust['token']}"))

    # Customer confirms the allocation -> confirmation screen.
    # NB: httpx needs dict-with-list for repeated form keys; a list of tuples
    # is silently treated as raw content and the form arrives empty.
    write_page("portal-done.html", post(f"/portal/{cust['token']}/allocate", data={
        "payment": "0", "invoice": ["0", "1"],
        "confirmed_by": "J. Doe", "role": "Finance Manager",
        "email": "j.doe@acme.example", "phone": "+237 650 11 12 22",
        "note": "May invoices",
    }))

    # Finance views: links page (after one allocation, with the stored contact
    # prefilled), allocations on file, and the captured customer contacts.
    write_page("remittance-batch.html",
               get(f"/tools/remittance-portal/batch/{batch_id}"))
    write_page("remittance-allocations.html",
               get("/tools/remittance-portal/allocations"))
    write_page("remittance-contacts.html",
               get("/tools/remittance-portal/contacts"))

    # ---- Vendor NIU verification (seed demo rows, restore after) -----------
    from app.services import vendors as _vendors
    vjson = ROOT / "data" / "vendors.json"
    pre_vendors = vjson.read_text(encoding="utf-8") if vjson.exists() else None
    parsed, _rej = _vendors.parse_lines(
        "SOCIETE GENERALE DES TRAVAUX; M012345678901A; ANR-2026-0042; "
        "2026-05-12; compta@sgt-cm.example\n"
        "CAMEROON SUPPLIES SARL; P098765432109B; ANR-2026-0117; "
        "2026-02-20; finance@camsup.example")
    _vendors.upsert_many(parsed)
    _vendors.apply_result({
        "niu": "M012345678901A", "status": "active", "message": "",
        "raison_sociale": "SOCIETE GENERALE DES TRAVAUX SA", "sigle": "SGT",
        "cni_rc": "RC/DLA/2010/B/123", "activite": "BTP",
        "regime": "REEL", "centre": "CIME DOUALA 1",
        "checked_at": "2026-06-10 16:00"})
    _vendors.apply_result({
        "niu": "P098765432109B", "status": "inactive",
        "message": "Ce NIU est inactif", "raison_sociale": "CAMEROON SUPPLIES",
        "sigle": "", "cni_rc": "", "activite": "COMMERCE GENERAL",
        "regime": "SIMPLIFIE", "centre": "CDI WOURI",
        "checked_at": "2026-06-10 16:00"})
    write_page("vendor-niu.html", get("/tools/vendor-niu"))
    if pre_vendors is not None:
        vjson.write_text(pre_vendors, encoding="utf-8")
    else:
        vjson.unlink(missing_ok=True)

    # ---- Clean up everything the export created ----------------------------
    # Restore remittance settings + contacts exactly as they were.
    if pre_rem_set is not None:
        rem_set.write_text(pre_rem_set, encoding="utf-8")
    else:
        rem_set.unlink(missing_ok=True)
    if pre_rem_con is not None:
        rem_con.write_text(pre_rem_con, encoding="utf-8")
    else:
        rem_con.unlink(missing_ok=True)
    # Remove only the sample batch + its statements (leave real batches alone).
    remit_dir = ROOT / "data" / "remittance"
    (remit_dir / "batches" / f"{batch_id}.json").unlink(missing_ok=True)
    for c in batch["customers"]:
        (remit_dir / "statements" / f"{c['token']}.json").unlink(missing_ok=True)
    if remit_dir.exists() and not any(remit_dir.rglob("*.json")):
        shutil.rmtree(remit_dir, ignore_errors=True)
    # Restore saved customer names exactly as they were before the export.
    if pre_customers is not None:
        customers_file.write_text(pre_customers, encoding="utf-8")
    elif customers_file.exists():
        customers_file.unlink()
    for p in OUTPUTS.glob("*"):
        if p.name not in pre_outputs:
            p.unlink(missing_ok=True)
    for p in UPLOADS.glob("*"):
        if p.name not in pre_uploads:
            p.unlink(missing_ok=True)

    pages = sorted(p.name for p in PREVIEW.glob("*.html"))
    print(f"\n{len(pages)} pages exported. Open: {PREVIEW / 'index.html'}")


if __name__ == "__main__":
    main()
