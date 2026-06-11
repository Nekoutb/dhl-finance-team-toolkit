"""Credit-hold register: which customers are CURRENTLY on credit hold.

The AR export carries no hold flag, so the team records reality here once and
keeps it updated (one click from the analysis screen). Every analysis then
compares policy-required state vs this register: who must be placed on hold,
who should be reviewed for release.

Stored in data/credit_holds.json as:
  { "<account-or-name-key>": {"name": "...", "since": "...", "note": "..."} }
"""
import json
from datetime import datetime
from threading import Lock

from ..config import DATA_DIR

_PATH = DATA_DIR / "credit_holds.json"
_lock = Lock()


def _norm(key):
    s = str(key or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _load():
    if _PATH.exists():
        try:
            return json.loads(_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save(data):
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    _PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def all_holds():
    return _load()


def lookup():
    """Return (set of held account keys, set of held names lowercased)."""
    data = _load()
    keys = {_norm(k) for k in data}
    names = {str(v.get("name", "")).strip().lower() for v in data.values()
             if v.get("name")}
    return keys, names


def is_held(account, name=""):
    keys, names = lookup()
    return _norm(account) in keys or (name or "").strip().lower() in names


def set_hold(account, name="", note=""):
    key = _norm(account) or str(name).strip()
    if not key:
        return
    with _lock:
        data = _load()
        if key not in data:
            data[key] = {"name": str(name).strip(), "note": str(note).strip(),
                         "since": datetime.now().strftime("%Y-%m-%d %H:%M")}
            _save(data)


def release(account, name=""):
    with _lock:
        data = _load()
        key = _norm(account)
        if key in data:
            del data[key]
            _save(data)
            return
        # fall back to a name match
        target = (name or "").strip().lower() or key.lower()
        for k in list(data):
            if str(data[k].get("name", "")).strip().lower() == target:
                del data[k]
        _save(data)


def bulk_add(text):
    """Add one customer per line: 'account' or 'account, name' or just a name."""
    added = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "," in line:
            account, name = line.split(",", 1)
        elif line.replace(" ", "").isdigit():
            account, name = line, ""
        else:
            account, name = "", line
        before = len(_load())
        set_hold(account.strip() or name.strip(), name.strip())
        if len(_load()) > before:
            added += 1
    return added
