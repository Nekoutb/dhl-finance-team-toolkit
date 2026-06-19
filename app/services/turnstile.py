"""Cloudflare Turnstile verification — bot/abuse protection on the login form.

Active only when enabled with a site key + secret key (created in the Cloudflare
dashboard). ``verify`` calls Cloudflare's siteverify endpoint:
- an explicit unsuccessful result (bot / bad / missing token) FAILS CLOSED;
- a network error reaching Cloudflare FAILS OPEN — Turnstile is a bot gate, not
  the credential check, so a Cloudflare outage must not lock staff out of their
  own accounts (the password + TOTP still protect every login).
"""
import logging

import httpx

_SITEVERIFY = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_log = logging.getLogger("uvicorn.error")


def active(cfg):
    """True when Turnstile is enabled AND both keys are configured."""
    t = (cfg or {}).get("turnstile", {}) or {}
    return bool(t.get("enabled") and t.get("site_key") and t.get("secret_key"))


def site_key(cfg):
    return ((cfg or {}).get("turnstile", {}) or {}).get("site_key", "")


def verify(secret, token, remoteip=""):
    """Verify a Turnstile token with Cloudflare. See module docstring for the
    fail-open/fail-closed policy."""
    if not token:
        return False                       # no challenge solved -> reject
    data = {"secret": secret, "response": token}
    if remoteip:
        data["remoteip"] = remoteip
    try:
        resp = httpx.post(_SITEVERIFY, data=data, timeout=10)
        return bool(resp.json().get("success"))
    except Exception as exc:  # noqa: BLE001
        _log.warning("Turnstile siteverify unreachable (%s) — allowing login; "
                     "password + 2FA still enforced.", exc)
        return True                        # fail OPEN on network error
