"""Auto-read vendor email replies and apply updated tax certificates.

When a vendor answers a renewal reminder with their new Attestation de
Conformité Fiscale attached, this service — polling the finance mailbox over
IMAP — reads the PDF, and IF its validity period extends beyond the one on
file, replaces it and updates the tracking table automatically (no human
upload). The extend-only rule is enforced by vendors.apply_certificate.

Safety: a certificate is only applied to a vendor that ALREADY exists in the
registry (we don't auto-create vendors from inbound mail). Every message is
deduped by Message-ID so nothing is ever applied twice, and the mailbox is
left untouched (messages are not deleted or flagged).

Credentials come from the `imap` config block; username/password fall back to
the SMTP mailbox (same account). Configure + enable under Settings.
"""
import email
import imaplib
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock

from ..config import DATA_DIR
from . import acf_reader, vendors

_LOG = DATA_DIR / "cert_inbox.json"
_lock = Lock()
MAX_PER_RUN = 60


def effective_cfg(cfg):
    """IMAP settings with SMTP fallback for host/user/password. None if off
    or insufficiently configured."""
    imap = (cfg or {}).get("imap") or {}
    if not imap.get("enabled"):
        return None
    smtp = (cfg or {}).get("smtp") or {}
    host = (imap.get("host") or "").strip()
    if not host:
        # Derive a sensible default from the SMTP host (smtp.X -> imap.X).
        sh = (smtp.get("host") or "").strip()
        host = ("imap." + sh.split(".", 1)[1]) if sh.startswith("smtp.") else sh
    user = (imap.get("username") or smtp.get("username") or "").strip()
    pwd = imap.get("password") or smtp.get("password") or ""
    if not (host and user and pwd):
        return None
    return {
        "host": host, "port": int(imap.get("port") or 993),
        "username": user, "password": pwd,
        "mailbox": imap.get("mailbox") or "INBOX",
        "since_days": int(imap.get("since_days") or 30),
    }


def _load_log():
    try:
        return json.loads(_LOG.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"last_checked": "", "processed_ids": [], "events": []}


def _save_log(log):
    log["processed_ids"] = log["processed_ids"][-2000:]
    log["events"] = log["events"][-200:]
    _LOG.parent.mkdir(parents=True, exist_ok=True)
    _LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2),
                    encoding="utf-8")


def last_checked():
    return _load_log().get("last_checked", "")


def recent_events(n=12):
    return list(reversed(_load_log().get("events", [])))[:n]


def test_connection(cfg):
    """Log in + select the mailbox. Returns (ok, message)."""
    creds = effective_cfg(cfg)
    if not creds:
        return False, ("IMAP is not configured/enabled — set the host and "
                       "credentials under Settings.")
    try:
        M = imaplib.IMAP4_SSL(creds["host"], creds["port"])
        try:
            M.login(creds["username"], creds["password"])
            typ, _ = M.select(creds["mailbox"], readonly=True)
            if typ != "OK":
                return False, f"could not open mailbox '{creds['mailbox']}'"
        finally:
            try:
                M.logout()
            except Exception:  # noqa: BLE001
                pass
    except imaplib.IMAP4.error as exc:
        return False, f"login failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"Connected to {creds['host']} as {creds['username']}."


def _pdf_parts(msg):
    """Yield (filename, bytes) for every PDF attachment in a message."""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = part.get_filename() or ""
        ctype = (part.get_content_type() or "").lower()
        if ctype == "application/pdf" or name.lower().endswith(".pdf"):
            payload = part.get_payload(decode=True)
            if payload:
                yield (name or "attachment.pdf", payload)


def _apply_pdf(raw, known_nius, verify_niu=None):
    """Read one PDF and apply it if it extends an existing vendor's cert.

    Returns (outcome, niu) where outcome ∈ unreadable / unmatched /
    not_newer / created / updated. (created only if the vendor pre-exists —
    here it means the vendor had no prior cert date.)
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
        tf.write(raw)
        tmp = Path(tf.name)
    try:
        parsed = acf_reader.read_acf(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    if not parsed.get("ok"):
        return "unreadable", parsed.get("niu", "")
    niu = parsed["niu"].upper()
    if niu not in known_nius:          # never auto-create from inbound mail
        return "unmatched", niu
    outcome = vendors.apply_certificate(parsed, enforce_newer=True)
    if outcome in ("created", "updated"):
        vendors.save_cert_file(niu, raw)
        if verify_niu:
            try:
                vendors.apply_result(verify_niu(niu))
            except Exception:  # noqa: BLE001 — never let DGI break the run
                pass
    return outcome, niu


def check_now(cfg, verify_niu=None, limit=MAX_PER_RUN):
    """Poll the mailbox once and auto-apply any newer certificates found.

    verify_niu: optional callable(niu)->dgi result, to refresh DGI status on a
    fresh certificate. Returns a summary dict (never raises)."""
    summary = {"ok": False, "error": "", "scanned": 0, "applied": 0,
               "not_newer": 0, "unmatched": 0, "unreadable": 0, "details": []}
    creds = effective_cfg(cfg)
    if not creds:
        summary["error"] = ("IMAP not configured/enabled — set it up under "
                            "Settings → Inbox.")
        return summary

    with _lock:
        log = _load_log()
        processed = set(log.get("processed_ids", []))
        known = set(vendors.all_nius())
        try:
            M = imaplib.IMAP4_SSL(creds["host"], creds["port"])
        except Exception as exc:  # noqa: BLE001
            summary["error"] = f"could not connect: {type(exc).__name__}"
            return summary
        try:
            M.login(creds["username"], creds["password"])
            M.select(creds["mailbox"])
            since = (datetime.now() - timedelta(days=creds["since_days"])) \
                .strftime("%d-%b-%Y")
            typ, data = M.search(None, f'(SINCE {since})')
            nums = data[0].split() if (typ == "OK" and data and data[0]) else []
            for num in nums[-limit:]:
                typ, msg_data = M.fetch(num, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                mid = (msg.get("Message-ID") or
                       f"no-id-{num.decode(errors='ignore')}").strip()
                if mid in processed:
                    continue
                pdfs = list(_pdf_parts(msg))
                if not pdfs:
                    processed.add(mid)        # nothing to do; don't re-scan
                    continue
                sender = (msg.get("From") or "")[:120]
                applied_here = False
                for _fname, raw in pdfs:
                    summary["scanned"] += 1
                    outcome, niu = _apply_pdf(raw, known, verify_niu)
                    if outcome in ("created", "updated"):
                        summary["applied"] += 1
                        applied_here = True
                        v = next((x for x in vendors.all_vendors()
                                  if x["niu"] == niu), {})
                        ci = vendors.cert_info(v)
                        log["events"].append({
                            "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "niu": niu, "from": sender, "outcome": "applied",
                            "new_expiry": ci.get("expires", ""),
                            "name": v.get("name", "")})
                    else:
                        summary[outcome] = summary.get(outcome, 0) + 1
                        if outcome in ("not_newer", "unmatched"):
                            log["events"].append({
                                "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                                "niu": niu, "from": sender, "outcome": outcome,
                                "new_expiry": "", "name": ""})
                processed.add(mid)
                if applied_here:
                    known = set(vendors.all_nius())
            summary["ok"] = True
        except imaplib.IMAP4.error as exc:
            summary["error"] = f"IMAP error: {exc}"
        except Exception as exc:  # noqa: BLE001
            summary["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                M.logout()
            except Exception:  # noqa: BLE001
                pass
        log["processed_ids"] = list(processed)
        log["last_checked"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _save_log(log)
    return summary
