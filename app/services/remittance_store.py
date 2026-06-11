"""Persistence for the remittance portal.

Each daily GL upload becomes a *batch*; each customer in it becomes a *statement*
addressed by an unguessable token (the customer's private link). Allocations are
written back onto the statement and kept on file for future reference.

Layout under data/remittance/:
  batches/<batch_id>.json        — one upload: metadata + per-customer summary
  statements/<token>.json        — one customer statement (+ allocation once done)
"""
import json
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock

from ..config import DATA_DIR

_BASE = DATA_DIR / "remittance"
_STMT = _BASE / "statements"
_BATCH = _BASE / "batches"
_lock = Lock()


def _ensure():
    _STMT.mkdir(parents=True, exist_ok=True)
    _BATCH.mkdir(parents=True, exist_ok=True)


def new_token():
    return uuid.uuid4().hex


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _read(path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_statement(token, data):
    _ensure()
    with _lock:
        _write(_STMT / f"{token}.json", data)


def load_statement(token):
    if not token or not token.isalnum():
        return None
    return _read(_STMT / f"{token}.json")


def save_batch(batch_id, data):
    _ensure()
    with _lock:
        _write(_BATCH / f"{batch_id}.json", data)


def load_batch(batch_id):
    if not batch_id or not batch_id.isalnum():
        return None
    return _read(_BATCH / f"{batch_id}.json")


def delete_batch(batch_id):
    """Remove a batch and all its customer statements."""
    batch = load_batch(batch_id)
    if batch is None:
        return False
    for c in batch.get("customers", []):
        (_STMT / f"{c['token']}.json").unlink(missing_ok=True)
    (_BATCH / f"{batch_id}.json").unlink(missing_ok=True)
    return True


def list_batches():
    _ensure()
    out = [_read(p) for p in _BATCH.glob("*.json")]
    out = [b for b in out if b]
    out.sort(key=lambda b: b.get("created_at", ""), reverse=True)
    return out


# --- Admin settings (e.g. finance emails that receive each reconciliation) ---
_SETTINGS = _BASE / "settings.json"


def load_settings():
    data = _read(_SETTINGS) or {}
    data.setdefault("recipients", [])
    return data


def save_settings(data):
    _ensure()
    with _lock:
        _write(_SETTINGS, data)


# --- Customer contact details captured at confirmation (kept for reuse) ------
_CONTACTS = _BASE / "contacts.json"


def get_contact(customer_key):
    if customer_key in (None, ""):
        return None
    return (_read(_CONTACTS) or {}).get(str(customer_key).strip())


def set_contact(customer_key, contact):
    if not customer_key:
        return
    _ensure()
    with _lock:
        data = _read(_CONTACTS) or {}
        contact = dict(contact)
        contact["updated_at"] = now_iso()
        data[str(customer_key).strip()] = contact
        _write(_CONTACTS, data)


def all_contacts():
    return _read(_CONTACTS) or {}


def delete_contact(customer_key):
    with _lock:
        data = _read(_CONTACTS) or {}
        if data.pop(str(customer_key).strip(), None) is not None:
            _write(_CONTACTS, data)


def list_allocations():
    _ensure()
    out = []
    for p in _STMT.glob("*.json"):
        data = _read(p)
        if data and data.get("status") == "allocated":
            out.append(data)
    out.sort(key=lambda d: d.get("allocated_at", ""), reverse=True)
    return out
