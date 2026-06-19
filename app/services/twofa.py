"""Time-based one-time passwords (TOTP) for staff two-factor authentication.

Secrets are base32 strings; codes are verified with a ±1 step window to tolerate
clock skew. Enrollment shows a QR encoding an otpauth:// URI that any
authenticator app (Google / Microsoft Authenticator, Authy, 1Password) reads.
"""
import io
import re

import pyotp
import qrcode
import qrcode.image.svg

ISSUER = "DHL Finance Toolkit"


def new_secret():
    """A fresh random base32 TOTP secret."""
    return pyotp.random_base32()


def verify(secret, code, window=1):
    """True if ``code`` is a currently-valid 6-digit TOTP for ``secret``."""
    if not secret or not code:
        return False
    code = str(code).strip().replace(" ", "")
    if not code.isdigit():
        return False
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=window)
    except Exception:  # noqa: BLE001 - never let a bad secret crash login
        return False


def provisioning_uri(secret, username, issuer=ISSUER):
    return pyotp.TOTP(secret).provisioning_uri(name=username or "user",
                                               issuer_name=issuer)


def qr_svg(uri):
    """Inline SVG QR for an otpauth URI (no PIL dependency)."""
    qr = qrcode.QRCode(box_size=9, border=2)
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    # Drop the XML prolog so the <svg> embeds cleanly inside the HTML page.
    return re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)
