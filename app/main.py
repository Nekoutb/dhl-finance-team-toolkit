"""FastAPI entry point for the Finance Team Toolkit.

Run with:  uvicorn app.main:app --reload
"""
import uuid
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import (APP_VERSION, BASE_DIR, OUTPUT_DIR, UPLOAD_DIR, ensure_dirs,
                     load_config, save_user_config)
from .services import (acf_reader, ar_master, bank_statement, ctp_dashboard,
                       ctp_rules, customers, dgi, emailer, holds,
                       remittance_pdf, remittance_store, vendors, xlsx_report)
from .tools import bank, momo
from .tools import ongoing_ctp as ctp
from .tools import orange_cameroun as orange
from .tools import registry
from .tools import remittance

ensure_dirs()

app = FastAPI(title="Finance Team Toolkit")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.globals["app_version"] = APP_VERSION
templates.env.globals["plan_labels"] = ctp_rules.PLAN_LABELS


@app.middleware("http")
async def no_browser_cache(request: Request, call_next):
    """Internal tool that changes often — never let browsers serve stale pages."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response

ALLOWED_EXT = {".xlsx", ".xlsm", ".xls"}
BANK_EXT = ALLOWED_EXT | {".pdf"}   # bank statements also arrive as PDFs


def _resolve_upload(token):
    """Find the stored upload for a token (uuid hex). Returns Path or None."""
    if not token or not token.isalnum():
        return None
    matches = list(UPLOAD_DIR.glob(f"{token}.*"))
    return matches[0] if matches else None


# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
def _home_stats():
    """Live figures for the dashboard, all gathered defensively."""
    stats = {"ar": None, "overdue": None, "unapplied": None, "as_of": "",
             "actions_due": 0, "place_on_hold": 0, "calls_due": 0,
             "pending_alloc": 0, "alloc_done": 0, "batch_total": 0,
             "batch_at": "", "vendors_pending": 0, "total_ok": None,
             "activity": []}
    try:
        recent = ctp.list_results(limit=3)
        if recent:
            result = ctp.load_result(recent[0]["token"])
            if result:
                s = result["summary"]
                stats.update(ar=s["total_ar"], overdue=s["overdue_total"],
                             unapplied=-s["credits_total"],
                             actions_due=s["actions_due"],
                             as_of=str(result.get("as_of", "")))
                tc = result.get("total_check")
                stats["total_ok"] = bool(tc and tc.get("matches")) if tc else None
                hc = ctp.hold_compare(result["customers"])
                stats["place_on_hold"] = len(hc["place_on_hold"])
                stats["calls_due"] = sum(
                    g["invoices"] for g in result.get("controls_summary", [])
                    if not g.get("auto") and "call" in g["action"].lower())
        for a in recent:
            stats["activity"].append((a.get("created_at", ""), "🧭",
                f"AR analysis — {a.get('source') or 'file'} "
                f"({a.get('invoices', 0)} invoices)",
                f"/tools/ongoing-ctp-monitoring/results/{a['token']}/dashboard"))
    except Exception:  # noqa: BLE001
        pass
    try:
        batches = remittance_store.list_batches()
        if batches:
            batch = batches[0]
            allocated = {s["token"] for s in remittance_store.list_allocations()}
            done = sum(1 for c in batch["customers"] if c["token"] in allocated)
            stats.update(batch_total=len(batch["customers"]), alloc_done=done,
                         pending_alloc=len(batch["customers"]) - done,
                         batch_at=batch.get("created_at", ""))
            stats["activity"].append((batch.get("created_at", ""), "🔗",
                f"GL uploaded — {batch.get('source', '')} "
                f"({len(batch['customers'])} customer links)",
                f"/tools/remittance-portal/batch/{batch['id']}"))
        for a in remittance_store.list_allocations()[:3]:
            stats["activity"].append((a.get("allocated_at", ""), "✅",
                f"{a.get('customer_name', '')} confirmed an allocation "
                f"({a['allocation']['total_payments']:,.0f})",
                "/tools/remittance-portal/allocations"))
    except Exception:  # noqa: BLE001
        pass
    try:
        stats["vendors_pending"] = len(vendors.pending_nius())
    except Exception:  # noqa: BLE001
        pass
    stats["activity"].sort(key=lambda x: x[0], reverse=True)
    stats["activity"] = stats["activity"][:6]
    return stats


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    cfg = load_config()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "cfg": cfg,
        "groups": registry.grouped(),
        "s": _home_stats(),
    })


# --------------------------------------------------------------------------- #
# Orange Cameroun tool
# --------------------------------------------------------------------------- #
@app.get("/tools/orange-cameroun", response_class=HTMLResponse)
def orange_home(request: Request, error: str = ""):
    cfg = load_config()
    return templates.TemplateResponse("orange/upload.html", {
        "request": request,
        "cfg": cfg,
        "tool": registry.by_slug("orange-cameroun"),
        "error": error,
        "saved_count": len(customers.all_names(orange.TOOL_SLUG)),
    })


@app.post("/tools/orange-cameroun/upload", response_class=HTMLResponse)
async def orange_upload(request: Request, file: UploadFile = File(...)):
    cfg = load_config()
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        return templates.TemplateResponse("orange/upload.html", {
            "request": request,
            "cfg": cfg,
            "tool": registry.by_slug("orange-cameroun"),
            "error": f"Unsupported file type '{ext or 'unknown'}'. "
                     "Please upload an Excel file (.xlsx, .xlsm or .xls).",
            "saved_count": len(customers.all_names(orange.TOOL_SLUG)),
        }, status_code=400)

    token = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{token}{ext}"
    dest.write_bytes(await file.read())

    try:
        model = orange.parse_for_review(dest)
    except Exception as exc:  # noqa: BLE001 - surface any parse error to the user
        return templates.TemplateResponse("orange/upload.html", {
            "request": request,
            "cfg": cfg,
            "tool": registry.by_slug("orange-cameroun"),
            "error": f"Could not read that file: {exc}",
            "saved_count": len(customers.all_names(orange.TOOL_SLUG)),
        }, status_code=400)

    return templates.TemplateResponse("orange/review.html", {
        "request": request,
        "cfg": cfg,
        "tool": registry.by_slug("orange-cameroun"),
        "model": model,
        "token": token,
        "original": file.filename or "orange_transaction.xlsx",
        "recipient": cfg["orange_cameroun"]["default_recipient"],
        "max_tx": orange.MAX_TRANSACTIONS,
    })


@app.post("/tools/orange-cameroun/generate", response_class=HTMLResponse)
async def orange_generate(request: Request):
    cfg = load_config()
    form = await request.form()
    token = form.get("token", "")
    original = form.get("original") or "orange_transaction.xlsx"
    recipient = (form.get("recipient") or "").strip()
    action = form.get("action") or "download"
    selected = [int(s) for s in form.getlist("selected") if s.isdigit()]

    upload_path = _resolve_upload(token)
    if not selected or upload_path is None:
        msg = "Please select at least one transaction." if upload_path else \
              "Your upload expired — please upload the file again."
        return HTMLResponse(
            f'<script>alert({msg!r});history.back();</script>', status_code=400)

    selected = selected[:orange.MAX_TRANSACTIONS]
    names_map = {}
    for idx in selected:
        name = (form.get(f"name_{idx}") or "").strip()
        if name:
            names_map[idx] = name

    out_pdf = OUTPUT_DIR / f"{Path(original).stem}_{token[:8]}.pdf"
    result = orange.build_receipt(upload_path, selected, names_map, cfg, out_pdf)

    customer = result["customer_name"] or "the customer"
    oc = cfg["orange_cameroun"]
    subject = oc["email_subject_template"].format(customer=customer)
    body = oc["email_body_template"].format(customer=customer)

    email_status, eml_name = None, None
    if action == "email":
        smtp = cfg["smtp"]
        if smtp.get("enabled") and recipient:
            try:
                emailer.send_via_smtp(smtp, recipient, subject, body, out_pdf)
                email_status = ("ok", f"Email sent to {recipient}.")
            except Exception as exc:  # noqa: BLE001
                email_status = ("error", f"SMTP send failed: {exc}. "
                                         "A downloadable .eml was created instead.")
                eml_path = OUTPUT_DIR / f"{out_pdf.stem}.eml"
                emailer.build_eml(eml_path, smtp.get("from_address", ""),
                                  recipient, subject, body, out_pdf)
                eml_name = eml_path.name
        else:
            eml_path = OUTPUT_DIR / f"{out_pdf.stem}.eml"
            emailer.build_eml(eml_path, smtp.get("from_address", ""),
                              recipient or "", subject, body, out_pdf)
            eml_name = eml_path.name
            reason = "SMTP is not configured" if not smtp.get("enabled") \
                else "no recipient address was given"
            email_status = ("info", f"Direct send skipped ({reason}). "
                                    "Download the .eml below and send it from Outlook.")

    return templates.TemplateResponse("orange/done.html", {
        "request": request,
        "cfg": cfg,
        "tool": registry.by_slug("orange-cameroun"),
        "pdf_name": out_pdf.name,
        "eml_name": eml_name,
        "customer": result["customer_name"],
        "count": result["count"],
        "recipient": recipient,
        "email_status": email_status,
    })


@app.get("/tools/orange-cameroun/customers", response_class=HTMLResponse)
def orange_customers(request: Request):
    cfg = load_config()
    return templates.TemplateResponse("orange/customers.html", {
        "request": request,
        "cfg": cfg,
        "tool": registry.by_slug("orange-cameroun"),
        "names": customers.all_names(orange.TOOL_SLUG),
    })


@app.post("/tools/orange-cameroun/customers/delete")
async def orange_customers_delete(request: Request):
    form = await request.form()
    customers.delete_name(orange.TOOL_SLUG, form.get("key") or "")
    return RedirectResponse("/tools/orange-cameroun/customers", status_code=303)


# --------------------------------------------------------------------------- #
# Ongoing CtP Monitoring
# --------------------------------------------------------------------------- #
def _ongoing_ctx(request, **extra):
    ctx = {
        "request": request,
        "cfg": load_config(),
        "tool": registry.by_slug("ongoing-ctp-monitoring"),
        "today": date.today().isoformat(),
    }
    ctx.update(extra)
    return ctx


def _master_summary():
    m = ctp.load_master()
    if not m:
        return None
    return {"source": m.get("source", ""), "uploaded_at": m.get("uploaded_at", ""),
            "stats": m.get("stats", {})}


@app.get("/tools/ongoing-ctp-monitoring", response_class=HTMLResponse)
def ongoing_ctp(request: Request, error: str = "", message: str = ""):
    return templates.TemplateResponse(
        "ongoing/upload.html",
        _ongoing_ctx(request, error=error, message=message,
                     recent=ctp.list_results(), master=_master_summary()))


async def _store_master_upload(files):
    """Parse + persist uploaded master file(s) — several files are merged into
    one master. Returns an error string or None."""
    files = [f for f in (files or []) if f and f.filename]
    if not files:
        return "No master file received."
    parsed_list, names = [], []
    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_EXT:
            return (f"Master file type '{ext or 'unknown'}' not supported "
                    "— use .xlsx or .xls.")
        dest = UPLOAD_DIR / f"master_{uuid.uuid4().hex[:8]}{ext}"
        dest.write_bytes(await file.read())
        try:
            parsed_list.append(ar_master.parse_master(dest))
        except Exception as exc:  # noqa: BLE001
            return f"Could not read master file '{file.filename}': {exc}"
        names.append(file.filename)
    combined = ar_master.combine(parsed_list) if len(parsed_list) > 1 \
        else parsed_list[0]
    if not combined["customers"]:
        return "No customers found in the master file(s) — check the layout."
    ctp.save_master(combined, source=" + ".join(names))
    return None


@app.post("/tools/ongoing-ctp-monitoring/master")
async def ongoing_master_upload(request: Request,
                                file: list[UploadFile] = File(...)):
    error = await _store_master_upload(file)
    if error:
        return templates.TemplateResponse("ongoing/upload.html", _ongoing_ctx(
            request, error=error, recent=ctp.list_results(),
            master=_master_summary()), status_code=400)
    m = ctp.load_master()
    msg = (f"Master file updated: {m['stats']['customers']} customers, "
           f"{m['stats']['with_segment']} with a segment, "
           f"{m['stats']['with_hold_flag']} on credit hold.")
    return RedirectResponse(
        f"/tools/ongoing-ctp-monitoring?message={msg}", status_code=303)


@app.get("/tools/ongoing-ctp-monitoring/master", response_class=HTMLResponse)
def ongoing_master_view(request: Request):
    return templates.TemplateResponse("ongoing/master.html", _ongoing_ctx(
        request, master=ctp.load_master()))


@app.post("/tools/ongoing-ctp-monitoring/analyze", response_class=HTMLResponse)
async def ongoing_analyze(request: Request, file: UploadFile = File(...),
                          master_file: list[UploadFile] = File(None),
                          tb_file: UploadFile = File(None),
                          as_of: str = Form(""), default_rank: str = Form("51")):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        return templates.TemplateResponse("ongoing/upload.html", _ongoing_ctx(
            request, error=f"Unsupported file type '{ext or 'unknown'}'. "
                           "Please upload an Excel file (.xlsx, .xlsm or .xls).",
            recent=ctp.list_results(), master=_master_summary()), status_code=400)

    # Optional master file(s) uploaded alongside the transaction file.
    real_masters = [f for f in (master_file or []) if f and f.filename]
    if real_masters:
        error = await _store_master_upload(real_masters)
        if error:
            return templates.TemplateResponse("ongoing/upload.html", _ongoing_ctx(
                request, error=error, recent=ctp.list_results(),
                master=_master_summary()), status_code=400)

    # Optional AR trial balance — drives the FIRST check (tx total vs TB).
    tb = None
    if tb_file and tb_file.filename:
        tb_ext = Path(tb_file.filename).suffix.lower()
        if tb_ext not in ALLOWED_EXT:
            return templates.TemplateResponse("ongoing/upload.html", _ongoing_ctx(
                request, error=f"Trial balance file type '{tb_ext}' not "
                               "supported — use .xlsx or .xls.",
                recent=ctp.list_results(), master=_master_summary()), status_code=400)
        tb_dest = UPLOAD_DIR / f"tb_{uuid.uuid4().hex[:8]}{tb_ext}"
        tb_dest.write_bytes(await tb_file.read())
        try:
            tb = ctp.parse_trial_balance(tb_dest)
        except Exception as exc:  # noqa: BLE001
            return templates.TemplateResponse("ongoing/upload.html", _ongoing_ctx(
                request, error=f"Could not read the trial balance: {exc}",
                recent=ctp.list_results(), master=_master_summary()), status_code=400)

    try:
        as_of_date = datetime.strptime(as_of, "%Y-%m-%d").date() if as_of else date.today()
    except ValueError:
        as_of_date = date.today()
    try:
        rank = int(default_rank)
    except (TypeError, ValueError):
        rank = 51

    token = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{token}{ext}"
    dest.write_bytes(await file.read())

    try:
        result = ctp.analyze(dest, as_of=as_of_date, default_rank=rank,
                             master=ctp.load_master(), tb=tb)
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse("ongoing/upload.html", _ongoing_ctx(
            request, error=f"Could not analyse that file: {exc}",
            recent=ctp.list_results(), master=_master_summary()), status_code=400)

    result["source"] = file.filename or "ar_breakdown.xlsx"
    result["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    ctp.save_result(token[:12], result)
    return RedirectResponse(
        f"/tools/ongoing-ctp-monitoring/results/{token[:12]}/dashboard",
        status_code=303)


def _render_ctp_results(request, token, status_code=200):
    """Render stored results, comparing against the current hold register and
    regenerating the Excel report so hold toggles are always reflected."""
    result = ctp.load_result(token)
    if result is None:
        return templates.TemplateResponse("ongoing/upload.html", _ongoing_ctx(
            request, error="That analysis was not found — please upload the "
                           "file again."), status_code=404)
    hold_cmp = ctp.hold_compare(result["customers"])
    report = OUTPUT_DIR / f"CtP_controls_{token[:8]}.xlsx"
    xlsx_report.build_ctp_report(report, result, hold_cmp)
    return templates.TemplateResponse("ongoing/results.html", _ongoing_ctx(
        request, result=result, hold_cmp=hold_cmp, token=token,
        report_name=report.name,
        recipient=load_config()["orange_cameroun"]["default_recipient"]),
        status_code=status_code)


@app.get("/tools/ongoing-ctp-monitoring/results/{token}", response_class=HTMLResponse)
def ongoing_results(request: Request, token: str):
    return _render_ctp_results(request, token)


@app.post("/tools/ongoing-ctp-monitoring/results/{token}/delete")
async def ongoing_delete(token: str):
    ctp.delete_result(token)
    return RedirectResponse(
        "/tools/ongoing-ctp-monitoring?message=Analysis deleted.", status_code=303)


@app.post("/tools/ongoing-ctp-monitoring/master/delete")
async def ongoing_master_delete():
    ctp.delete_master()
    return RedirectResponse(
        "/tools/ongoing-ctp-monitoring?message=Master file removed.",
        status_code=303)


@app.get("/tools/ongoing-ctp-monitoring/results/{token}/dashboard",
         response_class=HTMLResponse)
def ongoing_dashboard(request: Request, token: str, threshold: str = "100000"):
    result = ctp.load_result(token)
    if result is None:
        return templates.TemplateResponse("ongoing/upload.html", _ongoing_ctx(
            request, error="That analysis was not found — please upload the "
                           "file again."), status_code=404)
    try:
        thr = float(str(threshold).replace(",", "").strip() or 100000)
    except ValueError:
        thr = 100000.0
    hold_cmp = ctp.hold_compare(result["customers"])
    dash = ctp_dashboard.build(result, threshold=thr)
    bank = ctp.load_bank_matches(token)
    return templates.TemplateResponse("ongoing/dashboard.html", _ongoing_ctx(
        request, result=result, dash=dash, hold_cmp=hold_cmp, token=token,
        threshold=thr, bank=bank))


@app.post("/tools/ongoing-ctp-monitoring/results/{token}/bank")
async def ongoing_bank_match(request: Request, token: str,
                             file: UploadFile = File(...)):
    result = ctp.load_result(token)
    if result is None:
        return HTMLResponse("Analysis not found.", status_code=404)
    ext = Path(file.filename or "").suffix.lower()
    if ext in BANK_EXT:
        dest = UPLOAD_DIR / f"bank_{token}{ext}"
        dest.write_bytes(await file.read())
        try:
            bank = bank_statement.read_bank(dest)
            matches = bank_statement.match_customers(result["customers"], bank)
            ctp.save_bank_matches(token, {
                "matches": matches, "lines": len(bank["lines"]),
                "source": file.filename})
        except Exception:  # noqa: BLE001
            pass
    return RedirectResponse(
        f"/tools/ongoing-ctp-monitoring/results/{token}/dashboard",
        status_code=303)


@app.post("/tools/ongoing-ctp-monitoring/results/{token}/hold")
async def ongoing_toggle_hold(request: Request, token: str):
    form = await request.form()
    account = (form.get("account") or "").strip()
    name = (form.get("name") or "").strip()
    if form.get("op") == "release":
        holds.release(account, name)
    else:
        holds.set_hold(account, name, note="Set from CtP analysis")
    return RedirectResponse(
        f"/tools/ongoing-ctp-monitoring/results/{token}", status_code=303)


@app.get("/tools/ongoing-ctp-monitoring/holds", response_class=HTMLResponse)
def ongoing_holds(request: Request, message: str = ""):
    return templates.TemplateResponse("ongoing/holds.html", _ongoing_ctx(
        request, holds=holds.all_holds(), message=message))


@app.post("/tools/ongoing-ctp-monitoring/holds/update")
async def ongoing_holds_update(request: Request):
    form = await request.form()
    if form.get("op") == "release":
        holds.release((form.get("account") or "").strip(),
                      (form.get("name") or "").strip())
        message = "Customer removed from the hold register."
    else:
        added = holds.bulk_add(form.get("bulk") or "")
        message = f"{added} customer(s) added to the hold register."
    return RedirectResponse(
        f"/tools/ongoing-ctp-monitoring/holds?message={message}", status_code=303)


@app.post("/tools/ongoing-ctp-monitoring/email", response_class=HTMLResponse)
async def ongoing_email(request: Request, report: str = Form(...),
                        recipient: str = Form("")):
    cfg = load_config()
    safe = Path(report).name
    path = OUTPUT_DIR / safe
    recipient = recipient.strip()
    if not path.exists():
        status = ("error", "Report not found — please re-run the analysis.")
    else:
        subject = "Ongoing CtP Monitoring — controls report"
        body = ("Hello,\n\nPlease find attached the Collections Treatment Plan "
                "controls report.\n\nKind regards,\nFinance Team")
        smtp = cfg["smtp"]
        if smtp.get("enabled") and recipient:
            try:
                emailer.send_via_smtp(smtp, recipient, subject, body, path)
                status = ("ok", f"Report emailed to {recipient}.")
            except Exception as exc:  # noqa: BLE001
                status = ("error", f"SMTP send failed: {exc}")
        else:
            eml = OUTPUT_DIR / f"{path.stem}.eml"
            emailer.build_eml(eml, smtp.get("from_address", ""),
                              recipient or "", subject, body, path)
            reason = "SMTP is not configured" if not smtp.get("enabled") \
                else "no recipient address was given"
            status = ("info", f"Direct send skipped ({reason}). Download the .eml "
                              f'<a href="/download/{eml.name}">here</a> and send from Outlook.')
    return templates.TemplateResponse("ongoing/emailed.html",
                                      _ongoing_ctx(request, status=status))


# --------------------------------------------------------------------------- #
# Remittance & Allocation Portal
# --------------------------------------------------------------------------- #
_REMIT_TOOL = registry.by_slug("remittance-portal")


@app.get("/tools/remittance-portal", response_class=HTMLResponse)
def remittance_home(request: Request, error: str = "", message: str = ""):
    return templates.TemplateResponse("remittance/upload.html", {
        "request": request, "cfg": load_config(), "tool": _REMIT_TOOL,
        "error": error, "message": message,
        "batches": remittance_store.list_batches(),
    })


@app.post("/tools/remittance-portal/upload", response_class=HTMLResponse)
async def remittance_upload(request: Request, file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        return templates.TemplateResponse("remittance/upload.html", {
            "request": request, "cfg": load_config(), "tool": _REMIT_TOOL,
            "error": f"Unsupported file type '{ext or 'unknown'}'. Upload an Excel file (.xlsx, .xlsm or .xls).",
            "batches": remittance_store.list_batches(),
        }, status_code=400)

    token = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"gl_{token}{ext}"
    dest.write_bytes(await file.read())
    try:
        batch = remittance.build_statements_from_gl(dest, file.filename or "general_ledger.xlsx")
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse("remittance/upload.html", {
            "request": request, "cfg": load_config(), "tool": _REMIT_TOOL,
            "error": f"Could not read that ledger: {exc}",
            "batches": remittance_store.list_batches(),
        }, status_code=400)
    return _render_batch(request, batch["id"])


def _render_batch(request, batch_id, message=""):
    batch = remittance_store.load_batch(batch_id)
    if batch is None:
        return HTMLResponse("Batch not found.", status_code=404)
    allocated = {s["token"] for s in remittance_store.list_allocations()}
    cfg = load_config()
    return templates.TemplateResponse("remittance/batch.html", {
        "request": request, "cfg": cfg, "tool": _REMIT_TOOL,
        "batch": batch, "allocated": allocated,
        "contacts": remittance_store.all_contacts(),
        "smtp_enabled": bool(cfg["smtp"].get("enabled")),
        "message": message,
        "base_url": str(request.base_url).rstrip("/"),
    })


@app.get("/tools/remittance-portal/batch/{batch_id}", response_class=HTMLResponse)
def remittance_batch(request: Request, batch_id: str, message: str = ""):
    return _render_batch(request, batch_id, message=message)


@app.post("/tools/remittance-portal/batch/{batch_id}/delete")
async def remittance_batch_delete(batch_id: str):
    remittance_store.delete_batch(batch_id)
    return RedirectResponse(
        "/tools/remittance-portal?message=Batch and its customer links deleted "
        "(confirmed reconciliation PDFs remain on file).", status_code=303)


@app.post("/tools/remittance-portal/contacts/delete")
async def remittance_contact_delete(request: Request):
    form = await request.form()
    remittance_store.delete_contact(form.get("key") or "")
    return RedirectResponse("/tools/remittance-portal/contacts", status_code=303)


@app.get("/tools/remittance-portal/allocations", response_class=HTMLResponse)
def remittance_allocations(request: Request):
    return templates.TemplateResponse("remittance/allocations.html", {
        "request": request, "cfg": load_config(), "tool": _REMIT_TOOL,
        "allocations": remittance_store.list_allocations(),
    })


@app.get("/tools/remittance-portal/settings", response_class=HTMLResponse)
def remittance_settings(request: Request, message: str = ""):
    cfg = load_config()
    return templates.TemplateResponse("remittance/settings.html", {
        "request": request, "cfg": cfg, "tool": _REMIT_TOOL,
        "settings": remittance_store.load_settings(),
        "smtp_enabled": bool(cfg["smtp"].get("enabled")),
        "message": message,
    })


@app.post("/tools/remittance-portal/settings")
async def remittance_settings_save(request: Request):
    form = await request.form()
    recipients = [line.strip() for line in (form.get("recipients") or "").splitlines()
                  if line.strip() and "@" in line]
    settings = remittance_store.load_settings()
    settings["recipients"] = recipients
    remittance_store.save_settings(settings)
    msg = f"Saved — reconciliations will be sent to {len(recipients)} address(es)."
    return RedirectResponse(f"/tools/remittance-portal/settings?message={msg}",
                            status_code=303)


@app.get("/tools/remittance-portal/contacts", response_class=HTMLResponse)
def remittance_contacts(request: Request):
    return templates.TemplateResponse("remittance/contacts.html", {
        "request": request, "cfg": load_config(), "tool": _REMIT_TOOL,
        "contacts": remittance_store.all_contacts(),
    })


@app.post("/tools/remittance-portal/send-link/{token}")
async def remittance_send_link(request: Request, token: str):
    """Email a customer their statement link (SMTP if configured, else .eml)."""
    cfg = load_config()
    stmt = remittance_store.load_statement(token)
    if stmt is None:
        return HTMLResponse("Statement not found.", status_code=404)
    form = await request.form()
    to_addr = (form.get("email") or "").strip()
    batch_id = stmt.get("batch_id", "")
    if not to_addr or "@" not in to_addr:
        return RedirectResponse(
            f"/tools/remittance-portal/batch/{batch_id}?message=No valid email "
            f"for {stmt['customer_name']} — type one in the row and retry.",
            status_code=303)

    link = f"{str(request.base_url).rstrip('/')}/portal/{token}"
    subject = f"Your account statement & payment allocation — {cfg['organization']}"
    body = (f"Dear {stmt['customer_name']},\n\n"
            f"Please use your private link below to view your account statement, "
            f"select the payments you have made and match them with the invoices "
            f"they settle. You will receive a reconciliation for your records.\n\n"
            f"{link}\n\nKind regards,\n{cfg['organization']}")
    smtp = cfg["smtp"]
    if smtp.get("enabled"):
        try:
            emailer.send_via_smtp(smtp, to_addr, subject, body)
            msg = f"Link emailed to {to_addr}."
        except Exception as exc:  # noqa: BLE001
            msg = f"SMTP failed ({exc}) — configure SMTP in config.json."
    else:
        eml = OUTPUT_DIR / f"link_{stmt['customer_key'] or token[:8]}.eml"
        emailer.build_eml(eml, smtp.get("from_address", ""), to_addr, subject, body)
        msg = (f"SMTP not configured — a ready-to-send email was created: "
               f"download '{eml.name}' from the row and send it from Outlook.")
    return RedirectResponse(
        f"/tools/remittance-portal/batch/{batch_id}?message={msg}", status_code=303)


# --- Customer-facing portal ------------------------------------------------- #
def _balance_note(alloc, currency):
    diff = alloc["difference"]
    if abs(diff) < 0.005:
        return "Fully matched — payments equal the invoices settled."
    if diff > 0:
        return f"Payment on account (unallocated credit): {diff:,.2f} {currency}".strip()
    return f"Invoices not fully covered (short): {-diff:,.2f} {currency}".strip()


@app.get("/portal/{token}", response_class=HTMLResponse)
def portal_statement(request: Request, token: str, error: str = ""):
    stmt = remittance_store.load_statement(token)
    cfg = load_config()
    if stmt is None:
        return HTMLResponse("This link is invalid or has expired.", status_code=404)
    if stmt.get("status") == "allocated":
        return templates.TemplateResponse("portal/done.html", {
            "request": request, "cfg": cfg, "stmt": stmt,
            "pdf_name": stmt.get("pdf_name", ""),
            "balance_note": _balance_note(stmt["allocation"], stmt.get("currency", "")),
            "sent_note": "has been shared with our finance team",
        })
    # Prefill the contact block from what this customer confirmed before.
    contact = remittance_store.get_contact(stmt.get("customer_key")) or {}
    return templates.TemplateResponse("portal/statement.html", {
        "request": request, "cfg": cfg, "stmt": stmt, "contact": contact,
        "error": error,
    })


@app.post("/portal/{token}/allocate", response_class=HTMLResponse)
async def portal_allocate(request: Request, token: str):
    cfg = load_config()
    form = await request.form()
    payment_ids = [int(x) for x in form.getlist("payment") if x.isdigit()]
    invoice_ids = [int(x) for x in form.getlist("invoice") if x.isdigit()]
    confirmed_by = (form.get("confirmed_by") or "").strip()
    role = (form.get("role") or "").strip()
    email = (form.get("email") or "").strip()
    phone = (form.get("phone") or "").strip()
    note = (form.get("note") or "").strip()

    # Name, role, email and phone are obligatory for a valid confirmation.
    missing = [label for label, value in
               (("name", confirmed_by), ("role", role),
                ("email", email), ("phone", phone)) if not value]
    if missing or "@" not in email:
        reason = ("Please provide your " + ", ".join(missing) + "."
                  if missing else "Please provide a valid email address.")
        stmt = remittance_store.load_statement(token)
        if stmt is None:
            return HTMLResponse("This link is invalid or has expired.", status_code=404)
        return templates.TemplateResponse("portal/statement.html", {
            "request": request, "cfg": cfg, "stmt": stmt,
            "contact": {"name": confirmed_by, "role": role,
                        "email": email, "phone": phone},
            "error": reason,
        }, status_code=400)

    stmt = remittance.apply_allocation(
        token, payment_ids, invoice_ids, confirmed_by, note,
        contact={"role": role, "email": email, "phone": phone})
    if stmt is None:
        return HTMLResponse("This link is invalid or has expired.", status_code=404)

    # Reconciliation PDF, kept on file and attached to the evidence email.
    pdf = OUTPUT_DIR / f"Remittance_{stmt['customer_key'] or token[:8]}_{token[:8]}.pdf"
    pdf = OUTPUT_DIR / Path(pdf.name.replace("/", "-").replace("\\", "-")).name
    remittance_pdf.build_remittance_pdf(pdf, stmt, organization=cfg["organization"])
    stmt["pdf_name"] = pdf.name
    remittance_store.save_statement(token, stmt)

    # Auto-send to finance as evidence (or build an .eml fallback). Recipients
    # come from the remittance settings page; legacy single-recipient fallback.
    recipients = remittance_store.load_settings().get("recipients") or []
    if not recipients and cfg["orange_cameroun"]["default_recipient"]:
        recipients = [cfg["orange_cameroun"]["default_recipient"]]
    subject = f"Remittance allocation — {stmt['customer_name']} — {stmt['allocated_at']}"
    body = (f"{stmt['customer_name']} confirmed a payment allocation via the portal.\n\n"
            f"Payments: {stmt['allocation']['total_payments']:,.2f}\n"
            f"Invoices: {stmt['allocation']['total_invoices']:,.2f}\n"
            f"Difference: {stmt['allocation']['difference']:,.2f}\n"
            f"Confirmed by: {confirmed_by} ({role}, {email}, {phone})\n\n"
            "Reconciliation attached.")
    smtp = cfg["smtp"]
    if smtp.get("enabled") and recipients:
        try:
            emailer.send_via_smtp(smtp, recipients, subject, body, pdf)
            sent_note = "has been emailed to our finance team"
        except Exception:  # noqa: BLE001
            emailer.build_eml(OUTPUT_DIR / f"{pdf.stem}.eml",
                              smtp.get("from_address", ""), recipients, subject, body, pdf)
            sent_note = "has been recorded for our finance team"
    else:
        emailer.build_eml(OUTPUT_DIR / f"{pdf.stem}.eml",
                          smtp.get("from_address", ""), recipients, subject, body, pdf)
        sent_note = "has been recorded for our finance team"

    return templates.TemplateResponse("portal/done.html", {
        "request": request, "cfg": cfg, "stmt": stmt, "pdf_name": pdf.name,
        "balance_note": _balance_note(stmt["allocation"], stmt.get("currency", "")),
        "sent_note": sent_note,
    })


# --------------------------------------------------------------------------- #
# Bank Statements — collection activity
# --------------------------------------------------------------------------- #
_BANK_TOOL = registry.by_slug("bank-statements")


def _bank_home(request, error="", message="", status_code=200):
    return templates.TemplateResponse("bank/upload.html", {
        "request": request, "cfg": load_config(), "tool": _BANK_TOOL,
        "error": error, "message": message, "recent": bank.list_reports(),
        "has_ar": bool(ctp.list_results(limit=1)),
        "max_files": bank.MAX_FILES,
    }, status_code=status_code)


@app.get("/tools/bank-statements", response_class=HTMLResponse)
def bank_home(request: Request, error: str = "", message: str = ""):
    return _bank_home(request, error=error, message=message)


@app.post("/tools/bank-statements/upload")
async def bank_upload(request: Request, file: list[UploadFile] = File(...)):
    files = [f for f in (file or []) if f and f.filename]
    if not files:
        return _bank_home(request, error="No statement received.", status_code=400)
    if len(files) > bank.MAX_FILES:
        return _bank_home(request, error=f"Too many files ({len(files)}) — "
                          f"upload a maximum of {bank.MAX_FILES} statements at "
                          "a time.", status_code=400)
    stored = []
    for f in files:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in BANK_EXT:
            return _bank_home(request, error=f"Unsupported file type "
                              f"'{ext or 'unknown'}' in '{f.filename}' — upload "
                              "statements as .xlsx, .xlsm, .xls or .pdf.",
                              status_code=400)
        dest = UPLOAD_DIR / f"bankstmt_{uuid.uuid4().hex[:8]}{ext}"
        dest.write_bytes(await f.read())
        stored.append((dest, f.filename or "statement.xlsx"))
    try:
        report = bank.build_report(stored)
    except Exception as exc:  # noqa: BLE001
        return _bank_home(request, error=f"Could not read the statement(s): {exc}",
                          status_code=400)
    bank.save_report(report)
    return RedirectResponse(
        f"/tools/bank-statements/results/{report['token']}", status_code=303)


@app.post("/tools/bank-statements/results/{token}/delete")
async def bank_delete(token: str):
    bank.delete_report(token)
    return RedirectResponse(
        "/tools/bank-statements?error=&message=Report deleted.", status_code=303)


@app.get("/tools/bank-statements/results/{token}", response_class=HTMLResponse)
def bank_results(request: Request, token: str):
    report = bank.load_report(token)
    if report is None:
        return _bank_home(request, error="That report was not found — upload "
                          "the statement again.", status_code=404)
    return templates.TemplateResponse("bank/report.html", {
        "request": request, "cfg": load_config(), "tool": _BANK_TOOL,
        "r": report, "token": token,
    })


@app.get("/tools/bank-statements/results/{token}/export")
def bank_export(token: str):
    import pandas as pd
    report = bank.load_report(token)
    if report is None:
        return HTMLResponse("Report not found.", status_code=404)
    s = report["summary"]
    overview = pd.DataFrame([
        ("Statement(s)", report["source"]),
        ("Statements analysed", s.get("statement_count", 1)),
        ("Analysed", report["created_at"]),
        ("Period", f"{s['first_date']} — {s['last_date']}"),
        ("Credit transactions (collections)", s["credit_count"]),
        ("Total credited", round(s["total_credited"], 2)),
        ("Matched to customers", round(s["matched_total"], 2)),
        ("Matched %", round(s["matched_pct"], 1)),
        ("Unmatched credits", s["unmatched_count"]),
        ("Unmatched value", round(s["unmatched_total"], 2)),
    ], columns=["Metric", "Value"])
    banks = pd.DataFrame([{
        "Bank": b["bank"], "Statement file": b["source"],
        "Credit transactions": b["credit_count"],
        "Collected": round(b["total_credited"], 2),
    } for b in report.get("banks", [])])
    matched = pd.DataFrame([{
        "Customer": e["customer"], "Account": e["key"],
        "Bank(s)": " / ".join(e.get("banks", [])),
        "Collected": round(e["collected"], 2), "Payments": e["payments"],
        "AR position (net)": round(e["ar_total"], 2),
        "AR overdue": round(e["ar_overdue"], 2),
        "Coverage % of AR": round(e["coverage_pct"], 1)
            if e["coverage_pct"] is not None else "",
        "Remaining AR after payment": round(e["remaining"], 2),
        "Risk status": e["status"],
        "On hold": "Yes" if e["currently_held"] else "No",
        "Match confidence": e["best_score"],
    } for e in report["matched"]])
    unmatched = pd.DataFrame([{
        "Date": ln["date"], "Bank": ln.get("bank", ""),
        "Narration": ln["description"],
        "Credited": round(ln["credit"], 2),
    } for ln in report["unmatched"]])
    daily = pd.DataFrame([{
        "Date": d["date"], "Collections": round(d["total"], 2),
        "Payments": d["count"],
    } for d in report["daily"]])

    out = OUTPUT_DIR / f"Collections_{token[:8]}.xlsx"
    with pd.ExcelWriter(out, engine="xlsxwriter") as xl:
        header_fmt = xl.book.add_format({
            "bold": True, "font_color": "white", "bg_color": "#0b2545",
            "border": 1})
        for name, frame in (("Summary", overview),
                            ("Banks", banks),
                            ("Collections by customer", matched),
                            ("Unmatched credits", unmatched),
                            ("Daily collections", daily)):
            if frame.empty:
                frame = pd.DataFrame([{"Info": "none"}])
            frame.to_excel(xl, sheet_name=name, index=False)
            ws = xl.sheets[name]
            for idx, col in enumerate(frame.columns):
                width = max(len(str(col)),
                            *(frame[col].astype(str).map(len).tolist() or [0]))
                ws.set_column(idx, idx, min(max(width + 2, 10), 50))
                ws.write(0, idx, col, header_fmt)
            ws.freeze_panes(1, 0)
    return FileResponse(out, filename=out.name,
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet")


# --------------------------------------------------------------------------- #
# Mobile Money payments (Orange & MTN) -> PDF
# --------------------------------------------------------------------------- #
_MOMO_TOOL = registry.by_slug("momo")
_MOMO_EXT = {".xlsx", ".xlsm", ".xls", ".pdf"}


def _momo_home(request, error="", message="", status_code=200):
    return templates.TemplateResponse("momo/upload.html", {
        "request": request, "cfg": load_config(), "tool": _MOMO_TOOL,
        "error": error, "message": message,
        "saved_count": len(customers.all_names(momo.TOOL_SLUG)),
        "settings": momo.load_settings(),
        "smtp_enabled": bool(load_config()["smtp"].get("enabled")),
    }, status_code=status_code)


@app.get("/tools/momo", response_class=HTMLResponse)
def momo_home(request: Request, error: str = "", message: str = ""):
    return _momo_home(request, error=error, message=message)


@app.post("/tools/momo/settings")
async def momo_settings_save(request: Request):
    form = await request.form()
    recipient = (form.get("recipient") or "").strip()
    settings = momo.load_settings()
    settings["recipient"] = recipient if "@" in recipient else ""
    momo.save_settings(settings)
    msg = f"Saved — receipts will be emailed to {recipient}." if "@" in recipient \
        else "Recipient cleared."
    return RedirectResponse(f"/tools/momo?message={msg}", status_code=303)


@app.post("/tools/momo/upload", response_class=HTMLResponse)
async def momo_upload(request: Request, file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _MOMO_EXT:
        return _momo_home(request, error=f"Unsupported file type "
                          f"'{ext or 'unknown'}' — upload .xlsx, .xlsm, .xls or .pdf.",
                          status_code=400)
    token = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"momo_{token}{ext}"
    dest.write_bytes(await file.read())
    try:
        model = momo.parse_for_review(dest, filename=file.filename or "")
    except Exception as exc:  # noqa: BLE001
        return _momo_home(request, error=f"Could not read that report: {exc}",
                          status_code=400)
    if not model["rows"]:
        return _momo_home(request, error="No transaction lines were detected "
                          "in that report — send me the file and I'll tune the "
                          "parser to its layout.", status_code=400)
    return templates.TemplateResponse("momo/review.html", {
        "request": request, "cfg": load_config(), "tool": _MOMO_TOOL,
        "model": model, "token": token,
        "original": file.filename or f"momo_report{ext}",
        "recipient": momo.load_settings().get("recipient", ""),
    })


@app.post("/tools/momo/generate", response_class=HTMLResponse)
async def momo_generate(request: Request):
    cfg = load_config()
    form = await request.form()
    token = form.get("token", "")
    original = form.get("original") or "momo_report.xlsx"
    action = form.get("action") or "download"
    recipient = (form.get("recipient") or "").strip()
    selected = [int(s) for s in form.getlist("selected") if s.isdigit()]
    matches = list(UPLOAD_DIR.glob(f"momo_{token}.*")) if token.isalnum() else []
    if not selected or not matches:
        msg = "Please select at least one transaction." if matches else \
              "Your upload expired — please upload the report again."
        return HTMLResponse(
            f'<script>alert({msg!r});history.back();</script>', status_code=400)
    names_map = {idx: (form.get(f"name_{idx}") or "").strip()
                 for idx in selected}
    accounts_map = {idx: (form.get(f"acct_{idx}") or "").strip()
                    for idx in selected}
    result = momo.build_receipt(matches[0], selected, names_map, cfg,
                                OUTPUT_DIR, filename=original,
                                accounts_map=accounts_map)
    out_pdf = result["pdf_path"]

    email_status, eml_name = None, None
    if action == "email":
        who = result["customer_name"] or "the third party"
        subject = (f"Mobile Money transaction {result['reference']} — {who}")
        body = (f"Hello,\n\nPlease find attached the {result['provider']} "
                f"transaction receipt {result['reference']}"
                f" for {who}.\n\nKind regards,\nFinance Team")
        smtp = cfg["smtp"]
        if smtp.get("enabled") and recipient:
            try:
                emailer.send_via_smtp(smtp, recipient, subject, body, out_pdf)
                email_status = ("ok", f"Receipt emailed to {recipient}.")
            except Exception as exc:  # noqa: BLE001
                eml = OUTPUT_DIR / f"{out_pdf.stem}.eml"
                emailer.build_eml(eml, smtp.get("from_address", ""), recipient,
                                  subject, body, out_pdf)
                eml_name = eml.name
                email_status = ("error", f"SMTP send failed: {exc}. A "
                                         "ready-to-send .eml was created instead.")
        else:
            eml = OUTPUT_DIR / f"{out_pdf.stem}.eml"
            emailer.build_eml(eml, smtp.get("from_address", ""), recipient or "",
                              subject, body, out_pdf)
            eml_name = eml.name
            reason = "SMTP is not configured" if not smtp.get("enabled") \
                else "no recipient address was given"
            email_status = ("info", f"Direct send skipped ({reason}). Download "
                                    "the .eml below and send it from Outlook.")

    return templates.TemplateResponse("momo/done.html", {
        "request": request, "cfg": cfg, "tool": _MOMO_TOOL,
        "pdf_name": out_pdf.name, "result": result,
        "email_status": email_status, "eml_name": eml_name,
        "recipient": recipient,
    })


@app.get("/tools/momo/customers", response_class=HTMLResponse)
def momo_customers(request: Request):
    return templates.TemplateResponse("momo/customers.html", {
        "request": request, "cfg": load_config(), "tool": _MOMO_TOOL,
        "names": customers.all_names(momo.TOOL_SLUG),
        "accounts": customers.all_names(momo.ACCT_SLUG),
    })


@app.post("/tools/momo/customers/delete")
async def momo_customers_delete(request: Request):
    form = await request.form()
    key = form.get("key") or ""
    customers.delete_name(momo.TOOL_SLUG, key)
    customers.delete_name(momo.ACCT_SLUG, key)
    return RedirectResponse("/tools/momo/customers", status_code=303)


# --------------------------------------------------------------------------- #
# Vendor Tax Status (NIU) Verification
# --------------------------------------------------------------------------- #
_VENDOR_TOOL = registry.by_slug("vendor-niu")


@app.get("/tools/vendor-niu", response_class=HTMLResponse)
def vendor_home(request: Request, message: str = "", error: str = ""):
    rows = vendors.all_vendors()
    blocked = ok = 0
    remindable = 0
    for v in rows:
        clearance = vendors.payment_clearance(v)
        v["clearance"] = clearance
        if clearance["ok"]:
            ok += 1
        else:
            blocked += 1
            if v.get("email"):
                remindable += 1
        # expiring-but-ok vendors with email also deserve a reminder
        if clearance["ok"] and clearance["cert"]["state"] == "expiring" \
                and v.get("email"):
            remindable += 1
    return templates.TemplateResponse("vendor/index.html", {
        "request": request, "cfg": load_config(), "tool": _VENDOR_TOOL,
        "vendors": rows, "message": message, "error": error,
        "pending": sum(1 for v in rows if v["status"] == "pending"),
        "ok_to_pay": ok, "blocked": blocked, "remindable": remindable,
        "dgi_url": dgi.LOGIN_URL,
        "smtp_enabled": bool(load_config()["smtp"].get("enabled")),
    })


@app.post("/tools/vendor-niu/paste")
async def vendor_paste(request: Request):
    form = await request.form()
    parsed, rejected = vendors.parse_lines(form.get("vendors") or "")
    added, updated = vendors.upsert_many(parsed)
    msg = f"{added} vendor(s) added, {updated} updated."
    if rejected:
        msg += (f" {len(rejected)} line(s) skipped — no taxpayer ID recognised: "
                + "; ".join(rejected[:3]))
    return RedirectResponse(f"/tools/vendor-niu?message={msg}", status_code=303)


_CERT_DIR = (BASE_DIR / "data" / "certificates")
_NIU_STRICT = __import__("re").compile(r"^[PM]\d{12}[A-Z]$")


@app.post("/tools/vendor-niu/certificates")
async def vendor_certificates(request: Request,
                              file: list[UploadFile] = File(...)):
    """Bulk-upload tax clearance certificates (PDF). Each one is read, the
    vendor is created/updated from the document, and unchecked NIUs are
    verified live on DGI — the page then shows the compliance report."""
    files = [f for f in (file or []) if f and f.filename]
    if not files:
        return RedirectResponse("/tools/vendor-niu?error=No certificate received.",
                                status_code=303)
    if len(files) > 20:
        return RedirectResponse(
            f"/tools/vendor-niu?error=Too many files ({len(files)}) — upload "
            "at most 20 certificates at a time.", status_code=303)
    _CERT_DIR.mkdir(parents=True, exist_ok=True)
    created = updated = 0
    mismatches, skipped = [], []
    for f in files:
        if Path(f.filename or "").suffix.lower() != ".pdf":
            skipped.append(f"{f.filename} (not a PDF)")
            continue
        tmp = UPLOAD_DIR / f"acf_{uuid.uuid4().hex[:8]}.pdf"
        tmp.write_bytes(await f.read())
        parsed = acf_reader.read_acf(tmp)
        if not parsed["ok"]:
            skipped.append(f"{f.filename} ({parsed['error']})")
            continue
        # Keep the document as evidence — latest certificate per vendor.
        stored = _CERT_DIR / f"{parsed['niu']}.pdf"
        stored.write_bytes(tmp.read_bytes())
        outcome = vendors.apply_certificate(parsed, cert_file=stored.name)
        if outcome == "created":
            created += 1
        else:
            updated += 1
        if parsed["date_mismatch"]:
            mismatches.append(parsed["niu"])
    bits = [f"{created + updated} certificate(s) read — {created} vendor(s) "
            f"created, {updated} updated"]
    nius = vendors.pending_nius()
    if nius:
        for result in dgi.verify_many(nius):
            vendors.apply_result(result)
        bits.append(f"{len(nius)} NIU(s) checked on DGI Fiscalis")
    if mismatches:
        bits.append("⚠ header/body issue dates differ on: " + ", ".join(mismatches))
    if skipped:
        bits.append("skipped: " + "; ".join(skipped[:4]))
    return RedirectResponse(
        f"/tools/vendor-niu?message={'; '.join(bits)}.", status_code=303)


@app.get("/tools/vendor-niu/certificate/{niu}")
def vendor_certificate_view(niu: str):
    niu = (niu or "").strip().upper()
    if not _NIU_STRICT.fullmatch(niu):
        return HTMLResponse("Invalid taxpayer ID.", status_code=400)
    path = _CERT_DIR / f"{niu}.pdf"
    if not path.exists():
        return HTMLResponse("No certificate on file for this vendor.",
                            status_code=404)
    return FileResponse(path, media_type="application/pdf",
                        filename=f"ACF_{niu}.pdf")


@app.post("/tools/vendor-niu/upload")
async def vendor_upload(request: Request, file: UploadFile = File(...)):
    """Excel vendor list -> registry -> auto-verify -> the page IS the report."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        return RedirectResponse(
            f"/tools/vendor-niu?error=Unsupported file type '{ext}' — upload "
            "an Excel vendor list (.xlsx, .xlsm or .xls).", status_code=303)
    dest = UPLOAD_DIR / f"vendors_{uuid.uuid4().hex[:8]}{ext}"
    dest.write_bytes(await file.read())
    try:
        parsed, rejected = vendors.parse_workbook(dest)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            f"/tools/vendor-niu?error=Could not read that file: {exc}",
            status_code=303)
    if not parsed:
        return RedirectResponse(
            "/tools/vendor-niu?error=No taxpayer IDs recognised in that file — "
            "check the columns.", status_code=303)
    added, updated = vendors.upsert_many(parsed)
    msg = f"{added} vendor(s) added, {updated} updated from {file.filename}."
    if rejected:
        msg += f" {rejected} row(s) skipped (no taxpayer ID)."
    # Issue the report: verify whatever is still unchecked on DGI Fiscalis.
    nius = vendors.pending_nius()
    if nius:
        results = dgi.verify_many(nius)
        for result in results:
            vendors.apply_result(result)
        msg += f" Checked {len(results)} NIU(s) on DGI Fiscalis."
    msg += " Report below — see the OK-to-pay column; export to Excel anytime."
    return RedirectResponse(f"/tools/vendor-niu?message={msg}", status_code=303)


@app.post("/tools/vendor-niu/update")
async def vendor_update(request: Request):
    form = await request.form()
    vendors.set_fields(form.get("niu") or "",
                       cert_date=vendors.parse_date_token(form.get("cert_date"))
                       or (form.get("cert_date") or "").strip(),
                       email=form.get("email"))
    return RedirectResponse("/tools/vendor-niu?message=Vendor details saved.",
                            status_code=303)


@app.post("/tools/vendor-niu/remind")
async def vendor_remind(request: Request):
    """Email vendors whose certificate is pending/expired or NIU not active:
    overdue invoices will not be paid until the situation is regularised."""
    cfg = load_config()
    smtp = cfg["smtp"]
    sent, eml_count, skipped = 0, 0, 0
    for v in vendors.all_vendors():
        clearance = vendors.payment_clearance(v)
        needs = (not clearance["ok"]) or clearance["cert"]["state"] == "expiring"
        if not needs:
            continue
        if not v.get("email"):
            skipped += 1
            continue
        cert = clearance["cert"]
        issues = "\n".join(f"  - {reason}" for reason in clearance["reasons"])
        subject = f"Action required — tax compliance documents ({v['name'] or v['niu']})"
        body = (
            f"Dear {v['name'] or 'Supplier'},\n\n"
            f"Our records show the following pending issue(s) on your tax "
            f"compliance file (taxpayer ID {v['niu']}):\n\n{issues}\n\n"
            "Please note that a tax clearance certificate is valid for three "
            "(3) months from its date of issue, and payment requires a valid "
            "certificate together with an ACTIVE taxpayer status on the DGI "
            "platform.\n\n"
            "As a result, your overdue invoices will NOT be paid until the "
            "situation is regularised. Kindly send us your updated tax "
            "clearance certificate and/or regularise your taxpayer status, "
            f"{'before ' + cert['expires'] if cert['state'] == 'expiring' else 'as soon as possible'}.\n\n"
            f"Kind regards,\n{cfg['organization']} — Finance")
        if smtp.get("enabled"):
            try:
                emailer.send_via_smtp(smtp, v["email"], subject, body)
                sent += 1
                continue
            except Exception:  # noqa: BLE001
                pass
        eml = OUTPUT_DIR / f"reminder_{v['niu']}.eml"
        emailer.build_eml(eml, smtp.get("from_address", ""), v["email"],
                          subject, body)
        eml_count += 1
    bits = []
    if sent:
        bits.append(f"{sent} reminder(s) emailed")
    if eml_count:
        bits.append(f"{eml_count} ready-to-send .eml file(s) created in outputs "
                    "(reminder_<NIU>.eml)")
    if skipped:
        bits.append(f"{skipped} non-compliant vendor(s) have no email on file")
    msg = "; ".join(bits) or "No vendor currently needs a reminder."
    return RedirectResponse(f"/tools/vendor-niu?message={msg}", status_code=303)


@app.post("/tools/vendor-niu/verify")
async def vendor_verify(request: Request):
    form = await request.form()
    scope = form.get("scope") or "pending"
    nius = vendors.all_nius() if scope == "all" else vendors.pending_nius()
    if not nius:
        return RedirectResponse(
            "/tools/vendor-niu?message=Nothing to verify — paste vendors first.",
            status_code=303)
    results = dgi.verify_many(nius)
    for result in results:
        vendors.apply_result(result)
    counts = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    return RedirectResponse(
        f"/tools/vendor-niu?message=Checked {len(results)} NIU(s) on DGI Fiscalis: {summary}.",
        status_code=303)


@app.post("/tools/vendor-niu/delete")
async def vendor_delete(request: Request):
    form = await request.form()
    vendors.delete(form.get("niu") or "")
    return RedirectResponse("/tools/vendor-niu?message=Vendor removed.",
                            status_code=303)


@app.get("/tools/vendor-niu/export")
def vendor_export():
    import pandas as pd
    rows = vendors.all_vendors()
    status_labels = {"active": "ACTIVE", "inactive": "INACTIVE",
                     "not_found": "NOT FOUND", "error": "CHECK FAILED",
                     "pending": "Not yet checked"}
    def _row(v):
        clearance = vendors.payment_clearance(v)
        cert = clearance["cert"]
        return {
            "Vendor name": v["name"],
            "Taxpayer ID (NIU)": v["niu"],
            "Email": v.get("email", ""),
            "Tax clearance certificate": v["certificate"],
            "Certificate issued": cert["issued"],
            "Certificate expires (3 months)": cert["expires"],
            "Certificate status": cert["state"].upper(),
            "Status (DGI)": status_labels.get(v["status"], v["status"]),
            "OK TO PAY": "YES" if clearance["ok"] else "NO — DO NOT PAY",
            "Reason(s)": "; ".join(clearance["reasons"]),
            "Name on DGI file": v["raison_sociale"],
            "Sigle": v["sigle"],
            "CNI/RC": v["cni_rc"],
            "Activity": v["activite"],
            "Tax regime": v["regime"],
            "Tax centre": v["centre"],
            "DGI message": v["message"],
            "Checked at": v["checked_at"],
        }
    frame = pd.DataFrame([_row(v) for v in rows]) if rows else pd.DataFrame(
        [{"Vendor name": "(no vendors yet)"}])
    out = OUTPUT_DIR / f"Vendor_NIU_status_{datetime.now():%Y%m%d_%H%M}.xlsx"
    with pd.ExcelWriter(out, engine="xlsxwriter") as xl:
        frame.to_excel(xl, sheet_name="Vendor NIU status", index=False)
        ws = xl.sheets["Vendor NIU status"]
        header_fmt = xl.book.add_format({
            "bold": True, "font_color": "white", "bg_color": "#0b2545",
            "border": 1})
        for idx, col in enumerate(frame.columns):
            width = max(len(str(col)),
                        *(frame[col].astype(str).map(len).tolist() or [0]))
            ws.set_column(idx, idx, min(max(width + 2, 10), 44))
            ws.write(0, idx, col, header_fmt)
        ws.freeze_panes(1, 0)
    return FileResponse(out, filename=out.name,
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet")


# --------------------------------------------------------------------------- #
# App settings (SMTP — locked into the SaaS via config.json)
# --------------------------------------------------------------------------- #
@app.get("/settings", response_class=HTMLResponse)
def app_settings(request: Request, message: str = "", error: str = ""):
    cfg = load_config()
    return templates.TemplateResponse("settings.html", {
        "request": request, "cfg": cfg, "smtp": cfg["smtp"],
        "message": message, "error": error,
    })


@app.post("/settings/smtp")
async def app_settings_save(request: Request):
    form = await request.form()
    cfg = load_config()
    password = form.get("password") or ""
    updates = {"smtp": {
        "enabled": form.get("enabled") == "on",
        "host": (form.get("host") or "").strip(),
        "port": int(form.get("port") or 587),
        "use_tls": form.get("use_tls") == "on",
        "username": (form.get("username") or "").strip(),
        # Blank password field keeps the stored one.
        "password": password if password else cfg["smtp"].get("password", ""),
        "from_address": (form.get("from_address") or "").strip(),
    }}
    new_cfg = save_user_config(updates)
    state = "ENABLED — emails send directly" if new_cfg["smtp"]["enabled"] \
        else "saved but disabled (.eml fallback stays active)"
    return RedirectResponse(f"/settings?message=SMTP settings {state}.",
                            status_code=303)


@app.post("/settings/smtp/test")
async def app_settings_test(request: Request):
    form = await request.form()
    to_addr = (form.get("test_to") or "").strip()
    cfg = load_config()
    smtp = cfg["smtp"]
    if not to_addr or "@" not in to_addr:
        return RedirectResponse("/settings?error=Enter a valid test address.",
                                status_code=303)
    if not smtp.get("host"):
        return RedirectResponse("/settings?error=Set the SMTP host first.",
                                status_code=303)
    try:
        emailer.send_via_smtp(
            smtp, to_addr,
            f"Test — {cfg['app_name']}",
            "This is a test email from the Finance Team Toolkit. "
            "SMTP is configured correctly.")
        return RedirectResponse(
            f"/settings?message=✓ Test email sent to {to_addr} — SMTP works.",
            status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(
            f"/settings?error=Test failed: {type(exc).__name__}: {exc}",
            status_code=303)


# --------------------------------------------------------------------------- #
# Downloads
# --------------------------------------------------------------------------- #
@app.get("/download/{filename}")
def download(filename: str):
    safe = Path(filename).name  # strip any path components
    path = OUTPUT_DIR / safe
    if not path.exists():
        return HTMLResponse("File not found.", status_code=404)
    lower = safe.lower()
    if lower.endswith(".eml"):
        media = "message/rfc822"
    elif lower.endswith((".xlsx", ".xlsm")):
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        media = "application/pdf"
    return FileResponse(path, media_type=media, filename=safe)
