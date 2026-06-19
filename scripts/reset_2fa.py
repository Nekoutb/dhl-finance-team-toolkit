"""Emergency two-factor admin tool (run on the server as the app user).

  # list who is enrolled
  sudo -u www-data /opt/finance-toolkit/.venv/bin/python scripts/reset_2fa.py

  # reset ONE user (they re-enroll on next sign-in)
  sudo -u www-data /opt/finance-toolkit/.venv/bin/python scripts/reset_2fa.py jane.doe

  # kill-switch: turn the 2FA requirement OFF for everyone (last resort if the
  # login flow ever locks the whole team out)
  sudo -u www-data /opt/finance-toolkit/.venv/bin/python scripts/reset_2fa.py --disable-all
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import load_config, save_user_config  # noqa: E402
from app.services import auth  # noqa: E402


def main(argv):
    if "--disable-all" in argv:
        save_user_config({"auth": {"two_factor": False}})
        print("Two-factor requirement DISABLED for all users (kill-switch).")
        return
    auth_cfg = load_config().get("auth", {})
    if not argv:
        print("Users and 2FA status:")
        for u in auth.list_users():
            state = "enrolled" if auth.totp_confirmed(auth_cfg, u) else "not enrolled"
            print(f"  {u}: {state}")
        print("\nUsage: python scripts/reset_2fa.py <username>   (or --disable-all)")
        return
    username = argv[0]
    if username not in auth.list_users():
        print(f"Unknown user: {username}")
        raise SystemExit(1)
    if auth.reset_totp(username):
        print(f"2FA reset for '{username}'. They will enroll again on next sign-in.")
    else:
        print(f"'{username}' had no 2FA enrollment to reset.")


if __name__ == "__main__":
    main(sys.argv[1:])
