"""Configuration loading for the Finance Team Toolkit.

Settings live in ``config.json`` at the project root (git-ignored, may hold
secrets). Anything missing falls back to ``DEFAULT_CONFIG`` below, so the app
runs out of the box and you only override what you need.
"""
import json
from pathlib import Path

# Bump on every release so old-vs-new is visible in the footer of every page.
APP_VERSION = "v10.1 — 19 Jul 2026 · Account Stop: stricter name matching (every distinctive word must appear — no more loose look-alike matches) + a Transaction-date-on-statement column in all three payment groups; credit-hold daily graph moved to the top of the CtP portal; Orange Money: customer payments by month (name, AR account, phone, per-month totals + Excel) and removable processed files; cheque unpresented graph now updates the moment bank statements are uploaded"

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


def save_user_config(updates):
    """Deep-merge ``updates`` into config.json (the git-ignored user config).

    Used by the in-app Settings page (e.g. SMTP credentials) so configuration
    is locked into the SaaS without hand-editing files. Takes effect on the
    next request — every sender calls load_config() per request.
    """
    current = {}
    if CONFIG_PATH.exists():
        try:
            current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            current = {}
    merged = _deep_merge(current, updates)
    CONFIG_PATH.write_text(json.dumps(merged, indent=2, ensure_ascii=False),
                           encoding="utf-8")
    return load_config()
