"""Audit fix 4 — static assets long-cached via versioned URLs; pages no-store."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

client = TestClient(main.app)

r = client.get("/static/styles.css")
assert r.status_code == 200
assert "immutable" in r.headers["cache-control"], r.headers["cache-control"]
assert "max-age=31536000" in r.headers["cache-control"]
print("ok: /static/* cached for a year (immutable)")

r = client.get("/")
assert "no-store" in r.headers["cache-control"]
assert f"styles.css?v={main.ASSET_V}" in r.text
assert f"app.js?v={main.ASSET_V}" in r.text
print("ok: pages stay no-store and reference versioned asset URLs")

print("\nALL STATIC-CACHE TESTS PASSED")
