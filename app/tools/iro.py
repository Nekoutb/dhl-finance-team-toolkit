"""IRO Statements & Returns — the operator self-service layer on top of the
BIT & Cash AR reconciliation. STRICTLY ADDITIVE: nothing here changes the
existing reconciliation logic; both channels below simply create the same
sandboxes finance already reviews.

* Statements OUT: the Cash AR is grouped per operator account (the SAP
  account = the operator's DHL account number); each reseller receives an
  Excel statement of their OPEN airwaybills plus a personal secure link.
  An AWB already matched in an open or approved reconciliation is NOT open
  — it never goes out to an IRO a second time.
* References IN — portal: on their link the operator ticks the AWBs paid,
  enters the payment reference per AWB, attaches the bank deposit slip(s),
  submits. The tool builds the evidence file itself and opens a sandbox.
* References IN — email: a formatted mail (subject ``PAYREF <account>``)
  to the dedicated mailbox is read by the poller and lands the same way.
  Only mail from the registered operator email is processed; everything
  else is quarantined for finance to review.
"""
import email
import email.policy
import imaplib
import json
import os
import re
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from threading import Lock

from ..config import DATA_DIR, UPLOAD_DIR
from ..services import ai_ocr, emailer
from . import bitcash

IRO_DIR = DATA_DIR / "iro"
MAIL_LOG = IRO_DIR / "_mail.json"
MAX_OPEN_SUBMISSIONS = 10          # per operator — runaway/abuse guard
_lock = Lock()

_SUBJECT_RE = re.compile(r"PAYREF[\s:]+([A-Za-z0-9]+)", re.IGNORECASE)
_BODY_AWB_RE = re.compile(
    r"^\s*AWB[\s:]+(\d{4,})\s+([\d\s.,]+?)(?:\s+(\S.*?))?\s*$",
    re.IGNORECASE | re.MULTILINE)
_SLIP_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
_XLS_EXT = {".xlsx", ".xlsm", ".xls"}


def _atomic(path, payload):
    IRO_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _norm_ref(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


# --------------------------------------------------------------------------- #
# Matched AWBs: once selected in an open/approved sandbox they are no longer
# open and must never be re-sent to an operator (user rule).
# --------------------------------------------------------------------------- #
def matched_awbs():
    """Set of AWB digit-strings currently covered by a reconciliation."""
    data = bitcash.rows_store()
    gen_now = bitcash.rows_generation(data)
    cash_by_id = {r["id"]: r for r in data["cash"]}
    covered = set()
    for rec in bitcash.list_recons():
        if rec.get("status") == "approved":
            frozen = rec.get("frozen") or {}
            for row in frozen.get("ar_rows", []):
                if row.get("awb"):
                    covered.add(row["awb"])
            if frozen:
                continue
        if rec.get("status") in ("open", "approved") \
                and (rec.get("rows_gen") or {}) == gen_now:
            for i in rec.get("ar_selected", []):
                row = cash_by_id.get(i)
                if row and row.get("awb"):
                    covered.add(row["awb"])
    return covered


def operator_accounts():
    """The Cash AR grouped per operator account (SAP acct), OPEN rows only —
    matched AWBs excluded. Sorted by open amount, biggest first."""
    covered = matched_awbs()
    groups = {}
    for row in bitcash.rows_store()["cash"]:
        acct = (row.get("sap_acct") or "").strip()
        if not acct:
            continue
        if row.get("awb") and row["awb"] in covered:
            continue
        g = groups.setdefault(acct, {"account": acct, "names": Counter(),
                                     "rows": [], "total": 0.0})
        if (row.get("customer") or "").strip():
            g["names"][row["customer"].strip()] += 1
        g["rows"].append(row)
        g["total"] = round(g["total"] + (row.get("amount") or 0), 2)
    out = []
    for g in groups.values():
        name = g["names"].most_common(1)[0][0] if g["names"] else ""
        out.append({"account": g["account"], "name": name,
                    "rows": sorted(g["rows"], key=lambda r: r.get("awb", "")),
                    "count": len(g["rows"]), "total": g["total"]})
    out.sort(key=lambda g: -g["total"])
    return out


def account_entry(account):
    return next((g for g in operator_accounts()
                 if g["account"] == str(account)), None)


# --------------------------------------------------------------------------- #
# Operator records — email, personal token, send + submission history
# --------------------------------------------------------------------------- #
def _rec_path(account):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(account))
    return IRO_DIR / f"{safe}.json"


def load_record(account):
    p = _rec_path(account)
    if not p.exists():
        return {"account": str(account), "name": "", "email": "",
                "token": "", "sent": [], "submissions": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"account": str(account), "name": "", "email": "",
                "token": "", "sent": [], "submissions": []}


def save_record(rec):
    with _lock:
        _atomic(_rec_path(rec["account"]), rec)


def all_records():
    if not IRO_DIR.exists():
        return {}
    out = {}
    for p in IRO_DIR.glob("*.json"):
        if p.name.startswith("_"):
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
            out[rec.get("account", p.stem)] = rec
        except (json.JSONDecodeError, OSError):
            continue
    return out


def set_email(account, email_addr, name=""):
    rec = load_record(account)
    rec["email"] = str(email_addr or "").strip()
    if name:
        rec["name"] = name
    save_record(rec)
    return rec


def ensure_token(account, name=""):
    rec = load_record(account)
    if name:
        rec["name"] = name
    if not rec.get("token"):
        rec["token"] = uuid.uuid4().hex
        save_record(rec)
    return rec


def regenerate_token(account):
    rec = load_record(account)
    rec["token"] = uuid.uuid4().hex
    save_record(rec)
    return rec


def find_by_token(token):
    if not token or not str(token).isalnum():
        return None
    for rec in all_records().values():
        if rec.get("token") == str(token):
            return rec
    return None


def record_sent(account, to_addr, mode):
    rec = load_record(account)
    rec.setdefault("sent", []).append(
        {"at": datetime.now().strftime("%Y-%m-%d %H:%M"),
         "to": to_addr, "mode": mode})
    save_record(rec)
    return rec


def record_submission(account, entry):
    rec = load_record(account)
    rec.setdefault("submissions", []).append(entry)
    save_record(rec)
    return rec


def open_submission_count(rec):
    """Submissions whose sandbox is still open/reading (abuse guard)."""
    n = 0
    for s in rec.get("submissions", []):
        sandbox = bitcash.load_recon(s.get("recon_token", ""))
        if sandbox and sandbox.get("status") in ("reading", "open"):
            n += 1
    return n


# --------------------------------------------------------------------------- #
# Excel builders — the statement OUT and the generated evidence IN
# --------------------------------------------------------------------------- #
def build_statement_xlsx(out_path, entry):
    """The operator's statement: open AWBs + a blank Payment-reference
    column shaped exactly like the return format."""
    import xlsxwriter

    wb = xlsxwriter.Workbook(str(out_path))
    ws = wb.add_worksheet("Statement")
    f_title = wb.add_format({"bold": True, "font_size": 13,
                             "font_name": "Arial"})
    f_body = wb.add_format({"font_name": "Arial", "font_size": 10})
    f_head = wb.add_format({"font_name": "Arial", "font_size": 10,
                            "bold": True, "bg_color": "#0b2545",
                            "font_color": "white", "border": 1})
    f_cell = wb.add_format({"font_name": "Arial", "font_size": 10,
                            "border": 1})
    f_num = wb.add_format({"font_name": "Arial", "font_size": 10,
                           "border": 1, "num_format": "#,##0"})
    f_tot = wb.add_format({"font_name": "Arial", "font_size": 10,
                           "bold": True, "border": 1, "num_format": "#,##0",
                           "bg_color": "#eef3fb"})
    ws.set_column(0, 0, 16)
    ws.set_column(1, 2, 18)
    ws.set_column(3, 3, 14)
    ws.set_column(4, 4, 24)
    ws.write("A1", "Statement of account — open airwaybills", f_title)
    ws.write("A2", f"Account: {entry['account']}"
             + (f" · {entry['name']}" if entry.get("name") else ""), f_body)
    ws.write("A3", f"Generated {datetime.now().strftime('%d/%m/%Y %H:%M')} — "
                   "fill the Payment reference column for the AWBs your "
                   "deposit paid and return this file, or use your secure "
                   "link.", f_body)
    headers = ["Waybill", "Cash AR reference", "Doc N°", "Amount (XAF)",
               "Payment reference"]
    for c, h in enumerate(headers):
        ws.write(4, c, h, f_head)
    r = 5
    for row in entry["rows"]:
        ws.write(r, 0, row.get("awb") or row.get("assignment", ""), f_cell)
        ws.write(r, 1, row.get("reference", ""), f_cell)
        ws.write(r, 2, row.get("doc_no", ""), f_cell)
        ws.write_number(r, 3, row.get("amount") or 0, f_num)
        ws.write(r, 4, "", f_cell)
        r += 1
    ws.write(r, 0, "TOTAL", f_tot)
    for c in (1, 2, 4):
        ws.write(r, c, "", f_tot)
    ws.write_number(r, 3, entry["total"], f_tot)
    ws.freeze_panes(5, 0)
    wb.close()
    return Path(out_path)


def build_evidence_xlsx(out_path, lines):
    """The generated evidence file (Waybill | Amount | Payment reference)
    fed into the NORMAL reconciliation pipeline — one pipeline, no forks."""
    import xlsxwriter

    wb = xlsxwriter.Workbook(str(out_path))
    ws = wb.add_worksheet("Evidence")
    ws.write_row(0, 0, ["Waybill", "Amount", "Payment reference"])
    for i, ln in enumerate(lines, start=1):
        ws.write(i, 0, str(ln["awb"]))
        ws.write_number(i, 1, float(ln["amount"] or 0))
        ws.write(i, 2, str(ln.get("reference") or ""))
    wb.close()
    return Path(out_path)


# --------------------------------------------------------------------------- #
# Submission glue — both channels end HERE, in the existing sandbox pipeline
# --------------------------------------------------------------------------- #
def create_submission(account, lines=None, evidence_path=None,
                      slip_paths=None, references=None, channel="portal",
                      label="", slip_total=None):
    """Open a reconciliation sandbox from an operator's return. Either
    ``evidence_path`` (an uploaded Excel) or ``lines``
    ([{awb, amount, reference}]) must be given; ``slip_paths`` =
    [(path, source_name), ...]. ``slip_total`` — the banked amount already
    read from the deposit slip (skips a second read). Returns the recon
    token."""
    if not evidence_path:
        evidence_path = UPLOAD_DIR / f"iro_{uuid.uuid4().hex[:8]}.xlsx"
        build_evidence_xlsx(evidence_path, lines or [])
    slips = [(Path(p), src) for p, src in (slip_paths or [])]
    first = slips[0] if slips else None
    token = bitcash.create_recon_async(
        Path(evidence_path), "excel",
        label or f"IRO {account}",
        f"operator:{account}",
        slip_path=first[0] if first else None,
        slip_media=_slip_media(first[0]) if first else None,
        slip_source=first[1] if first else "",
        extra_slips=[(p, s) for p, s in slips[1:]],
        payment_refs=[r for r in (references or []) if r],
        slip_total=slip_total)
    record_submission(account, {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "references": [r for r in (references or []) if r][:10],
        "amount": slip_total,
        "channel": channel, "recon_token": token})
    return token


# --------------------------------------------------------------------------- #
# Portal slip pre-read: the deposit slip is read the moment the operator
# attaches it — the banked amount is populated automatically and locked
# (a bank deposit amount is never altered by hand).
# --------------------------------------------------------------------------- #
def preread_slip(blob, filename, ai_cfg):
    """Save an operator's deposit slip, read the banked total and check the
    slip mentions DHL as the payee. Returns {slip_id, source, total, dhl}
    — total None when unreadable (finance verifies manually)."""
    ext = Path(filename or "slip").suffix.lower()
    slip_id = f"iroslip_{uuid.uuid4().hex[:8]}"
    dest = UPLOAD_DIR / f"{slip_id}{ext}"
    dest.write_bytes(blob)
    total, dhl = None, False
    try:
        doc = ai_ocr.extract_payment_statement(
            blob, _slip_media(dest), ai_cfg)
        total = bitcash._to_float(doc.get("total")) or None
        blob_txt = " ".join(
            [str(doc.get("label", ""))]
            + [str(ln.get("description", "")) + " "
               + str(ln.get("reference", ""))
               for ln in doc.get("lines", [])]).lower()
        dhl = "dhl" in blob_txt
    except Exception:  # noqa: BLE001 — unreadable slip stays pure evidence
        pass
    sidecar = {"slip_id": slip_id, "name": dest.name,
               "source": filename or dest.name, "total": total, "dhl": dhl}
    (UPLOAD_DIR / f"{slip_id}.json").write_text(
        json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
    return sidecar


def load_preread(slip_id):
    """The saved slip + sidecar for a pre-read id, or None."""
    if not re.fullmatch(r"iroslip_[0-9a-f]{8}", str(slip_id or "")):
        return None
    side = UPLOAD_DIR / f"{slip_id}.json"
    if not side.exists():
        return None
    try:
        meta = json.loads(side.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    path = UPLOAD_DIR / meta.get("name", "")
    if not path.exists():
        return None
    meta["path"] = path
    return meta


def _slip_media(path):
    ext = Path(path).suffix.lower()
    if ext in _XLS_EXT:
        return "excel"
    return {"pdf": "application/pdf", "png": "image/png",
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp", "gif": "image/gif"}.get(
        ext.lstrip("."), "application/pdf")


# --------------------------------------------------------------------------- #
# Email channel — parse + poll (core is IMAP-free and fully testable)
# --------------------------------------------------------------------------- #
def send_evidence_copy(cfg, account, name, lines, refs, recon_token,
                       slip_paths=None, slip_total=None):
    """A copy of every portal submission is emailed into the operator
    mailbox as ADDED EVIDENCE (with the deposit slip attached). The subject
    is marked "Evidence copy" so the poller never re-ingests it. Returns
    "sent"/"skipped"."""
    smtp = cfg.get("smtp", {})
    to = (cfg.get("imap") or {}).get("username", "") \
        or cfg.get("orange_cameroun", {}).get("default_recipient", "")
    if not (smtp.get("enabled") and to):
        return "skipped"
    detail = "\n".join(
        f"  AWB {ln['awb']}  {ln['amount']:,.0f} XAF"
        + (f"  ref {ln['reference']}" if ln.get("reference") else "")
        for ln in (lines or []))
    body = (f"Evidence copy of a portal submission.\n\n"
            f"Operator account: {account} ({name or '—'})\n"
            f"Payment reference(s): {', '.join(refs) or '—'}\n"
            + (f"Banked amount read from the deposit slip: "
               f"{slip_total:,.0f} XAF\n" if slip_total else "")
            + f"Reconciliation: /tools/bit-cash-ar/recon/{recon_token}\n\n"
            f"{len(lines or [])} airwaybill(s):\n{detail}\n")
    first = (slip_paths or [None])[0]
    attachment = Path(first[0]) if first else None
    try:
        emailer.send_via_smtp(
            smtp, to, f"Evidence copy — IRO {account} — {recon_token}",
            body, attachment)
        return "sent"
    except Exception:  # noqa: BLE001 — the submission itself already landed
        return "skipped"


def _mail_log():
    if MAIL_LOG.exists():
        try:
            return json.loads(MAIL_LOG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"processed": [], "quarantine": []}


def _save_mail_log(log):
    with _lock:
        log["processed"] = log.get("processed", [])[-500:]
        log["quarantine"] = log.get("quarantine", [])[-100:]
        _atomic(MAIL_LOG, log)


def quarantine_list():
    return list(reversed(_mail_log().get("quarantine", [])))


def _quarantine(log, msg, reason):
    log.setdefault("quarantine", []).append({
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "from": str(msg.get("From", ""))[:80],
        "subject": str(msg.get("Subject", ""))[:100],
        "reason": reason})


def process_message(raw_bytes):
    """One raw email → an operator submission (or a quarantine entry).
    Returns {"status": "created"|"duplicate"|"quarantined", ...}."""
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    log = _mail_log()
    msg_id = str(msg.get("Message-ID", "")).strip() or \
        f"nohdr:{hash(raw_bytes)}"
    if msg_id in log.get("processed", []):
        return {"status": "duplicate"}
    log.setdefault("processed", []).append(msg_id)

    subject = str(msg.get("Subject", ""))
    if "evidence copy" in subject.lower():
        # Our own submission copies — email evidence, never re-ingested.
        _save_mail_log(log)
        return {"status": "copy"}
    m = _SUBJECT_RE.search(subject)
    if not m:
        _quarantine(log, msg, "subject does not carry PAYREF <account>")
        _save_mail_log(log)
        return {"status": "quarantined", "reason": "subject"}
    account = m.group(1)
    rec = load_record(account)
    sender = email.utils.parseaddr(str(msg.get("From", "")))[1].lower()
    if not rec.get("email") or sender != rec["email"].lower():
        _quarantine(log, msg, f"sender {sender or '?'} is not the "
                              f"registered email for {account}")
        _save_mail_log(log)
        return {"status": "quarantined", "reason": "sender"}

    IRO_DIR.mkdir(parents=True, exist_ok=True)
    evidence_path = None
    slips = []
    for part in msg.iter_attachments():
        fname = part.get_filename() or ""
        ext = Path(fname).suffix.lower()
        payload = part.get_payload(decode=True) or b""
        if not payload:
            continue
        dest = UPLOAD_DIR / f"iromail_{uuid.uuid4().hex[:8]}{ext}"
        dest.write_bytes(payload)
        if ext in _XLS_EXT and evidence_path is None:
            evidence_path = dest
        elif ext in _SLIP_EXT:
            slips.append((dest, fname or dest.name))

    body = msg.get_body(preferencelist=("plain",))
    body_text = body.get_content() if body else ""
    refs = re.findall(r"^\s*REF[\s:]+(\S.*?)\s*$", body_text,
                      re.IGNORECASE | re.MULTILINE)
    lines = [{"awb": a, "amount": bitcash._to_float(amt),
              "reference": (ref or (refs[0] if refs else "")).strip()}
             for a, amt, ref in _BODY_AWB_RE.findall(body_text)]

    if not evidence_path and not lines:
        _quarantine(log, msg, "no AWB Excel attached and no AWB lines in "
                              "the body")
        _save_mail_log(log)
        return {"status": "quarantined", "reason": "content"}

    token = create_submission(
        account, lines=lines, evidence_path=evidence_path,
        slip_paths=slips, references=refs or
        [ln["reference"] for ln in lines if ln.get("reference")],
        channel="email", label=f"IRO {account} — email")
    _save_mail_log(log)
    return {"status": "created", "recon_token": token, "account": account}


def poll_mailbox(imap_cfg):
    """Fetch UNSEEN mail from the dedicated mailbox and process each
    message. Returns a summary dict; raises nothing (errors reported)."""
    cfg = imap_cfg or {}
    if not cfg.get("enabled") or not cfg.get("host"):
        return {"ok": False, "error": "IMAP is not configured — fill the "
                                      "Operator mailbox section in Settings."}
    summary = {"ok": True, "seen": 0, "created": 0, "quarantined": 0,
               "duplicates": 0}
    try:
        port = int(cfg.get("port") or 993)
        if cfg.get("ssl", True):
            box = imaplib.IMAP4_SSL(cfg["host"], port)
        else:
            box = imaplib.IMAP4(cfg["host"], port)
        box.login(cfg.get("username", ""), cfg.get("password", ""))
        box.select(cfg.get("folder") or "INBOX")
        _typ, data = box.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []
        for num in ids:
            _typ, fetched = box.fetch(num, "(RFC822)")
            raw = fetched[0][1] if fetched and fetched[0] else b""
            summary["seen"] += 1
            try:
                result = process_message(raw)
            except Exception:  # noqa: BLE001 — one bad mail never stops the run
                result = {"status": "quarantined"}
            key = {"created": "created", "quarantined": "quarantined",
                   "duplicate": "duplicates"}.get(result.get("status"))
            if key:
                summary[key] += 1
            box.store(num, "+FLAGS", "\\Seen")
        box.logout()
    except Exception as exc:  # noqa: BLE001 — surfaced to the panel
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return summary
