"""Vendor registry for NIU verification + tax-clearance-certificate controls.

Vendors arrive pasted as lines OR as an Excel file. Each line/row carries:
vendor name, taxpayer ID (NIU — recognised by shape), tax clearance
certificate reference, certificate ISSUE date, and the vendor's email.

Controls (per the team's payment policy):
- a tax clearance certificate is valid for CERT_VALID_MONTHS (3) from issue;
- a vendor may only be paid when the certificate is valid at the time of
  payment AND the NIU is ACTIVE on the DGI platform (payment_clearance()).

Stored in data/vendors.json keyed by NIU.
"""
import calendar
import json
import re
from datetime import date, datetime
from threading import Lock

from ..config import DATA_DIR

_PATH = DATA_DIR / "vendors.json"
CERT_DIR = DATA_DIR / "certificates"      # stored certificate PDFs (evidence)
_lock = Lock()


def save_cert_file(niu, raw_bytes):
    """Persist a certificate PDF as the evidence on file for this vendor."""
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    path = CERT_DIR / f"{str(niu).strip().upper()}.pdf"
    path.write_bytes(raw_bytes)
    return path.name

NIU_RE = re.compile(r"^[A-Za-z][0-9]{8,12}[A-Za-z]?$")
DATE_RE = re.compile(r"^\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4}$|^\d{4}-\d{2}-\d{2}$")
CERT_VALID_MONTHS = 3
EXPIRY_WARNING_DAYS = 14


def add_months(d, months=CERT_VALID_MONTHS):
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def parse_date_token(value):
    """Parse a date in common layouts -> ISO string, or None."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value or "").strip().split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d.%m.%Y", "%d-%m-%Y",
                "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def cert_info(vendor, as_of=None):
    """Certificate validity: {state, issued, expires, days_left}.

    state: 'none' (no issue date recorded), 'valid', 'expiring' (valid but
    within EXPIRY_WARNING_DAYS of expiry), or 'expired'.
    """
    as_of = as_of or date.today()
    issued_raw = vendor.get("cert_date") or ""
    if not issued_raw:
        return {"state": "none", "issued": "", "expires": "", "days_left": None}
    try:
        issued = date.fromisoformat(issued_raw[:10])
    except ValueError:
        return {"state": "none", "issued": issued_raw, "expires": "",
                "days_left": None}
    expires = add_months(issued)
    days_left = (expires - as_of).days
    if days_left < 0:
        state = "expired"
    elif days_left <= EXPIRY_WARNING_DAYS:
        state = "expiring"
    else:
        state = "valid"
    return {"state": state, "issued": issued.isoformat(),
            "expires": expires.isoformat(), "days_left": days_left}


def payment_clearance(vendor, as_of=None):
    """The payment control: certificate valid now AND NIU active on DGI."""
    cert = cert_info(vendor, as_of)
    reasons = []
    status = vendor.get("status", "pending")
    if status != "active":
        labels = {"inactive": "NIU is INACTIVE on the tax platform",
                  "not_found": "NIU not found on the tax platform",
                  "error": "NIU check failed — re-verify",
                  "pending": "NIU not yet verified"}
        reasons.append(labels.get(status, f"NIU status: {status}"))
    if cert["state"] == "none":
        reasons.append("no tax clearance certificate issue date recorded")
    elif cert["state"] == "expired":
        reasons.append(f"tax clearance certificate expired {cert['expires']}")
    ok = not reasons
    if ok and cert["state"] == "expiring":
        reasons.append(f"certificate expires {cert['expires']} "
                       f"({cert['days_left']} day(s) left) — renew now")
    return {"ok": ok, "cert": cert, "reasons": reasons}


def _load():
    if _PATH.exists():
        try:
            return json.loads(_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save(data):
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                     encoding="utf-8")


def looks_like_niu(token):
    return bool(NIU_RE.fullmatch(str(token).strip()))


def parse_lines(text):
    """Parse pasted vendor lines -> (vendors, rejected_lines).

    Per line: NIU recognised by shape, email by '@', certificate issue date by
    date shape; the longest remaining chunk is the vendor name and whatever is
    left becomes the certificate reference.
    """
    vendors, rejected = [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"[\t;,]", line) if p.strip()]
        if not parts:
            continue
        niu = next((p for p in parts if looks_like_niu(p)), None)
        if not niu:
            rejected.append(raw)
            continue
        rest = [p for p in parts if p != niu]
        email = next((p for p in rest if "@" in p), "")
        rest = [p for p in rest if p != email]
        cert_date = next((parse_date_token(p) for p in rest
                          if DATE_RE.fullmatch(p)), None)
        rest = [p for p in rest if not DATE_RE.fullmatch(p)]
        name = max(rest, key=len) if rest else ""
        cert = " ".join(p for p in rest if p != name)
        vendors.append({"name": name, "niu": niu.upper(), "certificate": cert,
                        "cert_date": cert_date or "", "email": email})
    return vendors, rejected


# Column-name fragments for the Excel vendor list upload.
_XL_CANDIDATES = {
    "name": ["vendor name", "supplier name", "vendor", "supplier",
             "fournisseur", "raison sociale", "name"],
    "niu": ["taxpayer id", "tax payer id", "tax id", "niu", "contribuable",
            "matricule"],
    "certificate": ["certificate", "clearance", "attestation", "anr", "cert"],
    "cert_date": ["issue date", "date of issue", "issued", "certificate date",
                  "delivr", "emission", "émission", "valid from", "date"],
    "email": ["email", "e-mail", "mail", "courriel"],
}


def parse_workbook(path):
    """Parse an uploaded Excel vendor list -> (vendors, rejected_count)."""
    from . import excel_reader

    parsed = excel_reader.read_transactions(path)
    header = parsed["header"]
    lowered = {h: h.lower() for h in header}
    used, mapping = set(), {}
    for field, cands in _XL_CANDIDATES.items():
        for cand in cands:
            match = next((h for h in header
                          if h not in used and cand in lowered[h]), None)
            if match:
                mapping[field] = match
                used.add(match)
                break

    vendors, rejected = [], 0
    for row in parsed["rows"]:
        data = row["data"]
        g = lambda f: data.get(mapping[f]) if f in mapping else None  # noqa: E731
        niu = str(g("niu") or "").strip()
        if not looks_like_niu(niu):  # fall back: any NIU-shaped cell in the row
            niu = next((str(v).strip() for v in data.values()
                        if looks_like_niu(str(v).strip())), "")
        if not niu:
            rejected += 1
            continue
        vendors.append({
            "name": str(g("name") or "").strip(),
            "niu": niu.upper(),
            "certificate": str(g("certificate") or "").strip(),
            "cert_date": parse_date_token(g("cert_date")) or "",
            "email": str(g("email") or "").strip(),
        })
    return vendors, rejected


def upsert_many(parsed):
    """Add/update vendors; keeps existing verification status."""
    added = updated = 0
    with _lock:
        data = _load()
        for v in parsed:
            key = v["niu"]
            if key in data:
                data[key]["name"] = v["name"] or data[key]["name"]
                for field in ("certificate", "cert_date", "email"):
                    if v.get(field):
                        data[key][field] = v[field]
                updated += 1
            else:
                data[key] = {
                    "name": v["name"], "niu": key,
                    "certificate": v.get("certificate", ""),
                    "cert_date": v.get("cert_date", ""),
                    "email": v.get("email", ""),
                    "status": "pending", "message": "",
                    "raison_sociale": "", "sigle": "", "cni_rc": "",
                    "activite": "", "regime": "", "centre": "",
                    "checked_at": "",
                    "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                added += 1
        _save(data)
    return added, updated


def apply_certificate(parsed, cert_file="", enforce_newer=True):
    """Record a parsed tax clearance certificate (acf_reader.read_acf output).

    The certificate is the source of truth: its issue date, reference and
    email overwrite manual entries. Unknown NIUs auto-create the vendor using
    the taxpayer name from the document.

    Replacement rule (``enforce_newer``): when the vendor already has a
    certificate, the new one must expire LATER than the one on file — i.e. its
    validity period extends beyond the current one. A non-newer certificate is
    rejected and the one on file is kept. A successful, newer certificate also
    clears any "reminded — awaiting update" flag (the vendor has responded).

    Returns 'created', 'updated', or 'not_newer'.
    """
    key = parsed["niu"].upper()
    new_issue = parsed.get("issue_date", "")
    with _lock:
        data = _load()
        v = data.get(key)
        if v and enforce_newer and v.get("cert_date") and new_issue:
            try:
                old_exp = add_months(date.fromisoformat(v["cert_date"][:10]))
                new_exp = add_months(date.fromisoformat(new_issue[:10]))
                if new_exp <= old_exp:
                    return "not_newer"
            except ValueError:
                pass
        outcome = "updated" if v else "created"
        if not v:
            v = data[key] = {
                "name": parsed.get("name", ""), "niu": key,
                "certificate": "", "cert_date": "", "email": "",
                "status": "pending", "message": "",
                "raison_sociale": "", "sigle": "", "cni_rc": "",
                "activite": "", "regime": "", "centre": "",
                "checked_at": "",
                "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
        v["cert_date"] = new_issue
        if parsed.get("reference"):
            v["certificate"] = parsed["reference"]
        if parsed.get("email"):
            v["email"] = parsed["email"]
        if parsed.get("name") and not v.get("name"):
            v["name"] = parsed["name"]
        v["cert_source"] = "certificate"
        if cert_file:
            v["cert_file"] = cert_file
        # A newer certificate = the vendor answered the reminder.
        if v.get("awaiting_update"):
            v["awaiting_update"] = False
            v["responded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        _save(data)
    return outcome


def mark_reminded(niu, lang="en"):
    """Record that a renewal reminder was sent; flag the vendor as awaiting a
    response until a newer certificate replaces the one on file."""
    with _lock:
        data = _load()
        v = data.get(str(niu).strip().upper())
        if not v:
            return False
        v["last_reminded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        v["reminded_lang"] = "fr" if str(lang).lower().startswith("fr") else "en"
        v["awaiting_update"] = True
        v.pop("responded_at", None)
        _save(data)
    return True


def reminded_awaiting():
    """Vendors reminded to renew but who have not yet sent a newer certificate."""
    return [v for v in all_vendors() if v.get("awaiting_update")]


def reminder_text(vendor, lang, organization, as_of=None):
    """Bilingual renewal reminder -> (subject, body). lang: 'en' | 'fr'."""
    cert = cert_info(vendor, as_of)
    name = vendor.get("name") or vendor.get("niu")
    niu = vendor.get("niu", "")
    end = cert.get("expires") or ""
    days = cert.get("days_left")
    fr = str(lang).lower().startswith("fr")
    if fr:
        if cert["state"] == "expired":
            status = f"a expiré le {end}"
        elif end:
            status = f"expire le {end}" + (f" ({days} jour(s) restant(s))"
                                           if days is not None else "")
        else:
            status = "ne comporte pas de date de validité enregistrée"
        subject = f"Attestation de Conformité Fiscale — version à jour requise ({name})"
        body = (
            f"Cher partenaire {name},\n\n"
            f"Nos dossiers indiquent que votre Attestation de Conformité "
            f"Fiscale (NIU {niu}) {status}.\n\n"
            "Une attestation à jour est requise. Conformément aux lois et "
            "règlements fiscaux en vigueur, une attestation de conformité "
            "fiscale est valable trois (3) mois à compter de sa date de "
            "délivrance, et nous ne sommes pas en mesure de traiter le moindre "
            "paiement sans une attestation en cours de validité.\n\n"
            "Merci de nous faire parvenir votre attestation actualisée dans "
            "les meilleurs délais afin que nous puissions reprendre le "
            "traitement de vos paiements.\n\n"
            f"Cordialement,\n{organization} — Service Financier")
    else:
        if cert["state"] == "expired":
            status = f"expired on {end}"
        elif end:
            status = f"expires on {end}" + (f" ({days} day(s) left)"
                                            if days is not None else "")
        else:
            status = "has no recorded validity date"
        subject = f"Tax clearance certificate — updated version required ({name})"
        body = (
            f"Dear {name},\n\n"
            f"Our records show that your tax clearance certificate "
            f"(Attestation de Conformité Fiscale), taxpayer ID {niu}, "
            f"{status}.\n\n"
            "An updated certificate is required. In compliance with the "
            "applicable tax laws and regulations, a tax clearance certificate "
            "is valid for three (3) months from its date of issue, and we are "
            "unable to process any payment without a currently valid "
            "certificate.\n\n"
            "Please send us your updated tax clearance certificate as soon as "
            "possible so that we can resume processing your payments.\n\n"
            f"Kind regards,\n{organization} — Finance")
    return subject, body


def set_fields(niu, **fields):
    """Inline edit of a vendor's stored fields (e.g. cert_date, email)."""
    with _lock:
        data = _load()
        v = data.get(str(niu).strip().upper())
        if not v:
            return False
        for key, value in fields.items():
            if key in ("cert_date", "email", "certificate", "name"):
                v[key] = str(value or "").strip()
        if "cert_date" in fields:
            v["cert_source"] = "manual"
        _save(data)
    return True


def apply_result(result):
    with _lock:
        data = _load()
        v = data.get(result["niu"])
        if not v:
            return
        for field in ("status", "message", "raison_sociale", "sigle", "cni_rc",
                      "activite", "regime", "centre", "checked_at"):
            v[field] = result.get(field, v.get(field, ""))
        _save(data)


def all_vendors():
    """Vendors sorted: problems first, then pending, then active."""
    order = {"inactive": 0, "error": 1, "not_found": 2, "pending": 3, "active": 4}
    return sorted(_load().values(),
                  key=lambda v: (order.get(v["status"], 9), v["name"].lower()))


def pending_nius():
    return [v["niu"] for v in _load().values() if v["status"] == "pending"]


def all_nius():
    return list(_load().keys())


def delete(niu):
    with _lock:
        data = _load()
        if data.pop(str(niu).strip().upper(), None) is not None:
            _save(data)
