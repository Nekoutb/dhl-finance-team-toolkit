"""Audit fix 6 — friendly 500 page with reference code + branded 404."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402


@main.app.get("/_test_boom")
def _boom():
    raise RuntimeError("synthetic failure for the error-page test")


client = TestClient(main.app, raise_server_exceptions=False)

r = client.get("/_test_boom")
assert r.status_code == 500
assert "Something went wrong" in r.text
m = re.search(r"<code>([0-9A-F]{8})</code>", r.text)
assert m, "reference code missing"
assert "Internal Server Error" not in r.text
print(f"ok: 500 shows friendly page with reference code ({m.group(1)})")

r = client.get("/definitely-not-a-page", headers={"accept": "text/html"})
assert r.status_code == 404
assert "Page not found" in r.text and "dashboard" in r.text
print("ok: 404 shows branded page")

# API-style 404s (no HTML accept) keep the JSON contract.
r = client.get("/definitely-not-a-page", headers={"accept": "application/json"})
assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
print("ok: non-HTML 404 still returns JSON")

print("\nALL ERROR-PAGE TESTS PASSED")
