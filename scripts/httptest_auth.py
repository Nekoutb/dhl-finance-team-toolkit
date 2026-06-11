"""Verify staff authentication: gate when enabled, public paths open, login
flow, logout. Snapshots config.json and ALWAYS restores it (atexit) so the
app is never left locked after the test."""
import atexit
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.config import CONFIG_PATH, save_user_config
from app.services import auth

ROOT = Path(__file__).resolve().parent.parent
pre = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None


def restore():
    if pre is not None:
        CONFIG_PATH.write_text(pre, encoding="utf-8")
    elif CONFIG_PATH.exists():
        CONFIG_PATH.unlink()


atexit.register(restore)


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# --- With auth OFF (default), everything is open ---------------------------
save_user_config({"auth": {"enabled": False, "users": {}}})
from app.main import app          # import after config is set
client = TestClient(app)
r = client.get("/", follow_redirects=False)
check("auth off: dashboard open (200)", r.status_code == 200)

# --- Enable auth with a known user -----------------------------------------
save_user_config({"auth": {
    "enabled": True, "secret_key": "test-secret-key", "secure_cookies": False,
    "users": {"finance": auth.hash_password("s3cret-pass")},
}})

r = client.get("/", follow_redirects=False)
check("gated: dashboard redirects to /login", r.status_code == 303
      and "/login" in r.headers.get("location", ""))
r = client.get("/tools/vendor-niu", follow_redirects=False)
check("gated: a tool redirects to /login", r.status_code == 303)

# Public paths stay open even when gated
check("login page open", client.get("/login").status_code == 200)
check("static open", client.get("/static/styles.css").status_code == 200)
check("healthz open", client.get("/healthz").status_code == 200)
check("portal path not gated (404, not redirect)",
      client.get("/portal/deadbeef", follow_redirects=False).status_code == 404)

# Wrong password rejected
r = client.post("/login", data={"username": "finance", "password": "wrong"},
                follow_redirects=False)
check("wrong password rejected (401)", r.status_code == 401)

# Correct password -> cookie -> access granted
r = client.post("/login", data={"username": "finance",
                "password": "s3cret-pass", "next": "/tools/vendor-niu"},
                follow_redirects=False)
check("login sets session cookie", r.status_code == 303
      and auth.COOKIE in r.cookies)
client.cookies.set(auth.COOKIE, r.cookies[auth.COOKIE])
check("authed: tool now reachable",
      client.get("/tools/vendor-niu").status_code == 200)
check("authed: topbar shows sign out", "/logout" in client.get("/").text)

# Logout clears the cookie -> gated again
client.get("/logout", follow_redirects=False)
client.cookies.clear()
check("after logout: gated again",
      client.get("/", follow_redirects=False).status_code == 303)

print("\nALL AUTH CHECKS PASSED")
restore()
print("config.json restored (auth off)")
