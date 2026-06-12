"""Audit fix 1 — brute-force lockout + 12-char password policy."""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402
from app.config import CONFIG_PATH  # noqa: E402
from app.services import auth  # noqa: E402

client = TestClient(main.app)
cfg_snapshot = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None
USER = "lockout.test"
GOOD = "correct-horse-battery"

try:
    auth.clear_failures(USER)
    assert auth.add_user(USER, GOOD, secure_cookies=False, admin=True)

    # --- 1. five wrong passwords lock the account -----------------------------
    t0 = time.time()
    for n in range(auth.LOCKOUT_ATTEMPTS):
        r = client.post("/login", data={"username": USER, "password": "wrong"})
        assert r.status_code in (401, 429), r.status_code
    elapsed = time.time() - t0
    assert elapsed >= auth.FAIL_DELAY * auth.LOCKOUT_ATTEMPTS * 0.8, \
        f"failed attempts not slowed ({elapsed:.1f}s)"
    r = client.post("/login", data={"username": USER, "password": GOOD})
    assert r.status_code == 429 and "locked" in r.text.lower(), \
        (r.status_code, "correct password must NOT work while locked")
    print("ok: 5 wrong passwords -> locked 15 min (even the right password)")

    # --- 2. lock expires -> login works again ---------------------------------
    data = json.loads(auth._THROTTLE_PATH.read_text(encoding="utf-8"))
    data[USER]["until"] = time.time() - 1          # force-expire the lock
    auth._THROTTLE_PATH.write_text(json.dumps(data), encoding="utf-8")
    r = client.post("/login", data={"username": USER, "password": GOOD},
                    follow_redirects=False)
    assert r.status_code == 303, r.status_code
    assert not auth.locked_for(USER)
    print("ok: after expiry the correct password logs in and clears the slate")

    # --- 3. password policy: 12-char minimum ----------------------------------
    cookie = {auth.COOKIE: r.cookies.get(auth.COOKIE)}
    r = client.post("/settings/users/add",
                    data={"username": "shorty", "password": "elevenchars"},
                    cookies=cookie, follow_redirects=False)
    assert "at least 12" in r.headers["location"].replace("%20", " ")
    r = client.post("/settings/users/add",
                    data={"username": "shorty", "password": "twelve-chars!"},
                    cookies=cookie, follow_redirects=False)
    assert "added" in r.headers["location"]
    print("ok: 11-char password rejected, 12-char accepted")

finally:
    auth.clear_failures(USER)
    if cfg_snapshot is None:
        if CONFIG_PATH.exists():
            CONFIG_PATH.unlink()
    else:
        CONFIG_PATH.write_text(cfg_snapshot, encoding="utf-8")

print("\nALL LOCKOUT TESTS PASSED")
