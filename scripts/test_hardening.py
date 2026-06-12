"""Audit fixes 7/13/14 — upload size limit, per-page titles, URL-encoded msgs."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.testutil import smtp_guard  # noqa: E402

smtp_guard()

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

client = TestClient(main.app)

# --- fix 7: oversized upload refused by the middleware (413) -----------------
big = main.MAX_UPLOAD_BYTES + 1
r = client.post("/tools/momo/upload",
                headers={"content-length": str(big)},
                files={"file": ("huge.xlsx", b"x")})  # header is what's checked
assert r.status_code == 413, r.status_code
assert "Upload too large" in r.text
# a normal small upload still works (not blocked by the guard)
r = client.get("/tools/momo")
assert r.status_code == 200
print("ok: >50MB upload refused (413); normal requests unaffected")

# compliance allows a larger batch ceiling
assert main.MAX_BATCH_UPLOAD_BYTES > main.MAX_UPLOAD_BYTES
r = client.post("/tools/invoice-compliance/upload",
                headers={"content-length": str(main.MAX_BATCH_UPLOAD_BYTES + 1)},
                files={"invoices": ("x.pdf", b"x")})
assert r.status_code == 413
print("ok: compliance batch has its own higher ceiling")

# --- fix 13: per-page browser titles ----------------------------------------
checks = {
    "/": "Dashboard ·",
    "/tools/ongoing-ctp-monitoring": "CtP monitoring ·",
    "/tools/variance-analysis": "Variance analysis ·",
    "/tools/invoice-compliance": "Invoice compliance ·",
    "/tools/vendor-niu": "Vendor NIU check ·",
}
for path, want in checks.items():
    r = client.get(path)
    assert f"<title>{want}" in r.text, (path, want)
print("ok: every page sets a distinct <title>")

# --- fix 14: redirect_msg URL-encodes & and ? safely ------------------------
resp = main.redirect_msg("/settings", error="boom & bust? yes")
loc = resp.headers["location"]
assert "boom+%26+bust%3F+yes" in loc or "boom%20%26%20bust%3F%20yes" in loc, loc
assert loc.count("?") == 1, "stray ? would break query parsing: " + loc
print("ok: redirect_msg encodes & and ? (single query separator)")

print("\nALL HARDENING TESTS PASSED")
