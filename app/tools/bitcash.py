"""BIT & Cash AR — Bank In Transit and Cash Account Receivables tracking,
payment-statement reconciliation and CM01 journal-entry generation.

Two uploads: the BIT file and the Cash AR file (Excel). Each upload replaces
the previous file of its kind; the number of OPEN items counted at upload is
snapshotted per day so the section can disclose today's position and plot the
daily series. The parsed rows are also kept so customer payment statements
(pick-up sheets / remittance evidence) can be reconciled:

  - the statement's detailed invoice references (AWBs) are looked up in the
    Cash AR (Assignment column carries the AWB),
  - its TOTAL is looked up among the BIT amounts (the amount actually paid),
  - each statement gets a sandbox (auto-matches highlighted; the user adjusts
    selections, approves/unapproves),
  - approved sandboxes are written into the CM01_PC journal-entry template
    (one document per sandbox: a 40 line from the BIT + a 15 line per AWB).

"Open" items: when the file carries a status-like column (status / statut /
état / state / open …), rows whose value reads open/ouvert/o/blank count as
open; without such a column every row is an open item (these files are
typically extracts of open items only).
"""
import json
import os
import re
import threading
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock

from ..config import BASE_DIR, DATA_DIR
from ..services import ai_ocr, excel_reader

STORE_PATH = DATA_DIR / "bitcash.json"
ROWS_PATH = DATA_DIR / "bitcash_rows.json"
FILES_DIR = DATA_DIR / "bitcash" / "files"
RECON_DIR = DATA_DIR / "bitcash" / "recons"
TEMPLATE_PATH = BASE_DIR / "samples" / "cm01_pc_template.xlsx"
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
    kind, snapshot today's open counts into the daily history, and persist the
    parsed rows for reconciliation + journal-entry generation."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind}")
    counts = count_items(path)
    _persist_rows(kind, path)
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
    """{current: {bit, cash}, history: [(date, {bit_open, cash_open}), …],
    processing: {...}|None, processing_error: {...}|None}."""
    data = _load()
    return {"current": data.get("current", {}),
            "history": sorted(data.get("history", {}).items()),
            "processing": data.get("processing"),
            "processing_error": data.get("processing_error")}


def process_uploads_async(jobs):
    """Parse the uploaded BIT / Cash AR file(s) on a background thread — big
    extracts take longer than a web request may (proxy timeouts), so the
    upload responds instantly and the page shows progress until done.

    ``jobs`` = [(kind, stored_path, source_name)].
    """
    with _lock:
        data = _load()
        data["processing"] = {
            "started": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "files": [{"kind": k, "label": KINDS[k], "source": s}
                      for k, _p, s in jobs]}
        data.pop("processing_error", None)
        _save(data)

    def _runner():
        errors = []
        for kind, path, source in jobs:
            try:
                record_upload(kind, path, source)
            except Exception as exc:  # noqa: BLE001 — surfaced on the page
                errors.append(f"Could not read {source or kind}: "
                              f"{type(exc).__name__}: {exc}")
        with _lock:
            data = _load()
            data.pop("processing", None)
            if errors:
                data["processing_error"] = {
                    "at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "message": " · ".join(errors)}
            _save(data)

    threading.Thread(target=_runner, daemon=True).start()


# --------------------------------------------------------------------------- #
# Parsed rows kept for reconciliation + the journal entry
# --------------------------------------------------------------------------- #
def _atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _digits(v):
    return re.sub(r"\D", "", str(v or ""))


def _cellstr(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _to_float(v):
    try:
        return round(float(str(v).replace(",", "").replace(" ", "") or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _persist_rows(kind, path):
    """Parse an uploaded BIT / Cash AR file into the row store used by the
    reconciliation sandboxes and the journal entry."""
    parsed = excel_reader.read_transactions(path)
    header = parsed["header"]
    low = {h: _norm(h) for h in header}

    def col(*cands):
        for cand in cands:
            for h in header:
                if low[h] == cand:
                    return h
        for cand in cands:
            for h in header:
                if cand in low[h]:
                    return h
        return None

    with _lock:
        store = {}
        if ROWS_PATH.exists():
            try:
                store = json.loads(ROWS_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                store = {}
        if kind == "bit":
            c_gl = col("g/l account")
            c_asg = col("assignment")
            c_amt = col("company code currency value")
            c_date = col("posting date")
            c_ref = col("reference")
            c_txt = col("text")
            c_doc = col("document number")
            c_key = col("posting key")
            rows = []
            for i, r in enumerate(parsed["rows"]):
                d = r["data"]
                rows.append({
                    "id": i,
                    "gl_account": _cellstr(d.get(c_gl)),
                    "assignment": _cellstr(d.get(c_asg)),
                    "amount": _to_float(d.get(c_amt)),
                    "posting_date": _cellstr(d.get(c_date))[:10],
                    "reference": _cellstr(d.get(c_ref)),
                    "text": _cellstr(d.get(c_txt))[:60],
                    "doc_no": _cellstr(d.get(c_doc)),
                    "posting_key": _cellstr(d.get(c_key)),
                    "raw": [_cellstr(d.get(h)) for h in header],
                })
            store["bit_header"] = list(header)
            store["bit"] = rows
        else:
            c_acct = col("sap acct", "sap account")
            c_asg = col("assignment")
            c_ref = col("reference")
            c_amt = col("amount")
            c_name = col("customer account: name", "customer name", "name")
            c_doc = col("doc. no.", "doc no")
            rows = []
            for i, r in enumerate(parsed["rows"]):
                d = r["data"]
                rows.append({
                    "id": i,
                    "sap_acct": _cellstr(d.get(c_acct)),
                    "awb": _digits(d.get(c_asg)),
                    "assignment": _cellstr(d.get(c_asg)),
                    "reference": _cellstr(d.get(c_ref)),
                    "amount": _to_float(d.get(c_amt)),
                    "customer": _cellstr(d.get(c_name))[:40],
                    "doc_no": _cellstr(d.get(c_doc)),
                })
            store["cash"] = rows
        _atomic(ROWS_PATH, json.dumps(store, ensure_ascii=False))


def rows_store():
    if not ROWS_PATH.exists():
        return {"bit_header": [], "bit": [], "cash": []}
    try:
        data = json.loads(ROWS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"bit_header": [], "bit": [], "cash": []}
    data.setdefault("bit_header", [])
    data.setdefault("bit", [])
    data.setdefault("cash", [])
    return data


def search_cash(query, limit=20):
    """Find Cash AR invoices by AWB / reference digits or customer name — used
    by the sandbox to add invoices for clearance manually."""
    q = str(query or "").strip()
    if not q:
        return []
    qd = _digits(q)
    ql = q.lower()
    out = []
    for r in rows_store()["cash"]:
        if (qd and (qd in r["awb"] or qd in _digits(r["reference"]))) \
                or (not qd and ql in r["customer"].lower()):
            out.append(r)
            if len(out) >= limit:
                break
    return out


# --------------------------------------------------------------------------- #
# Reconciliation sandboxes (one per uploaded payment statement)
# --------------------------------------------------------------------------- #
def _recon_path(token):
    return RECON_DIR / f"{token}.json"


def save_recon(rec):
    _atomic(_recon_path(rec["token"]), json.dumps(rec, ensure_ascii=False))
    return rec["token"]


def load_recon(token):
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return None
    p = _recon_path(token)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def list_recons():
    """All sandboxes, open/reading first then approved, newest first inside."""
    if not RECON_DIR.exists():
        return []
    out = []
    for p in RECON_DIR.glob("*.json"):
        rec = load_recon(p.stem)
        if rec:
            out.append(rec)
    out.sort(key=lambda r: r.get("uploaded", ""), reverse=True)
    out.sort(key=lambda r: r.get("status") == "approved")
    return out


# A BIT payment rarely lands to the franc (rounded deposits, small bank
# charges) — when no exact match exists, candidates are searched within this
# margin of the evidence total (closest first).
BIT_MATCH_MARGIN = 1000.0


def automatch(statement):
    """Match a parsed statement against the stored Cash AR + BIT rows.

    The statement TOTAL is always recomputed arithmetically from its lines
    (evidence files often carry a broken formula or a text like
    "TOTAL: 523.571" in the total cell — never trusted); any differing total
    stated in the document is kept as ``stated_total`` for disclosure.

    Returns (lines, ar_selected, bit_candidates, bit_selected):
      - each statement line gains matched Cash AR row ids (by AWB digits,
        falling back to the reference column),
      - BIT candidates = rows whose |amount| is within BIT_MATCH_MARGIN of
        the evidence total, closest first; exactly one exact match ->
        auto-selected, otherwise the user chooses; none -> nothing selected.
    """
    data = rows_store()
    by_awb = {}
    for r in data["cash"]:
        if r["awb"]:
            by_awb.setdefault(r["awb"], []).append(r["id"])
    lines, ar_selected = [], []
    for ln in statement.get("lines", []):
        ref = _digits(ln.get("reference"))
        ids = list(by_awb.get(ref, []))
        if not ids and ref:
            ids = [r["id"] for r in data["cash"]
                   if ref and ref in _digits(r["reference"])]
        lines.append({**ln, "matched_ids": ids})
        ar_selected.extend(ids)

    # Arithmetic total of the evidences (authoritative).
    if lines:
        arith = round(sum(_to_float(ln.get("amount")) or 0 for ln in lines), 2)
        stated = _to_float(statement.get("total"))
        if arith:
            if stated and abs(stated - arith) > 0.005:
                statement["stated_total"] = stated
            statement["total"] = arith

    total = _to_float(statement.get("total"))
    cands, exact = [], []
    if total:
        near = sorted((r for r in data["bit"]
                       if abs(abs(r["amount"]) - abs(total)) <= BIT_MATCH_MARGIN),
                      key=lambda r: (abs(abs(r["amount"]) - abs(total)), r["id"]))
        cands = [r["id"] for r in near]
        exact = [r["id"] for r in near if abs(r["amount"]) == abs(total)]
    bit_selected = exact[0] if len(exact) == 1 \
        else (cands[0] if len(cands) == 1 else None)
    return lines, sorted(set(ar_selected)), cands, bit_selected


def create_recon_async(stored_path, media_type, source, uploaded_by):
    """Create a sandbox stub and AI-read/match the statement on a background
    thread (a scanned PDF can take a minute — never block the request)."""
    token = uuid.uuid4().hex[:12]
    save_recon({"token": token, "status": "reading",
                "uploaded": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "uploaded_by": uploaded_by or "", "source": source,
                "statement": None, "error": ""})
    from ..config import load_config

    ai_cfg = load_config().get("ai")

    def _runner():
        try:
            if media_type == "excel":
                statement = _read_excel_statement(stored_path)
            else:
                statement = ai_ocr.extract_payment_statement(
                    Path(stored_path).read_bytes(), media_type, ai_cfg)
            lines, ar_sel, cands, bit_sel = automatch(statement)
            statement["lines"] = lines
            # "Duplicate" = the EXACT amount appears more than once in the
            # BIT; near matches within the margin are choices, not duplicates.
            bit_amounts = {r["id"]: abs(r["amount"])
                           for r in rows_store()["bit"]}
            tot = abs(_to_float(statement.get("total")) or 0)
            exact_n = sum(1 for i in cands if bit_amounts.get(i) == tot)
            rec = load_recon(token) or {}
            rec.update({
                "status": "open", "statement": statement,
                "ar_selected": ar_sel, "bit_candidates": cands,
                "bit_selected": bit_sel,
                "bit_duplicate": exact_n > 1, "error": ""})
            save_recon(rec)
        except Exception as exc:  # noqa: BLE001 — surface, never crash silently
            rec = load_recon(token) or {"token": token}
            rec.update({"status": "error",
                        "error": f"{type(exc).__name__}: {exc}"})
            save_recon(rec)

    threading.Thread(target=_runner, daemon=True).start()
    return token


_REF_CANDS = ("awb", "waybill", "reference", "invoice", "ref")
_AMT_CANDS = ("amount", "montant", "value")


def _scan_statement_rows(path):
    """Raw fallback for evidence sheets the table reader mis-headers (e.g. an
    Excel table's phantom 'Column1/Column2' row above the real
    'Date / Waybill / Amount (Xaf)' header, decorative title rows, or a text
    'TOTAL: …' cell). Scans every sheet for the first row carrying BOTH a
    reference-like and an amount-like heading, then reads the lines below it
    (rows without a reference — like the TOTAL row — are skipped)."""
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            header_at = ref_col = amt_col = None
            for i, row in enumerate(rows):
                normed = [_norm(v) for v in row]
                rc = next((j for j, v in enumerate(normed)
                           if v and any(c in v for c in _REF_CANDS)), None)
                ac = next((j for j, v in enumerate(normed)
                           if v and any(c in v for c in _AMT_CANDS)), None)
                if rc is not None and ac is not None and rc != ac:
                    header_at, ref_col, amt_col = i, rc, ac
                    break
            if header_at is None:
                continue
            lines = []
            for row in rows[header_at + 1:]:
                ref = _digits(row[ref_col]) if ref_col < len(row) else ""
                amt = _to_float(row[amt_col]) if amt_col < len(row) else 0.0
                if ref and amt:
                    lines.append({"reference": ref, "amount": amt,
                                  "description": ""})
            if lines:
                return lines
        return []
    finally:
        wb.close()


def _read_excel_statement(path):
    """A typed (Excel) payment statement: reference + amount columns. Falls
    back to a raw row scan when the table reader finds no usable lines."""
    parsed = excel_reader.read_transactions(path)
    header = parsed["header"]
    low = {h: _norm(h) for h in header}

    def col(*cands):
        for cand in cands:
            for h in header:
                if cand in low[h]:
                    return h
        return None

    c_ref = col(*_REF_CANDS)
    c_amt = col(*_AMT_CANDS)
    c_desc = col("consignor", "customer", "client", "name", "description")
    lines = []
    for r in parsed["rows"]:
        d = r["data"]
        ref = _digits(d.get(c_ref)) if c_ref else ""
        amt = _to_float(d.get(c_amt)) if c_amt else 0.0
        if ref and amt:
            lines.append({"reference": ref, "amount": amt,
                          "description": _cellstr(d.get(c_desc))[:40]
                          if c_desc else ""})
    if not lines:
        lines = _scan_statement_rows(path)
    if not lines:
        raise ValueError(
            "no AWB/amount lines could be read — the file needs a "
            "'Waybill / AWB / Reference' column and an 'Amount' column")
    return {"label": Path(path).stem, "date": "",
            "total": round(sum(l["amount"] for l in lines), 2), "lines": lines}


def recon_view(recon):
    """Resolve a sandbox's row ids into full rows for the template, plus the
    live reconciliation difference, the manual plug and the residual left
    after it."""
    data = rows_store()
    statement = recon.get("statement") or {}
    total = abs(_to_float(statement.get("total")) or 0)
    bit_by_id = {r["id"]: r for r in data["bit"]}
    cash_by_id = {r["id"]: r for r in data["cash"]}
    ar_rows = [cash_by_id[i] for i in recon.get("ar_selected", [])
               if i in cash_by_id]
    matched_ids = {i for ln in statement.get("lines", [])
                   for i in ln.get("matched_ids", [])}
    cands = [{**bit_by_id[i],
              "delta": round(abs(bit_by_id[i]["amount"]) - total, 2)}
             for i in recon.get("bit_candidates", []) if i in bit_by_id]
    sel = recon.get("bit_selected")
    bit_row = bit_by_id.get(sel) if sel is not None else None
    ar_total = round(sum(r["amount"] for r in ar_rows), 2)
    bit_amount = abs(bit_row["amount"]) if bit_row else 0.0
    plug = recon.get("plug") or None
    plug_amount = round(_to_float((plug or {}).get("amount")) or 0, 2)
    difference = round(ar_total - bit_amount, 2)
    return {"ar_rows": ar_rows, "auto_matched_ids": matched_ids,
            "bit_candidates": cands, "bit_row": bit_row,
            "ar_total": ar_total, "bit_amount": bit_amount,
            "difference": difference, "plug": plug,
            "plug_amount": plug_amount,
            "residual": round(difference - plug_amount, 2),
            "margin": BIT_MATCH_MARGIN}


def set_plug(token, amount, account="", note=""):
    """Record (or clear, when the amount is 0/blank) the manual plug that
    absorbs a remaining reconciliation difference so the journal entry
    balances. Only while the sandbox is open."""
    rec = load_recon(token)
    if not rec or rec.get("status") not in ("open",):
        return None
    amt = _to_float(amount)
    if not amt:
        rec.pop("plug", None)
    else:
        rec["plug"] = {"amount": round(amt, 2),
                       "account": str(account or "").strip(),
                       "note": str(note or "").strip()}
    save_recon(rec)
    return rec


def set_selection(token, ar_ids, bit_id):
    rec = load_recon(token)
    if not rec or rec.get("status") not in ("open",):
        return None
    data = rows_store()
    valid_cash = {r["id"] for r in data["cash"]}
    valid_bit = {r["id"] for r in data["bit"]}
    rec["ar_selected"] = sorted({int(i) for i in ar_ids
                                 if str(i).lstrip("-").isdigit()
                                 and int(i) in valid_cash})
    rec["bit_selected"] = (int(bit_id) if str(bit_id).lstrip("-").isdigit()
                           and int(bit_id) in valid_bit else None)
    save_recon(rec)
    return rec


def set_status(token, approved, by):
    rec = load_recon(token)
    if not rec or rec.get("status") not in ("open", "approved"):
        return None
    if approved:
        rec["status"] = "approved"
        rec["approved_by"] = by or ""
        rec["approved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    else:
        rec["status"] = "open"
        rec.pop("approved_by", None)
        rec.pop("approved_at", None)
    save_recon(rec)
    return rec


def delete_recon(token):
    rec = load_recon(token)
    if rec and rec.get("status") != "approved":
        _recon_path(token).unlink(missing_ok=True)
        return True
    return False


# --------------------------------------------------------------------------- #
# CM01 journal-entry generation (approved sandboxes only)
# --------------------------------------------------------------------------- #
_MANUAL = "MANUAL RECIEPT "        # the template's exact wording (sic)


def _compact_date(d):
    """23.06.2026 -> 23626, exactly as the team's template writes dates."""
    return int(f"{d.day}{d.month}{d.strftime('%y')}")


def build_journal(out_path, now=None):
    """Write every APPROVED sandbox into the CM01 template as one document
    each (LI. 1..N): a posting-key-40 line from the selected BIT payment, then
    a posting-key-15 line per selected Cash AR invoice. Returns
    {count, lines, name} or None when nothing is approved/complete."""
    import openpyxl

    from openpyxl.styles import Font

    approved = [r for r in list_recons() if r.get("status") == "approved"
                and r.get("bit_selected") is not None and r.get("ar_selected")]
    if not approved:
        return None
    arial = Font(name="Arial", size=10)
    now = now or datetime.now()
    data = rows_store()
    bit_by_id = {r["id"]: r for r in data["bit"]}
    cash_by_id = {r["id"]: r for r in data["cash"]}

    wb = openpyxl.load_workbook(TEMPLATE_PATH)
    old_tab = next(n for n in wb.sheetnames if n.startswith("CM01_"))
    new_tab = f"CM01_{now.strftime('%d.%m.%Y')}_PC_001"
    ws = wb[old_tab]
    ws.title = new_tab
    # Renaming does NOT rewrite formulas — patch every reference by hand.
    for sheet in wb.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    cell.value = cell.value.replace(old_tab, new_tab)

    compact = _compact_date(now)
    r = 4
    lines = 0
    bank_rows = []
    for li, rec in enumerate(approved, 1):
        bit = bit_by_id.get(rec["bit_selected"])
        ars = [cash_by_id[i] for i in rec["ar_selected"] if i in cash_by_id]
        if not bit or not ars:
            continue
        bank_rows.append(bit)
        entries = [(bit["gl_account"], 40, abs(bit["amount"]),
                    bit["assignment"])]
        entries += [(a["sap_acct"], 15, a["amount"], a["awb"] or a["assignment"])
                    for a in ars]
        # Manual plug: the user-entered difference line that balances the
        # document (posting key 40 when the BIT is short, 50 when over).
        plug = rec.get("plug") or {}
        plug_amt = _to_float(plug.get("amount"))
        if plug_amt and plug.get("account"):
            entries.append((plug["account"], 40 if plug_amt > 0 else 50,
                            abs(plug_amt),
                            plug.get("note") or "DIFFERENCE PLUG"))
        for account, key, amount, assignment in entries:
            ws.cell(row=r, column=1, value=li)                       # LI.
            ws.cell(row=r, column=2, value="CM01")                   # Comp code
            ws.cell(row=r, column=3, value="DZ")                     # Doc type
            ws.cell(row=r, column=4, value=compact)                  # Post date
            ws.cell(row=r, column=5, value=compact)                  # Doc date
            ws.cell(row=r, column=6, value="XAF")                    # Currency
            ws.cell(row=r, column=7, value=_MANUAL)                  # Ref doc
            ws.cell(row=r, column=8, value=_MANUAL)                  # Header txt
            # column 9 (Tax calculation) stays empty
            acct = ws.cell(row=r, column=10)                         # Account
            acct.value = int(account) if str(account).isdigit() else account
            ws.cell(row=r, column=11, value=key)                     # Post key
            ws.cell(row=r, column=12, value=amount)                  # Amount
            asg = ws.cell(row=r, column=14)                          # Assignment
            asg.value = str(assignment)
            asg.number_format = "@"
            for col in range(1, 15):        # uniform Arial 10, no stray colours
                ws.cell(row=r, column=col).font = arial
            r += 1
            lines += 1

    # BANK DETAILS: the raw BIT line of each payment in the journal.
    bd_name = next((n for n in wb.sheetnames
                    if n.strip().upper() == "BANK DETAILS"), None)
    if bd_name:
        bd = wb[bd_name]
        for i, bit in enumerate(bank_rows, start=2):
            for c, val in enumerate(bit.get("raw", []), start=1):
                cell = bd.cell(row=i, column=c,
                               value=(float(val)
                                      if re.fullmatch(r"-?\d+\.?\d*", val)
                                      and c not in (9,) else val) if val
                               else None)
                cell.font = arial

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)
    name = f"CM01_PC TEMPLATE_{now.strftime('%d.%m.%y')}_PC.xlsx"
    for rec in approved:
        rec["generated_at"] = now.strftime("%Y-%m-%d %H:%M")
        save_recon(rec)
    return {"count": len(bank_rows), "lines": lines, "name": name}
