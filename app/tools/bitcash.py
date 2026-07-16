"""BIT & Cash AR — Bank In Transit and Cash Account Receivables tracking.

Two uploads: the BIT file and the Cash AR file (Excel). Each upload replaces
the previous file of its kind; the number of OPEN items counted at upload is
snapshotted per day so the section can disclose today's position and plot the
daily series.

"Open" items: when the file carries a status-like column (status / statut /
état / state / open …), rows whose value reads open/ouvert/o/blank count as
open; without such a column every row is an open item (these files are
typically extracts of open items only).
"""
import json
import re
import unicodedata
from datetime import datetime
from threading import Lock

from ..config import DATA_DIR
from ..services import excel_reader

STORE_PATH = DATA_DIR / "bitcash.json"
KINDS = {"bit": "BIT (Bank In Transit)", "cash": "Cash AR"}
_lock = Lock()

_STATUS_CANDIDATES = ["status", "statut", "etat", "état", "state",
                      "open/closed", "situation"]
_OPEN_WORDS = {"", "open", "ouvert", "ouverte", "o", "en cours", "pending",
               "outstanding", "not cleared", "non solde", "non soldé"}


def _norm(s):
    s = "".join(c for c in unicodedata.normalize("NFKD", str(s or ""))
                if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def count_items(path):
    """Parse one BIT / Cash AR file → {items, open_items, status_col}."""
    parsed = excel_reader.read_transactions(path)
    header = parsed["header"]
    status_col = next(
        (h for h in header
         if any(cand in _norm(h) for cand in _STATUS_CANDIDATES)), None)
    rows = parsed["rows"]
    if not status_col:
        return {"items": len(rows), "open_items": len(rows), "status_col": None}
    open_items = sum(
        1 for r in rows
        if _norm(r["data"].get(status_col)) in _OPEN_WORDS
        or _norm(r["data"].get(status_col)).startswith(("open", "ouvert")))
    return {"items": len(rows), "open_items": open_items,
            "status_col": status_col}


def _load():
    if STORE_PATH.exists():
        try:
            return json.loads(STORE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"current": {}, "history": {}}


def _save(data):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STORE_PATH)


def record_upload(kind, path, source):
    """Store the newly uploaded file's counts as the CURRENT position for its
    kind and snapshot today's open counts into the daily history."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    counts = count_items(path)
    with _lock:
        data = _load()
        data["current"][kind] = {
            **counts, "source": source,
            "uploaded": datetime.now().strftime("%Y-%m-%d %H:%M")}
        today = datetime.now().strftime("%Y-%m-%d")
        entry = data["history"].get(today, {})
        entry[f"{kind}_open"] = counts["open_items"]
        data["history"][today] = entry
        for key in sorted(data["history"])[:-180]:     # keep ~6 months
            del data["history"][key]
        _save(data)
    return counts


def status():
    """{current: {bit, cash}, history: [(date, {bit_open, cash_open}), …]}."""
    data = _load()
    return {"current": data.get("current", {}),
            "history": sorted(data.get("history", {}).items())}
