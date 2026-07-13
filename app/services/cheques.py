"""Cheque payment processing.

Read scanned cheques received from customers (cheque number, customer name,
amount, date) via the AI document reader, then scan the bank-statement lines
for each cheque number to confirm where — and for how much, on what date — the
cheque appears (i.e. cleared) on the bank statements.

Bank statements are NOT uploaded here: the rows come from the single Bank
Statements section (bank.all_statement_lines()) and are snapshotted into the
batch (banks.json) at creation so the result stays reproducible. Cheques are
AI-read on a background thread (mirrors the compliance engine) so a batch of
scans can be ingested at once; results.json updates as each completes.
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
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


def _save(token, payload):
    with _write_lock:
        _results_file(token).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _update_cheque(token, idx, updates):
    """Atomic read-modify-write so parallel workers never lose updates."""
    with _write_lock:
        payload = json.loads(_results_file(token).read_text(encoding="utf-8"))
        for c in payload["cheques"]:
            if c["idx"] == idx:
                c.update(updates)
                break
        if all(c["status"] != "pending" for c in payload["cheques"]):
            payload["status"] = "done"
        _results_file(token).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")


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


def create_batch(token, cheques, bank_rows, bank_summary):
    """``cheques`` = list of {filename, stored, media}; persist the batch."""
    _batch_path(token).mkdir(parents=True, exist_ok=True)
    _banks_file(token).write_text(json.dumps({"rows": bank_rows},
                                  ensure_ascii=False), encoding="utf-8")
    payload = {
        "status": "running",
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
            rows.append({
                "batch": d.name, "idx": c.get("idx", 0),
                "uploaded": b.get("created_at", ""),
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
                "cleared": primary_appearance(c.get("appearances")),
                "matched_at": c.get("matched_at", ""),
                "treated": treated,
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
    (register-wide refresh; no AI re-read). Returns the batch count refreshed."""
    if not BATCH_DIR.exists():
        return 0
    n = 0
    for d in BATCH_DIR.iterdir():
        if d.is_dir() and refresh_matches(d.name, bank_rows, bank_summary):
            n += 1
    return n


def refresh_matches(token, bank_rows, bank_summary):
    """Re-match every already-read cheque in a batch against the CURRENT bank
    statement lines — no AI re-read, so it just picks up changed/added bank
    statements. Returns the updated batch, or None."""
    payload = load_batch(token)
    if not payload:
        return None
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    for c in payload["cheques"]:
        res = c.get("result")
        if res and res.get("cheque_number"):
            c["appearances"] = find_appearances(res["cheque_number"], bank_rows)
            # First-match timestamp: set once, kept across later refreshes so a
            # cheque is flagged "newly matched" only the first time it clears.
            if c["appearances"] and not c.get("matched_at"):
                c["matched_at"] = now
            elif not c["appearances"]:
                c.pop("matched_at", None)
    payload["banks"] = bank_summary
    payload["bank_line_count"] = len(bank_rows)
    payload["refreshed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _save(token, payload)
    _banks_file(token).write_text(json.dumps({"rows": bank_rows},
                                  ensure_ascii=False), encoding="utf-8")
    return payload


def _process_one(token, idx, ai_cfg, bank_rows):
    payload = load_batch(token)
    entry = next((c for c in payload["cheques"] if c["idx"] == idx), None)
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
        payload = load_batch(token)
        if payload:
            payload["status"] = "done"
            _save(token, payload)

    threading.Thread(target=_runner, daemon=True).start()


def delete_batch(token):
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return
    import shutil
    shutil.rmtree(_batch_path(token), ignore_errors=True)


def build_register_excel(out_path, rows):
    """The full ELECTRONIC CHEQUE REGISTER as one Excel sheet — every cheque
    ever uploaded, its single disclosed clearing line, and the treated mark."""
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
    widths = [17, 14, 22, 28, 14, 12, 22, 18, 22, 13, 16, 46, 22]
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)
    headers = ["Uploaded", "Cheque N°", "Issuing bank", "Client name",
               "Cheque amount", "Cheque date", "Scan file",
               "On bank statement?", "Bank credited", "Date credited",
               "Amount credited/debited", "Reference seen on statement",
               "Treated in accounting"]
    for c, h in enumerate(headers):
        ws.write(0, c, h, head)
    r = 1
    for row in rows:
        ws.write(r, 0, row["uploaded"], cell)
        if row["status"] != "done":
            ws.write(r, 1, row["status"].upper(), no_f)
            ws.write(r, 2, row.get("error", ""), wrap)
            ws.write(r, 6, row.get("filename", ""), cell)
            r += 1
            continue
        ws.write(r, 1, row["cheque_number"], cell)
        ws.write(r, 2, row["issuing_bank"], cell)
        ws.write(r, 3, row["customer"], cell)
        amt = row["amount"]
        ws.write_number(r, 4, amt, num) if isinstance(amt, (int, float))             else ws.write(r, 4, "", cell)
        ws.write(r, 5, row["cheque_date"], cell)
        ws.write(r, 6, row.get("filename", ""), cell)
        ws.write(r, 7, "YES" if row["found"] else "NOT FOUND",
                 ok_f if row["found"] else no_f)
        cl = row.get("cleared") or {}
        ws.write(r, 8, cl.get("bank", ""), cell)
        ws.write(r, 9, cl.get("date", ""), cell)
        ca = cl.get("amount")
        ws.write_number(r, 10, ca, num) if isinstance(ca, (int, float))             else ws.write(r, 10, "", cell)
        ws.write(r, 11, cl.get("text", ""), wrap)
        t = row.get("treated")
        ws.write(r, 12, f"YES — {t['by']} ({t['at']})" if t else "pending",
                 ok_f if t else cell)
        r += 1
    wb.close()
    return out_path
