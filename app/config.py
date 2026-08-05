"""Configuration loading for the Finance Team Toolkit.

Settings live in ``config.json`` at the project root (git-ignored, may hold
secrets). Anything missing falls back to ``DEFAULT_CONFIG`` below, so the app
runs out of the box and you only override what you need.
"""
import json
import os
from pathlib import Path

# Bump on every release so old-vs-new is visible in the footer of every page.
APP_VERSION = "v11.14 — 5 Aug 2026 · Cheque register: when a bank returns a whole statement page as one block, the register no longer quotes that block as the clearing line — the run of dates and the 120-digit amount are replaced by “not isolated”. The cheque stays matched, because the reference in the block is genuine; only the date and amount that belong to the page rather than the cheque are withheld. A properly read statement line always wins over a block."  # lint:country-ok (release note, not behaviour)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"
CONFIG_PATH = BASE_DIR / "config.json"

# Multi-country groundwork (v12). PLATFORM_DIR holds what belongs to the whole
# platform rather than to one country — the identity database, cross-country
# indexes and operational logs. TENANTS_DIR will hold one directory per country,
# each an exact clone of today's data/ layout. Neither is used for reads yet;
# they exist so the migration has a settled home to move things into.
PLATFORM_DIR = DATA_DIR / "platform"
TENANTS_DIR = DATA_DIR / "tenants"
IDENTITY_DB = Path(os.environ.get("FT_IDENTITY_DB")
                   or (PLATFORM_DIR / "identity.db"))

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
        # OFF by default — the scan-to-pay page and its Cash Reconciliation
        # section stay hidden and refuse requests until this is turned on.
        "enabled": False,
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
    # Branch CASH accounts: resellers raise airwaybills on these as well as on
    # their own account, so every operator statement carries them. Cameroon's
    # are CASHCM<branch>; another country sets its own prefix here.
    "cash_account_prefix": "CASHCM",
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


# --------------------------------------------------------------------------- #
# load_config cache
#
# load_config() runs on EVERY request (the middleware, then ~70 more call
# sites). Re-reading and re-parsing config.json each time is the app's biggest
# fixed per-request cost — and it grows with the user list, which lives in the
# same file. The cache below turns that into a single stat().
#
# Invalidation is a (mtime_ns, size, inode) stamp. Every writer goes through
# write_config_file(), which is a temp-file + os.replace — so the inode ALWAYS
# changes on a write and the stamp can never miss an update, even for two
# writes inside the same filesystem timestamp tick.
#
# The cached dict is returned BY REFERENCE, not copied — a defensive deep copy
# of a large config would cost about as much as the parse it avoids. Callers
# must therefore treat the result as READ-ONLY. Every mutator in the codebase
# already goes through auth._load_raw() (a separate raw read) instead, and
# scripts/test_config_cache.py asserts that no request mutates it.
# --------------------------------------------------------------------------- #
_config_cache = None            # (stamp, merged_dict) — rebound atomically
_config_parses = 0              # real parses done — asserted by the tests


def config_parse_count():
    """How many times the config has actually been read+parsed from disk.
    Lets a test prove a cache hit skipped the work, with no timing flake."""
    return _config_parses


def _config_stamp():
    """Identity of the config file right now, or None when it is absent."""
    try:
        st = CONFIG_PATH.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))


def load_config():
    """Return the merged configuration (defaults + user config.json).

    Cached against the file's stamp — treat the result as read-only.
    """
    global _config_cache, _config_parses
    stamp = _config_stamp()
    cached = _config_cache
    if cached is not None and cached[0] == stamp:
        return cached[1]
    _config_parses += 1
    if stamp is None:                       # no config.json (login-free dev)
        merged = _deep_merge(DEFAULT_CONFIG, {})
    else:
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            merged = _deep_merge(DEFAULT_CONFIG, user)
        except (json.JSONDecodeError, OSError):
            # Never let a broken config file take the app down. Cached against
            # the broken file's own stamp, so fixing it invalidates at once.
            merged = _deep_merge(DEFAULT_CONFIG, {})
    _config_cache = (stamp, merged)
    return merged


def invalidate_config_cache():
    """Drop the cached config — for tests, and for any writer that bypasses
    write_config_file()."""
    global _config_cache
    _config_cache = None


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
            invalidate_config_cache()
            return
        except PermissionError:
            time.sleep(0.01 * (attempt + 1))
    os.replace(tmp, CONFIG_PATH)
    invalidate_config_cache()


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
