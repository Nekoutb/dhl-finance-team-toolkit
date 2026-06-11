"""Send the receipt by email, or build an .eml the user can open in Outlook."""
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


_SUBTYPES = {
    ".pdf": "pdf",
    ".xlsx": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _attach(msg, attachment_path):
    path = Path(attachment_path)
    msg.add_attachment(
        path.read_bytes(),
        maintype="application",
        subtype=_SUBTYPES.get(path.suffix.lower(), "octet-stream"),
        filename=path.name,
    )


def _to_header(to_addr):
    """Accept a single address or a list of addresses."""
    if isinstance(to_addr, (list, tuple, set)):
        return ", ".join(a for a in to_addr if a)
    return to_addr or ""


def send_via_smtp(smtp_cfg, to_addr, subject, body, attachment_path=None):
    """Send directly via SMTP. Raises on failure so the caller can report it."""
    msg = EmailMessage()
    msg["From"] = smtp_cfg.get("from_address") or smtp_cfg.get("username") or ""
    msg["To"] = _to_header(to_addr)
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment_path:
        _attach(msg, attachment_path)

    host = smtp_cfg["host"]
    port = int(smtp_cfg.get("port", 587))
    if port == 465:
        # Implicit SSL (e.g. Yahoo/Gmail on 465): TLS from the first byte.
        with smtplib.SMTP_SSL(host, port, timeout=30,
                              context=ssl.create_default_context()) as server:
            if smtp_cfg.get("username"):
                server.login(smtp_cfg["username"], smtp_cfg.get("password", ""))
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            if smtp_cfg.get("use_tls", True):
                server.starttls(context=ssl.create_default_context())
            if smtp_cfg.get("username"):
                server.login(smtp_cfg["username"], smtp_cfg.get("password", ""))
            server.send_message(msg)


def build_eml(out_path, from_addr, to_addr, subject, body, attachment_path=None):
    """Write a .eml file (double-click opens it in Outlook ready to send)."""
    msg = EmailMessage()
    msg["From"] = from_addr or ""
    msg["To"] = _to_header(to_addr)
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment_path:
        _attach(msg, attachment_path)
    Path(out_path).write_bytes(bytes(msg))
    return Path(out_path)
