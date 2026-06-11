"""A tiny JSON-backed store mapping a customer key (phone/account) to a name.

Namespaced per tool so different tools can keep independent registries:
    { "orange-cameroun": { "2376XXXXXXXX": "ACME Sarl" } }
"""
import json
from threading import Lock

from ..config import DATA_DIR

_PATH = DATA_DIR / "customers.json"
_lock = Lock()


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


def get_name(tool, key):
    if key in (None, ""):
        return None
    return _load().get(tool, {}).get(str(key).strip())


def set_name(tool, key, name):
    if not key or not name:
        return
    with _lock:
        data = _load()
        data.setdefault(tool, {})[str(key).strip()] = str(name).strip()
        _save(data)


def delete_name(tool, key):
    with _lock:
        data = _load()
        if data.get(tool, {}).pop(str(key).strip(), None) is not None:
            _save(data)
            return True
    return False


def all_names(tool):
    return _load().get(tool, {})
