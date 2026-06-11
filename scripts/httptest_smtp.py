"""Exercise the in-app SMTP settings (save, password retention, test send)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.config import CONFIG_PATH, load_config
from app.main import app
from app.services import emailer

client = TestClient(app)
pre = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None


def check(label, cond):
    print(f"[{'OK ' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(1)


# Settings page
r = client.get("/settings")
check("settings page 200", r.status_code == 200)
check("shows SMTP form", 'name="host"' in r.text and 'name="from_address"' in r.text)

# Save full settings (enabled)
r = client.post("/settings/smtp", data={
    "enabled": "on", "host": "smtp.office365.com", "port": "587",
    "use_tls": "on", "username": "finance.cm@test.example",
    "password": "s3cret-app-pass", "from_address": "finance.cm@test.example",
}, follow_redirects=True)
check("save -> ENABLED message", "ENABLED" in r.text)
cfg = load_config()
check("config.json locked in", cfg["smtp"]["enabled"] is True
      and cfg["smtp"]["host"] == "smtp.office365.com"
      and cfg["smtp"]["password"] == "s3cret-app-pass")
check("password NOT echoed in page", "s3cret-app-pass" not in r.text)
check("password masked placeholder", "saved" in r.text)

# Re-save with blank password -> stored one kept
r = client.post("/settings/smtp", data={
    "enabled": "on", "host": "smtp.office365.com", "port": "587",
    "use_tls": "on", "username": "finance.cm@test.example",
    "password": "", "from_address": "finance.cm@test.example",
}, follow_redirects=True)
check("blank password keeps stored one",
      load_config()["smtp"]["password"] == "s3cret-app-pass")

# Test-send: success path (SMTP mocked)
sent = {}
real_send = emailer.send_via_smtp
def fake_send(smtp_cfg, to_addr, subject, body, attachment_path=None):
    sent["to"] = to_addr
emailer.send_via_smtp = fake_send
try:
    r = client.post("/settings/smtp/test", data={"test_to": "me@test.example"},
                    follow_redirects=True)
finally:
    emailer.send_via_smtp = real_send
check("test send ok message", "SMTP works" in r.text and sent.get("to") == "me@test.example")

# Test-send: failure path surfaces the error
def boom(smtp_cfg, to_addr, subject, body, attachment_path=None):
    raise ConnectionError("connection refused")
emailer.send_via_smtp = boom
try:
    r = client.post("/settings/smtp/test", data={"test_to": "me@test.example"},
                    follow_redirects=True)
finally:
    emailer.send_via_smtp = real_send
check("test failure surfaced", "Test failed" in r.text and "ConnectionError" in r.text)

print("\nALL SMTP SETTINGS CHECKS PASSED")

# Restore config.json exactly as it was
if pre is not None:
    CONFIG_PATH.write_text(pre, encoding="utf-8")
else:
    CONFIG_PATH.unlink(missing_ok=True)
print("config.json restored")
