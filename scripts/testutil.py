"""Shared test helpers.

smtp_guard(): snapshot config.json, force smtp.enabled=False for the test run
(so suites exercise the .eml fallback deterministically and NEVER send real
mail with the user's credentials), and restore the exact original config on
exit — even when a check fails (atexit survives SystemExit).
"""
import atexit
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import CONFIG_PATH, save_user_config


def smtp_guard():
    pre = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None

    def restore():
        if pre is not None:
            CONFIG_PATH.write_text(pre, encoding="utf-8")
        elif CONFIG_PATH.exists():
            CONFIG_PATH.unlink()

    atexit.register(restore)
    save_user_config({"smtp": {"enabled": False}})
