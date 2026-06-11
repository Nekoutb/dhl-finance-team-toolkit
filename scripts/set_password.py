"""Create/update a staff login and enable authentication.

    python scripts/set_password.py <username> <password> [--insecure-cookies]

Writes a PBKDF2 hash + a generated secret key into config.json, sets
auth.enabled = true, and (by default) marks cookies secure for HTTPS. Pass
--insecure-cookies only for local HTTP testing.
"""
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config, save_user_config
from app.services import auth


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        print(__doc__)
        raise SystemExit(1)
    username, password = args[0], args[1]
    secure = "--insecure-cookies" not in sys.argv

    auth_cfg = load_config().get("auth", {})
    secret = auth_cfg.get("secret_key") or secrets.token_urlsafe(48)
    users = dict(auth_cfg.get("users", {}))
    users[username] = auth.hash_password(password)

    save_user_config({"auth": {
        "enabled": True,
        "secret_key": secret,
        "secure_cookies": secure,
        "users": users,
    }})
    print(f"Staff login set for '{username}'. Auth is now ENABLED "
          f"(secure_cookies={secure}). Restart the server to apply.")
    print(f"Total users: {', '.join(users)}")


if __name__ == "__main__":
    main()
