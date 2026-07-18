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
from urllib.parse import quote, urlencode, urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import (APP_VERSION, BASE_DIR, OUTPUT_DIR, UPLOAD_DIR, ensure_dirs,
                     load_config, save_user_config)
from .services import (ai_ocr, ar_master, auth, bank_statement,
                       charts, cheques, ctp_dashboard, ctp_rules, customers,
                       emailer, eno_allocation, holds, remittance_pdf,
                       remittance_store, turnstile, variance,
                       xlsx_report)
from .tools import account_stop, bank, bitcash, quickstmt
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

# Per-area access control: every /tools/<slug> path is gated by the user's
# access rights (set by an admin under Administration). The home Dashboard is
# an assignable area too (pseudo-slug "dashboard").
_TOOL_SLUGS = {t["slug"] for t in registry.TOOLS}
_DASHBOARD_AREA = {"slug": "dashboard", "name": "Dashboard (home overview)",
                   "category": "Home", "icon": "🏠"}
_ASSIGNABLE_AREAS = [_DASHBOARD_AREA] + registry.TOOLS
_ASSIGNABLE_SLUGS = ["dashboard"] + registry.all_slugs()


def _slug_for_path(path):
    """The tool slug a path belongs to, e.g. /tools/<slug>/... -> <slug>."""
    if not path.startswith("/tools/"):
        return None
    return (path[len("/tools/"):].split("/", 1)[0]) or None


def _access_denied(request, read_only):
    """A friendly 403 when a user lacks rights to an area."""
    if read_only:
        heading = "Read-only access"
        detail = ("You have read-only access to this area — you can view it but "
                  "not make changes. Ask an administrator for read & modify "
                  "rights if you need them.")
    else:
        heading = "No access to this area"
        detail = ("Your account doesn't have access to this area. An "
                  "administrator can grant it under Administration.")
    return templates.TemplateResponse("error.html", {
        "request": request, "cfg": load_config(), "icon": "🔒",
        "heading": heading, "detail": detail, "ref": ""}, status_code=403)


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
        limit = MAX_UPLOAD_BYTES
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
    # Per-area access rights drive the nav/landing (what a user may see) and
    # are enforced on every /tools/<slug> request (what a user may view/do).
    if request.state.is_admin:
        request.state.allowed = set(_TOOL_SLUGS)
    else:
        acc = auth.get_access(auth_cfg, user)
        request.state.allowed = {s for s, lvl in acc.items()
                                 if lvl in auth._ACCESS_LEVELS} & _TOOL_SLUGS
    request.state.dashboard_ok = (
        request.state.is_admin
        or auth.area_level(auth_cfg, user, "dashboard") != "none")
    slug = _slug_for_path(request.url.path)
    if slug in _TOOL_SLUGS:
        level = auth.area_level(auth_cfg, user, slug)
        if level == "none":
            return _access_denied(request, read_only=False)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") \
                and level != auth.ACCESS_MODIFY:
            return _access_denied(request, read_only=True)
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


def _login_page(request, cfg, nxt, error="", status_code=200):
    t = cfg.get("turnstile", {})
    return templates.TemplateResponse("login.html", {
        "request": request, "cfg": cfg, "next": nxt, "error": error,
        "turnstile_active": turnstile.active(cfg),
        "turnstile_site_key": t.get("site_key", ""),
    }, status_code=status_code)


def _safe_next(nxt):
    """A safe local redirect target. Reject absolute and protocol-relative URLs
    ("//evil.com", "/\\evil.com") that browsers would follow off-site after
    login — an open-redirect / phishing vector."""
    nxt = nxt or "/"
    if not nxt.startswith("/") or nxt.startswith("//") or nxt.startswith("/\\"):
        return "/"
    return nxt


def _finish_login(auth_cfg, username, nxt):
    """Issue the full 12h session cookie."""
    nxt = _safe_next(nxt)
    nxt = nxt + ("&" if "?" in nxt else "?") + "welcome=1"
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(
        auth.COOKIE, auth.issue(auth_cfg.get("secret_key", ""), username),
        max_age=auth.MAX_AGE, httponly=True, samesite="lax",
        secure=bool(auth_cfg.get("secure_cookies")))
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: str = ""):
    cfg = load_config()
    if not auth.enabled(cfg.get("auth", {})):
        return RedirectResponse("/", status_code=303)
    return _login_page(request, cfg, _safe_next(next), error=error)


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    nxt = _safe_next(form.get("next") or "/")
    cfg = load_config()
    auth_cfg = cfg.get("auth", {})

    # Cloudflare Turnstile (when configured): reject a failed bot challenge.
    if turnstile.active(cfg):
        ip = request.client.host if request.client else ""
        if not turnstile.verify(cfg["turnstile"]["secret_key"],
                                form.get("cf-turnstile-response") or "", ip):
            return _login_page(request, cfg, nxt, status_code=400,
                               error="Please complete the human-verification "
                                     "challenge and try again.")

    # Brute-force throttle: a locked username can't even attempt a login.
    wait = auth.locked_for(username)
    if wait:
        return _login_page(request, cfg, nxt, status_code=429,
                           error=f"Too many failed attempts — this account is "
                                 f"locked for another {max(1, wait // 60)} "
                                 f"minute(s).")

    stored = auth_cfg.get("users", {}).get(username)
    if stored and auth.verify_password(password, stored):
        auth.clear_failures(username)
        return _finish_login(auth_cfg, username, nxt)

    client_ip = request.client.host if request.client else ""
    auth.record_failure(username, ip=client_ip)
    await asyncio.sleep(auth.FAIL_DELAY)   # slow down password guessing
    return _login_page(request, cfg, nxt, status_code=401,
                       error="Incorrect username or password.")


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
             "batch_at": "", "total_ok": None, "unpresented_cheques": None,
             "activity": []}
    try:
        stats["unpresented_cheques"] = cheques.register_stats()["unpresented"]
    except Exception:  # noqa: BLE001
        pass
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
    stats["activity"].sort(key=lambda x: x[0], reverse=True)
    stats["activity"] = stats["activity"][:6]
    return stats


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, welcome: str = ""):
    cfg = load_config()
    # The home Dashboard is itself an access-controlled area. A user without it
    # lands on their first accessible tool (or an access-denied if they have none).
    if not getattr(request.state, "dashboard_ok", True):
        allowed = getattr(request.state, "allowed", set()) or set()
        first = next((t["slug"] for t in registry.TOOLS if t["slug"] in allowed), None)
        if first:
            return RedirectResponse(f"/tools/{first}", status_code=303)
        return _access_denied(request, read_only=False)
    # Show only the areas this user may access (admins see everything).
    allowed = getattr(request.state, "allowed", None)
    groups = registry.grouped()
    if allowed is not None:
        groups = {cat: [t for t in tools if t["slug"] in allowed]
                  for cat, tools in groups.items()}
        groups = {cat: tools for cat, tools in groups.items() if tools}
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "cfg": cfg,
        "groups": groups,
        "s": _home_stats(),
        "trend_ar": _dash_trend_ar(),
        "trend_bitcash": _dash_trend_bitcash(),
        "welcome": bool(welcome) and getattr(request.state, "user", None),
    })


def _dash_trend_ar():
    """Daily % of receivables over 60 / over 90 days, from the CtP snapshots."""
    try:
        hist = ctp.stats_history()
    except Exception:  # noqa: BLE001
        return ""
    if not hist:
        return ""
    return charts.line_svg(
        [d[5:] for d, _v in hist],
        [{"name": "> 60 days %", "color": "#f59e0b",
          "values": [v.get("pct_over_60") for _d, v in hist]},
         {"name": "> 90 days %", "color": "#dc2626",
          "values": [v.get("pct_over_90") for _d, v in hist]}])


def _dash_trend_bitcash():
    """Daily open BIT & Cash AR items, from the BIT & Cash AR uploads."""
    try:
        hist = bitcash.status()["history"]
    except Exception:  # noqa: BLE001
        return ""
    if not hist:
        return ""
    return charts.line_svg(
        [d[5:] for d, _v in hist],
        [{"name": "Open BIT items", "color": "#3b82f6",
          "values": [v.get("bit_open") for _d, v in hist]},
         {"name": "Open Cash AR items", "color": "#16a34a",
          "values": [v.get("cash_open") for _d, v in hist]}])


# --------------------------------------------------------------------------- #
# Orange Cameroun tool
# --------------------------------------------------------------------------- #
def _orange_ar_enrich(rows):
    """Attach the AR-ledger view to Orange correspondants: the account and
    balance per the latest AR analysis — joined on the AR account number
    first, else matched on the remembered customer name."""
    result, _tok = _latest_ctp_result()
    if not result:
        for row in rows:
            row["ar"] = None
        return rows, None
    custs = result.get("customers", [])

    def _norm(v):
        s = str(v or "").strip()
        return s[:-2] if s.endswith(".0") else s

    by_acct = {_norm(c.get("key")): c for c in custs}
    for row in rows:
        hit, via = by_acct.get(_norm(row.get("account"))), "account"
        if hit is None and (row.get("name") or "").strip():
            m = bank_statement.best_customer_for(row["name"], custs)
            hit, via = (m[0] if m else None), "name"
        row["ar"] = None if hit is None else {
            "customer": hit.get("customer", ""),
            "key": str(hit.get("key", "")),
            "total_ar": hit.get("total_ar", 0) or 0,
            "overdue": hit.get("overdue", 0) or 0, "via": via}
    return rows, {"as_of": str(result.get("as_of", "")),
                  "source": result.get("source", "")}


@app.get("/tools/orange-cameroun", response_class=HTMLResponse)
def orange_home(request: Request, error: str = ""):
    cfg = load_config()
    return templates.TemplateResponse("orange/upload.html", {
        "request": request,
        "cfg": cfg,
        "tool": registry.by_slug("orange-cameroun"),
        "error": error,
        "saved_count": len(customers.all_names(orange.TOOL_SLUG)),
        "uploads": orange.list_uploads(),
    })


@app.post("/tools/orange-cameroun/upload", response_class=HTMLResponse)
async def orange_upload(request: Request, file: UploadFile = File(...)):
    cfg = load_config()
    ext = Path(file.filename or "").suffix.lower()

    def _err(message, code=400):
        return templates.TemplateResponse("orange/upload.html", {
            "request": request, "cfg": cfg,
            "tool": registry.by_slug("orange-cameroun"), "error": message,
            "saved_count": len(customers.all_names(orange.TOOL_SLUG)),
            "uploads": orange.list_uploads(),
        }, status_code=code)

    if ext not in ALLOWED_EXT:
        return _err(f"Unsupported file type '{ext or 'unknown'}'. Please upload "
                    "the Orange Money statement (.xlsx or .xls).")

    token = uuid.uuid4().hex
    dest = UPLOAD_DIR / f"{token}{ext}"
    dest.write_bytes(await file.read())

    try:
        model = orange.parse_for_review(dest)
    except Exception as exc:  # noqa: BLE001 - surface any parse error to the user
        return _err(f"Could not read that file: {exc}")

    if not model["collections"]:
        return _err("No successful collections (credits) were found. Make sure "
                    "this is the Orange Money monthly ‘Relevé de vos opérations’ "
                    "export for the merchant account.")

    # AR-ledger columns + persist this upload so it stays consultable.
    _rows, ar_meta = _orange_ar_enrich(model["correspondants"])
    try:
        htoken = orange.save_upload_summary(
            model, source=file.filename or "orange_statement.xls")
    except Exception:  # noqa: BLE001 — history must never block a review
        htoken = ""

    return templates.TemplateResponse("orange/review.html", {
        "request": request, "cfg": cfg,
        "tool": registry.by_slug("orange-cameroun"),
        "model": model, "token": token,
        "original": file.filename or "orange_statement.xls",
        "htoken": htoken, "ar_meta": ar_meta,
    })


@app.post("/tools/orange-cameroun/generate", response_class=HTMLResponse)
async def orange_generate(request: Request):
    cfg = load_config()
    form = await request.form()
    token = form.get("token", "")

    upload_path = _resolve_upload(token)
    if upload_path is None:
        return HTMLResponse(
            "<script>alert('Your upload expired \\u2014 please upload the file "
            "again.');history.back();</script>", status_code=400)

    # Per-correspondant identities the user confirmed/added on the review page.
    identities = {}
    for key, value in form.multi_items():
        if key.startswith("name_"):
            identities.setdefault(key[5:], {})["name"] = value
        elif key.startswith("acct_"):
            identities.setdefault(key[5:], {})["account"] = value

    try:
        out_zip = OUTPUT_DIR / f"orange_{token[:12]}.zip"
        result = orange.build_zip(upload_path, identities, out_zip)
    except Exception as exc:  # noqa: BLE001
        return HTMLResponse(
            f"<script>alert({('Could not generate receipts: ' + str(exc))!r});"
            "history.back();</script>", status_code=500)
    try:
        orange.mark_generated(form.get("htoken", ""), identities)
    except Exception:  # noqa: BLE001 — history must never block the ZIP
        pass

    return templates.TemplateResponse("orange/done.html", {
        "request": request, "cfg": cfg,
        "tool": registry.by_slug("orange-cameroun"),
        "result": result, "zip_file": out_zip.name,
    })


@app.get("/tools/orange-cameroun/history/{htoken}", response_class=HTMLResponse)
def orange_history(request: Request, htoken: str):
    u = orange.load_upload(htoken)
    if u is None:
        return RedirectResponse(
            "/tools/orange-cameroun?error=That upload was not found.",
            status_code=303)
    rows, ar_meta = _orange_ar_enrich(u.get("correspondants", []))
    return templates.TemplateResponse("orange/history.html", {
        "request": request, "cfg": load_config(),
        "tool": registry.by_slug("orange-cameroun"),
        "u": u, "rows": rows, "ar_meta": ar_meta,
    })


@app.get("/tools/orange-cameroun/customers", response_class=HTMLResponse)
def orange_customers(request: Request):
    cfg = load_config()
    return templates.TemplateResponse("orange/customers.html", {
        "request": request, "cfg": cfg,
        "tool": registry.by_slug("orange-cameroun"),
        "names": customers.all_names(orange.TOOL_SLUG),
        "accounts": customers.all_names(orange.ACCT_SLUG),
    })


@app.post("/tools/orange-cameroun/customers/delete")
async def orange_customers_delete(request: Request):
    form = await request.form()
    key = form.get("key") or ""
    customers.delete_name(orange.TOOL_SLUG, key)
    customers.delete_name(orange.ACCT_SLUG, key)
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


def _held_chart():
    """Daily 'accounts on credit hold' line — from the CtP daily snapshots."""
    hist = ctp.stats_history()
    if not hist:
        return ""
    return charts.line_svg(
        [d[5:] for d, _v in hist],
        [{"name": "Accounts on credit hold", "color": "#dc2626",
          "values": [v.get("held") for _d, v in hist]}])


@app.get("/tools/ongoing-ctp-monitoring", response_class=HTMLResponse)
def ongoing_ctp(request: Request, error: str = "", message: str = ""):
    return templates.TemplateResponse(
        "ongoing/upload.html",
        _ongoing_ctx(request, error=error, message=message,
                     recent=ctp.list_results(), master=_master_summary(),
                     held_chart=_held_chart()))


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

    # Optional AR trial balance — drives the FIRST check (tx total vs TB) AND
    # is the credit-hold source: when it carries a per-customer "Account
    # stop/open" (X = on hold) and/or "Critical" column, those flags drive the
    # hold register + critical analysis (they win over the master file).
    tb = None
    tb_holds = None
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
            parsed_tb = ar_master.parse_master(tb_dest)
            if (parsed_tb["stats"].get("has_hold_column")
                    or parsed_tb["stats"].get("has_critical_column")):
                tb_holds = parsed_tb
        except Exception:  # noqa: BLE001 — TB hold extraction is best-effort
            tb_holds = None

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

    # Hold/critical come from the AR trial balance when present (it wins), else
    # the master file. Merge so both customer data sets are available.
    parts = [p for p in (ctp.load_master(), tb_holds)
             if p and p.get("customers")]
    effective_master = (ar_master.combine(parts) if len(parts) > 1
                        else (parts[0] if parts else None))
    try:
        result = ctp.analyze(dest, as_of=as_of_date, default_rank=rank,
                             master=effective_master, tb=tb)
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse("ongoing/upload.html", _ongoing_ctx(
            request, error=f"Could not analyse that file: {exc}",
            recent=ctp.list_results(), master=_master_summary()), status_code=400)

    result["source"] = file.filename or "ar_breakdown.xlsx"
    result["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    ctp.save_result(token[:12], result)
    try:
        ctp.record_daily_stats(result)
    except Exception:  # noqa: BLE001 — stats must never fail an upload
        pass
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
    # Per-customer status keyed by account, so the invoice table can show each
    # line's customer credit-stop / on-hold / critical standing.
    cust_status = {c["key"]: {"k": c.get("credit_stop_key", "ok"),
                              "l": c.get("credit_stop", ""),
                              "held": c.get("currently_held", False),
                              "critical": c.get("critical", False)}
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


@app.post("/tools/ongoing-ctp-monitoring/results/{token}/refresh")
async def ongoing_refresh(token: str):
    # Re-apply the current master file + credit-hold register to this stored
    # analysis (no need to re-upload the transaction file).
    result = ctp.load_result(token)
    if result is None:
        return RedirectResponse(
            "/tools/ongoing-ctp-monitoring?error=That analysis was not found.",
            status_code=303)
    ctp.reapply_master(result)
    ctp.hold_compare(result["customers"])
    result["refreshed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    ctp.save_result(token, result)
    return RedirectResponse(
        f"/tools/ongoing-ctp-monitoring/results/{token}/dashboard",
        status_code=303)


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
    pseudo = {"lines": [{"description": r["text"], "amount": r["amount"],
                         "credit": r["amount"], "debit": 0.0,
                         "date": r["date"], "bank": r["bank"]}
                        for r in bank_rows]} if bank_rows else None
    # Only customers ON HOLD can be "held customers paying" — match just those
    # (not all customers) so a large AR x many bank lines can't stall the page.
    held_custs = [c for c in result["customers"]
                  if c.get("currently_held") and c["total_ar"] > 0]
    held_paying = []
    if pseudo and held_custs:
        held_paying = bank_statement.match_customers(held_custs, pseudo)
    # Payments identification: every open-AR customer whose name closely
    # matches a payer on the bank statements OR a client name on the cheque
    # register — with the amount seen, the source, the bank and the date.
    open_custs = [c for c in result["customers"] if c["total_ar"] > 0]
    pay_id = []
    if pseudo and open_custs:
        for m in bank_statement.match_customers(open_custs, pseudo):
            amt = m["bank_amount"]
            if not isinstance(amt, (int, float)):
                amt = bank_statement.parse_amount(amt)
            pay_id.append({
                "customer": m["customer"], "key": m["key"],
                "payment_term": next(
                    (c.get("payment_term", "") for c in open_custs
                     if c["key"] == m["key"]), ""),
                "total_ar": m["total_ar"], "amount": amt,
                "source": "Bank", "bank": m.get("bank", ""),
                "date": m["bank_date"], "score": m["score"]})
    if open_custs:
        for r in cheques.register_rows():
            if r["status"] != "done" or not r.get("customer") \
                    or r.get("duplicate_of"):
                continue
            best = bank_statement.best_customer_for(r["customer"], open_custs)
            if not best:
                continue
            c, score = best
            cl = r.get("cleared") or {}
            pay_id.append({
                "customer": c["customer"], "key": c["key"],
                "payment_term": c.get("payment_term", ""),
                "total_ar": c["total_ar"],
                "amount": r.get("amount") or cl.get("amount") or 0,
                "source": "Cheque",
                "bank": cl.get("bank") or r.get("issuing_bank", ""),
                "date": cl.get("date") or r.get("cheque_date", ""),
                "score": score})
    pay_id.sort(key=lambda x: -(x["amount"] or 0))
    return templates.TemplateResponse("ongoing/dashboard.html", _ongoing_ctx(
        request, result=result, dash=dash, dash_me=dash_me, month_end=me,
        hold_cmp=hold_cmp, token=token, threshold=thr,
        held_paying=held_paying, pay_id=pay_id,
        bank_available=bool(bank_rows),
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
            request, error=f"Document reading failed: {exc}"))

    # Stage the PDF so Generate can attach it without a re-upload.
    pdf_token = uuid.uuid4().hex[:12]
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    (UPLOAD_DIR / f"enoinv_{pdf_token}.pdf").write_bytes(pdf_bytes)

    # Plain decimal strings (never scientific notation) for the form inputs.
    prefill = {cid: f"{extraction['lines'][cid]:.2f}".rstrip("0").rstrip(".")
               for cid in refs if cid in extraction["lines"]}
    extra_refs = [r for r in extraction["lines"] if r not in refs]
    matched = len(prefill)
    note = (f"Invoice read — {matched}/{len(refs)} customer "
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
# Cheque Payment Processing
# --------------------------------------------------------------------------- #
_CHEQUE_TOOL = registry.by_slug("cheque-processing")
# Per-file cap (the middleware already bounds the whole request to 50 MB).
_MAX_CHEQUE_FILE_BYTES = 15 * 1024 * 1024


def _register_with_ar(register):
    """Attach the AR trial-balance view to each read cheque: the customer,
    account number, account balance and overdue balance from the latest CtP
    analysis, matched on the client name read off the cheque (same matcher
    and 0.86 bar as the payments identification). row["ar"] = None when no
    confident match."""
    result, _tok = _latest_ctp_result()
    custs = (result or {}).get("customers", [])
    memo = {}
    for row in register:
        row["ar"] = None
        name = (row.get("customer") or "").strip()
        if not custs or row.get("status") != "done" or not name \
                or row.get("duplicate_of"):
            continue
        mkey = name.lower()
        if mkey not in memo:
            hit = bank_statement.best_customer_for(name, custs)
            memo[mkey] = None if not hit else {
                "customer": hit[0].get("customer", ""),
                "key": str(hit[0].get("key", "")),
                "total_ar": hit[0].get("total_ar", 0) or 0,
                "overdue": hit[0].get("overdue", 0) or 0,
                "score": round(hit[1], 2)}
        row["ar"] = memo[mkey]
    ar_meta = ({"as_of": str(result.get("as_of", "")),
                "source": result.get("source", "")} if result else None)
    return register, ar_meta


def _cheque_ctx(request, **extra):
    cfg = load_config()
    _rows, bank_banks = bank.all_statement_lines()
    register, ar_meta = _register_with_ar(cheques.register_rows())
    # Today's headline numbers + the daily series for the graph. Recording on
    # every render keeps the day's snapshot tracking the current truth.
    stats = cheques.register_stats(register)
    cheques.record_daily_stats(stats)
    history = cheques.stats_history()
    stats_chart = charts.line_svg(
        [d[5:] for d, _v in history],            # MM-DD labels
        [{"name": "Cheques on file", "color": "#3b82f6",
          "values": [v["total"] for _d, v in history]},
         {"name": "Unpresented", "color": "#dc2626",
          "values": [v["unpresented"] for _d, v in history]}])
    ctx = {"request": request, "cfg": cfg, "tool": _CHEQUE_TOOL,
           "message": "", "error": "", "recent": cheques.list_recent(),
           "ai_ready": ai_ocr.is_configured(cfg),
           "max_cheques": cheques.MAX_CHEQUES,
           "register": register, "stats": stats, "stats_chart": stats_chart,
           "ar_meta": ar_meta,
           "can_treat": _can_treat(request),
           # username -> display alias (set by an admin) for "Uploaded by".
           "aliases": auth.alias_map(cfg.get("auth", {})),
           "bank_lines": len(_rows), "bank_banks": bank_banks}
    ctx.update(extra)
    return ctx


@app.get("/tools/cheque-processing", response_class=HTMLResponse)
def cheque_home(request: Request, message: str = "", error: str = ""):
    return templates.TemplateResponse(
        "cheques/index.html", _cheque_ctx(request, message=message, error=error))


@app.post("/tools/cheque-processing/register/refresh")
async def cheque_register_refresh(request: Request):
    # Re-check EVERY cheque in the register against the current bank statement
    # lines (no AI re-read) — run after new statements are uploaded.
    bank_rows, bank_summary = bank.all_statement_lines()
    n = cheques.refresh_all(bank_rows, bank_summary)
    return redirect_msg("/tools/cheque-processing",
                        message=f"Register refreshed — {n} upload(s) re-matched "
                                f"against {len(bank_rows)} bank statement line(s).")


@app.post("/tools/cheque-processing/upload")
async def cheque_upload(request: Request):
    cfg = load_config()
    if not ai_ocr.is_configured(cfg):
        return templates.TemplateResponse("cheques/index.html", _cheque_ctx(
            request, error="Document reading is not configured — paste your "
            "document-reading key under Settings → Document reading first."))
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

    cheques.create_batch(token, cheque_meta, bank_rows, bank_summary,
                         uploaded_by=getattr(request.state, "user", None) or "")
    cheques.run_batch_async(token)
    # Straight back to the register — it self-refreshes while cheques are read.
    return redirect_msg("/tools/cheque-processing",
                        message=f"{len(cheque_meta)} cheque(s) uploaded — "
                                "reading them into the register now.")


@app.post("/tools/cheque-processing/register/resume")
async def cheque_register_resume(request: Request):
    # Restart the background scan reader for any upload stuck with pending
    # cheques (stalled read / service restart mid-batch).
    n = cheques.resume_pending()
    if not n:
        return redirect_msg("/tools/cheque-processing",
                            message="Nothing to resume — no cheque is waiting "
                                    "to be read.")
    return redirect_msg("/tools/cheque-processing",
                        message=f"Resumed reading for {n} upload(s) — the "
                                "register refreshes as each cheque completes.")


@app.get("/tools/cheque-processing/register/export")
def cheque_register_export():
    out = OUTPUT_DIR / "Electronic_cheque_register.xlsx"
    rows, _ar_meta = _register_with_ar(cheques.register_rows())
    cheques.build_register_excel(out, rows)
    return FileResponse(out, filename="Electronic_cheque_register.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet")


# --------------------------------------------------------------------------- #
# Account Stop — payments traced for accounts on credit stop
# --------------------------------------------------------------------------- #
_ACCT_STOP_TOOL = registry.by_slug("account-stop")


@app.get("/tools/account-stop", response_class=HTMLResponse)
def account_stop_home(request: Request):
    view = account_stop.build_view()
    return templates.TemplateResponse("account_stop/index.html", {
        "request": request, "cfg": load_config(), "tool": _ACCT_STOP_TOOL,
        **view,
    })


def _can_treat(request):
    """Only the finance profile (CtP-Portal modify access) or an administrator
    may mark a cheque as treated in the accounting records — the cheque-register
    profile (cheque-processing access only) may not."""
    if getattr(request.state, "is_admin", True):
        return True
    auth_cfg = load_config().get("auth", {})
    return auth.can_modify(auth_cfg, getattr(request.state, "user", None),
                           "ongoing-ctp-monitoring")


@app.post("/tools/cheque-processing/register/treat")
async def cheque_register_treat(request: Request):
    if not _can_treat(request):
        return redirect_msg("/tools/cheque-processing",
                            error="Only the finance profile can mark a cheque "
                                  "as treated in the accounting records.")
    form = await request.form()
    token = (form.get("batch") or "").strip()
    try:
        idx = int(form.get("idx") or "")
    except ValueError:
        idx = -1
    on = form.get("action") != "unmark"
    user = getattr(request.state, "user", None) or "finance"
    if not re.fullmatch(r"[0-9a-f]+", token) or \
            not cheques.set_treated(token, idx, by=user, on=on):
        return redirect_msg("/tools/cheque-processing",
                            error="That cheque was not found on the register.")
    return redirect_msg("/tools/cheque-processing",
                        message="Cheque marked as treated in the accounting "
                                "records." if on else "Treated mark removed.")


# --------------------------------------------------------------------------- #
# Bank Statements — collection activity
# --------------------------------------------------------------------------- #
_BANK_TOOL = registry.by_slug("bank-statements")


def _bank_home(request, error="", message="", status_code=200):
    cfg = load_config()
    banks = cfg.get("banks", []) or []
    # One card per configured bank: every month's statement on file (prior
    # months are kept side by side; same month re-upload overrides).
    slots, slot_tokens = [], set()
    for n in banks:
        monthly = bank.slot_reports(n)
        slot_tokens.update(r.get("token", "") for r in monthly)
        slots.append({"name": n, "months": monthly})
    # Reports not tied to any configured bank (legacy / renamed banks) —
    # surfaced so they can be cleared.
    legacy = [r for r in bank.list_reports(limit=50)
              if r["token"] not in slot_tokens]
    return templates.TemplateResponse("bank/upload.html", {
        "request": request, "cfg": cfg, "tool": _BANK_TOOL,
        "error": error, "message": message,
        "slots": slots, "legacy": legacy,
        "this_month": datetime.now().strftime("%Y-%m"),
        "has_ar": bool(ctp.list_results(limit=1)),
        "max_files": bank.MAX_FILES,
    }, status_code=status_code)


@app.get("/tools/bank-statements", response_class=HTMLResponse)
def bank_home(request: Request, error: str = "", message: str = ""):
    return _bank_home(request, error=error, message=message)


@app.post("/tools/bank-statements/upload")
async def bank_upload(request: Request, bank_name: str = Form("", alias="bank"),
                      form_period: str = Form("", alias="period"),
                      file: list[UploadFile] = File(...)):
    cfg = load_config()
    banks = cfg.get("banks", []) or []
    name = (bank_name or "").strip()
    if not banks:
        return _bank_home(request, error="No banks configured yet — add your "
                          "banks in Settings → Bank statements first.",
                          status_code=400)
    if name not in banks:
        return _bank_home(request, error="Choose one of your configured banks "
                          "for this statement.", status_code=400)
    files = [f for f in (file or []) if f and f.filename]
    if not files:
        return _bank_home(request, error="No statement received.", status_code=400)
    if len(files) > bank.MAX_FILES:
        return _bank_home(request, error=f"Too many files ({len(files)}) — "
                          f"upload a maximum of {bank.MAX_FILES} files at a time.",
                          status_code=400)
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
    ai_cfg = cfg.get("ai")
    ai_key = bool((ai_cfg or {}).get("api_key", "").strip())
    # Statement month (YYYY-MM): prior months are kept side by side and stay
    # searchable; re-uploading the SAME bank+month overrides that statement.
    period = (form_period or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        period = datetime.now().strftime("%Y-%m")
    token = bank.bank_token(name, period)
    # AI-read PDF/scanned statements on a BACKGROUND thread — that call can
    # exceed the reverse-proxy timeout (502). Spreadsheets read instantly.
    needs_ai = ai_key and any(Path(n).suffix.lower() in bank._AI_EXT
                              for _p, n in stored)
    if needs_ai:
        bank.start_report(stored, ai_cfg=ai_cfg, token=token, bank_slot=name,
                          period=period)
        return RedirectResponse(
            f"/tools/bank-statements/results/{token}", status_code=303)
    try:
        report = bank.build_report(stored, ai_cfg=ai_cfg, token=token,
                                   bank_slot=name, period=period)
    except Exception as exc:  # noqa: BLE001
        return _bank_home(request, error=f"Could not read the statement(s): {exc}",
                          status_code=400)
    bank.save_report(report)
    return RedirectResponse(
        f"/tools/bank-statements/results/{token}", status_code=303)


@app.post("/tools/bank-statements/results/{token}/refresh")
async def bank_refresh(token: str):
    # Re-link this statement to the latest AR analysis (no re-upload needed).
    if bank.refresh_report(token) is None:
        return RedirectResponse(
            "/tools/bank-statements?error=Could not refresh that statement.",
            status_code=303)
    return RedirectResponse(
        f"/tools/bank-statements/results/{token}", status_code=303)


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
# Quick Account Statement — one-click customer statement from the latest CtP
# --------------------------------------------------------------------------- #
_QSTMT_TOOL = registry.by_slug("quick-statement")


def _latest_ctp_result():
    recent = ctp.list_results(limit=1)
    return (ctp.load_result(recent[0]["token"]), recent[0]["token"]) \
        if recent else (None, None)


@app.get("/tools/quick-statement", response_class=HTMLResponse)
def quick_statement_home(request: Request, message: str = "", error: str = "",
                         xlsx: str = ""):
    result, _tok = _latest_ctp_result()
    return templates.TemplateResponse("quickstmt/index.html", {
        "request": request, "cfg": load_config(), "tool": _QSTMT_TOOL,
        "message": message, "error": error,
        "customers": quickstmt.picker_entries(result),
        "as_of": (result or {}).get("as_of", ""),
        "source": (result or {}).get("source", ""),
        "xlsx": Path(xlsx).name if xlsx else "",
    })


@app.get("/tools/quick-statement/generate")
def quick_statement_generate(request: Request, key: str = ""):
    result, _tok = _latest_ctp_result()
    if not result:
        return RedirectResponse("/tools/quick-statement?error=No CtP analysis "
                                "on file yet — run one first.", status_code=303)
    data = quickstmt.statement_data(result, (key or "").strip())
    if data is None:
        return RedirectResponse("/tools/quick-statement?error=Choose a "
                                "customer from the list.", status_code=303)
    # Unguessable suffix — /download is filename-keyed.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", data["customer"])[:28]
    stem = f"Statement_{safe}_{datetime.now().strftime('%Y%m%d')}_" \
           f"{uuid.uuid4().hex[:8]}"
    xlsx_path = OUTPUT_DIR / f"{stem}.xlsx"
    quickstmt.build_statement_xlsx(xlsx_path, data)
    note = (f" ({len(data['accounts'])} accounts combined — same customer "
            "name)") if data["combined"] else ""
    return RedirectResponse(
        "/tools/quick-statement?" + urlencode({
            "message": f"Statement ready for {data['customer']}{note} — "
                       f"{len(data['rows'])} open item(s), total "
                       f"{data['total']:,.0f} {data['currency']}.",
            "xlsx": xlsx_path.name}),
        status_code=303)


# --------------------------------------------------------------------------- #
# BIT & Cash AR — daily open-items tracking
# --------------------------------------------------------------------------- #
_BITCASH_TOOL = registry.by_slug("bit-cash-ar")


def _file_age_days(uploaded):
    """Days since an upload timestamp ('%Y-%m-%d %H:%M'), or None."""
    try:
        return (datetime.now()
                - datetime.strptime(uploaded, "%Y-%m-%d %H:%M")).days
    except (TypeError, ValueError):
        return None


def _bitcash_home(request, error="", message="", status_code=200,
                  journal="", jname="", pack=""):
    st = bitcash.status()
    # Age of the files on record, so stale BIT / Cash AR files stand out.
    for kind in ("bit", "cash"):
        cur = st["current"].get(kind)
        if cur:
            cur["age_days"] = _file_age_days(cur.get("uploaded", ""))
    history = st["history"]
    chart = charts.line_svg(
        [d[5:] for d, _v in history],
        [{"name": "Open BIT items", "color": "#3b82f6",
          "values": [v.get("bit_open") for _d, v in history]},
         {"name": "Open Cash AR items", "color": "#16a34a",
          "values": [v.get("cash_open") for _d, v in history]}])
    return templates.TemplateResponse("bitcash/index.html", {
        "request": request, "cfg": load_config(), "tool": _BITCASH_TOOL,
        "error": error, "message": message,
        "current": st["current"], "chart": chart,
        "processing": st.get("processing"),
        "processing_error": st.get("processing_error"),
        "recons": bitcash.list_recons(),
        "journal": Path(journal).name if journal else "",
        "jname": Path(jname).name if jname else "",
        "pack": Path(pack).name if pack else "",
        "ai_ready": ai_ocr.is_configured(load_config()),
    }, status_code=status_code)


@app.get("/tools/bit-cash-ar", response_class=HTMLResponse)
def bitcash_home(request: Request, message: str = "", error: str = "",
                 journal: str = "", jname: str = "", pack: str = ""):
    return _bitcash_home(request, error=error, message=message,
                         journal=journal, jname=jname, pack=pack)


@app.post("/tools/bit-cash-ar/upload")
async def bitcash_upload(request: Request,
                         bit_file: UploadFile = File(None),
                         cash_file: UploadFile = File(None)):
    uploads = [("bit", bit_file), ("cash", cash_file)]
    real = [(k, f) for k, f in uploads if f and f.filename]
    if not real:
        return _bitcash_home(request, error="Choose the BIT file, the Cash AR "
                             "file, or both.", status_code=400)
    jobs = []
    for kind, f in real:
        ext = Path(f.filename or "").suffix.lower()
        if ext not in ALLOWED_EXT:
            return _bitcash_home(request, error=f"{f.filename}: upload Excel "
                                 "files (.xlsx/.xls).", status_code=400)
        dest = UPLOAD_DIR / f"bitcash_{kind}_{uuid.uuid4().hex[:8]}{ext}"
        dest.write_bytes(await f.read())
        jobs.append((kind, dest, f.filename or ""))
    # Big extracts take longer than a web request may — parse in the
    # background and let the page refresh itself until the counts land.
    bitcash.process_uploads_async(jobs)
    names = " + ".join(bitcash.KINDS[k] for k, _p, _s in jobs)
    return redirect_msg("/tools/bit-cash-ar",
                        message=f"{names} received — counting the open items "
                                "now; this page refreshes itself until the "
                                "new position is on record.")


_STMT_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif",
             ".xlsx", ".xlsm", ".xls"}


@app.post("/tools/bit-cash-ar/recon/upload")
async def bitcash_recon_upload(request: Request,
                               statement: UploadFile = File(...)):
    store = bitcash.rows_store()
    if not store["bit"] or not store["cash"]:
        return _bitcash_home(request, error="Upload the BIT and Cash AR files "
                             "first — the statement is reconciled against "
                             "them.", status_code=400)
    ext = Path(statement.filename or "").suffix.lower()
    if ext not in _STMT_EXT:
        return _bitcash_home(request, error=f"Unsupported statement type "
                             f"'{ext or 'unknown'}' — upload a PDF, an image "
                             "or an Excel file.", status_code=400)
    if ext in (".xlsx", ".xlsm", ".xls"):
        media = "excel"
    else:
        media = ai_ocr.media_type_for(statement.filename) or "application/pdf"
        if not ai_ocr.is_configured(load_config()):
            return _bitcash_home(request, error="Document reading is not configured "
                                 "— paste the document-reading key under Settings "
                                 "→ Document reading to read scanned "
                                 "statements.", status_code=400)
    dest = UPLOAD_DIR / f"stmt_{uuid.uuid4().hex[:8]}{ext}"
    dest.write_bytes(await statement.read())
    token = bitcash.create_recon_async(
        dest, media, statement.filename or "statement",
        getattr(request.state, "user", None) or "")
    return RedirectResponse(f"/tools/bit-cash-ar/recon/{token}",
                            status_code=303)


@app.get("/tools/bit-cash-ar/recon/{token}", response_class=HTMLResponse)
def bitcash_recon(request: Request, token: str, q: str = "",
                  message: str = "", error: str = ""):
    rec = bitcash.load_recon(token)
    if rec is None:
        return _bitcash_home(request, error="That reconciliation was not "
                             "found.", status_code=404)
    view = bitcash.recon_view(rec) if rec.get("status") in ("open", "approved") \
        else None
    return templates.TemplateResponse("bitcash/recon.html", {
        "request": request, "cfg": load_config(), "tool": _BITCASH_TOOL,
        "rec": rec, "view": view, "token": token,
        "q": q, "search_hits": bitcash.search_cash(q) if q else [],
        "message": message, "error": error,
    })


@app.post("/tools/bit-cash-ar/recon/{token}/select")
async def bitcash_recon_select(request: Request, token: str):
    form = await request.form()
    rec = bitcash.set_selection(token, form.getlist("ar"), form.get("bit"))
    if rec is None:
        return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                            error="Selections can only change while the "
                                  "reconciliation is open.")
    return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                        message="Selection saved.")


@app.post("/tools/bit-cash-ar/recon/{token}/plug")
async def bitcash_recon_plug(request: Request, token: str):
    form = await request.form()
    amount = (form.get("amount") or "").strip()
    account = (form.get("account") or "").strip()
    if (bitcash._to_float(amount) or 0) and not account:
        return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                            error="Give the plug a G/L account so its journal "
                                  "line can be created.")
    rec = bitcash.set_plug(token, amount, account, form.get("note", ""))
    if rec is None:
        return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                            error="The plug can only change while the "
                                  "reconciliation is open.")
    if rec.get("plug"):
        p = rec["plug"]
        return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                            message=f"Manual plug saved — {p['amount']:,.0f} "
                                    f"XAF on {p['account']} goes into the "
                                    "journal entry.")
    return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                        message="Manual plug removed.")


@app.post("/tools/bit-cash-ar/recon/{token}/slip")
async def bitcash_recon_slip(request: Request, token: str,
                             slip: UploadFile = File(...)):
    if bitcash.load_recon(token) is None:
        return redirect_msg("/tools/bit-cash-ar", error="Not found.")
    ext = Path(slip.filename or "").suffix.lower()
    if ext not in _STMT_EXT:
        return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                            error="Deposit slip: upload a PDF, a photo or an "
                                  "Excel file.")
    bitcash.FILES_DIR.mkdir(parents=True, exist_ok=True)
    name = f"slip_{token}{ext}"
    (bitcash.FILES_DIR / name).write_bytes(await slip.read())
    rec = bitcash.set_slip(token, name, slip.filename or name)
    if rec is None:
        return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                            error="The deposit slip can only be attached to "
                                  "an open or approved reconciliation.")
    return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                        message="Deposit slip attached — it is kept with "
                                "this reconciliation's evidences and goes "
                                "into the journal pack.")


@app.get("/tools/bit-cash-ar/recon/{token}/evidence/{which}")
def bitcash_recon_evidence(token: str, which: str):
    rec = bitcash.load_recon(token)
    if rec is None:
        return redirect_msg("/tools/bit-cash-ar", error="Not found.")
    name = rec.get("file", "") if which == "advice" \
        else (rec.get("slip") or {}).get("name", "")
    path = bitcash.FILES_DIR / Path(name).name if name else None
    if not name or not path.exists():
        return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                            error="That evidence file is not on record for "
                                  "this reconciliation.")
    label = rec.get("source") if which == "advice" \
        else (rec.get("slip") or {}).get("source")
    media = {
        ".pdf": "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument"
                 ".spreadsheetml.sheet",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif",
    }.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media,
                        filename=Path(label or path.name).name)


@app.post("/tools/bit-cash-ar/recon/{token}/email")
async def bitcash_recon_email(request: Request, token: str):
    rec = bitcash.load_recon(token)
    if rec is None:
        return redirect_msg("/tools/bit-cash-ar", error="Not found.")
    form = await request.form()
    recipient = (form.get("recipient") or "").strip()
    subject = (form.get("subject") or "").strip() \
        or "Payment reconciliation — clarification needed"
    body = (form.get("body") or "").strip()
    if not body:
        return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                            error="The email body is empty.")
    advice = rec.get("file", "")
    attach = bitcash.FILES_DIR / Path(advice).name \
        if advice and (bitcash.FILES_DIR / Path(advice).name).exists() \
        else None
    cfg = load_config()
    smtp = cfg["smtp"]
    if smtp.get("enabled") and recipient:
        try:
            emailer.send_via_smtp(smtp, recipient, subject, body, attach)
        except Exception as exc:  # noqa: BLE001
            return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                                error=f"SMTP send failed: {exc}")
        bitcash.set_variance_email(token, recipient, "smtp")
        return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                            message=f"Clarification email sent to "
                                    f"{recipient}.")
    eml = OUTPUT_DIR / f"variance_{token}_{uuid.uuid4().hex[:8]}.eml"
    emailer.build_eml(eml, smtp.get("from_address", ""), recipient or "",
                      subject, body, attach)
    bitcash.set_variance_email(token, recipient, "eml", eml.name)
    reason = "SMTP is not configured" if not smtp.get("enabled") \
        else "no recipient address was given"
    return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                        message=f"{reason} — a ready-to-send email was "
                                "created; download it below and send it "
                                "from your mailbox.")


@app.post("/tools/bit-cash-ar/recon/{token}/approve")
async def bitcash_recon_approve(request: Request, token: str):
    rec = bitcash.load_recon(token)
    if rec is None:
        return redirect_msg("/tools/bit-cash-ar", error="Not found.")
    view = bitcash.recon_view(rec)
    if rec.get("bit_selected") is None or not rec.get("ar_selected"):
        return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                            error="Select the BIT payment and at least one "
                                  "Cash AR invoice before approving.")
    bitcash.set_status(token, True, getattr(request.state, "user", None))
    note = "" if view["residual"] == 0 else \
        f" (note: {view['residual']:,.0f} remains unplugged)"
    return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                        message="Reconciliation approved — it will be included "
                                "in the journal entry." + note)


@app.post("/tools/bit-cash-ar/recon/{token}/unapprove")
async def bitcash_recon_unapprove(request: Request, token: str):
    if bitcash.set_status(token, False, None) is None:
        return redirect_msg("/tools/bit-cash-ar", error="Not found.")
    return redirect_msg(f"/tools/bit-cash-ar/recon/{token}",
                        message="Approval removed — the sandbox is open again.")


@app.post("/tools/bit-cash-ar/recon/{token}/delete")
async def bitcash_recon_delete(token: str):
    if not bitcash.delete_recon(token):
        return redirect_msg("/tools/bit-cash-ar",
                            error="Approved reconciliations can't be deleted — "
                                  "unapprove first.")
    return redirect_msg("/tools/bit-cash-ar", message="Reconciliation removed.")


@app.post("/tools/bit-cash-ar/journal")
def bitcash_journal():
    # POST so the per-area middleware requires MODIFY access to generate.
    from datetime import datetime as _dt
    now = _dt.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    out = OUTPUT_DIR / f"cm01_journal_{stamp}.xlsx"
    result = bitcash.build_journal(out, now=now)
    if result is None:
        return redirect_msg("/tools/bit-cash-ar",
                            error="No approved reconciliation is ready — "
                                  "approve at least one sandbox (with a BIT "
                                  "payment and invoices selected) first.")
    # The audit pack: journal + per-sandbox advice copy / deposit slip /
    # evidences Excel.
    pack = OUTPUT_DIR / f"cm01_pack_{stamp}_{uuid.uuid4().hex[:8]}.zip"
    try:
        bitcash.build_pack(pack, out, result["name"], result["tokens"])
        pack_name = pack.name
    except Exception:  # noqa: BLE001 — journal alone still downloadable
        pack_name = ""
    return RedirectResponse(
        "/tools/bit-cash-ar?" + urlencode({
            "message": f"Journal entry ready — {result['count']} "
                       f"reconciliation(s), {result['lines']} line(s). "
                       "Download the journal or the full pack below.",
            "journal": out.name, "jname": result["name"],
            "pack": pack_name}),
        status_code=303)


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


# --------------------------------------------------------------------------- #
# Administration — per-user access rights (admin-only)
# --------------------------------------------------------------------------- #
@app.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request, message: str = "", error: str = ""):
    if not getattr(request.state, "is_admin", True):
        return _access_denied(request, read_only=False)
    cfg = load_config()
    auth_cfg = cfg.get("auth", {})
    rows = [{
        "username": u,
        "is_admin": auth.is_admin(auth_cfg, u),
        "access": auth.get_access(auth_cfg, u),
        "alias": auth.get_alias(auth_cfg, u),
    } for u in auth.list_users()]
    return templates.TemplateResponse("admin.html", {
        "request": request, "cfg": cfg, "message": message, "error": error,
        "users": rows, "areas": _ASSIGNABLE_AREAS,
        "current_user": getattr(request.state, "user", None),
        "auth_enabled": auth.enabled(auth_cfg),
        "min_password": auth.MIN_PASSWORD_LEN,
    })


@app.post("/admin/access")
async def admin_set_access(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return RedirectResponse(
            "/admin?error=Administrator access required.", status_code=303)
    form = await request.form()
    username = (form.get("username") or "").strip()
    if username not in auth.list_users():
        return RedirectResponse("/admin?error=Unknown user.", status_code=303)
    # Each area is posted as access:<slug> = none | read | modify.
    access_map = {}
    for slug in _ASSIGNABLE_SLUGS:
        val = form.get(f"access:{slug}")
        if val in (auth.ACCESS_READ, auth.ACCESS_MODIFY):
            access_map[slug] = val
    auth.set_access(username, access_map)
    granted = len(access_map)
    return redirect_msg(
        "/admin",
        message=f"Access rights updated for '{username}' "
                f"({granted} area(s) granted).")


@app.post("/admin/alias")
async def admin_set_alias(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return RedirectResponse(
            "/admin?error=Administrator access required.", status_code=303)
    form = await request.form()
    username = (form.get("username") or "").strip()
    if username not in auth.list_users():
        return RedirectResponse("/admin?error=Unknown user.", status_code=303)
    alias = (form.get("alias") or "").strip()[:24]
    auth.set_alias(username, alias)
    return redirect_msg(
        "/admin",
        message=(f"Alias for '{username}' set to '{alias}' — uploads now show "
                 f"as {alias}." if alias
                 else f"Alias for '{username}' cleared."))


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
        "turnstile": cfg.get("turnstile", {}),
        "turnstile_active": turnstile.active(cfg),
        "banks": cfg.get("banks", []),
    })


@app.post("/settings/users/add")
async def settings_user_add(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    form = await request.form()
    dest = "/admin" if form.get("redirect") == "/admin" else "/settings"
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    if not username:
        return RedirectResponse(f"{dest}?error=Username is required.",
                                status_code=303)
    if len(password) < auth.MIN_PASSWORD_LEN:
        return RedirectResponse(
            f"{dest}?error=Password must be at least "
            f"{auth.MIN_PASSWORD_LEN} characters.", status_code=303)
    make_admin = form.get("is_admin") == "on"
    auth.add_user(username, password,
                  secure_cookies=(request.url.scheme == "https"),
                  admin=make_admin)
    role = "administrator" if make_admin else "employee"
    return redirect_msg(
        dest,
        message=f"User '{username}' added as {role}. Staff login is enabled.")


@app.post("/settings/users/delete")
async def settings_user_delete(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    form = await request.form()
    dest = "/admin" if form.get("redirect") == "/admin" else "/settings"
    username = (form.get("username") or "").strip()
    if username == getattr(request.state, "user", None):
        return RedirectResponse(
            f"{dest}?error=You can't delete the account you're signed in with.",
            status_code=303)
    if auth.delete_user(username):
        return redirect_msg(dest, message=f"User '{username}' removed.")
    return RedirectResponse(
        f"{dest}?error=Could not remove that user (the last user can't be removed).",
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
    state = ("saved — document reading is ON"
             if new_cfg["ai"]["api_key"] else "cleared — Document reading is OFF")
    return RedirectResponse(f"/settings?message=document-reading key {state}.",
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
            "/settings?message=✓ document-reading key works — document "
            "reading is ready.", status_code=303)
    except (ai_ocr.AiNotConfigured, ai_ocr.AiReadError) as exc:
        return redirect_msg("/settings", error=f"Key test failed: {exc}")


@app.post("/settings/security")
async def app_settings_security(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    form = await request.form()
    cfg = load_config()
    cur = cfg.get("turnstile", {})
    secret = form.get("turnstile_secret") or ""
    updates = {
        "turnstile": {
            "enabled": form.get("turnstile_enabled") == "on",
            "site_key": (form.get("turnstile_site") or "").strip(),
            # Blank secret field keeps the stored one.
            "secret_key": secret if secret else cur.get("secret_key", ""),
        },
    }
    new_cfg = save_user_config(updates)
    ts = "active" if turnstile.active(new_cfg) else (
        "enabled but missing keys" if new_cfg["turnstile"].get("enabled") else "off")
    return RedirectResponse(
        f"/settings?message=Security saved — Turnstile {ts}.",
        status_code=303)


@app.post("/settings/banks")
async def app_settings_banks(request: Request):
    blocked = _admin_block(request)
    if blocked:
        return blocked
    form = await request.form()
    names, seen = [], set()
    for line in (form.get("banks") or "").splitlines():
        n = line.strip()
        if n and n.lower() not in seen:        # trim blanks + de-dup
            names.append(n)
            seen.add(n.lower())
    save_user_config({"banks": names})         # a list REPLACES (allows removal)
    return RedirectResponse(
        f"/settings?message=Saved {len(names)} bank(s) — one upload slot each.",
        status_code=303)


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
def download(filename: str, download_as: str = ""):
    safe = Path(filename).name  # strip any path components
    path = OUTPUT_DIR / safe
    if not path.exists():
        return HTMLResponse("File not found.", status_code=404)
    lower = safe.lower()
    if lower.endswith(".eml"):
        media = "message/rfc822"
    elif lower.endswith((".xlsx", ".xlsm")):
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif lower.endswith(".zip"):
        media = "application/zip"
    else:
        media = "application/pdf"
    # Optional friendly download name (e.g. a zip stored under a token but
    # presented as "Collections Avril 2026 - 658902134.zip").
    dl_name = Path(download_as).name if download_as else safe
    return FileResponse(path, media_type=media, filename=dl_name)
