"""Vendor registry for NIU verification.

Vendors are pasted as lines (straight from Excel — tab, ';' or ',' separated):
    vendor name <sep> taxpayer ID (NIU) <sep> tax clearance certificate
Order is flexible: the NIU is recognised by shape (Cameroon NIU: letter +
digits + letter, up to 14 chars); the longest remaining text is the name and
whatever is left becomes the certificate reference.

Stored in data/vendors.json keyed by NIU.
"""
import json
import re
from datetime import datetime
from threading import Lock

from ..config import DATA_DIR

_PATH = DATA_DIR / "vendors.json"
_lock = Lock()

NIU_RE = re.compile(r"^[A-Za-z][0-9]{8,12}[A-Za-z]?$")


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
    """Parse pasted vendor lines -> (vendors, rejected_lines)."""
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
        # Longest remaining chunk = the vendor name; the rest = certificate ref.
        name = max(rest, key=len) if rest else ""
        cert = " ".join(p for p in rest if p != name)
        vendors.append({"name": name, "niu": niu.upper(), "certificate": cert})
    return vendors, rejected


def upsert_many(parsed):
    """Add/update pasted vendors; keeps existing verification status."""
    added = updated = 0
    with _lock:
        data = _load()
        for v in parsed:
            key = v["niu"]
            if key in data:
                data[key]["name"] = v["name"] or data[key]["name"]
                if v["certificate"]:
                    data[key]["certificate"] = v["certificate"]
                updated += 1
            else:
                data[key] = {
                    "name": v["name"], "niu": key,
                    "certificate": v["certificate"],
                    "status": "pending", "message": "",
                    "raison_sociale": "", "sigle": "", "cni_rc": "",
                    "activite": "", "regime": "", "centre": "",
                    "checked_at": "",
                    "added_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                added += 1
        _save(data)
    return added, updated


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
