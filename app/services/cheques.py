"""Cheque payment processing.

Read scanned cheques received from customers (cheque number, customer name,
amount, date) via the AI document reader, then scan the bank-statement lines
for each cheque number to confirm where — and for how much, on what date — the
cheque appears (i.e. cleared) on the bank statements.

Bank statements are NOT uploaded here: the rows come from the single Bank
Statements section (bank.all_statement_lines()) and are snapshotted into the
batch (banks.json) at creation so the result stays reproducible. Cheques are
AI-read on a background thread so a batch of
scans can be ingested at once; results.json updates as each completes.
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime

from ..config import DATA_DIR, load_config
from . import ai_ocr

BATCH_DIR = DATA_DIR / "cheques"
MAX_CHEQUES = 25
MIN_CHEQUE_DIGITS = 4          # below this a "cheque number" is too short to match


def _digit_runs(text):
    return re.findall(r"\d+", str(text or ""))


def find_appearances(cheque_no, bank_rows):
    """Every bank-statement line whose text contains the cheque number.

    Matches the cheque number as a WHOLE run of digits — so '123456' matches
    'CHQ 123456' but neither the longer '1234567' nor a different '12345'.
    Exact (no leading-zero stripping) on purpose: for a reconciliation a false
    "cleared" is worse than a "not found — check manually". Each appearance:
    {bank, amount, date, text} — ``text`` is the statement narration the cheque
    reference was seen in. De-duplicated on (bank, amount, date).
    """
    cheque_no = re.sub(r"\D", "", str(cheque_no or ""))
    if len(cheque_no) < MIN_CHEQUE_DIGITS:
        return []
    seen, out = set(), []
    for r in bank_rows:
        if cheque_no not in _digit_runs(r.get("text")):
            continue
        # The same clearing event often shows on several statement extracts
        # (e.g. the Jan-2 extract and the fuller Jan-3 one both carry the Jan-1
        # transaction) — key on bank+amount+date so it is disclosed ONCE.
        try:
            amt = round(float(r.get("amount") or 0), 2)
        except (TypeError, ValueError):
            amt = r.get("amount")
        key = (r.get("bank", ""), amt, r.get("date", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append({"bank": r.get("bank", ""), "amount": r.get("amount"),
                    "date": r.get("date", ""),
                    "text": str(r.get("text", ""))[:120]})
    return out


def primary_appearance(appearances):
    """The ONE clearing line disclosed on the register: the appearance with the
    latest transaction date (a cheque clears once; later statement extracts
    repeating the same event are deduplicated upstream)."""
    if not appearances:
        return None
    return max(appearances, key=lambda a: str(a.get("date") or ""))


def ref_snippet(appearance, cheque_no, window=24):
    """A short, readable extract of the statement narration centred on the
    cheque number — the full raw text is a jumble of dates and figures, so the
    register shows just the reference in context."""
    if not appearance:
        return ""
    text = re.sub(r"\s+", " ", str(appearance.get("text") or "")).strip()
    num = re.sub(r"\D", "", str(cheque_no or ""))
    pos = text.find(num) if num else -1
    if pos < 0:
        return text[:60] + ("…" if len(text) > 60 else "")
    start, end = max(0, pos - window), min(len(text), pos + len(num) + window)
    return (("…" if start else "") + text[start:end]
            + ("…" if end < len(text) else ""))


_ORD = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(n):
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{_ORD.get(n % 10, 'th')}"


def matched_label(matched_at, cleared=None):
    """Human match date — e.g. ``July 20th 2026`` — FIXED IN TIME: built from
    the ``matched_at`` stamp written the first time the cheque was seen on a
    statement (a later refresh never overwrites it). Cheques matched before
    the stamp existed fall back to the statement's credited date, equally
    fixed."""
    raw = str(matched_at or "").strip()
    if not raw and cleared:
        raw = str(cleared.get("date") or "").strip()
    if not raw:
        return ""
    day = raw.split(" ")[0]
    for cand, fmt in ((raw, "%Y-%m-%d %H:%M"), (day, "%Y-%m-%d"),
                      (day, "%d/%m/%Y"), (day, "%d-%m-%Y"), (day, "%d.%m.%Y")):
        try:
            d = datetime.strptime(cand, fmt)
            return f"{d.strftime('%B')} {_ordinal(d.day)} {d.year}"
        except ValueError:
            continue
    return raw


# --------------------------------------------------------------------------- #
# Batch processing (background thread; bank rows kept separate from results)
# --------------------------------------------------------------------------- #
def _batch_path(token):
    return BATCH_DIR / token


def _results_file(token):
    return _batch_path(token) / "results.json"


def _banks_file(token):
    return _batch_path(token) / "banks.json"


_write_lock = threading.Lock()


@contextmanager
def _register_lock():
    """CROSS-PROCESS lock for register writes. Production runs several
    gunicorn workers sharing the same results.json files, and a
    threading.Lock cannot serialise processes — same O_EXCL lockfile
    pattern as the config lock (short timeout, stale-break, best-effort
    fallback). Windows raises PermissionError instead of FileExistsError
    when the lockfile is mid-delete — both mean "contended"."""
    import os
    import time
    BATCH_DIR.mkdir(parents=True, exist_ok=True)
    lockfile = BATCH_DIR / ".register.lock"
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
            import os as _os
            _os.close(fd)
            try:
                lockfile.unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_write(path, text):
    """Write via a temp file + os.replace so a concurrent reader can NEVER see
    a half-written file. (A parallel cheque-reader once read results.json
    mid-write, got partial JSON, crashed silently and left its cheque stuck on
    'pending' forever — this makes that impossible.)"""
    import os
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _save(token, payload):
    with _write_lock:
        _atomic_write(_results_file(token),
                      json.dumps(payload, ensure_ascii=False))


def _update_cheque(token, idx, updates):
    """Atomic read-modify-write so parallel workers (and other gunicorn
    processes) never lose updates. A batch deleted mid-read is a no-op."""
    with _register_lock():
        with _write_lock:
            p = _results_file(token)
            if not p.exists():
                return
            payload = json.loads(p.read_text(encoding="utf-8"))
            for c in payload["cheques"]:
                if c["idx"] == idx:
                    c.update(updates)
                    break
            if all(c["status"] != "pending" for c in payload["cheques"]):
                payload["status"] = "done"
            try:
                _atomic_write(p, json.dumps(payload, ensure_ascii=False))
            except FileNotFoundError:
                return


def load_batch(token):
    p = _results_file(token)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _load_bank_rows(token):
    p = _banks_file(token)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("rows", [])
    except (json.JSONDecodeError, OSError):
        return []


def list_recent(limit=8):
    if not BATCH_DIR.exists():
        return []
    out = []
    for d in BATCH_DIR.iterdir():
        b = load_batch(d.name) if d.is_dir() else None
        if b:
            done = sum(1 for c in b["cheques"] if c["status"] != "pending")
            out.append({"token": d.name, "created_at": b.get("created_at", ""),
                        "total": len(b["cheques"]), "done": done,
                        "status": b.get("status", "")})
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out[:limit]


def create_batch(token, cheques, bank_rows, bank_summary, uploaded_by=""):
    """``cheques`` = list of {filename, stored, media}; persist the batch.
    ``uploaded_by`` = the signed-in username — shown (via its alias, e.g. X1)
    in the register's "Uploaded by" column."""
    _batch_path(token).mkdir(parents=True, exist_ok=True)
    _atomic_write(_banks_file(token),
                  json.dumps({"rows": bank_rows}, ensure_ascii=False))
    payload = {
        "status": "running",
        "uploaded_by": uploaded_by or "",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "banks": bank_summary,
        "bank_line_count": len(bank_rows),
        "cheques": [{"idx": i, "filename": c["filename"], "stored": c["stored"],
                     "media": c["media"], "status": "pending",
                     "result": None, "appearances": [], "error": ""}
                    for i, c in enumerate(cheques)],
    }
    _save(token, payload)
    return payload


def register_rows():
    """The ELECTRONIC CHEQUE REGISTER: every cheque across ALL uploads, newest
    first. One row per cheque: the read details (cheque no, issuing bank,
    client, amount, date) + whether its number was found on any bank statement
    on file, and where (bank credited, date, amount, narration seen)."""
    rows = []
    if not BATCH_DIR.exists():
        return rows
    for d in BATCH_DIR.iterdir():
        b = load_batch(d.name) if d.is_dir() else None
        if not b:
            continue
        for c in b["cheques"]:
            res = c.get("result") or {}
            treated = c.get("treated")
            found = bool(c.get("appearances"))
            cleared = primary_appearance(c.get("appearances"))
            rows.append({
                "batch": d.name, "idx": c.get("idx", 0),
                "uploaded": b.get("created_at", ""),
                "uploaded_by": b.get("uploaded_by", ""),
                "filename": c.get("filename", ""),
                "status": c.get("status", ""),
                "error": c.get("error", ""),
                "cheque_number": res.get("cheque_number", ""),
                "customer": res.get("customer_name", ""),
                "issuing_bank": res.get("drawee_bank", ""),
                "amount": res.get("amount"),
                "currency": res.get("amount_currency", ""),
                "cheque_date": res.get("cheque_date", ""),
                "found": found,
                # ONE disclosed clearing line per cheque (latest-dated).
                "cleared": cleared,
                # A short, readable snippet of the narration around the cheque
                # number — instead of the full haphazard statement text.
                "ref_snippet": ref_snippet(cleared, res.get("cheque_number", "")),
                "matched_at": c.get("matched_at", ""),
                # "Matched July 20th 2026" — the fixed-in-time match date
                # shown on the register (empty while unpresented).
                "matched_label": (matched_label(c.get("matched_at", ""),
                                                cleared) if found else ""),
                "treated": treated,
                "duplicate_of": c.get("duplicate_of"),
                # Newly matched = seen on a statement but not yet treated in the
                # accounting records — surfaced at the top, highlighted.
                "new": found and not treated,
            })
    # Base order: newest activity first (first match date, else upload date)…
    rows.sort(key=lambda r: (r["matched_at"] or r["uploaded"],
                             r["batch"], r["idx"]), reverse=True)
    # …then newly-matched cheques float to the top (stable sort keeps order).
    rows.sort(key=lambda r: not r["new"])
    return rows


def find_duplicate(token, chq):
    """Is this freshly-read cheque already on the register? Identity = same
    cheque number AND same amount, in an EARLIER batch (same-batch entries are
    read concurrently, so they are not compared against each other). Returns
    {batch, uploaded, uploaded_by} of the original, or None."""
    num = re.sub(r"\D", "", str(chq.get("cheque_number") or ""))
    amt = chq.get("amount")
    if len(num) < MIN_CHEQUE_DIGITS or not BATCH_DIR.exists():
        return None
    for d in BATCH_DIR.iterdir():
        if not d.is_dir() or d.name == token:
            continue
        b = load_batch(d.name)
        if not b:
            continue
        for c in b["cheques"]:
            res = c.get("result") or {}
            if re.sub(r"\D", "", str(res.get("cheque_number") or "")) != num:
                continue
            a2 = res.get("amount")
            same_amt = (isinstance(amt, (int, float))
                        and isinstance(a2, (int, float))
                        and abs(amt - a2) < 0.01) or amt == a2
            if same_amt:
                return {"batch": d.name,
                        "uploaded": b.get("created_at", ""),
                        "uploaded_by": b.get("uploaded_by", "")}
    return None


# --- Daily register statistics (cheques on file / unpresented) ---------------
STATS_PATH = DATA_DIR / "cheque_daily_stats.json"


def register_stats(rows=None):
    """Today's headline numbers for the register: total cheques sitting on the
    platform (read, non-duplicate), how many are UNPRESENTED (not yet seen on
    any bank statement), plus reading/duplicate counts for the banner."""
    rows = register_rows() if rows is None else rows
    done = [r for r in rows if r["status"] == "done" and not r.get("duplicate_of")]
    total = len(done)
    presented = sum(1 for r in done if r["found"])
    return {"total": total, "presented": presented,
            "unpresented": total - presented,
            "reading": sum(1 for r in rows
                           if r["status"] in ("pending", "running")),
            "duplicates": sum(1 for r in rows if r.get("duplicate_of"))}


def record_daily_stats(stats):
    """Persist today's snapshot {date: {total, unpresented}} — called on every
    register render so the day's entry tracks the current truth. Keeps ~180
    days for the daily graph."""
    hist = {}
    if STATS_PATH.exists():
        try:
            hist = json.loads(STATS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            hist = {}
    hist[datetime.now().strftime("%Y-%m-%d")] = {
        "total": stats["total"], "unpresented": stats["unpresented"]}
    for key in sorted(hist)[:-180]:
        del hist[key]
    with _write_lock:
        _atomic_write(STATS_PATH, json.dumps(hist, ensure_ascii=False))
    return hist


def stats_history():
    """[(date, {total, unpresented}), …] oldest first, for the daily graph."""
    if not STATS_PATH.exists():
        return []
    try:
        hist = json.loads(STATS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return sorted(hist.items())


def set_treated(token, idx, by, on=True):
    """Mark a cheque as treated in the accounting records (finance only —
    enforced by the route). Returns True when the cheque exists."""
    payload = load_batch(token)
    if not payload or not any(c["idx"] == idx for c in payload["cheques"]):
        return False
    _update_cheque(token, idx, {"treated": (
        {"by": by or "finance", "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
        if on else None)})
    return True


def refresh_all(bank_rows, bank_summary):
    """Re-match EVERY batch's cheques against the current bank statement lines
    (register-wide refresh; no AI re-read). Returns the batch count refreshed.
    One vanished/broken batch (e.g. deleted mid-refresh) never aborts the
    rest."""
    if not BATCH_DIR.exists():
        return 0
    n = 0
    for d in BATCH_DIR.iterdir():
        if not d.is_dir():
            continue
        try:
            if refresh_matches(d.name, bank_rows, bank_summary):
                n += 1
        except OSError:
            continue
    return n


def refresh_matches(token, bank_rows, bank_summary):
    """Re-match every already-read cheque in a batch against the CURRENT bank
    statement lines — no AI re-read, so it just picks up changed/added bank
    statements. Returns the updated batch, or None.

    The slow part (scanning every statement line) runs on a snapshot; the
    result is then MERGED per-cheque into a freshly loaded payload under the
    register lock, so a concurrent admin deletion is never clobbered (a
    whole-payload save here used to resurrect deleted cheques) and a batch
    removed mid-refresh is simply skipped instead of crashing the refresh."""
    snapshot = load_batch(token)
    if not snapshot:
        return None
    updates = {}
    for c in snapshot["cheques"]:
        res = c.get("result")
        if res and res.get("cheque_number"):
            updates[c["idx"]] = {
                "apps": find_appearances(res["cheque_number"], bank_rows),
                # Was it already matched before this refresh? Decides the
                # matched_at stamp below (fixed in time).
                "had": bool(c.get("appearances")),
            }
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _register_lock():
        with _write_lock:
            payload = load_batch(token)
            if not payload:
                return None                 # batch deleted meanwhile
            for c in payload["cheques"]:
                upd = updates.get(c["idx"])
                if upd is None:
                    continue
                c["appearances"] = upd["apps"]
                # First-match timestamp: set once, kept across later
                # refreshes. A cheque matched BEFORE the stamp existed is
                # backfilled with its statement credited date — never with
                # "now" — so the disclosed Matched date stays fixed in time.
                if c["appearances"] and not c.get("matched_at"):
                    if upd["had"]:
                        old_date = str((primary_appearance(c["appearances"])
                                        or {}).get("date") or "")
                        if old_date:
                            c["matched_at"] = old_date
                    else:
                        c["matched_at"] = now
                elif not c["appearances"]:
                    c.pop("matched_at", None)
            payload["banks"] = bank_summary
            payload["bank_line_count"] = len(bank_rows)
            payload["refreshed_at"] = now
            try:
                _atomic_write(_results_file(token),
                              json.dumps(payload, ensure_ascii=False))
                _atomic_write(_banks_file(token),
                              json.dumps({"rows": bank_rows},
                                         ensure_ascii=False))
            except FileNotFoundError:
                return None                 # directory removed meanwhile
    return payload


def _process_one(token, idx, ai_cfg, bank_rows):
    # Read under the write lock (with a retry) so a parallel worker's save can
    # never hand us a partial file — a silent crash here used to leave the
    # cheque stuck on "pending" with no error.
    entry = None
    for _ in range(3):
        with _write_lock:
            payload = load_batch(token)
        if payload is not None:
            entry = next((c for c in payload["cheques"] if c["idx"] == idx), None)
            break
        import time as _t
        _t.sleep(0.2)
    if entry is None:
        return
    stored = _batch_path(token) / entry["stored"]
    media = entry.get("media") or ai_ocr.media_type_for(entry["stored"])
    try:
        chq = ai_ocr.extract_cheque(stored.read_bytes(), media, ai_cfg)
        appearances = find_appearances(chq["cheque_number"], bank_rows)
        upd = {"status": "done", "result": chq,
               "appearances": appearances, "error": ""}
        if appearances:      # first time this cheque is seen on a statement
            upd["matched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        # Duplicate upload? Same cheque number + amount already on the register
        # — flagged (with when/who uploaded the original) rather than hidden.
        dup = find_duplicate(token, chq)
        if dup:
            upd["duplicate_of"] = dup
    except (ai_ocr.AiNotConfigured, ai_ocr.AiReadError) as exc:
        upd = {"status": "error", "result": None, "appearances": [],
               "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — never kill the batch
        upd = {"status": "error", "result": None, "appearances": [],
               "error": f"{type(exc).__name__}: {exc}"}
    _update_cheque(token, idx, upd)


def run_batch_async(token):
    """Read every pending cheque on a background thread, then match each
    cheque number against the parsed bank-statement rows."""
    ai_cfg = load_config().get("ai")
    bank_rows = _load_bank_rows(token)

    def _runner():
        payload = load_batch(token)
        idxs = [c["idx"] for c in payload["cheques"] if c["status"] == "pending"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            for idx in idxs:
                pool.submit(_process_one, token, idx, ai_cfg, bank_rows)
        # Safety net: retry anything still pending once, sequentially, then
        # only report "done" when nothing is left pending.
        payload = load_batch(token)
        if payload:
            for c in payload["cheques"]:
                if c["status"] == "pending":
                    _process_one(token, c["idx"], ai_cfg, bank_rows)
        # Done-marking is a locked merge on fresh data — never a whole-payload
        # save over someone else's concurrent write (or a deleted batch).
        with _register_lock():
            with _write_lock:
                payload = load_batch(token)
                if payload and all(c["status"] != "pending"
                                   for c in payload["cheques"]):
                    payload["status"] = "done"
                    try:
                        _atomic_write(_results_file(token),
                                      json.dumps(payload, ensure_ascii=False))
                    except FileNotFoundError:
                        pass

    threading.Thread(target=_runner, daemon=True).start()


def pending_count():
    """Cheques still awaiting an AI read across all uploads."""
    return sum(1 for r in register_rows()
               if r["status"] not in ("done", "error"))


def resume_pending():
    """Re-launch the background reader for every upload that still has pending
    cheques (e.g. after a stalled read or a service restart mid-batch).
    Returns the number of uploads resumed."""
    if not BATCH_DIR.exists():
        return 0
    n = 0
    for d in BATCH_DIR.iterdir():
        b = load_batch(d.name) if d.is_dir() else None
        if b and any(c["status"] == "pending" for c in b["cheques"]):
            run_batch_async(d.name)
            n += 1
    return n


def delete_batch(token):
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return
    import shutil
    shutil.rmtree(_batch_path(token), ignore_errors=True)


def scan_path(token, idx):
    """Locate a cheque's uploaded SCAN file: returns (Path, media, filename) or
    None. Used to view the scan inline and to bundle it as evidence. The stored
    name is validated so a crafted token/idx can never escape the batch dir."""
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return None
    b = load_batch(token)
    if not b:
        return None
    c = next((c for c in b["cheques"] if c.get("idx") == idx), None)
    stored = str((c or {}).get("stored") or "")
    if not c or not stored or "/" in stored or "\\" in stored or ".." in stored:
        return None
    p = _batch_path(token) / stored
    if not p.exists():
        return None
    media = c.get("media") or ai_ocr.media_type_for(stored) \
        or "application/octet-stream"
    return p, media, c.get("filename") or stored


def delete_cheque(token, idx):
    """Remove ONE cheque from its upload — reserved by the route for the
    ADMINISTRATOR deleting a flagged DUPLICATE (the register stays permanent
    for every original cheque). The stored scan file goes with it, and an
    upload left empty disappears entirely. Returns the removed cheque dict,
    or None when it does not exist."""
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return None
    removed, keep = None, []
    with _register_lock():
        with _write_lock:
            p = _results_file(token)
            if not p.exists():
                return None
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
            for c in payload.get("cheques", []):
                if removed is None and c.get("idx") == idx:
                    removed = c
                else:
                    keep.append(c)
            if removed is None:
                return None
            payload["cheques"] = keep
            _atomic_write(p, json.dumps(payload, ensure_ascii=False))
        stored = str(removed.get("stored") or "")
        if stored and "/" not in stored and "\\" not in stored \
                and ".." not in stored:
            try:
                (_batch_path(token) / stored).unlink()
            except OSError:
                pass
        if not keep:
            delete_batch(token)
    return removed


def build_register_excel(out_path, rows, aliases=None):
    """The full ELECTRONIC CHEQUE REGISTER as one Excel sheet — every cheque
    ever uploaded, its single disclosed clearing line, and the treated mark.
    ``aliases`` maps usernames to display aliases for the Uploaded-by column."""
    aliases = aliases or {}
    import xlsxwriter

    wb = xlsxwriter.Workbook(str(out_path))
    head = wb.add_format({"bold": True, "bg_color": "#0b2545",
                          "font_color": "white", "border": 1})
    cell = wb.add_format({"border": 1})
    wrap = wb.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    ok_f = wb.add_format({"border": 1, "bold": True, "font_color": "#157a4a"})
    no_f = wb.add_format({"border": 1, "bold": True, "font_color": "#bd3727"})
    num = wb.add_format({"border": 1, "num_format": "#,##0.00"})

    ws = wb.add_worksheet("Cheque register")
    widths = [17, 13, 14, 22, 28, 28, 14, 15, 15, 14, 12, 22, 18, 22, 13, 16,
              46, 22, 26, 16, 13, 15, 18]
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)
    headers = ["Uploaded", "Uploaded by", "Cheque N°", "Issuing bank",
               "Client name", "AR customer (latest analysis)", "AR account",
               "AR balance", "Overdue balance",
               "Cheque amount", "Cheque date", "Scan file",
               "On bank statement?", "Bank credited", "Date credited",
               "Amount credited/debited", "Reference seen on statement",
               "Treated in accounting", "Duplicate",
               "BIT transaction reference", "BIT transaction date",
               "BIT amount", "Matched on"]
    for c, h in enumerate(headers):
        ws.write(0, c, h, head)
    r = 1
    for row in rows:
        ws.write(r, 0, row["uploaded"], cell)
        who = row.get("uploaded_by", "")
        ws.write(r, 1, aliases.get(who, who), cell)
        if row["status"] != "done":
            ws.write(r, 2, row["status"].upper(), no_f)
            ws.write(r, 3, row.get("error", ""), wrap)
            ws.write(r, 11, row.get("filename", ""), cell)
            r += 1
            continue
        ws.write(r, 2, row["cheque_number"], cell)
        ws.write(r, 3, row["issuing_bank"], cell)
        ws.write(r, 4, row["customer"], cell)
        ar = row.get("ar") or {}
        ws.write(r, 5, ar.get("customer", ""), cell)
        ws.write(r, 6, ar.get("key", ""), cell)
        bal, ovd = ar.get("total_ar"), ar.get("overdue")
        ws.write_number(r, 7, bal, num) if isinstance(bal, (int, float)) \
            else ws.write(r, 7, "", cell)
        ws.write_number(r, 8, ovd, num) if isinstance(ovd, (int, float)) \
            else ws.write(r, 8, "", cell)
        amt = row["amount"]
        ws.write_number(r, 9, amt, num) if isinstance(amt, (int, float)) \
            else ws.write(r, 9, "", cell)
        ws.write(r, 10, row["cheque_date"], cell)
        ws.write(r, 11, row.get("filename", ""), cell)
        ws.write(r, 12, "YES" if row["found"] else "NOT FOUND",
                 ok_f if row["found"] else no_f)
        cl = row.get("cleared") or {}
        ws.write(r, 13, cl.get("bank", ""), cell)
        ws.write(r, 14, cl.get("date", ""), cell)
        ca = cl.get("amount")
        ws.write_number(r, 15, ca, num) if isinstance(ca, (int, float)) \
            else ws.write(r, 15, "", cell)
        ws.write(r, 16, row.get("ref_snippet") or cl.get("text", ""), wrap)
        t = row.get("treated")
        ws.write(r, 17, f"YES — {t['by']} ({t['at']})" if t else "pending",
                 ok_f if t else cell)
        dup = row.get("duplicate_of")
        d_who = (dup or {}).get("uploaded_by", "")
        ws.write(r, 18, (f"DUPLICATE of upload {dup['uploaded']} by "
                         f"{aliases.get(d_who, d_who) or 'unknown'}") if dup else "",
                 no_f if dup else cell)
        bit = row.get("bit")
        ws.write(r, 19, (bit or {}).get("reference", "")
                 or (bit or {}).get("doc_no", ""),
                 ok_f if bit and bit.get("aligned")
                 else (no_f if bit else cell))
        ws.write(r, 20, (bit or {}).get("date", ""), cell)
        ba = (bit or {}).get("amount")
        ws.write_number(r, 21, ba, num) if isinstance(ba, (int, float)) \
            else ws.write(r, 21, "", cell)
        ws.write(r, 22, row.get("matched_label", ""), cell)
        r += 1
    wb.close()
    return out_path
