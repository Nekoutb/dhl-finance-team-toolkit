"""Staff authentication for public deployment.

Self-contained signed-cookie auth (no external session store):
- passwords are PBKDF2-HMAC-SHA256 hashes stored in config.json;
- the session cookie is an itsdangerous-signed token with a 12h lifetime.

Auth is ENFORCED ONLY when config.auth.enabled is true and at least one user
exists — so the local/dev experience stays login-free, while a public
deployment (set up via scripts/set_password.py) is locked down.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..config import CONFIG_PATH

COOKIE = "ft_session"
MAX_AGE = 60 * 60 * 12          # 12 hours
_ROUNDS = 200_000

# Paths reachable without logging in: the login flow, static assets, customer
# remittance links (token-secured), generated-file downloads (token names),
# and the health probe.
PUBLIC_PREFIXES = ("/login", "/logout", "/static/", "/portal/", "/download/",
                   "/healthz", "/favicon.ico")


def hash_password(password, salt=None):
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ROUNDS)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password, stored):
    try:
        scheme, b64salt, b64dk = (stored or "").split("$")
        if scheme != "pbkdf2":
            return False
        salt = base64.b64decode(b64salt)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ROUNDS)
        return hmac.compare_digest(base64.b64encode(dk).decode(), b64dk)
    except (ValueError, AttributeError):
        return False


def _serializer(secret):
    return URLSafeTimedSerializer(secret or "insecure-dev", salt="ft-auth")


def issue(secret, username):
    return _serializer(secret).dumps({"u": username})


def read_token(secret, token, max_age=MAX_AGE):
    if not token:
        return None
    try:
        return _serializer(secret).loads(token, max_age=max_age).get("u")
    except (BadSignature, SignatureExpired):
        return None


def is_public(path):
    return path == "/login" or any(
        path == p.rstrip("/") or path.startswith(p) for p in PUBLIC_PREFIXES)


def enabled(auth_cfg):
    return bool(auth_cfg.get("enabled") and auth_cfg.get("users"))


# --- User administration (writes config.json directly so deletions stick;
#     config.save_user_config deep-merges, which can't remove a user) ---------
def _load_raw():
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_raw(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                           encoding="utf-8")


def list_users():
    return sorted(_load_raw().get("auth", {}).get("users", {}).keys())


def add_user(username, password, secure_cookies=None):
    """Create/replace a user. Enables auth and ensures a secret key exists.
    secure_cookies: set on first enable (True for HTTPS, False for local HTTP)."""
    username = (username or "").strip()
    if not username or not password:
        return False
    cfg = _load_raw()
    a = cfg.setdefault("auth", {})
    a.setdefault("users", {})[username] = hash_password(password)
    a["enabled"] = True
    if not a.get("secret_key"):
        a["secret_key"] = secrets.token_urlsafe(48)
    if "secure_cookies" not in a and secure_cookies is not None:
        a["secure_cookies"] = bool(secure_cookies)
    _save_raw(cfg)
    return True


def delete_user(username):
    cfg = _load_raw()
    users = cfg.get("auth", {}).get("users", {})
    if username in users and len(users) > 1:   # never remove the last user
        del users[username]
        _save_raw(cfg)
        return True
    return False


def change_password(username, new_password):
    if not new_password:
        return False
    cfg = _load_raw()
    users = cfg.get("auth", {}).get("users", {})
    if username in users:
        users[username] = hash_password(new_password)
        _save_raw(cfg)
        return True
    return False
