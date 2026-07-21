"""Configuration loading for the Finance Team Toolkit.

Settings live in ``config.json`` at the project root (git-ignored, may hold
secrets). Anything missing falls back to ``DEFAULT_CONFIG`` below, so the app
runs out of the box and you only override what you need.
"""
import json
import os
from pathlib import Path

# Bump on every release so old-vs-new is visible in the footer of every page.
APP_VERSION = "v11.3 — 21 Jul 2026 · Remittance portal: each customer keeps ONE permanent link that always shows their latest position (uploading the AR transaction file — in the remittance portal or the CtP portal — refreshes it in place instead of minting new links); customers no longer tick — the tool auto-applies their payments to their oldest invoices (whole invoices first) and they only Confirm; Cash AR ageing now reads far more date-column names and formats (dash dates, Excel serials) and says which column it aged from, so the buckets stop looking frozen"

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "app_name": "Finance Team Toolkit",
    "organization": "DHL Finance Team",
    "orange_cameroun": {
        "company_label": "Orange Cameroun S.A. — Orange Money",
        "document_title": "Mobile Money Transaction Receipt",
        # The email address the team sends these receipts to.
        "default_recipient": "",
        "email_subject_template": "Orange Money Transaction — {customer}",
        "email_body_template": (
            "Hello,\n\n"
            "Please find attached the Orange Money transaction receipt"
            " for {customer}.\n\n"
            "Kind regards,\nFinance Team"
        ),
    },
    # Fill this in to send email directly. If disabled, the app instead
    # produces a ready-to-send .eml file you can open in Outlook.
    "smtp": {
        "enabled": False,
        "host": "",
        "port": 587,
        "use_tls": True,
        "username": "",
        "password": "",
        "from_address": "",
    },
    # MyDHLPay: the public scan-to-pay page. The USSD template + merchant
    # number the Pay buttons dial (Orange Money merchant payment).
    "mydhlpay": {
        "merchant": "675153953",
        "ussd_template": "*126*1*1*{merchant}*{amount}#",
    },
    # Operator mailbox (IRO returns): the dedicated inbox the tool READS to
    # ingest "PAYREF <account>" emails. Credentials live only in config.json.
    "imap": {
        "enabled": False,
        "host": "",
        "port": 993,
        "ssl": True,
        "username": "",
        "password": "",
        "folder": "INBOX",
    },
    # AI document reading (scanned invoices that have no text layer). The
    # Anthropic API key is pasted in Settings and lives only in config.json
    # (git-ignored) — never in the repo.
    "ai": {
        "api_key": "",
        "model": "claude-opus-4-8",
    },
    # Bank statements: the admin defines the banks (one slot each) in Settings.
    # Each configured bank holds exactly one current statement; re-uploading
    # overrides it. Empty = no slots yet (configure in Settings → Bank statements).
    "banks": [],
    # Staff login. OFF locally (login-free dev); turned ON for public
    # deployment via scripts/set_password.py. secure_cookies -> True behind
    # HTTPS so the session cookie is never sent in clear.
    "auth": {
        "enabled": False,
        "secret_key": "",
        "secure_cookies": False,
        "users": {},
    },
    # Cloudflare Turnstile — bot/abuse protection on the login form. Inactive
    # until enabled with a site key + secret key (created in the Cloudflare
    # dashboard). When off, the login page behaves exactly as before.
    "turnstile": {
        "enabled": False,
        "site_key": "",
        "secret_key": "",
    },
}


def _deep_merge(base, override):
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config():
    """Return the merged configuration (defaults + user config.json)."""
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            return _deep_merge(DEFAULT_CONFIG, user)
        except (json.JSONDecodeError, OSError):
            # Never let a broken config file take the app down.
            return _deep_merge(DEFAULT_CONFIG, {})
    return _deep_merge(DEFAULT_CONFIG, {})


def ensure_dirs():
    for directory in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def _config_write_lock():
    """Cross-process lock for config writes — several gunicorn workers (and
    admin actions) must never interleave a read-modify-write on the file
    that holds users, access maps and the session signing key."""
    import time
    import uuid as _uuid

    class _Lock:
        lockfile = CONFIG_PATH.with_suffix(".lock")

        def __enter__(self):
            deadline = time.time() + 3.0
            while True:
                try:
                    self.fd = os.open(self.lockfile,
                                      os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    return self
                # Windows raises PermissionError (not FileExistsError) when
                # the lockfile is mid-delete by the releasing thread — both
                # simply mean "contended, try again".
                except (FileExistsError, PermissionError):
                    try:
                        if time.time() - self.lockfile.stat().st_mtime > 10:
                            self.lockfile.unlink(missing_ok=True)
                            continue
                    except OSError:
                        pass
                    if time.time() > deadline:
                        self.fd = None
                        return self
                    time.sleep(0.03)

        def __exit__(self, *_exc):
            if self.fd is not None:
                os.close(self.fd)
                self.lockfile.unlink(missing_ok=True)
            return False
    _Lock.uuid = _uuid       # keep the import referenced
    return _Lock()


def write_config_file(payload):
    """ATOMIC config write: unique temp file + os.replace, so a reader can
    never see a half-written file (a torn read silently reverts the app to
    defaults — including the auth signing key — logging everyone out).
    On Windows a concurrent reader (or antivirus scan) can hold the target
    open for a moment and make os.replace throw PermissionError — retried
    briefly; the final attempt surfaces the error."""
    import time
    tmp = CONFIG_PATH.with_name(
        f"{CONFIG_PATH.name}.{os.getpid()}.{os.urandom(3).hex()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, CONFIG_PATH)
            return
        except PermissionError:
            time.sleep(0.01 * (attempt + 1))
    os.replace(tmp, CONFIG_PATH)


def save_user_config(updates):
    """Deep-merge ``updates`` into config.json (the git-ignored user config).

    Used by the in-app Settings page (e.g. SMTP credentials) so configuration
    is locked into the SaaS without hand-editing files. Takes effect on the
    next request — every sender calls load_config() per request. The whole
    read-modify-write runs under a cross-process lock and the write is
    atomic.
    """
    with _config_write_lock():
        current = {}
        if CONFIG_PATH.exists():
            try:
                current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                current = {}
        merged = _deep_merge(current, updates)
        write_config_file(merged)
    return load_config()
