"""MyDHLPay — scan-to-pay for walk-in customers, and the Cash
Reconciliation section that consumes it.

The public one-pager (/pay) lets a customer scan (or type) the airwaybills
they intend to pay. Each session gets a UNIQUE transaction code —
DDMM + three letters, e.g. 1907ABC — which the payer copies and pastes as
the transaction reference when the Orange Money request reaches their
phone; the page pre-fills the USSD dial string per airwaybill (same code
for every AWB in the session, only the amount differs).

Because the session stores the scanned AWBs against the code, finance can
turn it into a normal reconciliation sandbox in one click — the code is
hunted in the BIT first (reference-first matching), and everything
downstream (approval freeze, journal pack) is the existing pipeline.
"""
import json
import os
import re
import secrets
import string
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock

from ..config import DATA_DIR, UPLOAD_DIR
from . import bitcash
from . import iro

PAY_DIR = DATA_DIR / "mydhlpay"
CODE_RE = re.compile(r"^\d{4}[A-Z]{3}$")
MAX_AWBS_PER_SESSION = 30
MAX_OPEN_SESSIONS_PER_DAY = 300      # abuse guard on the public page
_lock = Lock()


def _atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _path(code):
    return PAY_DIR / f"{code}.json"


def _short_name(name):
    """Privacy on the public page: enough to recognise yourself, no more."""
    s = str(name or "").strip()
    return s if len(s) <= 12 else s[:12] + "…"


def new_code(now=None):
    """DDMM + three letters (e.g. 1907ABC), unique among the sessions on
    file. Raises RuntimeError when the day's namespace is exhausted."""
    now = now or datetime.now()
    prefix = now.strftime("%d%m")
    for _ in range(80):
        letters = "".join(secrets.choice(string.ascii_uppercase)
                          for _ in range(3))
        code = prefix + letters
        if not _path(code).exists():
            return code
    raise RuntimeError("no free transaction code available")


def sessions_today(now=None):
    now = now or datetime.now()
    prefix = now.strftime("%d%m")
    if not PAY_DIR.exists():
        return 0
    return sum(1 for p in PAY_DIR.glob(f"{prefix}*.json"))


def create_session():
    if sessions_today() >= MAX_OPEN_SESSIONS_PER_DAY:
        return None
    code = new_code()
    session = {"code": code,
               "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "status": "open", "awbs": []}
    with _lock:
        _atomic(_path(code), session)
    return session


def load_session(code):
    code = str(code or "").upper()
    if not CODE_RE.fullmatch(code):
        return None
    p = _path(code)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_session(session):
    with _lock:
        _atomic(_path(session["code"]), session)


def lookup_awb(awb):
    """Exact-digit match in the Cash AR on record — never a listing."""
    digits = re.sub(r"\D", "", str(awb or ""))
    if len(digits) < 8:
        return None, digits
    for r in bitcash.rows_store()["cash"]:
        if r.get("awb") == digits:
            return r, digits
    return None, digits


def add_awb(code, awb, amount=None):
    """Add an airwaybill to a session (creating the session when ``code``
    is empty). Returns a dict for the page:
      {error} | {need_amount, awb} | {session, entry}."""
    session = load_session(code) if code else None
    if code and session is None:
        return {"error": "That payment session was not found — refresh "
                         "the page and scan again."}
    if session and session.get("status") != "open":
        return {"error": "This payment session is already being "
                         "reconciled — start a new one."}
    row, digits = lookup_awb(awb)
    if len(digits) < 8:
        return {"error": "That does not look like an airwaybill number — "
                         "10 digits are expected."}
    if session and any(a["awb"] == digits for a in session["awbs"]):
        return {"error": f"Airwaybill {digits} is already on your list."}
    if session and len(session["awbs"]) >= MAX_AWBS_PER_SESSION:
        return {"error": "This session is full — pay these first, then "
                         "start a new one."}
    if row is None:
        amt = bitcash._to_float(amount) if amount is not None else 0.0
        if not amt:
            return {"need_amount": True, "awb": digits}
        entry = {"awb": digits, "amount": round(amt, 2), "customer": "",
                 "customer_short": "", "account": "", "on_record": False}
    else:
        entry = {"awb": digits, "amount": row.get("amount") or 0,
                 "customer": row.get("customer", ""),
                 "customer_short": _short_name(row.get("customer", "")),
                 "account": row.get("sap_acct", ""), "on_record": True}
    if session is None:
        session = create_session()
        if session is None:
            return {"error": "The payment service is busy — please try "
                             "again later."}
    session["awbs"].append(entry)
    save_session(session)
    return {"session": session, "entry": entry}


def session_total(session):
    return round(sum(a.get("amount") or 0 for a in session.get("awbs", [])),
                 2)


def ussd_for(cfg_pay, amount):
    """The dial string for one airwaybill, e.g. *126*1*1*675153953*53639#."""
    template = (cfg_pay or {}).get("ussd_template",
                                   "*126*1*1*{merchant}*{amount}#")
    merchant = (cfg_pay or {}).get("merchant", "675153953")
    return template.format(merchant=merchant, amount=int(round(amount or 0)))


def list_sessions(limit=200):
    """Newest first, headline fields for the Cash Reconciliation section."""
    if not PAY_DIR.exists():
        return []
    out = []
    for p in PAY_DIR.glob("*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({"code": s.get("code", p.stem),
                    "created": s.get("created", ""),
                    "status": s.get("status", "open"),
                    "count": len(s.get("awbs", [])),
                    "total": session_total(s),
                    "off_record": sum(1 for a in s.get("awbs", [])
                                      if not a.get("on_record")),
                    "recon_token": s.get("recon_token", "")})
    out.sort(key=lambda s: s.get("created", ""), reverse=True)
    return out[:limit]


def delete_session(code):
    s = load_session(code)
    if s is None or s.get("status") == "sandboxed":
        return False
    _path(s["code"]).unlink(missing_ok=True)
    return True


def create_sandbox(code):
    """Turn a session into a NORMAL reconciliation sandbox: the scanned
    AWBs become the evidence lines, the transaction code the payment
    reference the BIT is searched for first. Returns the recon token or
    None."""
    session = load_session(code)
    if session is None or not session.get("awbs") \
            or session.get("status") == "sandboxed":
        return None
    evidence = UPLOAD_DIR / f"pay_{uuid.uuid4().hex[:8]}.xlsx"
    iro.build_evidence_xlsx(evidence, [
        {"awb": a["awb"], "amount": a["amount"],
         "reference": session["code"]} for a in session["awbs"]])
    token = bitcash.create_recon_async(
        evidence, "excel", f"MyDHLPay {session['code']}",
        "mydhlpay", payment_refs=[session["code"]])
    session["status"] = "sandboxed"
    session["recon_token"] = token
    save_session(session)
    return token
