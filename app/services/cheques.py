"""Cheque payment processing.

Read scanned cheques received from customers (cheque number, customer name,
amount, date) via the AI document reader, then scan every uploaded bank
statement for each cheque number to confirm where — and for how much, on what
date — the cheque appears (i.e. cleared) on the bank statements.

Cheques are AI-read on a background thread (mirrors the compliance engine) so a
batch of scans can be ingested at once; results.json updates as each completes.
Bank statements are parsed once at upload (no AI) and kept in banks.json.
"""
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR, load_config
from . import ai_ocr, bank_statement

BATCH_DIR = DATA_DIR / "cheques"
MAX_CHEQUES = 25
MAX_BANK_FILES = 10
MIN_CHEQUE_DIGITS = 4          # below this a "cheque number" is too short to match

# Bank letterheads we recognise in a statement's header lines (else file name).
_BANK_WORDS = ("BANK", "BANQUE", "ECOBANK", "AFRILAND", "BICEC", "SGBC",
               "SOCIETE GENERALE", "UBA", "BGFI", "SCB", "CCA", "CITIBANK",
               "STANDARD CHARTERED", "ACCESS", "NFC", "CBC")


def detect_bank(metadata, filename=""):
    """Identify the bank from the statement header lines, else the file name."""
    for line in metadata or []:
        if any(w in str(line).upper() for w in _BANK_WORDS):
            return str(line).strip()[:60]
    return Path(filename).stem or (str(metadata[0]).strip()[:60] if metadata
                                   else "Unknown bank")


def _digit_runs(text):
    return re.findall(r"\d+", str(text or ""))


def find_appearances(cheque_no, bank_rows):
    """Every bank-statement line whose text contains the cheque number.

    Matches the cheque number as a WHOLE run of digits — so '123456' matches
    'CHQ 123456' but neither the longer '1234567' nor a different '12345'.
    Exact (no leading-zero stripping) on purpose: for a reconciliation a false
    "cleared" is worse than a "not found — check manually". Each appearance:
    {bank, amount, date}. De-duplicated on (bank, amount, date).
    """
    cheque_no = re.sub(r"\D", "", str(cheque_no or ""))
    if len(cheque_no) < MIN_CHEQUE_DIGITS:
        return []
    seen, out = set(), []
    for r in bank_rows:
        if cheque_no not in _digit_runs(r.get("text")):
            continue
        key = (r.get("bank", ""), r.get("amount"), r.get("date", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append({"bank": r.get("bank", ""), "amount": r.get("amount"),
                    "date": r.get("date", "")})
    return out


def parse_bank_files(files):
    """``files`` = list of (path, source_name). Returns (rows, summary).

    rows: flat list of {bank, text, amount, date} across all statements, used
    to search for cheque numbers. summary: per-file {bank, source, lines, error}.
    """
    rows, summary = [], []
    for path, source in list(files)[:MAX_BANK_FILES]:
        try:
            parsed = bank_statement.read_bank(path)
        except Exception as exc:  # noqa: BLE001 — a bad file must not block the batch
            summary.append({"bank": Path(source).stem or "statement",
                            "source": source, "lines": 0,
                            "error": f"{type(exc).__name__}: {exc}"})
            continue
        bank = detect_bank(parsed.get("metadata"), source)
        n = 0
        for ln in parsed["lines"]:
            amt = ln["credit"] if ln["credit"] > 0 else (
                ln["debit"] if ln["debit"] > 0
                else bank_statement.parse_amount(ln["amount"]))
            rows.append({"bank": bank,
                         "text": ln.get("text") or ln["description"],
                         "amount": round(amt, 2), "date": ln["date"]})
            n += 1
        summary.append({"bank": bank, "source": source, "lines": n, "error": ""})
    return rows, summary


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


def build_excel(out_path, payload):
    """One-sheet cheque register: each cheque + where it appears on the banks."""
    import xlsxwriter

    wb = xlsxwriter.Workbook(str(out_path))
    head = wb.add_format({"bold": True, "bg_color": "#0b2545",
                          "font_color": "white", "border": 1})
    cell = wb.add_format({"border": 1})
    wrap = wb.add_format({"border": 1, "text_wrap": True, "valign": "top"})
    ok_f = wb.add_format({"border": 1, "bold": True, "font_color": "#157a4a"})
    no_f = wb.add_format({"border": 1, "bold": True, "font_color": "#bd3727"})
    num = wb.add_format({"border": 1, "num_format": "#,##0.00"})

    ws = wb.add_worksheet("Cheques")
    widths = [22, 16, 26, 16, 14, 20, 16, 22, 18, 16]
    for i, w in enumerate(widths):
        ws.set_column(i, i, w)
    headers = ["File", "Cheque number", "Customer name", "Amount", "Cheque date",
               "Drawee bank", "On bank statement?", "Bank(s)",
               "Amount per statement", "Transaction date"]
    for c, h in enumerate(headers):
        ws.write(0, c, h, head)
    r = 1
    for c in payload["cheques"]:
        ws.write(r, 0, c["filename"], cell)
        res = c.get("result")
        if c["status"] == "error" or not res:
            ws.write(r, 1, "ERROR", no_f)
            ws.write(r, 2, c.get("error", ""), wrap)
            r += 1
            continue
        ws.write(r, 1, res.get("cheque_number", ""), cell)
        ws.write(r, 2, res.get("customer_name", ""), cell)
        amt = res.get("amount")
        ws.write_number(r, 3, amt, num) if isinstance(amt, (int, float)) \
            else ws.write(r, 3, "", cell)
        ws.write(r, 4, res.get("cheque_date", ""), cell)
        ws.write(r, 5, res.get("drawee_bank", ""), cell)
        apps = c.get("appearances", [])
        ws.write(r, 6, "YES" if apps else "NOT FOUND", ok_f if apps else no_f)
        ws.write(r, 7, "\n".join(a["bank"] for a in apps), wrap)
        ws.write(r, 8, "\n".join(f"{a['amount']:,.2f}"
                                 if isinstance(a["amount"], (int, float))
                                 else str(a["amount"]) for a in apps), wrap)
        ws.write(r, 9, "\n".join(a["date"] for a in apps), wrap)
        r += 1
    wb.close()
    return out_path
