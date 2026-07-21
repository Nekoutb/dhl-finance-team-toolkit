"""Persistence for the remittance portal.

Each daily GL upload becomes a *batch*; each customer in it becomes a *statement*
addressed by an unguessable token (the customer's private link). Allocations are
written back onto the statement and kept on file for future reference.

Layout under data/remittance/:
  batches/<batch_id>.json        — one upload: metadata + per-customer summary
  statements/<token>.json        — one customer statement (+ allocation once done)
"""
import json
import os
import time
import uuid
from contextlib import contextmanager
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


@contextmanager
def _store_lock():
    """CROSS-PROCESS lock for read-modify-write on the shared registries
    (links.json / contacts.json). Production runs several gunicorn workers, and
    a threading.Lock cannot serialise processes — same O_EXCL lockfile pattern
    as the config lock (short timeout, stale-break, best-effort fallback)."""
    _ensure()
    lockfile = _BASE / ".store.lock"
    fd = None
    deadline = time.time() + 3.0
    while True:
        try:
            fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):
            try:
                if time.time() - lockfile.stat().st_mtime > 10:
                    lockfile.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                break                       # proceed unlocked (best effort)
            time.sleep(0.03)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            try:
                lockfile.unlink(missing_ok=True)
            except OSError:
                pass


def new_token():
    return uuid.uuid4().hex


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _write(path, data):
    """ATOMIC write (unique temp + os.replace) so a concurrent reader can never
    see a half-written file — a torn read of a shared registry (links.json)
    would otherwise reset it and re-mint every 'permanent' link."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{os.urandom(3).hex()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:          # Windows: reader/AV holds the target
            time.sleep(0.01 * (attempt + 1))
    os.replace(tmp, path)


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
    """Remove a batch and the statements it still owns.

    Tokens are STABLE per customer (v11.3), so the same token is recorded in
    every batch that customer appears in, and the live statement is overwritten
    in place. Deleting an OLD batch must therefore NOT remove a statement a
    NEWER upload has since taken over — only unlink a statement whose batch_id
    still matches THIS batch (i.e. this batch is the one currently on the
    customer's link). A confirmed allocation about to be removed is archived
    first so the confirmation stays on file."""
    batch = load_batch(batch_id)
    if batch is None:
        return False
    for c in batch.get("customers", []):
        token = c.get("token")
        if not token:
            continue
        stmt = load_statement(token)
        # A newer upload took the link over — leave the live statement intact.
        if stmt is not None and stmt.get("batch_id") != batch_id:
            continue
        if stmt is not None and stmt.get("status") == "allocated":
            archive_statement(stmt)      # keep the confirmation on record
        (_STMT / f"{token}.json").unlink(missing_ok=True)
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
    data.setdefault("forward_default", "")
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
    with _store_lock():           # cross-process read-modify-write
        data = _read(_CONTACTS) or {}
        contact = dict(contact)
        contact["updated_at"] = now_iso()
        data[str(customer_key).strip()] = contact
        _write(_CONTACTS, data)


def all_contacts():
    return _read(_CONTACTS) or {}


def delete_contact(customer_key):
    with _store_lock():
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


# --- Stable per-customer link tokens -----------------------------------------
# A customer keeps ONE permanent /portal/<token> link across re-uploads: the
# statement under that token is refreshed in place with each new AR file, so a
# link already shared never goes stale (and old links are never orphaned).
_LINKS = _BASE / "links.json"


def link_token(customer_key):
    """The stable token already issued to this customer, or None."""
    if customer_key in (None, ""):
        return None
    return (_read(_LINKS) or {}).get(str(customer_key).strip())


def set_link_token(customer_key, token):
    """Persist customer_key -> token so the customer's link is reused next time.
    Cross-process read-modify-write: concurrent uploads in different gunicorn
    workers must not lose each other's new mappings (which would re-mint a
    customer's 'permanent' link)."""
    if not customer_key or not token:
        return
    with _store_lock():
        data = _read(_LINKS) or {}
        data[str(customer_key).strip()] = token
        _write(_LINKS, data)


def all_link_tokens():
    return _read(_LINKS) or {}


def archive_statement(statement):
    """Keep a CONFIRMED statement on file under a fresh archival token before
    its stable link is refreshed with new data — so the audit trail and the
    confirmed-allocations view (list_allocations, which globs statements/) never
    lose a confirmation. Returns the archival token, or None when there is
    nothing worth archiving."""
    if not statement or statement.get("status") != "allocated":
        return None
    arch = dict(statement)
    arch_token = new_token()
    arch["token"] = arch_token
    arch["archived_from"] = statement.get("token", "")
    arch["archived_at"] = now_iso()
    save_statement(arch_token, arch)
    return arch_token
