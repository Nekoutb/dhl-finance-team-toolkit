"""Staff authentication for public deployment.

Self-contained signed-cookie auth (no external session store):
- passwords are PBKDF2-HMAC-SHA256 hashes stored in config.json;
- the session cookie is an itsdangerous-signed token with a 12h lifetime;
- failed logins are throttled: 5 wrong passwords lock the username for
  15 minutes (file-backed so it holds across all gunicorn workers).

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
import time
from threading import Lock

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..config import CONFIG_PATH, DATA_DIR

COOKIE = "ft_session"
MAX_AGE = 60 * 60 * 12          # 12 hours
_ROUNDS = 200_000

MIN_PASSWORD_LEN = 12
LOCKOUT_ATTEMPTS = 5            # wrong passwords before the username locks
LOCKOUT_SECONDS = 15 * 60       # lock duration
FAIL_DELAY = 0.8                # seconds added to every failed attempt
_THROTTLE_PATH = DATA_DIR / "auth_throttle.json"
_throttle_lock = Lock()

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


# --- Brute-force throttle (file-backed: consistent across workers) -----------
def _load_throttle():
    try:
        return json.loads(_THROTTLE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_throttle(data):
    _THROTTLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _THROTTLE_PATH.write_text(json.dumps(data), encoding="utf-8")


def locked_for(username):
    """Seconds the username stays locked, or 0 if it may attempt a login."""
    key = (username or "").strip().lower()
    if not key:
        return 0
    with _throttle_lock:
        entry = _load_throttle().get(key)
    if not entry:
        return 0
    remaining = entry.get("until", 0) - time.time()
    return max(0, int(remaining))


def record_failure(username, ip=""):
    """Count a wrong password; lock the username after LOCKOUT_ATTEMPTS."""
    key = (username or "").strip().lower()
    if not key:
        return
    with _throttle_lock:
        data = _load_throttle()
        # Drop expired locks so the file never grows.
        now = time.time()
        data = {k: v for k, v in data.items()
                if v.get("until", 0) > now or v.get("fails", 0) > 0}
        entry = data.get(key, {"fails": 0, "until": 0})
        if entry.get("until", 0) <= now:        # previous lock expired
            entry["until"] = 0
            if entry.get("locked"):
                entry["fails"] = 0
                entry["locked"] = False
        entry["fails"] = entry.get("fails", 0) + 1
        entry["ip"] = ip
        if entry["fails"] >= LOCKOUT_ATTEMPTS:
            entry["until"] = now + LOCKOUT_SECONDS
            entry["locked"] = True
            entry["fails"] = 0
        data[key] = entry
        _save_throttle(data)


def clear_failures(username):
    key = (username or "").strip().lower()
    with _throttle_lock:
        data = _load_throttle()
        if key in data:
            del data[key]
            _save_throttle(data)


def enabled(auth_cfg):
    return bool(auth_cfg.get("enabled") and auth_cfg.get("users"))


def is_admin(auth_cfg, user):
    """Admins see Settings & user onboarding; employees don't.

    Local no-auth mode is admin (login-free dev). Legacy configs without an
    'admins' list treat every user as admin so nobody is locked out until
    the list is set explicitly.
    """
    if not enabled(auth_cfg):
        return True
    if not user:
        return False
    admins = auth_cfg.get("admins")
    if admins is None:
        return True
    return user in admins


# --- Per-area access rights (read-only vs read/modify) -----------------------
# Stored in config.auth.access = {username: {tool_slug: "read"|"modify"}}.
# A slug absent from the map = NO access. Admins (and login-free local mode)
# implicitly get full ("modify") access to every area.
ACCESS_READ = "read"
ACCESS_MODIFY = "modify"
_ACCESS_LEVELS = (ACCESS_READ, ACCESS_MODIFY)


def get_access(auth_cfg, username):
    """The {slug: level} access map for a user (empty for unknown/none)."""
    if not username:
        return {}
    return dict((auth_cfg.get("access") or {}).get(username, {}) or {})


def area_level(auth_cfg, user, slug):
    """A user's level for an area: 'modify' | 'read' | 'none'.

    Admins and login-free local mode get 'modify' on every area.
    """
    if is_admin(auth_cfg, user):
        return ACCESS_MODIFY
    if not user:
        return "none"
    level = get_access(auth_cfg, user).get(slug)
    return level if level in _ACCESS_LEVELS else "none"


def can_read(auth_cfg, user, slug):
    return area_level(auth_cfg, user, slug) in _ACCESS_LEVELS


def can_modify(auth_cfg, user, slug):
    return area_level(auth_cfg, user, slug) == ACCESS_MODIFY


def set_access(username, access_map):
    """Persist a user's {slug: level} access (writes config.json directly).

    Anything that isn't 'read'/'modify' is dropped (= no access to that area).
    """
    username = (username or "").strip()
    if not username:
        return False
    cfg = _load_raw()
    a = cfg.setdefault("auth", {})
    a.setdefault("access", {})[username] = {
        s: l for s, l in (access_map or {}).items() if l in _ACCESS_LEVELS}
    _save_raw(cfg)
    return True


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


def add_user(username, password, secure_cookies=None, admin=False):
    """Create/replace a user. Enables auth and ensures a secret key exists.
    secure_cookies: set on first enable (True for HTTPS, False for local HTTP).
    admin: grants access to Settings & onboarding. The first user (or any
    pre-'admins'-era user) stays an admin — the list is materialised from the
    existing users the first time a non-legacy user is added."""
    username = (username or "").strip()
    if not username or not password:
        return False
    cfg = _load_raw()
    a = cfg.setdefault("auth", {})
    if "admins" not in a:
        # Legacy users were all implicitly admins — keep them that way.
        a["admins"] = sorted(a.get("users", {}).keys())
    a.setdefault("users", {})[username] = hash_password(password)
    if admin and username not in a["admins"]:
        a["admins"].append(username)
    if not admin and username in a["admins"] and len(a["admins"]) > 1:
        a["admins"].remove(username)   # re-adding an admin as employee demotes
    if not a["admins"]:
        a["admins"] = [username]       # never end up with zero admins
    a["enabled"] = True
    if not a.get("secret_key"):
        a["secret_key"] = secrets.token_urlsafe(48)
    if "secure_cookies" not in a and secure_cookies is not None:
        a["secure_cookies"] = bool(secure_cookies)
    _save_raw(cfg)
    return True


def delete_user(username):
    cfg = _load_raw()
    a = cfg.get("auth", {})
    users = a.get("users", {})
    if username in users and len(users) > 1:   # never remove the last user
        del users[username]
        if username in a.get("admins", []):
            a["admins"].remove(username)
        if username in a.get("access", {}):
            del a["access"][username]
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
