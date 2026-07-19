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
import hashlib
import imaplib
import json
import os
import re
import time
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
DEPOSITS_PATH = IRO_DIR / "_deposits.json"
MAX_OPEN_SUBMISSIONS = 10          # per operator — runaway/abuse guard
_lock = Lock()

_SUBJECT_RE = re.compile(r"PAYREF[\s:]+([A-Za-z0-9]+)", re.IGNORECASE)
_BODY_AWB_RE = re.compile(
    r"^\s*AWB[\s:]+(\d{4,})\s+([\d\s.,]+?)(?:\s+(\S.*?))?\s*$",
    re.IGNORECASE | re.MULTILINE)
_SLIP_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}
_XLS_EXT = {".xlsx", ".xlsm", ".xls"}
_MIN_SLIP_BYTES = 20 * 1024        # smaller images = signature logos, noise


def _atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp per writer — several processes (gunicorn workers + the
    # mail-poller cron) may write the same store; a shared tmp name would
    # let one replace the other's bytes or crash on a vanished file.
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class _FileLock:
    """Cross-PROCESS lock via an O_EXCL sentinel file (the in-process
    threading lock cannot serialise gunicorn workers or the cron poller).
    Read-modify-write cycles on the JSON stores run inside it. On timeout
    it proceeds unlocked — availability over a 500 on a landed submission;
    a stale sentinel (crashed holder) is broken after 10 s."""

    def __init__(self, target, timeout=3.0, stale=10.0):
        self.lockfile = Path(f"{target}.lock")
        self.timeout, self.stale = timeout, stale
        self.fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        self.lockfile.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                self.fd = os.open(self.lockfile,
                                  os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                return self
            except FileExistsError:
                try:
                    if time.time() - self.lockfile.stat().st_mtime \
                            > self.stale:
                        self.lockfile.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.time() > deadline:
                    return self
                time.sleep(0.05)

    def __exit__(self, *_exc):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            self.lockfile.unlink(missing_ok=True)
        return False


def _mutate_record(account, fn):
    """Load → mutate → save an operator record atomically ACROSS processes."""
    path = _rec_path(account)
    with _FileLock(path):
        rec = load_record(account)
        fn(rec)
        with _lock:
            _atomic(path, rec)
    return rec


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
    def _fn(rec):
        rec["email"] = str(email_addr or "").strip()
        if name:
            rec["name"] = name
    return _mutate_record(account, _fn)


def ensure_token(account, name=""):
    def _fn(rec):
        if name:
            rec["name"] = name
        if not rec.get("token"):
            rec["token"] = uuid.uuid4().hex
    return _mutate_record(account, _fn)


def regenerate_token(account):
    def _fn(rec):
        rec["token"] = uuid.uuid4().hex
    return _mutate_record(account, _fn)


def find_by_token(token):
    if not token or not str(token).isalnum():
        return None
    for rec in all_records().values():
        if rec.get("token") == str(token):
            return rec
    return None


def record_sent(account, to_addr, mode):
    def _fn(rec):
        rec.setdefault("sent", []).append(
            {"at": datetime.now().strftime("%Y-%m-%d %H:%M"),
             "to": to_addr, "mode": mode})
    return _mutate_record(account, _fn)


def record_submission(account, entry):
    def _fn(rec):
        rec.setdefault("submissions", []).append(entry)
    return _mutate_record(account, _fn)


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
                      label="", slip_total=None, slip_info=None):
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
        slip_total=slip_total, slip_info=slip_info)
    record_submission(account, {
        "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "references": [r for r in (references or []) if r][:10],
        "amount": slip_total,
        "channel": channel, "recon_token": token})
    clear_draft(account)               # the return is sent — draft done
    return token


# --------------------------------------------------------------------------- #
# Deposit history — the same bank deposit must never be accepted twice.
# Identity: exact file hash, OR the same (account, banked total, slip date)
# read off a different photo of the same slip.
# --------------------------------------------------------------------------- #
def _deposits():
    if DEPOSITS_PATH.exists():
        try:
            return json.loads(DEPOSITS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _match_deposit(entries, sha, account, total, slip_date):
    for e in entries:
        if sha and e.get("sha") == sha:
            return e
        if account and total and slip_date \
                and e.get("account") == str(account) \
                and e.get("total") == total \
                and e.get("date") == slip_date:
            return e
    return None


def find_prior_deposit(sha=None, account=None, total=None, slip_date=None):
    """The earlier deposit entry this slip duplicates, or None (check only)."""
    return _match_deposit(_deposits(), sha, account, total, slip_date)


def claim_deposit(sha, account, total, slip_date, reference, recon_token):
    """Atomic check-AND-record under the cross-process lock: two racing
    submissions of the same deposit cannot both pass. Returns the PRIOR
    entry when the deposit was already claimed (reject), else None after
    claiming it."""
    with _FileLock(DEPOSITS_PATH):
        entries = _deposits()
        prior = _match_deposit(entries, sha, account, total, slip_date)
        if prior:
            return prior
        entries.append({"sha": sha, "account": str(account), "total": total,
                        "date": slip_date,
                        "reference": str(reference or "")[:40],
                        "recon_token": recon_token,
                        "at": datetime.now().strftime("%Y-%m-%d %H:%M")})
        with _lock:
            _atomic(DEPOSITS_PATH, entries[-1000:])
    return None


def duplicate_message(prior, account=None):
    # The prior reference is disclosed only to the SAME account — a hash
    # collision across operators must not leak another party's details.
    same = account is not None \
        and prior.get("account") == str(account)
    return (f"This bank deposit slip was already submitted on "
            f"{prior.get('at', '—')}"
            + (f" (reference {prior['reference']})"
               if same and prior.get("reference") else "")
            + " — the same deposit cannot be reported twice. If this is a "
              "different deposit, contact the DHL finance team.")


# --------------------------------------------------------------------------- #
# Draft selections — ticks the IRO made but has not sent yet survive a
# closed browser and are restored on the next visit.
# --------------------------------------------------------------------------- #
DRAFT_QUIET_SECONDS = 20       # a late autosave never resurrects a draft
_MAX_DRAFT_TICKS = 500


def save_draft(account, ticks, reference="", payment_date=""):
    """Returns the saved draft, or None when the save was refused (a
    submission just cleared the draft — a late in-flight autosave from the
    old page must not bring the sent selections back)."""
    result = {}

    def _fn(rec):
        cleared = rec.get("draft_cleared_epoch", 0)
        if cleared and time.time() - cleared < DRAFT_QUIET_SECONDS:
            result["refused"] = True
            return
        clean = {}
        for k, v in list((ticks or {}).items())[:_MAX_DRAFT_TICKS]:
            clean[str(k)[:20]] = str(v or "")[:40]
        rec["draft"] = {"ticks": clean,
                        "reference": str(reference or "")[:40],
                        "payment_date": str(payment_date or "")[:10],
                        "saved_at":
                        datetime.now().strftime("%Y-%m-%d %H:%M")}
        result["draft"] = rec["draft"]
    _mutate_record(account, _fn)
    return result.get("draft")


def clear_draft(account):
    def _fn(rec):
        rec.pop("draft", None)
        rec["draft_cleared_epoch"] = time.time()
    _mutate_record(account, _fn)


# --------------------------------------------------------------------------- #
# Portal slip pre-read: the deposit slip is read the moment the operator
# attaches it — the banked amount AND the deposit date are populated
# automatically; the amount is locked (a bank deposit amount is never
# altered by hand). A slip already on the deposit history is REJECTED here.
# --------------------------------------------------------------------------- #
_MONTHS = {"janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4,
           "mai": 5, "juin": 6, "juillet": 7, "aout": 8, "août": 8,
           "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
           "décembre": 12, "january": 1, "february": 2, "march": 3,
           "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
           "september": 9, "october": 10, "november": 11, "december": 12}


def _iso_date(value):
    """'17/07/2026', '2026-07-17…' or '07 Juillet 2026 à 13:50' ->
    '2026-07-17' ('' when unparseable). French month names included —
    Cameroonian deposit slips are usually in French."""
    s = str(value or "").strip()
    m = re.match(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    m = re.match(r"(\d{1,2})(?:er)?\s+([A-Za-zéûî]+)\.?\s+(\d{4})", s)
    if m:
        month = _MONTHS.get(m.group(2).lower())
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(1)):02d}"
    return ""


def preread_slip(blob, filename, ai_cfg, account=""):
    """Save an operator's deposit slip and read EVERYTHING off it with the
    dedicated slip extractor: banked total, deposit date, depositor,
    beneficiary (the DHL check), account credited, slip reference and the
    beneficiary bank. Returns the sidecar dict — or {"duplicate": message}
    when this deposit was already submitted (same file, or same
    account/amount/date)."""
    sha = hashlib.sha256(blob).hexdigest()
    prior = find_prior_deposit(sha=sha)
    if prior:
        return {"duplicate": duplicate_message(prior, account)}
    ext = Path(filename or "slip").suffix.lower()
    slip_id = f"iroslip_{uuid.uuid4().hex[:8]}"
    dest = UPLOAD_DIR / f"{slip_id}{ext}"
    dest.write_bytes(blob)
    info = {}
    total, dhl, slip_date = None, False, ""
    try:
        info = ai_ocr.extract_deposit_slip(blob, _slip_media(dest), ai_cfg)
        total = bitcash._to_float(info.get("amount_xaf")) or None
        slip_date = _iso_date(info.get("date"))
        dhl = "dhl" in str(info.get("beneficiary", "")).lower()
    except Exception:  # noqa: BLE001 — unreadable slip stays pure evidence
        info = {}
    if total and slip_date:
        prior = find_prior_deposit(account=account, total=total,
                                   slip_date=slip_date)
        if prior:
            dest.unlink(missing_ok=True)
            return {"duplicate": duplicate_message(prior, account)}
    sidecar = {"slip_id": slip_id, "name": dest.name,
               "source": filename or dest.name, "total": total,
               "date": slip_date, "dhl": dhl, "sha": sha,
               "account": str(account or ""),
               "bank": info.get("bank", ""),
               "depositor": info.get("depositor", ""),
               "beneficiary": info.get("beneficiary", ""),
               "account_credited": info.get("account_credited", ""),
               "slip_reference": info.get("slip_reference", "")}
    (UPLOAD_DIR / f"{slip_id}.json").write_text(
        json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
    return sidecar


# --------------------------------------------------------------------------- #
# Deposit-slip LIST (Excel): banks hand out statements of deposits; the IRO
# uploads the table and simply TICKS the deposits funding this return.
# --------------------------------------------------------------------------- #
def parse_deposit_list(path):
    """An Excel of deposit slips → [{date, bank, reference, amount}].
    Columns are found by name (date / banque|bank / ref / montant|amount),
    with the same tolerance the evidence reader has."""
    from ..services import excel_reader

    parsed = excel_reader.read_transactions(path)
    header = parsed["header"]
    low = {h: re.sub(r"\s+", " ", str(h or "")).strip().lower()
           for h in header}

    def col(*cands):
        for cand in cands:
            for h in header:
                if cand in low[h]:
                    return h
        return None

    c_date = col("date")
    c_bank = col("banque", "bank")
    c_ref = col("reference", "référence", "ref", "bordereau", "slip")
    c_amt = col("montant", "amount", "value")
    rows = []
    for r in parsed["rows"]:
        d = r["data"]
        amount = bitcash._to_float(d.get(c_amt)) if c_amt else 0.0
        ref = bitcash._cellstr(d.get(c_ref)) if c_ref else ""
        if not amount and not ref:
            continue
        rows.append({
            "date": _iso_date(d.get(c_date))
            or bitcash._cellstr(d.get(c_date))[:10] if c_date else "",
            "bank": bitcash._cellstr(d.get(c_bank))[:30] if c_bank else "",
            "reference": ref[:40],
            "amount": amount})
        if len(rows) >= 100:
            break
    return rows


def load_preread(slip_id, account=None):
    """The saved slip + sidecar for a pre-read id, or None. A sidecar is
    bound to the operator it was pre-read for — another account's slip_id
    resolves to nothing."""
    if not re.fullmatch(r"iroslip_[0-9a-f]{8}", str(slip_id or "")):
        return None
    side = UPLOAD_DIR / f"{slip_id}.json"
    if not side.exists():
        return None
    try:
        meta = json.loads(side.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if account is not None and meta.get("account") != str(account):
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
def statement_email(entry, link, account):
    """The statement email an operator receives — written like a person,
    not an automation. Returns (subject, body)."""
    name = (entry.get("name") or "").title() or "partner"
    total = entry.get("total", 0)
    count = entry.get("count", 0)
    subject = (f"Your DHL account {account} — {count} airwaybill(s) "
               f"awaiting your payment details")
    body = (
        f"Dear {name},\n\n"
        f"I hope business is going well on your side.\n\n"
        f"Going through our books, your DHL account {account} still shows "
        f"{count} airwaybill(s) open for a total of {total:,.0f} XAF — the "
        f"full detail is in the attached statement. In most cases the money "
        f"has already been banked and we simply need your deposit details "
        f"to close the loop.\n\n"
        f"Could you take two minutes on your personal page? Tick the "
        f"airwaybills your deposit covered, put the deposit reference next "
        f"to them and attach a photo of the bank deposit slip — the rest "
        f"happens on our side:\n\n"
        f"    {link}\n\n"
        f"If email is easier for you, just reply with the subject "
        f"\"PAYREF {account}\", attach the deposit slip, and list the "
        f"airwaybills paid (one per line, e.g. \"AWB 8525614075 59600\").\n\n"
        f"Whatever is matched drops off your next statement automatically, "
        f"so you will never be asked twice for the same shipment. And if "
        f"any figure looks off to you, tell me — we will look at it "
        f"together.\n\n"
        f"Thank you for the good collaboration.\n\n"
        f"Kind regards,\n"
        f"The Finance team — DHL Express Cameroon")
    return subject, body


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
        f"    AWB {ln['awb']}  —  {ln['amount']:,.0f} XAF"
        + (f"  (ref {ln['reference']})" if ln.get("reference") else "")
        for ln in (lines or []))
    who = (name or "").title() or f"the operator on account {account}"
    body = (
        f"Hello team,\n\n"
        f"{who} (account {account}) has just reported a deposit through "
        f"their statement page"
        + (f" — {slip_total:,.0f} XAF banked per the deposit slip attached "
           "to this email" if slip_total else "")
        + f".\n\nDeposit reference(s): {', '.join(refs) or 'none given'}\n"
        f"Airwaybills covered ({len(lines or [])}):\n{detail}\n\n"
        f"The reconciliation is already matched and waiting for review "
        f"here: /tools/bit-cash-ar/recon/{recon_token}\n\n"
        f"This message is the email evidence of the submission — keep it "
        f"in this mailbox.")
    first = (slip_paths or [None])[0]
    attachment = Path(first[0]) if first else None
    try:
        emailer.send_via_smtp(
            smtp, to, f"Evidence copy — deposit reported by {account} "
                      f"({recon_token})",
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
    slip_shas = []
    for part in msg.iter_attachments():
        fname = part.get_filename() or ""
        ext = Path(fname).suffix.lower()
        payload = part.get_payload(decode=True) or b""
        if not payload:
            continue
        if ext in _SLIP_EXT and len(payload) < _MIN_SLIP_BYTES:
            continue                    # signature logos etc. — not a slip
        if ext in _SLIP_EXT:
            # The same deposit slip is never accepted twice — even by mail.
            prior = find_prior_deposit(
                sha=hashlib.sha256(payload).hexdigest())
            if prior:
                _quarantine(log, msg, "the attached deposit slip was "
                                      f"already submitted on "
                                      f"{prior.get('at', '—')} — rejected")
                _save_mail_log(log)
                return {"status": "quarantined", "reason": "duplicate"}
        dest = UPLOAD_DIR / f"iromail_{uuid.uuid4().hex[:8]}{ext}"
        dest.write_bytes(payload)
        if ext in _XLS_EXT and evidence_path is None:
            evidence_path = dest
        elif ext in _SLIP_EXT:
            slips.append((dest, fname or dest.name))
            slip_shas.append(hashlib.sha256(payload).hexdigest())

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

    all_refs = refs or [ln["reference"] for ln in lines
                        if ln.get("reference")]
    # Claim the deposits ATOMICALLY before creating the sandbox — two
    # racing copies of the same mail cannot both pass.
    for sha in slip_shas:
        prior = claim_deposit(sha, account, None, "",
                              all_refs[0] if all_refs else "", "email")
        if prior:
            _quarantine(log, msg, "the attached deposit slip was already "
                                  f"submitted on {prior.get('at', '—')} — "
                                  "rejected")
            _save_mail_log(log)
            return {"status": "quarantined", "reason": "duplicate"}
    token = create_submission(
        account, lines=lines, evidence_path=evidence_path,
        slip_paths=slips, references=all_refs,
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
