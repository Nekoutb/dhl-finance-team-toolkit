"""Cron entry point: read the operator mailbox and turn PAYREF emails into
reconciliation sandboxes. Safe to run any time — does nothing when IMAP is
not configured/enabled in Settings, processes each message at most once,
and quarantines anything it does not trust.

Install on the server (see deploy/finance-toolkit-mail.cron):
    */5 * * * * www-data cd /opt/finance-toolkit && \
        .venv/bin/python scripts/poll_operator_mail.py >> \
        data/iro_mail_cron.log 2>&1
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config  # noqa: E402
from app.tools import iro  # noqa: E402


def main():
    result = iro.poll_mailbox(load_config().get("imap"))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not result.get("ok"):
        print(f"[{stamp}] skipped: {result.get('error')}")
        return
    if result["seen"]:
        print(f"[{stamp}] {result['seen']} message(s): "
              f"{result['created']} created, "
              f"{result['quarantined']} quarantined, "
              f"{result['duplicates']} duplicate(s)")


if __name__ == "__main__":
    main()
