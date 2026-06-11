"""Read a DGI "Attestation de Conformité Fiscale" (tax clearance certificate).

Extraction tuned against real ACF PDFs:
- NIU            : 14 characters, P or M + 12 digits + letter (top-left,
                   under the taxpayer's name)
- issue date     : the body sentence "… valable pour une durée de 3 mois à
                   compter du DD/MM/YYYY" is the legal validity anchor; the
                   top-right header date is the fallback (a mismatch between
                   the two is flagged, not silently resolved)
- ACF reference, taxpayer name, email, phone : captured for the registry.

Digital PDFs only (the DGI portal issues text-based PDFs); a scanned file
with no text layer is reported as unreadable rather than guessed at.
"""
import re
from datetime import datetime

NIU_RE = re.compile(r"\b([PM])\s?(\d{12})\s?([A-Z])\b")
DATE_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")
COMPTER_RE = re.compile(r"compter\s+du\s+(\d{2}/\d{2}/\d{4})", re.IGNORECASE)
REF_RE = re.compile(r"R[ée]f[ée]?rence\s+ACF\s*:?\s*(\d{6,})", re.IGNORECASE)
NAME_RE = re.compile(r"contribuable\s*:?\s*(.+?)\s+de\s+NIU", re.DOTALL | re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"\b237\d{9}\b")


def _to_iso(ddmmyyyy):
    try:
        return datetime.strptime(ddmmyyyy, "%d/%m/%Y").date().isoformat()
    except (TypeError, ValueError):
        return None


def read_acf(path):
    """Parse one certificate PDF. Never raises; returns {ok, error, ...}."""
    result = {"ok": False, "error": "", "niu": "", "issue_date": "",
              "reference": "", "name": "", "email": "", "phone": "",
              "date_mismatch": False}
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"could not open the PDF ({type(exc).__name__})"
        return result

    if not text.strip():
        result["error"] = ("no text layer — this looks like a scan/photo; "
                          "please use the digital PDF from the DGI")
        return result

    m = NIU_RE.search(text)
    if not m:
        result["error"] = ("no taxpayer ID found (expected 14 characters "
                          "starting with P or M)")
        return result
    result["niu"] = "".join(m.groups()).upper()

    body_date = COMPTER_RE.search(text)
    header_date = DATE_RE.search(text)
    issue = body_date.group(1) if body_date else \
        (header_date.group(1) if header_date else None)
    if not issue:
        result["error"] = "no issue date found on the certificate"
        return result
    iso = _to_iso(issue)
    if not iso:
        result["error"] = f"unreadable issue date '{issue}'"
        return result
    result["issue_date"] = iso
    if body_date and header_date and body_date.group(1) != header_date.group(1):
        result["date_mismatch"] = True

    ref = REF_RE.search(text)
    result["reference"] = f"ACF {ref.group(1)}" if ref else ""
    name = NAME_RE.search(text)
    if name:
        result["name"] = re.sub(r"\s+", " ", name.group(1)).strip(" :.,")
    email = EMAIL_RE.search(text)
    result["email"] = email.group(0) if email else ""
    phone = PHONE_RE.search(text)
    result["phone"] = phone.group(0) if phone else ""
    result["ok"] = True
    return result
