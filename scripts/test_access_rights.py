"""Administration — per-user, per-area access rights (read-only vs read/modify).

Snapshots config.json and ALWAYS restores it (atexit) so the app is never left
locked or with test users after the run.
"""
import atexit
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.config import CONFIG_PATH
from app.services import auth

ROOT = Path(__file__).resolve().parent.parent
PW = "longenough-pass"
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


# --- 1. pure access-level logic ---------------------------------------------
acfg = {"enabled": True, "users": {"a": "x", "e": "x"}, "admins": ["a"],
        "access": {"e": {"orange-cameroun": "read", "bank-statements": "modify"}}}
check("admin gets modify everywhere", auth.area_level(acfg, "a", "variance-analysis") == "modify")
check("employee read area", auth.area_level(acfg, "e", "orange-cameroun") == "read")
check("employee modify area", auth.area_level(acfg, "e", "bank-statements") == "modify")
check("employee ungranted area = none", auth.area_level(acfg, "e", "variance-analysis") == "none")
check("can_read true / can_modify false on read area",
      auth.can_read(acfg, "e", "orange-cameroun") and not auth.can_modify(acfg, "e", "orange-cameroun"))
check("anonymous = none when enabled", auth.area_level(acfg, None, "orange-cameroun") == "none")
check("login-free dev = full access", auth.area_level({"enabled": False}, None, "x") == "modify")

# --- 2. set up auth with an admin + an employee with scoped access -----------
raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
raw["auth"] = {
    "enabled": True, "secret_key": "test-secret-key", "secure_cookies": False,
    "users": {"adm": auth.hash_password(PW), "emp": auth.hash_password(PW)},
    "admins": ["adm"],
    "access": {"emp": {"orange-cameroun": "read", "bank-statements": "modify"}},
}
CONFIG_PATH.write_text(json.dumps(raw), encoding="utf-8")

from app.main import app  # noqa: E402 — import after config is set
client = TestClient(app)


def login(username):
    client.cookies.clear()
    r = client.post("/login", data={"username": username, "password": PW},
                    follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE in r.cookies, (username, r.status_code)
    client.cookies.set(auth.COOKIE, r.cookies[auth.COOKIE])


# --- 3. employee: scoped access enforced ------------------------------------
login("emp")
check("read area: GET allowed", client.get("/tools/orange-cameroun").status_code == 200)
r = client.post("/tools/orange-cameroun/upload", follow_redirects=False)
check("read area: POST (modify) blocked 403", r.status_code == 403 and "Read-only access" in r.text)
check("modify area: GET allowed", client.get("/tools/bank-statements").status_code == 200)
r = client.post("/tools/bank-statements/upload", data={"bank": ""},
                follow_redirects=False)
check("modify area: POST not blocked", r.status_code != 403)
r = client.get("/tools/variance-analysis", follow_redirects=False)
check("ungranted area: GET blocked 403", r.status_code == 403 and "No access to this area" in r.text)

# nav + landing show only granted areas
home = client.get("/").text
check("landing shows granted areas", "Orange Money" in home and "Bank Statements" in home)
check("landing hides ungranted areas",
      "Variance analysis" not in home and "CtP Portal" not in home)

# employee can't reach Administration
check("employee blocked from /admin", client.get("/admin").status_code == 403)
r = client.post("/admin/access", data={"username": "emp", "access:variance-analysis": "modify"},
                follow_redirects=False)
check("employee can't set access", r.status_code == 303 and "error" in r.headers["location"])
check("employee's access unchanged",
      "variance-analysis" not in auth.get_access(
          json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["auth"], "emp"))
check("employee blocked from /admin", client.get("/admin").status_code == 403)
# the home Dashboard is its own grantable area — emp has not been granted it
r = client.get("/", follow_redirects=False)
check("no dashboard access -> / redirects to a tool",
      r.status_code == 303 and "/tools/" in r.headers.get("location", ""))
print("ok: employee — scoped read/modify enforced; Administration locked")

# --- 4. admin: full access + can grant rights -------------------------------
login("adm")
check("admin reaches every area", client.get("/tools/variance-analysis").status_code == 200)
r = client.get("/admin")
check("admin sees Administration page", r.status_code == 200
      and "Administration" in r.text and "emp" in r.text and "Read &amp; modify" in r.text)
check("admin matrix includes the Dashboard area", "Dashboard (home overview)" in r.text)
r = client.post("/admin/access",
                data={"username": "emp", "access:dashboard": "read",
                      "access:bank-statements": "modify",
                      "access:orange-cameroun": "read",
                      "access:variance-analysis": "modify"},
                follow_redirects=False)
check("admin grants access -> redirect", r.status_code == 303)
new_access = auth.get_access(json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["auth"], "emp")
check("granted area persisted", new_access.get("variance-analysis") == "modify")
check("dashboard grant persisted", new_access.get("dashboard") == "read")
print("ok: admin — full access, can grant per-area rights (incl. Dashboard)")

# --- 5. the newly granted areas now work for the employee -------------------
login("emp")
check("newly granted area: GET allowed", client.get("/tools/variance-analysis").status_code == 200)
check("dashboard now granted -> home loads",
      client.get("/", follow_redirects=False).status_code == 200)
print("ok: employee gains access immediately after the grant")

# --- 6. deleting a user removes their access map -----------------------------
login("adm")
client.post("/settings/users/delete", data={"username": "emp", "redirect": "/admin"},
            follow_redirects=False)
check("deleted user gone", "emp" not in auth.list_users())
check("deleted user's access cleaned",
      "emp" not in (json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                    ["auth"].get("access", {})))
print("ok: removing a user also clears their access rights")

print("\nALL ACCESS-RIGHTS TESTS PASSED")
restore()
print("config.json restored")
