"""Audit fix 9 — CSRF protection via strict Origin/Referer verification."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

client = TestClient(main.app)
LOGIN = {"username": "x", "password": "y"}

# --- 1. cross-site POST is rejected ------------------------------------------
r = client.post("/login", data=LOGIN, headers={"origin": "https://evil.example"})
assert r.status_code == 403 and "Request blocked" in r.text, r.status_code
print("ok: cross-site Origin rejected (403)")

# --- 2. sandboxed 'null' origin is rejected ----------------------------------
r = client.post("/login", data=LOGIN, headers={"origin": "null"})
assert r.status_code == 403
print("ok: Origin null rejected (sandboxed-iframe attack)")

# --- 3. Referer fallback (no Origin) -----------------------------------------
r = client.post("/login", data=LOGIN,
                headers={"referer": "https://evil.example/lure.html"})
assert r.status_code == 403
print("ok: cross-site Referer (no Origin) rejected")

# --- 4. same-origin POST passes the guard -------------------------------------
r = client.post("/login", data=LOGIN,
                headers={"origin": "http://testserver"})
assert r.status_code != 403, r.status_code   # 401 wrong password = passed guard
print("ok: same-origin POST passes (reaches the login handler)")

# matching referer also passes
r = client.post("/login", data=LOGIN,
                headers={"referer": "http://testserver/login?next=/"})
assert r.status_code != 403
print("ok: same-origin Referer passes")

# --- 5. headerless clients (tests/CLI) unaffected -----------------------------
r = client.post("/login", data=LOGIN)
assert r.status_code != 403
print("ok: non-browser POST (no Origin/Referer) unaffected")

# --- 6. GETs never blocked -----------------------------------------------------
r = client.get("/", headers={"origin": "https://evil.example"})
assert r.status_code == 200
print("ok: GETs are never blocked")

# --- 7. portal allocation flow keeps working same-origin ----------------------
r = client.get("/portal/doesnotexist", headers={"origin": "https://evil.example"})
assert r.status_code == 404           # GET untouched
r = client.post("/portal/doesnotexist/allocate", data={},
                headers={"origin": "https://evil.example"})
assert r.status_code == 403           # cross-site portal POST blocked
r = client.post("/portal/doesnotexist/allocate", data={},
                headers={"origin": "http://testserver"})
assert r.status_code == 404           # same-origin reaches the handler
print("ok: customer portal protected and functional")

print("\nALL CSRF TESTS PASSED")
