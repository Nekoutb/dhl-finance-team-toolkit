"""Two-factor authentication (TOTP) + Cloudflare Turnstile login flow.

Covers: mandatory enrollment on first login, the enroll→confirm→session path,
the verify path on later logins, wrong-code rejection, wrong-password rejection,
the Turnstile gate (mocked), and admin reset. Self-manages config.json.
"""
import atexit
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import CONFIG_PATH  # noqa: E402
from app.services import auth  # noqa: E402

_pre = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None


def _restore():
    if _pre is not None:
        CONFIG_PATH.write_text(_pre, encoding="utf-8")
    elif CONFIG_PATH.exists():
        CONFIG_PATH.unlink()


atexit.register(_restore)

USER, PW = "jane", "password-123456"
CONFIG_PATH.write_text(json.dumps({"auth": {
    "enabled": True, "two_factor": True, "secret_key": "test-secret-key-abc123",
    "secure_cookies": False, "users": {USER: auth.hash_password(PW)},
    "admins": [USER]}}), encoding="utf-8")

import pyotp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app import main, config  # noqa: E402
from app.services import turnstile  # noqa: E402


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


def client():
    return TestClient(main.app, follow_redirects=False)


def code_for(user):
    secret = auth.totp_secret(config.load_config()["auth"], user)
    return pyotp.TOTP(secret).now() if secret else None


# --- 1. password OK but not enrolled -> forced to enrollment -----------------
c = client()
r = c.post("/login", data={"username": USER, "password": PW, "next": "/"})
check("correct password (unenrolled) -> redirect to enroll",
      r.status_code == 303 and r.headers["location"].startswith("/login/enroll"))
check("no full session issued yet", c.cookies.get(auth.COOKIE) is None)
check("pending cookie set", c.cookies.get(auth.PENDING_COOKIE) is not None)

# --- 2. wrong password -> 401, no pending -----------------------------------
cbad = client()
rb = cbad.post("/login", data={"username": USER, "password": "wrong-password-x"})
check("wrong password -> 401", rb.status_code == 401)
check("wrong password issues no pending cookie",
      cbad.cookies.get(auth.PENDING_COOKIE) is None)

# --- 3. enrollment page renders the QR + secret; confirm with a real code ----
r = c.get("/login/enroll")
check("enroll page renders", r.status_code == 200 and "<svg" in r.text)
r = c.post("/login/enroll", data={"code": "000000", "next": "/"})
check("enroll with a bad code is rejected", r.status_code == 401)
r = c.post("/login/enroll", data={"code": code_for(USER), "next": "/"})
check("enroll with the real code -> full session",
      r.status_code == 303 and c.cookies.get(auth.COOKIE) is not None)
check("user is now marked enrolled",
      auth.totp_confirmed(config.load_config()["auth"], USER))

# --- 4. an authenticated request now succeeds -------------------------------
home = c.get("/")
check("session works after enrollment", home.status_code == 200)

# --- 5. next login -> verify step (not enroll) ------------------------------
c2 = client()
r = c2.post("/login", data={"username": USER, "password": PW, "next": "/"})
check("enrolled user -> redirect to verify",
      r.status_code == 303 and r.headers["location"].startswith("/login/verify"))
r = c2.post("/login/verify", data={"code": "000000", "next": "/"})
check("verify with wrong code -> 401", r.status_code == 401)
r = c2.post("/login/verify", data={"code": code_for(USER), "next": "/"})
check("verify with real code -> full session",
      r.status_code == 303 and c2.cookies.get(auth.COOKIE) is not None)

# --- 6. a pending token can't be used as a real session --------------------
cf = client()
cf.post("/login", data={"username": USER, "password": PW})       # gets pending
pending = cf.cookies.get(auth.PENDING_COOKIE)
forged = client()
forged.cookies.set(auth.COOKIE, pending)
check("pending token rejected as a full session (redirected to login)",
      forged.get("/").status_code == 303)

# --- 7. Turnstile gate (mocked) --------------------------------------------
config.save_user_config({"turnstile": {
    "enabled": True, "site_key": "0xSITE", "secret_key": "0xSECRET"}})
_orig = turnstile.verify
turnstile.verify = lambda *a, **k: False
try:
    ct = client()
    r = ct.post("/login", data={"username": USER, "password": PW})
    check("Turnstile failure blocks login (400)", r.status_code == 400)
    check("Turnstile widget appears on the login page",
          "challenges.cloudflare.com/turnstile" in ct.get("/login").text)
    turnstile.verify = lambda *a, **k: True
    ct2 = client()
    r = ct2.post("/login", data={"username": USER, "password": PW,
                                 "cf-turnstile-response": "tok"})
    check("Turnstile success lets login proceed to 2FA",
          r.status_code == 303 and r.headers["location"].startswith("/login/verify"))
finally:
    turnstile.verify = _orig
    config.save_user_config({"turnstile": {"enabled": False}})

# --- 8. admin reset forces re-enrollment ------------------------------------
auth.reset_totp(USER)
check("reset clears enrollment",
      not auth.totp_confirmed(config.load_config()["auth"], USER))
c3 = client()
r = c3.post("/login", data={"username": USER, "password": PW})
check("after reset, user is sent back to enrollment",
      r.status_code == 303 and r.headers["location"].startswith("/login/enroll"))

# --- 9. open-redirect hardening on the next param --------------------------
check("absolute next rejected", main._safe_next("https://evil.com") == "/")
check("protocol-relative next rejected", main._safe_next("//evil.com") == "/")
check("backslash next rejected", main._safe_next("/\\evil.com") == "/")
check("local next preserved", main._safe_next("/tools/orange-cameroun") == "/tools/orange-cameroun")

print("\nALL 2FA + TURNSTILE TESTS PASSED")
_restore()
