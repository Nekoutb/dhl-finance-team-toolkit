"""Scheduled inbox poll — auto-apply vendor certificate replies.

Run by cron on the server (see deploy/DEPLOY.md). Reads the finance mailbox,
applies any newer tax certificates vendors have emailed back, refreshes their
DGI status, and updates the tracking table — all without human intervention.
No-ops quietly when IMAP is disabled in config.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config            # noqa: E402
from app.services import dgi, inbox           # noqa: E402


def main():
    cfg = load_config()
    if not (cfg.get("imap") or {}).get("enabled"):
        return 0
    summary = inbox.check_now(cfg, verify_niu=dgi.verify_niu)
    from datetime import datetime
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not summary["ok"]:
        print(f"[{stamp}] inbox poll error: {summary['error']}")
        return 1
    print(f"[{stamp}] inbox poll: scanned {summary['scanned']}, "
          f"applied {summary['applied']}, not_newer {summary['not_newer']}, "
          f"unmatched {summary['unmatched']}, unreadable {summary['unreadable']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
