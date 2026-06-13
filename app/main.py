"""FastAPI entry point for the Finance Team Toolkit.

Run with:  uvicorn app.main:app --reload
"""
import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import (APP_VERSION, BASE_DIR, OUTPUT_DIR, UPLOAD_DIR, ensure_dirs,
                     load_config, save_user_config)
from .services import (acf_reader, ai_ocr, ar_master, auth, bank_statement,
                       cheques, compliance, ctp_dashboard, ctp_rules, customers,
                       dgi, emailer, eno_allocation, holds, inbox, remittance_pdf,
                       remittance_store, variance, vendors, xlsx_report)
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
# Cache-buster for static assets: changes with every release, so /static/
# files can be cached for a year and still update instantly on deploy.
ASSET_V = hashlib.md5(APP_VERSION.encode()).hexdigest()[:8]
templates.env.globals["asset_v"] = ASSET_V


MAX_UPLOAD_BYTES = 50 * 1024 * 1024            # 50 MB per request
MAX_BATCH_UPLOAD_BYTES = 150 * 1024 * 1024     # compliance: up to 25 PDFs


@app.middleware("http")
async def gate_and_cache(request: Request, call_next):
    """Staff-login gate (when configured) + no-stale-cache header.

    Auth is enforced only when config.auth is enabled with users; otherwise
    this is a pass-through (login-free local use). Public paths — the login
    flow, static assets, customer portal links, downloads, health — are always
    open. The signed-cookie user is exposed on request.state.user for templates.
    """
    # CSRF protection: a state-changing POST from a browser must originate
    # from OUR own pages. Every modern browser attaches an Origin header to
    # cross-site POSTs — reject any whose host differs from ours (Referer is
    # the fallback for older browsers; the sandboxed "null" origin is always
    # hostile). Non-browser clients send neither header and pass — they don't
    # carry a victim's cookies, so CSRF doesn't apply to them.
    if request.method == "POST":
        origin = request.headers.get("origin", "")
        source = origin or request.headers.get("referer", "")
        if origin == "null":
            csrf_blocked = True
        elif source:
            src_host = (urlparse(source).netloc or "").lower()
            csrf_blocked = src_host != (request.headers.get("host", "")).lower()
        else:
            csrf_blocked = False
        if csrf_blocked:
            return templates.TemplateResponse("error.html", {
                "request": request, "cfg": load_config(),
                "icon": "🛡️", "heading": "Request blocked",
                "detail": "This form submission came from another website "
                          "(cross-site request), so it was rejected for your "
                          "protection. Open the page on this site and submit "
                          "again.",
                "ref": "",
            }, status_code=403)

    # Upload abuse guard: refuse oversized request bodies BEFORE reading them
    # into memory. Browsers always send Content-Length on form posts.
    if request.method == "POST":
        limit = (MAX_BATCH_UPLOAD_BYTES
                 if request.url.path == "/tools/invoice-compliance/upload"
                 else MAX_UPLOAD_BYTES)
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > limit:
            return templates.TemplateResponse("error.html", {
                "request": request, "cfg": load_config(),
                "icon": "📦", "heading": "Upload too large",
                "detail": f"That upload is {int(length) / 1_048_576:,.0f} MB — "
                          f"the limit is {limit // 1_048_576} MB per upload. "
                          "Split the file(s) and try again.",
                "ref": "",
            }, status_code=413)
    cfg = load_config()
    auth_cfg = cfg.get("auth", {})
    user = None
    if auth.enabled(auth_cfg):
        user = auth.read_token(auth_cfg.get("secret_key", ""),
                               request.cookies.get(auth.COOKIE))
        if user is None and not auth.is_public(request.url.path):
            nxt = request.url.path
            return RedirectResponse(f"/login?next={nxt}", status_code=303)
    request.state.user = user
    request.state.is_admin = auth.is_admin(auth_cfg, user)
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        # Versioned URLs (?v=<release hash>) make long caching safe: a new
        # release changes the URL, so browsers fetch fresh files instantly.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    # Baseline security headers (HSTS only matters behind HTTPS; harmless
    # locally). Frame-blocking protects the login & portal from clickjacking.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if request.url.scheme == "https" or cfg.get("auth", {}).get("secure_cookies"):
        response.headers["Strict-Transport-Security"] = \
            "max-age=31536000; includeSubDomains"
    return response


def redirect_msg(path, message="", error="", status_code=303):
    """303 redirect to ``path`` with a URL-encoded message/error param.

    Use this whenever the banner text contains dynamic content (an exception,
    a filename, a customer name…) so a stray '&' or '?' can't garble the URL.
    """
    params = {}
    if message:
        params["message"] = message
    if error:
        params["error"] = error
    sep = "&" if "?" in path else "?"
    target = f"{path}{sep}{urlencode(params)}" if params else path
    return RedirectResponse(target, status_code=status_code)


@app.get("/healthz")
def healthz():
    return {"status": "ok", "version": APP_VERSION}


# --------------------------------------------------------------------------- #
# Friendly error pages — never show users a bare "Internal Server Error".
# --------------------------------------------------------------------------- #
_log = logging.getLogger("uvicorn.error")


@app.exception_handler(StarletteHTTPException)
async def http_error_page(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404 and "text/html" in (request.headers.get("accept") or ""):
        return templates.TemplateResponse("error.html", {
            "request": request, "cfg": load_config(),
            "icon": "🧭", "heading": "Page not found",
            "detail": "That page doesn't exist (or the link has expired). "
                      "Use the dashboard to find the tool you need.",
            "ref": "",
        }, status_code=404)
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def unexpected_error_page(request: Request, exc: Exception):
    ref = uuid.uuid4().hex[:8].upper()
    _log.exception("Unhandled error %s on %s %s", ref, request.method,
                   request.url.path)
    return templates.TemplateResponse("error.html", {
        "request": request, "cfg": load_config(),
        "icon": "⚠️", "heading": "Something went wrong",
        "detail": "The error has been logged and nothing you entered was "
                  "lost on our side. Please go back and try again — if it "
                  "keeps happening, report the reference below.",
        "ref": ref,
    }, status_code=500)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str = ""):
    if not auth.enabled(load_config().get("auth", {})):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {
        "request": request, "cfg": load_config(), "next": next, "error": error})


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    nxt = form.get("next") or "/"
    if not nxt.startswith("/"):
        nxt = "/"

    # Brute-force throttle: a locked username can't even attempt a login.
    wait = auth.locked_for(username)
    if wait:
        return templates.TemplateResponse("login.html", {
            "request": request, "cfg": load_config(), "next": nxt,
            "error": f"Too many failed attempts — this account is locked for "
                     f"another {max(1, wait // 60)} minute(s)."},
            status_code=429)

    auth_cfg = load_config().get("auth", {})
    stored = auth_cfg.get("users", {}).get(username)
    if stored and auth.verify_password(password, stored):
        auth.clear_failures(username)
        # One-time welcome banner on the page they land on.
        nxt = nxt + ("&" if "?" in nxt else "?") + "welcome=1"
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(
            auth.COOKIE, auth.issue(auth_cfg.get("secret_key", ""), username),
            max_age=auth.MAX_AGE, httponly=True, samesite="lax",
            secure=bool(auth_cfg.get("secure_cookies")))
        return resp

    client_ip = request.client.host if request.client else ""
    auth.record_failure(username, ip=client_ip)
    await asyncio.sleep(auth.FAIL_DELAY)   # slow down password guessing
    return templates.TemplateResponse("login.html", {
        "request": request, "cfg": load_config(), "next": nxt,
        "error": "Incorrect username or password."}, status_code=401)


@app.post("/logout")
def logout():
    # POST-only so the cross-site CSRF gate covers it — a malicious
    # <img src=".../logout"> can no longer force-log-out a signed-in user.
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp

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
def dashboard(request: Request, welcome: str = ""):
    cfg = load_config()
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "cfg": cfg,
        "groups": registry.grouped(),
        "s": _home_stats(),
        "welcome": bool(welcome) and getattr(request.state, "user", None),
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
# CtP Portal
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
def ongoing_master_view(request: Request, priority: str = "", message: str = ""):
    pri = ctp.load_priority()
    return templates.TemplateResponse("ongoing/master.html", _ongoing_ctx(
        request, master=ctp.load_master(), priority_keys=set(pri["keys"]),
        priority_count=len(pri["keys"]), priority_on=bool(priority),
        priority_updated=pri.get("updated_at", ""), message=message))


@app.post("/tools/ongoing-ctp-monitoring/priority/toggle")
async def ongoing_priority_toggle(request: Request):
    form = await request.form()
    key = (form.get("key") or "").strip()
    ctp.toggle_priority(key)
    suffix = "?priority=1" if form.get("filtered") else ""
    return RedirectResponse(f"/tools/ongoing-ctp-monitoring/master{suffix}",
                            status_code=303)


@app.post("/tools/ongoing-ctp-monitoring/priority/bulk")
async def ongoing_priority_bulk(request: Request):
    """Set the priority list from pasted account numbers / customer names —
    lines are resolved against the master file (account first, then name)."""
    form = await request.form()
    lines = [ln.strip() for ln in (form.get("keys") or "").splitlines()
             if ln.strip()]
    master = ctp.load_master() or {}
    by_account = {}
    by_name = {}
    for key, c in (master.get("customers") or {}).items():
        if c.get("account"):
            by_account[str(c["account"]).strip()] = key
        if c.get("name"):
            by_name[str(c["name"]).strip().lower()] = key
    keys, unmatched = set(), []
    for ln in lines:
        key = by_account.get(ln) or by_name.get(ln.lower())
        if key:
            keys.add(key)
        else:
            keys.add(ln)            # keep as-is (matches analyses by key)
            unmatched.append(ln)
    ctp.save_priority(keys)
    msg = f"Priority list saved — {len(keys)} customer(s)."
    if unmatched:
        msg += (f" {len(unmatched)} line(s) not found in the master file "
                "(kept as typed — they match analyses by account/name): "
                + ", ".join(unmatched[:5])
                + ("…" if len(unmatched) > 5 else ""))
    return redirect_msg("/tools/ongoing-ctp-monitoring/master", message=msg)


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


def _render_ctp_results(request, token, status_code=200, priority=""):
    """Render stored results, comparing against the current hold register and
    regenerating the Excel report so hold toggles are always reflected.
    priority='1' narrows the WHOLE analysis to the priority customers."""
    result = ctp.load_result(token)
    if result is None:
        return templates.TemplateResponse("ongoing/upload.html", _ongoing_ctx(
            request, error="That analysis was not found — please upload the "
                           "file again."), status_code=404)
    pri_keys = ctp.priority_set()
    priority_on = bool(priority) and bool(pri_keys)
    if priority_on:
        result = ctp.filter_result_priority(result, pri_keys)
    hold_cmp = ctp.hold_compare(result["customers"])
    # Per-customer credit-stop status, keyed by account, so the invoice table
    # can show each line's customer credit-stop standing.
    cust_status = {c["key"]: {"k": c.get("credit_stop_key", "ok"),
                              "l": c.get("credit_stop", "")}
                   for c in result["customers"]}
    report = OUTPUT_DIR / (f"CtP_controls_{token[:8]}_priority.xlsx"
                           if priority_on else f"CtP_controls_{token[:8]}.xlsx")
    xlsx_report.build_ctp_report(report, result, hold_cmp)
    return templates.TemplateResponse("ongoing/results.html", _ongoing_ctx(
        request, result=result, hold_cmp=hold_cmp, token=token,
        report_name=report.name, cust_status=cust_status,
        priority_on=priority_on, priority_count=len(pri_keys),
        recipient=load_config()["orange_cameroun"]["default_recipient"]),
        status_code=status_code)


@app.get("/tools/ongoing-ctp-monitoring/results/{token}", response_class=HTMLResponse)
def ongoing_results(request: Request, token: str, priority: str = ""):
    return _render_ctp_results(request, token, priority=priority)


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
def ongoing_dashboard(request: Request, token: str, threshold: str = "100000",
                      priority: str = ""):
    result = ctp.load_result(token)
    if result is None:
        return templates.TemplateResponse("ongoing/upload.html", _ongoing_ctx(
            request, error="That analysis was not found — please upload the "
                           "file again."), status_code=404)
    try:
        thr = float(str(threshold).replace(",", "").strip() or 100000)
    except ValueError:
        thr = 100000.0
    # ⭐ Priority filter: rebuild EVERY dashboard section on the priority
    # customers only — works on any analysis, past or future.
    pri_keys = ctp.priority_set()
    priority_on = bool(priority) and bool(pri_keys)
    if priority_on:
        result = ctp.filter_result_priority(result, pri_keys)
    hold_cmp = ctp.hold_compare(result["customers"])
    dash = ctp_dashboard.build(result, threshold=thr)
    # Second KPI line: the same metrics aged to the CURRENT MONTH-END, so the
    # team sees how the aging picture (and who trips a credit hold) will look at
    # period close if nothing is collected before then. The aging compares each
    # invoice date to month-end (e.g. 30 Jun) instead of to the analysis date.
    me = ctp_dashboard.month_end(result["as_of"])
    dash_me = ctp_dashboard.build(result, threshold=thr, as_of=me) if me else None
    # Focused control — customers ON CREDIT HOLD whose names appear in the bank
    # statement credit narrations (possible payments received). The statements
    # come from the single Bank Statements section (no upload on this page).
    bank_rows, _bank_banks = bank.all_statement_lines()
    held_paying = []
    if bank_rows:
        pseudo = {"lines": [{"description": r["text"], "amount": r["amount"],
                             "credit": r["amount"], "debit": 0.0,
                             "date": r["date"], "bank": r["bank"]}
                            for r in bank_rows]}
        held_paying = [m for m in bank_statement.match_customers(
            result["customers"], pseudo) if m.get("currently_held")]
    return templates.TemplateResponse("ongoing/dashboard.html", _ongoing_ctx(
        request, result=result, dash=dash, dash_me=dash_me, month_end=me,
        hold_cmp=hold_cmp, token=token, threshold=thr,
        held_paying=held_paying, bank_available=bool(bank_rows),
        bank_line_count=len(bank_rows),
        priority_on=priority_on, priority_count=len(pri_keys)))


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
        subject = "CtP Portal — controls report"
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
def remittance_allocations(request: Request, message: str = "", error: str = ""):
    cfg = load_config()
    return templates.TemplateResponse("remittance/allocations.html", {
        "request": request, "cfg": cfg, "tool": _REMIT_TOOL,
        "allocations": remittance_store.list_allocations(),
        "forward_default": remittance_store.load_settings().get("forward_default", ""),
        "smtp_enabled": bool(cfg["smtp"].get("enabled")),
        "message": message, "error": error,
    })


@app.post("/tools/remittance-portal/forward/{token}")
async def remittance_forward(request: Request, token: str):
    """Forward a customer's confirmed allocation (summary + PDF) by email."""
    cfg = load_config()
    stmt = remittance_store.load_statement(token)
    if stmt is None or stmt.get("status") != "allocated":
        return HTMLResponse("Allocation not found.", status_code=404)
    form = await request.form()
    to_addr = (form.get("email") or "").strip() \
        or remittance_store.load_settings().get("forward_default", "")
    if not to_addr or "@" not in to_addr:
        return RedirectResponse(
            "/tools/remittance-portal/allocations?error=Type a valid email "
            "address (or set a default under Settings).", status_code=303)
    alloc = stmt["allocation"]
    deposit_line = ""
    if alloc.get("deposit_confirmed"):
        deposit_line = (f"Deposit (excess held on account): {alloc['deposit_amount']:,.2f} "
                        "to offset future invoices\n")
    subject = (f"FWD: Remittance allocation — {stmt['customer_name']} — "
               f"{alloc['allocated_at']}")
    body = (f"Forwarded from the Finance Team Toolkit.\n\n"
            f"{stmt['customer_name']} (Account N° {stmt.get('customer_key', '')}) "
            f"confirmed this payment allocation via the portal on "
            f"{alloc['allocated_at']}.\n\n"
            f"Payments: {alloc['total_payments']:,.2f}\n"
            f"Invoices: {alloc['total_invoices']:,.2f}\n"
            f"Difference: {alloc['difference']:,.2f}\n"
            f"{deposit_line}"
            f"Confirmed by: {alloc['confirmed_by']} ({alloc.get('role', '')}, "
            f"{alloc.get('email', '')}, {alloc.get('phone', '')})\n"
            f"Customer note: {alloc.get('note') or '—'}\n\n"
            "Reconciliation PDF attached.")
    pdf = OUTPUT_DIR / stmt.get("pdf_name", "") if stmt.get("pdf_name") else None
    attachment = pdf if (pdf and pdf.exists()) else None
    smtp = cfg["smtp"]
    if smtp.get("enabled"):
        try:
            emailer.send_via_smtp(smtp, to_addr, subject, body, attachment)
            return redirect_msg(
                "/tools/remittance-portal/allocations",
                message=f"Allocation for {stmt['customer_name']} forwarded "
                        f"to {to_addr}.")
        except Exception as exc:  # noqa: BLE001
            return redirect_msg("/tools/remittance-portal/allocations",
                                error=f"SMTP failed: {exc}")
    eml = OUTPUT_DIR / f"fwd_allocation_{token[:8]}.eml"
    emailer.build_eml(eml, smtp.get("from_address", ""), to_addr, subject,
                      body, attachment)
    return redirect_msg(
        "/tools/remittance-portal/allocations",
        message=f"SMTP not configured — ready-to-send email created: "
                f"{eml.name} (in downloads).")


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
    forward_default = (form.get("forward_default") or "").strip()
    settings["forward_default"] = forward_default if "@" in forward_default else ""
    remittance_store.save_settings(settings)
    msg = f"Saved — reconciliations will be sent to {len(recipients)} address(es)."
    return redirect_msg("/tools/remittance-portal/settings", message=msg)


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
        return redirect_msg(
            f"/tools/remittance-portal/batch/{batch_id}",
            message=f"No valid email for {stmt['customer_name']} — type one "
                    "in the row and retry.")

    link = f"{str(request.base_url).rstrip('/')}/portal/{token}"
    subject = f"Your account statement & payment allocation — {cfg['organization']}"
    greeting = (f"Hi {stmt['customer_name']}"
                + (f" (Account N° {stmt['customer_key']})"
                   if stmt.get("customer_key") else "") + ",")
    body = (f"{greeting}\n\n"
            f"Please use your private link below to view your account statement, "
            f"select the payments you have made and match them with the invoices "
            f"they settle. Kindly apply your payments to your OLDEST outstanding "
            f"invoices first. You will receive a reconciliation for your records.\n\n"
            f"{link}\n\nKind regards,\n{cfg['organization']}")
    html_body = (
        f"<html><body style=\"font-family:Segoe UI,Arial,sans-serif;"
        f"font-size:14px;color:#16203a;line-height:1.6;\">"
        f"<p>{greeting}</p>"
        f"<p>Please use your private link below to view your account "
        f"statement, select the payments you have made and match them with "
        f"the invoices they settle. Kindly apply your payments to your "
        f"<strong>oldest outstanding invoices first</strong>. You will receive "
        f"a reconciliation for your records.</p>"
        f"<p><a href=\"{link}\" style=\"display:inline-block;background:"
        f"#3e5fe0;color:#ffffff;text-decoration:none;padding:10px 22px;"
        f"border-radius:8px;font-weight:600;\">Open my statement &amp; "
        f"allocate payments</a></p>"
        f"<p style=\"font-size:12px;color:#54627c;\">Or copy this link into "
        f"your browser: <a href=\"{link}\">{link}</a></p>"
        f"<p>Kind regards,<br>{cfg['organization']}</p></body></html>")
    smtp = cfg["smtp"]
    if smtp.get("enabled"):
        try:
            emailer.send_via_smtp(smtp, to_addr, subject, body,
                                  html_body=html_body)
            msg = f"Link emailed to {to_addr}."
        except Exception as exc:  # noqa: BLE001
            msg = f"SMTP failed ({exc}) — configure SMTP in config.json."
    else:
        eml = OUTPUT_DIR / f"link_{stmt['customer_key'] or token[:8]}.eml"
        emailer.build_eml(eml, smtp.get("from_address", ""), to_addr, subject,
                          body, html_body=html_body)
        msg = (f"SMTP not configured — a ready-to-send email was created: "
               f"download '{eml.name}' from the row and send it from Outlook.")
    return redirect_msg(f"/tools/remittance-portal/batch/{batch_id}", message=msg)


# --- Customer-facing portal ------------------------------------------------- #
def _balance_note(alloc, currency):
    diff = alloc["difference"]
    if abs(diff) < 0.005:
        return "Fully matched — payments equal the invoices settled."
    if diff > 0:
        if alloc.get("deposit_confirmed"):
            return (f"Excess payment of {diff:,.2f} {currency} is recorded as "
                    "a deposit on your account, to be offset against future "
                    "invoices.").strip()
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

    # Name, role, email and phone are obligatory for a valid confirmation. Any
    # excess payment is classified as a deposit AUTOMATICALLY (see
    # apply_allocation) — the customer is informed, not asked to opt in.
    missing = [label for label, value in
               (("name", confirmed_by), ("role", role),
                ("email", email), ("phone", phone)) if not value]
    reason = ""
    if missing or "@" not in email:
        reason = ("Please provide your " + ", ".join(missing) + "."
                  if missing else "Please provide a valid email address.")
    if reason:
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
    deposit_line = ""
    if stmt["allocation"].get("deposit_confirmed"):
        deposit_line = (f"Deposit (excess held on account): {stmt['allocation']['deposit_amount']:,.2f} "
                        "to offset future invoices\n")
    body = (f"{stmt['customer_name']} confirmed a payment allocation via the portal.\n\n"
            f"Payments: {stmt['allocation']['total_payments']:,.2f}\n"
            f"Invoices: {stmt['allocation']['total_invoices']:,.2f}\n"
            f"Difference: {stmt['allocation']['difference']:,.2f}\n"
            f"{deposit_line}"
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
# Vendor Invoice Allocation (MTN / Orange / ENEO)
# --------------------------------------------------------------------------- #
_ALLOC_TOOL = registry.by_slug("vendor-invoice-allocation")
_ENO_DIR = BASE_DIR / "data" / "allocation" / "eno"
_ENO_KEY_SAMPLE = BASE_DIR / "samples" / "eno_allocation_key.xlsx"


def _eno_seed_key():
    """Seed the allocation key from the bundled sample on first use."""
    if eno_allocation.load_key() is None and _ENO_KEY_SAMPLE.exists():
        try:
            eno_allocation.save_key(
                eno_allocation.parse_key_workbook(_ENO_KEY_SAMPLE),
                source="eno_allocation_key.xlsx (seed)")
        except Exception:  # noqa: BLE001
            pass


def _eno_recent(limit=8):
    if not _ENO_DIR.exists():
        return []
    out = []
    for p in _ENO_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({"token": p.stem, "invoice_no": d.get("invoice_no", ""),
                    "created_at": d.get("created_at", ""),
                    "total_ttc": d.get("computed", {}).get("total_ttc", 0),
                    "has_pdf": (p.with_suffix(".pdf")).exists()})
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out[:limit]


def _alloc_ctx(request, **extra):
    _eno_seed_key()
    cfg = load_config()
    ctx = {"request": request, "cfg": cfg, "tool": _ALLOC_TOOL,
           "eno_key": eno_allocation.load_key(), "eno_recent": _eno_recent(),
           "default_tva": eno_allocation.DEFAULT_TVA_RATE, "result": None,
           "message": "", "error": "", "prefill": {}, "extraction": None,
           "pdf_token": "", "ai_ready": ai_ocr.is_configured(cfg)}
    ctx.update(extra)
    return ctx


@app.get("/tools/vendor-invoice-allocation", response_class=HTMLResponse)
def alloc_home(request: Request, message: str = "", error: str = ""):
    return templates.TemplateResponse("allocation/index.html",
                                      _alloc_ctx(request, message=message, error=error))


@app.post("/tools/vendor-invoice-allocation/eno/key")
async def alloc_eno_key(request: Request, file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        return RedirectResponse("/tools/vendor-invoice-allocation?error="
                                "Upload the allocation key as an Excel file.",
                                status_code=303)
    dest = UPLOAD_DIR / f"enokey_{uuid.uuid4().hex[:8]}{ext}"
    dest.write_bytes(await file.read())
    try:
        parsed = eno_allocation.parse_key_workbook(dest)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse(f"/tools/vendor-invoice-allocation?error="
                                f"Could not read the key: {exc}", status_code=303)
    if not parsed["customers"]:
        return RedirectResponse("/tools/vendor-invoice-allocation?error="
                                "No customers/functions found in that key file.",
                                status_code=303)
    eno_allocation.save_key(parsed, source=file.filename or "key.xlsx")
    n = len(parsed["customers"])
    return RedirectResponse(f"/tools/vendor-invoice-allocation?message="
                            f"Allocation key updated — {n} customer(s).",
                            status_code=303)


@app.post("/tools/vendor-invoice-allocation/eno/extract", response_class=HTMLResponse)
async def alloc_eno_extract(request: Request):
    """AI-read a scanned ENEO invoice and prefill the per-customer amounts."""
    key = eno_allocation.load_key()
    if not key:
        return RedirectResponse("/tools/vendor-invoice-allocation?error="
                                "Upload the allocation key first.", status_code=303)
    form = await request.form()
    upload = form.get("invoice_pdf")
    if upload is None or not getattr(upload, "filename", "") \
            or Path(upload.filename).suffix.lower() != ".pdf":
        return templates.TemplateResponse("allocation/index.html", _alloc_ctx(
            request, error="Choose the scanned ENEO invoice (PDF) first."))
    pdf_bytes = await upload.read()
    refs = key.get("order") or list(key["customers"].keys())
    cfg = load_config()
    try:
        extraction = ai_ocr.extract_eneo_invoice(pdf_bytes, refs, cfg.get("ai"))
    except (ai_ocr.AiNotConfigured, ai_ocr.AiReadError) as exc:
        return templates.TemplateResponse("allocation/index.html", _alloc_ctx(
            request, error=f"AI reading failed: {exc}"))

    # Stage the PDF so Generate can attach it without a re-upload.
    pdf_token = uuid.uuid4().hex[:12]
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / f"enoinv_{pdf_token}.pdf").write_bytes(pdf_bytes)

    # Plain decimal strings (never scientific notation) for the form inputs.
    prefill = {cid: f"{extraction['lines'][cid]:.2f}".rstrip("0").rstrip(".")
               for cid in refs if cid in extraction["lines"]}
    extra_refs = [r for r in extraction["lines"] if r not in refs]
    matched = len(prefill)
    note = (f"AI read the invoice — {matched}/{len(refs)} customer "
            f"reference(s) matched and prefilled. Review the amounts, "
            f"adjust if needed, then build the répartition.")
    if extra_refs:
        note += (f" References on the invoice but NOT in the allocation key "
                 f"(ignored): {', '.join(extra_refs)}.")
    return templates.TemplateResponse("allocation/index.html", _alloc_ctx(
        request, message=note, prefill=prefill, extraction=extraction,
        invoice_no=extraction.get("invoice_no", ""), pdf_token=pdf_token))


@app.post("/tools/vendor-invoice-allocation/eno/generate", response_class=HTMLResponse)
async def alloc_eno_generate(request: Request):
    key = eno_allocation.load_key()
    if not key:
        return RedirectResponse("/tools/vendor-invoice-allocation?error="
                                "Upload the allocation key first.", status_code=303)
    form = await request.form()
    invoice_no = (form.get("invoice_no") or "").strip()
    try:
        tva_rate = float(form.get("tva_rate") or eno_allocation.DEFAULT_TVA_RATE)
    except ValueError:
        tva_rate = eno_allocation.DEFAULT_TVA_RATE
    ht_by_customer = {}
    for cid in (key.get("order") or list(key["customers"].keys())):
        raw = (form.get(f"ht_{cid}") or "").replace(",", "").replace(" ", "")
        try:
            ht_by_customer[cid] = float(raw) if raw else 0.0
        except ValueError:
            ht_by_customer[cid] = 0.0

    computed = eno_allocation.compute(key, ht_by_customer, tva_rate)
    token = uuid.uuid4().hex[:12]
    _ENO_DIR.mkdir(parents=True, exist_ok=True)
    record = {"invoice_no": invoice_no, "tva_rate": tva_rate,
              "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
              "computed": computed}
    (_ENO_DIR / f"{token}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8")
    eno_allocation.build_excel(_ENO_DIR / f"{token}.xlsx", computed, invoice_no)

    upload = form.get("invoice_pdf")
    if upload is not None and getattr(upload, "filename", ""):
        if Path(upload.filename).suffix.lower() == ".pdf":
            (_ENO_DIR / f"{token}.pdf").write_bytes(await upload.read())
    else:
        # Invoice staged by the AI-read step — attach it as evidence.
        pdf_token = (form.get("pdf_token") or "").strip()
        if re.fullmatch(r"[0-9a-f]{12}", pdf_token):
            staged = UPLOAD_DIR / f"enoinv_{pdf_token}.pdf"
            if staged.exists():
                (_ENO_DIR / f"{token}.pdf").write_bytes(staged.read_bytes())

    return templates.TemplateResponse("allocation/index.html", _alloc_ctx(
        request, result=computed, result_token=token, invoice_no=invoice_no,
        message=f"REPARTITION ENEO generated — total TTC "
                f"{computed['total_ttc']:,.0f}."))


@app.get("/tools/vendor-invoice-allocation/eno/export/{token}")
def alloc_eno_export(token: str):
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return HTMLResponse("Invalid reference.", status_code=400)
    path = _ENO_DIR / f"{token}.xlsx"
    if not path.exists():
        return HTMLResponse("Sheet not found.", status_code=404)
    return FileResponse(path, filename=f"REPARTITION_ENEO_{token[:8]}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet")


@app.post("/tools/vendor-invoice-allocation/eno/email/{token}")
async def alloc_eno_email(request: Request, token: str):
    """Email the generated REPARTITION ENEO (Excel attached) to the user."""
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return HTMLResponse("Invalid reference.", status_code=400)
    record_path = _ENO_DIR / f"{token}.json"
    xlsx = _ENO_DIR / f"{token}.xlsx"
    if not record_path.exists() or not xlsx.exists():
        return HTMLResponse("Allocation not found.", status_code=404)
    form = await request.form()
    to_addr = (form.get("email") or "").strip()
    record = json.loads(record_path.read_text(encoding="utf-8"))
    invoice_no = record.get("invoice_no", "")
    if not to_addr or "@" not in to_addr:
        msg, ok = "Type a valid email address first.", False
    else:
        cfg = load_config()
        subject = f"REPARTITION ENEO {invoice_no or token[:8]} — {cfg['organization']}"
        body = (f"Please find attached the REPARTITION ENEO"
                f"{' for invoice ' + invoice_no if invoice_no else ''}.\n\n"
                f"Total HT: {record['computed']['total_ht']:,.2f}\n"
                f"TVA ({record['computed']['tva_rate']:g}%): "
                f"{record['computed']['tva']:,.2f}\n"
                f"Total TTC: {record['computed']['total_ttc']:,.2f}\n\n"
                f"Generated by the {cfg['app_name']}.")
        smtp = cfg["smtp"]
        if smtp.get("enabled"):
            try:
                emailer.send_via_smtp(smtp, to_addr, subject, body, xlsx)
                msg, ok = f"Répartition emailed to {to_addr}.", True
            except Exception as exc:  # noqa: BLE001
                msg, ok = f"SMTP failed: {exc}", False
        else:
            eml = OUTPUT_DIR / f"repartition_eno_{token[:8]}.eml"
            emailer.build_eml(eml, smtp.get("from_address", ""), to_addr,
                              subject, body, xlsx)
            msg, ok = (f"SMTP not configured — ready-to-send email created "
                       f"({eml.name}).", True)
    return templates.TemplateResponse("allocation/index.html", _alloc_ctx(
        request, result=record["computed"], result_token=token,
        invoice_no=invoice_no,
        message=msg if ok else "", error="" if ok else msg))


@app.get("/tools/vendor-invoice-allocation/eno/invoice/{token}")
def alloc_eno_invoice(token: str):
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return HTMLResponse("Invalid reference.", status_code=400)
    path = _ENO_DIR / f"{token}.pdf"
    if not path.exists():
        return HTMLResponse("No invoice on file.", status_code=404)
    return FileResponse(path, media_type="application/pdf",
                        filename=f"ENEO_invoice_{token[:8]}.pdf")


# --------------------------------------------------------------------------- #
# Variance Analysis — expense & balance-sheet accounts, CY vs PY
# --------------------------------------------------------------------------- #
_VAR_TOOL = registry.by_slug("variance-analysis")


def _variance_ctx(request, **extra):
    ctx = {"request": request, "cfg": load_config(), "tool": _VAR_TOOL,
           "result": None, "message": "", "error": "",
           "recent": variance.list_recent()}
    ctx.update(extra)
    return ctx


@app.get("/tools/variance-analysis", response_class=HTMLResponse)
def variance_home(request: Request, message: str = "", error: str = ""):
    return templates.TemplateResponse(
        "variance/index.html",
        _variance_ctx(request, message=message, error=error))


@app.post("/tools/variance-analysis/analyze", response_class=HTMLResponse)
async def variance_analyze(request: Request):
    form = await request.form()
    files = {}
    for key in ("py_tb", "py_gl", "cy_tb", "cy_gl"):
        up = form.get(key)
        if up is None or not getattr(up, "filename", ""):
            return templates.TemplateResponse(
                "variance/index.html",
                _variance_ctx(request, error="All four files are required — "
                              "prior-year TB & GL plus current-year TB & GL."))
        ext = Path(up.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            return templates.TemplateResponse(
                "variance/index.html",
                _variance_ctx(request, error=f"{up.filename}: upload Excel "
                              "files (.xlsx/.xls)."))
        dest = UPLOAD_DIR / f"var_{key}_{uuid.uuid4().hex[:8]}{ext}"
        dest.write_bytes(await up.read())
        files[key] = dest
    try:
        result = variance.build_analysis(
            variance.parse_tb(files["py_tb"]), variance.parse_gl(files["py_gl"]),
            variance.parse_tb(files["cy_tb"]), variance.parse_gl(files["cy_gl"]))
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse(
            "variance/index.html",
            _variance_ctx(request, error=f"Could not read the files: {exc}"))
    if not result["expense"] and not result["balance"]:
        return templates.TemplateResponse(
            "variance/index.html",
            _variance_ctx(request, error="No expense or balance-sheet "
                          "accounts were recognised — check the trial "
                          "balances have Account / Name / Balance columns."))
    token = uuid.uuid4().hex[:12]
    label = (form.get("label") or "").strip()
    try:
        variance.save_result(token, result, {
            "label": label,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
        variance.build_excel(variance.OUT_DIR / f"{token}.xlsx", result, label)
    except Exception as exc:  # noqa: BLE001 — never crash the worker
        return templates.TemplateResponse(
            "variance/index.html",
            _variance_ctx(request, error=f"Analysis computed but could not be "
                          f"saved/exported: {exc}"))
    n = len(result["expense"]) + len(result["balance"])
    return templates.TemplateResponse("variance/index.html", _variance_ctx(
        request, result=result, result_token=token,
        message=f"Variance analysis ready — {n} account(s) compared."))


@app.get("/tools/variance-analysis/view/{token}", response_class=HTMLResponse)
def variance_view(request: Request, token: str):
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return HTMLResponse("Invalid reference.", status_code=400)
    saved = variance.load_result(token)
    if not saved:
        return HTMLResponse("Analysis not found.", status_code=404)
    return templates.TemplateResponse("variance/index.html", _variance_ctx(
        request, result=saved["result"], result_token=token,
        message=f"Saved analysis {saved['meta'].get('label') or token[:8]}."))


@app.get("/tools/variance-analysis/export/{token}")
def variance_export(token: str):
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return HTMLResponse("Invalid reference.", status_code=400)
    path = variance.OUT_DIR / f"{token}.xlsx"
    if not path.exists():
        return HTMLResponse("Export not found.", status_code=404)
    return FileResponse(path, filename=f"variance_analysis_{token[:8]}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet")


# --------------------------------------------------------------------------- #
# Vendor Invoice Compliance Engine — CGI checklist with AI OCR
# --------------------------------------------------------------------------- #
_COMP_TOOL = registry.by_slug("invoice-compliance")


def _comp_ctx(request, **extra):
    cfg = load_config()
    ctx = {"request": request, "cfg": cfg, "tool": _COMP_TOOL,
           "message": "", "error": "", "recent": compliance.list_recent(),
           "ai_ready": ai_ocr.is_configured(cfg),
           "max_invoices": compliance.MAX_INVOICES}
    ctx.update(extra)
    return ctx


@app.get("/tools/invoice-compliance", response_class=HTMLResponse)
def compliance_home(request: Request, message: str = "", error: str = ""):
    return templates.TemplateResponse(
        "compliance/index.html",
        _comp_ctx(request, message=message, error=error))


@app.post("/tools/invoice-compliance/upload")
async def compliance_upload(request: Request):
    cfg = load_config()
    if not ai_ocr.is_configured(cfg):
        return templates.TemplateResponse("compliance/index.html", _comp_ctx(
            request, error="AI reading is not configured — paste your "
            "Anthropic API key under Settings → AI document reading first."))
    form = await request.form()
    uploads = [u for u in form.getlist("invoices")
               if u is not None and getattr(u, "filename", "")]
    pdfs = [u for u in uploads
            if Path(u.filename).suffix.lower() == ".pdf"]
    if not pdfs:
        return templates.TemplateResponse("compliance/index.html", _comp_ctx(
            request, error="Choose at least one invoice PDF."))
    if len(pdfs) > compliance.MAX_INVOICES:
        return templates.TemplateResponse("compliance/index.html", _comp_ctx(
            request, error=f"Maximum {compliance.MAX_INVOICES} invoices per "
            "batch — split the upload."))
    token = uuid.uuid4().hex[:12]
    batch_dir = compliance.BATCH_DIR / token
    batch_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for i, up in enumerate(pdfs):
        (batch_dir / f"{i:03d}.pdf").write_bytes(await up.read())
        names.append(Path(up.filename).name)
    compliance.create_batch(token, names)
    compliance.run_batch_async(token)
    return RedirectResponse(f"/tools/invoice-compliance/batch/{token}",
                            status_code=303)


@app.get("/tools/invoice-compliance/batch/{token}", response_class=HTMLResponse)
def compliance_batch(request: Request, token: str):
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return HTMLResponse("Invalid reference.", status_code=400)
    payload = compliance.load_batch(token)
    if not payload:
        return HTMLResponse("Batch not found.", status_code=404)
    done = sum(1 for i in payload["invoices"] if i["status"] != "pending")
    return templates.TemplateResponse("compliance/batch.html", {
        "request": request, "cfg": load_config(), "tool": _COMP_TOOL,
        "batch": payload, "token": token, "done": done,
        "total": len(payload["invoices"]),
        "running": payload["status"] != "done",
    })


@app.get("/tools/invoice-compliance/batch/{token}/export")
def compliance_export(token: str):
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return HTMLResponse("Invalid reference.", status_code=400)
    payload = compliance.load_batch(token)
    if not payload:
        return HTMLResponse("Batch not found.", status_code=404)
    out = compliance.BATCH_DIR / token / "report.xlsx"
    compliance.build_excel(out, payload)
    return FileResponse(out, filename=f"invoice_compliance_{token[:8]}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet")


@app.get("/tools/invoice-compliance/batch/{token}/pdf/{idx}")
def compliance_pdf(token: str, idx: int):
    if not re.fullmatch(r"[0-9a-f]+", token or "") or idx < 0 or idx > 999:
        return HTMLResponse("Invalid reference.", status_code=400)
    path = compliance.BATCH_DIR / token / f"{idx:03d}.pdf"
    if not path.exists():
        return HTMLResponse("Invoice not found.", status_code=404)
    return FileResponse(path, media_type="application/pdf")


# --------------------------------------------------------------------------- #
# Cheque Payment Processing
# --------------------------------------------------------------------------- #
_CHEQUE_TOOL = registry.by_slug("cheque-processing")
# Per-file cap (the middleware already bounds the whole request to 50 MB).
_MAX_CHEQUE_FILE_BYTES = 15 * 1024 * 1024


def _cheque_ctx(request, **extra):
    cfg = load_config()
    _rows, bank_banks = bank.all_statement_lines()
    ctx = {"request": request, "cfg": cfg, "tool": _CHEQUE_TOOL,
           "message": "", "error": "", "recent": cheques.list_recent(),
           "ai_ready": ai_ocr.is_configured(cfg),
           "max_cheques": cheques.MAX_CHEQUES,
           "bank_lines": len(_rows), "bank_banks": bank_banks}
    ctx.update(extra)
    return ctx


@app.get("/tools/cheque-processing", response_class=HTMLResponse)
def cheque_home(request: Request, message: str = "", error: str = ""):
    return templates.TemplateResponse(
        "cheques/index.html", _cheque_ctx(request, message=message, error=error))


@app.post("/tools/cheque-processing/upload")
async def cheque_upload(request: Request):
    cfg = load_config()
    if not ai_ocr.is_configured(cfg):
        return templates.TemplateResponse("cheques/index.html", _cheque_ctx(
            request, error="AI reading is not configured — paste your "
            "Anthropic API key under Settings → AI document reading first."))
    form = await request.form()
    cheque_uploads = [u for u in form.getlist("cheques")
                      if u is not None and getattr(u, "filename", "")]
    valid = [(u, ai_ocr.media_type_for(u.filename)) for u in cheque_uploads]
    valid = [(u, m) for u, m in valid if m]
    if not valid:
        return templates.TemplateResponse("cheques/index.html", _cheque_ctx(
            request, error="Choose at least one cheque scan (JPG, PNG, WEBP "
            "or PDF)."))
    if len(valid) > cheques.MAX_CHEQUES:
        return templates.TemplateResponse("cheques/index.html", _cheque_ctx(
            request, error=f"Maximum {cheques.MAX_CHEQUES} cheques per batch — "
            "split the upload."))

    # Read + size-check every cheque before writing anything, so an oversized
    # file is rejected without leaving a half-written batch on disk.
    cheque_blobs = []
    for up, media in valid:
        data = await up.read()
        if len(data) > _MAX_CHEQUE_FILE_BYTES:
            return templates.TemplateResponse("cheques/index.html", _cheque_ctx(
                request, error=f"“{Path(up.filename).name}” is too large — max "
                f"{_MAX_CHEQUE_FILE_BYTES // 1048576} MB per cheque scan."))
        cheque_blobs.append((Path(up.filename).name, media, data))

    # Bank statements are NOT uploaded here — they come from the single Bank
    # Statements section. Snapshot the current lines into this batch.
    bank_rows, bank_summary = bank.all_statement_lines()

    token = uuid.uuid4().hex[:12]
    batch_dir = cheques.BATCH_DIR / token
    batch_dir.mkdir(parents=True, exist_ok=True)

    cheque_meta = []
    for i, (name, media, data) in enumerate(cheque_blobs):
        stored = f"{i:03d}{Path(name).suffix.lower()}"
        (batch_dir / stored).write_bytes(data)
        cheque_meta.append({"filename": name, "stored": stored, "media": media})

    cheques.create_batch(token, cheque_meta, bank_rows, bank_summary)
    cheques.run_batch_async(token)
    return RedirectResponse(f"/tools/cheque-processing/batch/{token}",
                            status_code=303)


@app.get("/tools/cheque-processing/batch/{token}", response_class=HTMLResponse)
def cheque_batch(request: Request, token: str):
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return HTMLResponse("Invalid reference.", status_code=400)
    payload = cheques.load_batch(token)
    if not payload:
        return HTMLResponse("Batch not found.", status_code=404)
    done = sum(1 for c in payload["cheques"] if c["status"] != "pending")
    found = sum(1 for c in payload["cheques"] if c.get("appearances"))
    return templates.TemplateResponse("cheques/batch.html", {
        "request": request, "cfg": load_config(), "tool": _CHEQUE_TOOL,
        "batch": payload, "token": token, "done": done,
        "total": len(payload["cheques"]), "found": found,
        "running": payload["status"] != "done"})


@app.get("/tools/cheque-processing/batch/{token}/export")
def cheque_export(token: str):
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return HTMLResponse("Invalid reference.", status_code=400)
    payload = cheques.load_batch(token)
    if not payload:
        return HTMLResponse("Batch not found.", status_code=404)
    out = cheques.BATCH_DIR / token / "cheques.xlsx"
    cheques.build_excel(out, payload)
    return FileResponse(out, filename=f"cheques_{token[:8]}.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet")


@app.get("/tools/cheque-processing/batch/{token}/scan/{idx}")
def cheque_scan(token: str, idx: int):
    if not re.fullmatch(r"[0-9a-f]+", token or "") or idx < 0 or idx > 999:
        return HTMLResponse("Invalid reference.", status_code=400)
    payload = cheques.load_batch(token)
    entry = next((c for c in (payload or {}).get("cheques", [])
                  if c["idx"] == idx), None)
    if not entry:
        return HTMLResponse("Cheque not found.", status_code=404)
    path = cheques.BATCH_DIR / token / entry["stored"]
    if not path.exists():
        return HTMLResponse("Cheque not found.", status_code=404)
    return FileResponse(path, media_type=entry.get("media")
                        or "application/octet-stream")


@app.post("/tools/cheque-processing/batch/{token}/delete")
async def cheque_delete(token: str):
    cheques.delete_batch(token)
    return RedirectResponse(
        "/tools/cheque-processing?message=Cheque batch deleted.",
        status_code=303)


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
    return redirect_msg("/tools/momo", message=msg)


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
        v["cert"] = clearance["cert"]
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
    # Certificate validity tracking: vendors that have a certificate on file.
    tracked = [v for v in rows if v["cert"]["state"] != "none"]
    awaiting = sum(1 for v in rows if v.get("awaiting_update"))
    cfg = load_config()
    return templates.TemplateResponse("vendor/index.html", {
        "request": request, "cfg": cfg, "tool": _VENDOR_TOOL,
        "vendors": rows, "tracked": tracked, "awaiting": awaiting,
        "message": message, "error": error,
        "pending": sum(1 for v in rows if v["status"] == "pending"),
        "ok_to_pay": ok, "blocked": blocked, "remindable": remindable,
        "dgi_url": dgi.LOGIN_URL,
        "smtp_enabled": bool(cfg["smtp"].get("enabled")),
        "inbox_on": bool((cfg.get("imap") or {}).get("enabled")),
        "inbox_last": inbox.last_checked(),
        "inbox_events": inbox.recent_events(8),
    })


@app.post("/tools/vendor-niu/email")
async def vendor_set_email(request: Request):
    """Amend ONLY the reminder email for a vendor (emails change over time);
    leaves the certificate date and everything else untouched."""
    form = await request.form()
    niu = (form.get("niu") or "").strip().upper()
    email = (form.get("email") or "").strip()
    if email and "@" not in email:
        return redirect_msg("/tools/vendor-niu",
                            error="That doesn't look like a valid email address.")
    if not vendors.set_fields(niu, email=email):
        return redirect_msg("/tools/vendor-niu", error="Vendor not found.")
    note = (f"Reminder email for {niu} set to {email}." if email
            else f"Reminder email for {niu} cleared.")
    return redirect_msg("/tools/vendor-niu", message=note)


@app.post("/tools/vendor-niu/remind-one")
async def vendor_remind_one(request: Request):
    """Email ONE vendor a renewal reminder in English or French, and flag them
    as awaiting an updated certificate until a newer one is uploaded."""
    cfg = load_config()
    form = await request.form()
    niu = (form.get("niu") or "").strip().upper()
    lang = "fr" if (form.get("lang") or "en").lower().startswith("fr") else "en"
    vendor = next((v for v in vendors.all_vendors() if v["niu"] == niu), None)
    if not vendor:
        return redirect_msg("/tools/vendor-niu", error="Vendor not found.")
    if not vendor.get("email"):
        return redirect_msg("/tools/vendor-niu",
                            error=f"No email on file for {vendor['name'] or niu} "
                                  "— add one in the table first.")
    subject, body = vendors.reminder_text(vendor, lang, cfg["organization"])
    smtp = cfg["smtp"]
    sent = False
    if smtp.get("enabled"):
        try:
            emailer.send_via_smtp(smtp, vendor["email"], subject, body)
            sent = True
        except Exception:  # noqa: BLE001
            pass
    if not sent:
        eml = OUTPUT_DIR / f"reminder_{niu}_{lang}.eml"
        emailer.build_eml(eml, smtp.get("from_address", ""), vendor["email"],
                          subject, body)
    vendors.mark_reminded(niu, lang)
    where = "emailed" if sent else "created as a ready-to-send .eml"
    lname = "French" if lang == "fr" else "English"
    return redirect_msg(
        "/tools/vendor-niu",
        message=f"{lname} renewal reminder {where} to {vendor['email']} — "
                f"{vendor['name'] or niu} flagged as awaiting an updated "
                "certificate.")


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


_CERT_DIR = vendors.CERT_DIR
_NIU_STRICT = __import__("re").compile(r"^[PM]\d{12}[A-Z]$")


@app.post("/tools/vendor-niu/check-inbox")
async def vendor_check_inbox(request: Request):
    """Read the mailbox now and auto-apply any newer certificates vendors
    have emailed back — the same job the scheduled poller runs."""
    cfg = load_config()
    summary = inbox.check_now(cfg, verify_niu=dgi.verify_niu)
    if not summary["ok"]:
        return redirect_msg("/tools/vendor-niu",
                            error=f"Inbox check failed: {summary['error']}")
    bits = [f"{summary['applied']} certificate(s) auto-applied"]
    if summary["not_newer"]:
        bits.append(f"{summary['not_newer']} not newer (kept the one on file)")
    if summary["unmatched"]:
        bits.append(f"{summary['unmatched']} from unknown taxpayer ID(s)")
    if summary["unreadable"]:
        bits.append(f"{summary['unreadable']} unreadable")
    if summary["scanned"] == 0:
        bits = ["no new certificate attachments found"]
    return redirect_msg("/tools/vendor-niu",
                        message="Inbox checked — " + "; ".join(bits) + ".")


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
    created = updated = not_newer = 0
    mismatches, skipped, stale = [], [], []
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
        stored = _CERT_DIR / f"{parsed['niu']}.pdf"
        # Apply BEFORE touching the stored evidence: a non-newer certificate is
        # rejected and the one on file must be left intact.
        outcome = vendors.apply_certificate(parsed, cert_file=stored.name)
        if outcome == "not_newer":
            not_newer += 1
            stale.append(parsed["niu"])
            continue
        # Accepted — keep this document as the evidence on file.
        stored.write_bytes(tmp.read_bytes())
        if outcome == "created":
            created += 1
        else:
            updated += 1
        if parsed["date_mismatch"]:
            mismatches.append(parsed["niu"])
    bits = [f"{created + updated} certificate(s) accepted — {created} vendor(s) "
            f"created, {updated} updated"]
    if not_newer:
        bits.append(f"{not_newer} ignored — not newer than the certificate on "
                    f"file ({', '.join(stale[:4])})")
    nius = vendors.pending_nius()
    if nius:
        for result in dgi.verify_many(nius):
            vendors.apply_result(result)
        bits.append(f"{len(nius)} NIU(s) checked on DGI Fiscalis")
    if mismatches:
        bits.append("⚠ header/body issue dates differ on: " + ", ".join(mismatches))
    if skipped:
        bits.append("skipped: " + "; ".join(skipped[:4]))
    return redirect_msg("/tools/vendor-niu", message="; ".join(bits) + ".")


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
        return redirect_msg("/tools/vendor-niu",
                            error=f"Could not read that file: {exc}")
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
    return redirect_msg("/tools/vendor-niu", message=msg)


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
    return redirect_msg(
        "/tools/vendor-niu",
        message=f"Checked {len(results)} NIU(s) on DGI Fiscalis: {summary}.")


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
def _admin_block(request):
    """Settings & onboarding are admin-only once an admins list exists."""
    if not getattr(request.state, "is_admin", True):
        return RedirectResponse(
            "/settings?error=Administrator access required for that action.",
            status_code=303)
    return None


@app.get("/settings", response_class=HTMLResponse)
def app_settings(request: Request, message: str = "", error: str = ""):
    cfg = load_config()
    auth_cfg = cfg.get("auth", {})
    return templates.TemplateResponse("settings.html", {
        "request": request, "cfg": cfg, "smtp": cfg["smtp"],
        "message": message, "error": error,
        "users": auth.list_users(),
        "admins": auth_cfg.get("admins"),
        "auth_enabled": auth.enabled(auth_cfg),
        "current_user": getattr(request.state, "user", None),
        "is_admin": getattr(request.state, "is_admin", True),
        "ai": cfg.get("ai", {}),
        "ai_ready": ai_ocr.is_configured(cfg),
        "imap": cfg.get("imap", {}),
        # The resolved IMAP account (with SMTP fallback) — None if not usable.
        "imap_resolved": inbox.effective_cfg(cfg),
    })


@app.post("/settings/users/add")
async def settings_user_add(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    if not username:
        return RedirectResponse("/settings?error=Username is required.",
                                status_code=303)
    if len(password) < auth.MIN_PASSWORD_LEN:
        return RedirectResponse(
            f"/settings?error=Password must be at least "
            f"{auth.MIN_PASSWORD_LEN} characters.", status_code=303)
    make_admin = form.get("is_admin") == "on"
    auth.add_user(username, password,
                  secure_cookies=(request.url.scheme == "https"),
                  admin=make_admin)
    role = "administrator" if make_admin else "employee"
    return redirect_msg(
        "/settings",
        message=f"User '{username}' added as {role}. Staff login is enabled.")


@app.post("/settings/users/delete")
async def settings_user_delete(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    form = await request.form()
    username = (form.get("username") or "").strip()
    if username == getattr(request.state, "user", None):
        return RedirectResponse(
            "/settings?error=You can't delete the account you're signed in with.",
            status_code=303)
    if auth.delete_user(username):
        return redirect_msg("/settings",
                            message=f"User '{username}' removed.")
    return RedirectResponse(
        "/settings?error=Could not remove that user (the last user can't be removed).",
        status_code=303)


@app.post("/settings/account/password")
async def settings_change_password(request: Request):
    form = await request.form()
    user = getattr(request.state, "user", None)
    cfg = load_config()
    stored = cfg.get("auth", {}).get("users", {}).get(user or "")
    if not user or not stored:
        return RedirectResponse(
            "/settings?error=Sign in before changing a password.", status_code=303)
    current = form.get("current_password") or ""
    new = form.get("new_password") or ""
    confirm = form.get("confirm_password") or ""
    if not auth.verify_password(current, stored):
        return RedirectResponse(
            "/settings?error=Your current password is incorrect.", status_code=303)
    if len(new) < auth.MIN_PASSWORD_LEN:
        return RedirectResponse(
            f"/settings?error=New password must be at least "
            f"{auth.MIN_PASSWORD_LEN} characters.", status_code=303)
    if new != confirm:
        return RedirectResponse(
            "/settings?error=The new passwords don't match.", status_code=303)
    auth.change_password(user, new)
    return RedirectResponse("/settings?message=Your password has been changed.",
                            status_code=303)


@app.post("/settings/company")
async def settings_company_save(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    form = await request.form()
    save_user_config({"company": {
        "niu": (form.get("niu") or "").strip().upper().replace(" ", ""),
        "legal_name": (form.get("legal_name") or "").strip(),
    }})
    return RedirectResponse(
        "/settings?message=Company identity saved — the compliance engine "
        "now verifies every invoice is billed to this NIU and legal name.",
        status_code=303)


@app.post("/settings/ai")
async def settings_ai_save(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    form = await request.form()
    cfg = load_config()
    api_key = (form.get("api_key") or "").strip()
    if form.get("clear_key") == "on":
        api_key_value = ""
    else:
        # Blank field keeps the stored key (so saving twice is harmless).
        api_key_value = api_key if api_key else cfg.get("ai", {}).get("api_key", "")
    new_cfg = save_user_config({"ai": {"api_key": api_key_value}})
    state = ("saved — AI invoice reading is ON"
             if new_cfg["ai"]["api_key"] else "cleared — AI reading is OFF")
    return RedirectResponse(f"/settings?message=Anthropic API key {state}.",
                            status_code=303)


@app.post("/settings/ai/test")
async def settings_ai_test(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    cfg = load_config()
    try:
        ai_ocr.test_key(cfg.get("ai"))
        return RedirectResponse(
            "/settings?message=✓ Anthropic API key works — AI invoice "
            "reading is ready.", status_code=303)
    except (ai_ocr.AiNotConfigured, ai_ocr.AiReadError) as exc:
        return redirect_msg("/settings", error=f"AI key test failed: {exc}")


@app.post("/settings/imap")
async def settings_imap_save(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    form = await request.form()
    cfg = load_config()
    password = form.get("password") or ""
    save_user_config({"imap": {
        "enabled": form.get("enabled") == "on",
        "host": (form.get("host") or "").strip(),
        "port": int(form.get("port") or 993),
        "username": (form.get("username") or "").strip(),
        # Blank password keeps the stored one (and falls back to SMTP).
        "password": password if password else cfg.get("imap", {}).get("password", ""),
        "mailbox": (form.get("mailbox") or "INBOX").strip(),
        "since_days": int(form.get("since_days") or 30),
    }})
    state = "ENABLED — vendor replies are auto-read" \
        if form.get("enabled") == "on" else "saved but disabled"
    return redirect_msg("/settings", message=f"Inbox auto-read {state}.")


@app.post("/settings/imap/test")
async def settings_imap_test(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    ok, msg = inbox.test_connection(load_config())
    if ok:
        return redirect_msg("/settings", message=f"✓ {msg}")
    return redirect_msg("/settings", error=f"IMAP test failed: {msg}")


@app.post("/settings/smtp")
async def app_settings_save(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return blocked
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
    blocked = _admin_block(request)
    if blocked:
        return blocked
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
        return redirect_msg(
            "/settings",
            message=f"✓ Test email sent to {to_addr} — SMTP works.")
    except Exception as exc:  # noqa: BLE001
        return redirect_msg(
            "/settings", error=f"Test failed: {type(exc).__name__}: {exc}")


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
