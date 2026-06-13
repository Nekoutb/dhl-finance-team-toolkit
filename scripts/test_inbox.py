"""Auto-read vendor certificate replies (IMAP + acf_reader mocked)."""
import sys
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()

from app.services import acf_reader, inbox, vendors  # noqa: E402

NIU = "M033445560002Y"            # unique to this test
UNKNOWN = "M999000111222Z"


def _scrub():
    vendors.delete(NIU)
    inbox._LOG.unlink(missing_ok=True)


# --- a fake IMAP server returning two messages (one with a PDF) -------------
class FakeIMAP:
    def __init__(self, messages):
        self._messages = messages          # {num: rfc822 bytes}

    def login(self, u, p):
        return ("OK", [b""])

    def select(self, mailbox, readonly=False):
        return ("OK", [b"1"])

    def search(self, charset, query):
        return ("OK", [b" ".join(self._messages.keys())])

    def fetch(self, num, spec):
        return ("OK", [(num + b" (RFC822)", self._messages[num])])

    def logout(self):
        return ("BYE", [b""])


def _msg(subject, sender, attach_pdf=None, mid="<m1@vendor>"):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = sender
    m["Message-ID"] = mid
    m.set_content("Please find our updated certificate attached.")
    if attach_pdf is not None:
        m.add_attachment(attach_pdf, maintype="application", subtype="pdf",
                         filename="ACF.pdf")
    return m.as_bytes()


_scrub()
real_imap = inbox.imaplib.IMAP4_SSL
real_read = acf_reader.read_acf
CFG = {"imap": {"enabled": True, "host": "imap.test", "port": 993,
                "username": "fin@test", "password": "x", "mailbox": "INBOX",
                "since_days": 30}}
try:
    today = date.today()
    older = (today - timedelta(days=20)).isoformat()
    newer = (today - timedelta(days=1)).isoformat()

    # seed an existing vendor with an older certificate
    assert vendors.apply_certificate(
        {"ok": True, "niu": NIU, "issue_date": older, "reference": "",
         "name": "BETA SARL", "email": "ap@beta.cm", "date_mismatch": False}) \
        == "created"

    # the inbound PDF bytes encode which issue date acf_reader should "read"
    def fake_read(path):
        raw = Path(path).read_bytes()
        if b"NEWERCERT" in raw:
            return {"ok": True, "niu": NIU, "issue_date": newer, "reference": "",
                    "name": "BETA SARL", "email": "", "date_mismatch": False}
        if b"OLDERCERT" in raw:
            return {"ok": True, "niu": NIU, "issue_date": older, "reference": "",
                    "name": "", "email": "", "date_mismatch": False}
        if b"UNKNOWNCERT" in raw:
            return {"ok": True, "niu": UNKNOWN, "issue_date": newer,
                    "reference": "", "name": "X", "email": "",
                    "date_mismatch": False}
        return {"ok": False, "error": "no text layer", "niu": ""}
    acf_reader.read_acf = fake_read

    # --- run 1: a NEWER cert from the known vendor is auto-applied ----------
    msgs = {b"1": _msg("Re: certificate", "BETA <ap@beta.cm>",
                       attach_pdf=b"%PDF NEWERCERT", mid="<n1@beta>")}
    inbox.imaplib.IMAP4_SSL = lambda h, p: FakeIMAP(msgs)
    s = inbox.check_now(CFG)
    assert s["ok"] and s["applied"] == 1, s
    v = next(x for x in vendors.all_vendors() if x["niu"] == NIU)
    assert vendors.cert_info(v)["issued"] == newer, "newer cert not applied"
    assert (vendors.CERT_DIR / f"{NIU}.pdf").exists(), "evidence PDF not stored"
    print("ok: newer certificate emailed by a known vendor is auto-applied")

    # --- run 1 repeat: same Message-ID is NOT reprocessed -------------------
    s = inbox.check_now(CFG)
    assert s["applied"] == 0 and s["scanned"] == 0, "message reprocessed!"
    print("ok: already-seen message is never applied twice (dedupe by Message-ID)")

    # --- run 2: an OLDER cert is read but rejected (validity must extend) ---
    msgs2 = {b"1": _msg("Re", "BETA <ap@beta.cm>",
                        attach_pdf=b"%PDF OLDERCERT", mid="<o1@beta>")}
    inbox.imaplib.IMAP4_SSL = lambda h, p: FakeIMAP(msgs2)
    s = inbox.check_now(CFG)
    assert s["not_newer"] == 1 and s["applied"] == 0, s
    v = next(x for x in vendors.all_vendors() if x["niu"] == NIU)
    assert vendors.cert_info(v)["issued"] == newer, "older cert wrongly replaced"
    print("ok: an older certificate is read but rejected, table unchanged")

    # --- run 3: an UNKNOWN taxpayer is not auto-created ---------------------
    msgs3 = {b"1": _msg("Hi", "x@x.cm", attach_pdf=b"%PDF UNKNOWNCERT",
                        mid="<u1@x>")}
    inbox.imaplib.IMAP4_SSL = lambda h, p: FakeIMAP(msgs3)
    s = inbox.check_now(CFG)
    assert s["unmatched"] == 1 and s["applied"] == 0
    assert UNKNOWN not in vendors.all_nius(), "inbound mail auto-created a vendor!"
    print("ok: unknown taxpayer ID is logged, never auto-created (anti-abuse)")

    # --- disabled config is a quiet no-op -----------------------------------
    s = inbox.check_now({"imap": {"enabled": False}})
    assert not s["ok"] and "not configured" in s["error"]
    print("ok: disabled IMAP is a safe no-op")

    # events were logged
    assert any(e["outcome"] == "applied" for e in inbox.recent_events())
    print("ok: automatic updates are recorded in the audit log")

finally:
    inbox.imaplib.IMAP4_SSL = real_imap
    acf_reader.read_acf = real_read
    _scrub()

print("\nALL INBOX TESTS PASSED")
