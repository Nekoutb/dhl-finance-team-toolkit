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

# --- Admin: settings page shows user management; onboard + password change --
from app.services import auth as auth_admin

r = client.get("/settings")
check("settings shows staff users + your account",
      "Staff users" in r.text and "Your account" in r.text and "finance" in r.text)

# Onboard a new user
r = client.post("/settings/users/add",
                data={"username": "amadou", "password": "welcome123"},
                follow_redirects=False)
check("add user -> redirect", r.status_code == 303)
check("new user persisted (hashed)", "amadou" in auth_admin.list_users())

# Short password rejected
r = client.post("/settings/users/add", data={"username": "x", "password": "12"},
                follow_redirects=True)
check("short password rejected", "at least 6" in r.text)

# New user can log in
client.cookies.clear()
r = client.post("/login", data={"username": "amadou", "password": "welcome123"},
                follow_redirects=False)
check("new user can sign in", r.status_code == 303 and auth.COOKIE in r.cookies)
client.cookies.set(auth.COOKIE, r.cookies[auth.COOKIE])

# Change own password (wrong current -> rejected; correct -> changed)
r = client.post("/settings/account/password", follow_redirects=True, data={
    "current_password": "nope", "new_password": "newpass1", "confirm_password": "newpass1"})
check("wrong current password rejected", "current password is incorrect" in r.text)
r = client.post("/settings/account/password", follow_redirects=True, data={
    "current_password": "welcome123", "new_password": "newpass1",
    "confirm_password": "newpass1"})
check("password changed", "password has been changed" in r.text)
check("old password no longer works",
      not auth_admin.verify_password("welcome123",
          __import__("app.config", fromlist=["load_config"]).load_config()["auth"]["users"]["amadou"]))

# Can't delete the account you're signed in as
r = client.post("/settings/users/delete", data={"username": "amadou"},
                follow_redirects=True)
check("can't delete self", "delete the account you" in r.text.lower())

# Delete the other user (finance) is allowed (2 users exist)
r = client.post("/settings/users/delete", data={"username": "finance"},
                follow_redirects=True)
check("delete other user works", "finance" not in auth_admin.list_users())

# Now amadou is the last user -> can't be removed
client.cookies.clear()
r = client.post("/login", data={"username": "amadou", "password": "newpass1"},
                follow_redirects=False)
client.cookies.set(auth.COOKIE, r.cookies[auth.COOKIE])
r = client.post("/settings/users/delete", data={"username": "amadou"},
                follow_redirects=True)
check("last user can't be removed", "amadou" in auth_admin.list_users())

# Logout clears the cookie -> gated again
client.get("/logout", follow_redirects=False)
client.cookies.clear()
check("after logout: gated again",
      client.get("/", follow_redirects=False).status_code == 303)

print("\nALL AUTH CHECKS PASSED")
restore()
print("config.json restored (auth off)")
