"""Bank Statements — collection activity reports.

Upload a bank statement; credited balances are treated as collections. The
narration text is matched against customer names to identify who paid, and —
when a CtP analysis exists — each payer is linked to their receivables
position (net AR, overdue, risk status, coverage of the payment).

Reports persist under data/bank/<token>.json so they can be revisited.
"""
import hashlib
import json
import re
import threading
import uuid
from datetime import datetime

from pathlib import Path

from ..config import DATA_DIR
from ..services import ai_ocr, bank_statement
from . import ongoing_ctp as ctp

_AI_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif"}

TOOL_SLUG = "bank-statements"
STORE_DIR = DATA_DIR / "bank"
MAX_FILES = 10

_BANK_WORDS = ("BANK", "BANQUE", "ECOBANK", "AFRILAND", "BICEC", "SGBC",
               "SOCIETE GENERALE", "UBA", "BGFI", "SCB", "CCA", "CITIBANK",
               "STANDARD CHARTERED", "ACCESS", "NFC", "CBC")


def detect_bank(metadata, filename=""):
    """Identify the bank from the statement's header lines (else file name)."""
    for line in metadata or []:
        upper = str(line).upper()
        if any(w in upper for w in _BANK_WORDS):
            # The line itself usually IS the bank name/letterhead.
            return str(line).strip()[:60]
    if metadata:
        return str(metadata[0]).strip()[:60]
    from pathlib import Path as _P
    return _P(filename).stem or "Unknown bank"


def _latest_ar_customers():
    """Customers from the most recent CtP analysis (with hold flags set)."""
    recent = ctp.list_results(limit=1)
    if not recent:
        return [], None
    result = ctp.load_result(recent[0]["token"])
    if not result:
        return [], None
    ctp.hold_compare(result["customers"])
    meta = {"token": recent[0]["token"],
            "as_of": str(result.get("as_of", "")),
            "source": result.get("source", "")}
    return result["customers"], meta


def _read_statement(path, source, ai_cfg):
    """Read ONE statement into normalised lines. PDF/image statements are read
    by the document reader (when a key is set); spreadsheets — and any
    file it can't read — use the column-based table reader.

    Returns (lines, bank_name, method, note). Each line has description, payer,
    credit, debit, date, amount, text.
    """
    ext = Path(path).suffix.lower()
    # ai_cfg is the "ai" sub-config ({api_key, model}); check the key directly
    # (ai_ocr.is_configured expects the FULL config dict).
    if ext in _AI_EXT and (ai_cfg or {}).get("api_key", "").strip():
        try:
            media = ai_ocr.media_type_for(str(path)) or "application/pdf"
            data = ai_ocr.extract_bank_statement(Path(path).read_bytes(),
                                                 media, ai_cfg)
            lines = [{
                "description": ln["description"], "payer": ln.get("payer", ""),
                "credit": ln["credit"], "debit": ln["debit"],
                "date": bank_statement.normalize_date(ln["date"]),
                "amount": ln["credit"] or -ln["debit"] or "",
                "text": (ln.get("payer", "") + " " + ln["description"]).strip(),
            } for ln in data["lines"]]
            return lines, (data.get("bank_name") or detect_bank([], source)), \
                "scan reader", ""
        except (ai_ocr.AiNotConfigured, ai_ocr.AiReadError) as exc:
            note = f"Scan read failed ({exc}); used the table reader."
        except Exception as exc:  # noqa: BLE001
            note = f"Scan read error ({type(exc).__name__}); used the table reader."
        parsed = bank_statement.read_bank(path)
        return parsed["lines"], detect_bank(parsed["metadata"], source), \
            "table reader", note
    parsed = bank_statement.read_bank(path)
    note = ("" if ext not in _AI_EXT
            else "Add the document-reading key in Settings so scanned PDF "
                 "statements can be read in full.")
    return parsed["lines"], detect_bank(parsed["metadata"], source), \
        "table reader", note


def bank_token(bank_name, period=""):
    """Stable 12-hex id for a configured bank slot + statement month
    (YYYY-MM). Re-uploading the same bank for the same month overwrites that
    month's statement; other months (e.g. prior months) accumulate and stay
    searchable. Legacy (pre-period) slots used the bare bank key."""
    key = "bankslot:" + (bank_name or "").strip().lower()
    if period:
        key += "|" + str(period).strip()
    return hashlib.md5(key.encode("utf-8")).hexdigest()[:12]


def slot_report(bank_name, period=""):
    """The statement report for a bank slot (+month), or None."""
    return load_report(bank_token(bank_name, period))


def slot_reports(bank_name):
    """Every stored statement for a configured bank (all months), newest
    period first — includes legacy pre-period uploads (period '')."""
    name = (bank_name or "").strip()
    out = []
    if not STORE_DIR.exists():
        return out
    for path in STORE_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if (data.get("bank_slot") or "").strip().lower() == name.lower():
            out.append(data)
    out.sort(key=lambda d: (d.get("period", ""), d.get("created_at", "")),
             reverse=True)
    return out


def build_report(files, ai_cfg=None, token=None, bank_slot="", period=""):
    """``files`` = list of (path, source_name) — up to MAX_FILES statements,
    combined into one collection report. PDF/scanned statements are read automatically
    when ``ai_cfg`` has an API key.

    ``token`` pins the report id (a bank slot reuses its stable token so an
    upload OVERRIDES that bank's previous statement); ``bank_slot`` is the
    configured bank name that owns this statement (used as the bank label)."""
    if not isinstance(files, (list, tuple)):
        files = [(files, "")]
    files = list(files)[:MAX_FILES]

    credits, debits_total, banks = [], 0.0, []
    statement_lines = []        # every line (credit + debit) for cross-tool search
    for path, source in files:
        lines, bank_name, method, note = _read_statement(path, source, ai_cfg)
        if bank_slot:           # the configured slot name is the bank of record
            bank_name = bank_slot
        file_credits = [dict(ln, bank=bank_name)
                        for ln in lines if ln["credit"] > 0]
        credits.extend(file_credits)
        debits_total += sum(ln["debit"] for ln in lines)
        for ln in lines:
            amt = ln["credit"] if ln["credit"] > 0 else (
                ln["debit"] if ln["debit"] > 0
                else bank_statement.parse_amount(ln.get("amount")))
            statement_lines.append({
                "bank": bank_name,
                "text": ln.get("text") or ln["description"],
                "amount": round(amt, 2), "date": ln["date"]})
        banks.append({
            "bank": bank_name, "source": source, "method": method, "note": note,
            "line_count": len(lines),
            "credit_count": len(file_credits),
            "total_credited": sum(ln["credit"] for ln in file_credits),
        })

    return _assemble_report(
        credits, statement_lines, banks, debits_total,
        token=token or uuid.uuid4().hex[:12], bank_slot=bank_slot,
        period=period,
        source=" + ".join(s for _p, s in files if s) or "statement")


def _assemble_report(credits, statement_lines, banks, debits_total, *,
                     token, bank_slot, source, period=""):
    """Build the report dict from already-read lines, running the AR matching
    and grouping against the CURRENT latest CtP analysis. Shared by build_report
    (fresh upload) and refresh_report (re-link to the latest AR)."""
    customers, ar_meta = _latest_ar_customers()

    # Payments received per payer, INDEPENDENT of any AR match — every credit
    # grouped by its originator (payer after "Donneur d'ordre", else narration).
    by_payer = {}
    for ln in credits:
        payer = (ln.get("payer") or "").strip() or \
            (ln["description"][:60].strip() or "(unnamed)")
        e = by_payer.setdefault(payer.upper(), {
            "payer": payer, "total": 0.0, "count": 0, "banks": [], "dates": []})
        e["total"] += ln["credit"]
        e["count"] += 1
        if ln["bank"] not in e["banks"]:
            e["banks"].append(ln["bank"])
        if ln["date"] and ln["date"] not in e["dates"]:
            e["dates"].append(ln["date"])
    payments_by_payer = sorted(by_payer.values(), key=lambda e: -e["total"])

    by_customer, unmatched = {}, []
    for ln in credits:
        match_text = (ln.get("payer", "") + " " + ln["description"]).strip()
        best = bank_statement.best_customer_for(match_text, customers) \
            if customers else None
        if best:
            c, score = best
            key = c["key"]
            entry = by_customer.setdefault(key, {
                "key": key, "customer": c["customer"],
                "collected": 0.0, "payments": 0, "best_score": 0.0,
                "ar_total": c["total_ar"], "ar_overdue": c["overdue"],
                "status": c["status"], "status_key": c["status_key"],
                "currently_held": c.get("currently_held", False),
                "banks": [], "lines": [],
            })
            entry["collected"] += ln["credit"]
            entry["payments"] += 1
            entry["best_score"] = max(entry["best_score"], score)
            if ln["bank"] not in entry["banks"]:
                entry["banks"].append(ln["bank"])
            entry["lines"].append(ln)
        else:
            unmatched.append(ln)

    matched = sorted(by_customer.values(), key=lambda e: -e["collected"])
    for e in matched:
        e["coverage_pct"] = (e["collected"] / e["ar_total"] * 100) \
            if e["ar_total"] else None
        e["remaining"] = e["ar_total"] - e["collected"]

    daily = {}
    for ln in credits:
        d = daily.setdefault(ln["date"] or "(no date)", {"total": 0.0, "count": 0})
        d["total"] += ln["credit"]
        d["count"] += 1
    daily_rows = sorted(({"date": k, **v} for k, v in daily.items()),
                        key=lambda r: r["date"])

    total_credited = sum(ln["credit"] for ln in credits)
    matched_total = sum(e["collected"] for e in matched)
    return {
        "token": token,
        "bank_slot": bank_slot,
        "period": period,
        "status": "done",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": source,
        "banks": banks,
        "metadata": [],
        "summary": {
            "statement_count": len(banks),
            "credit_count": len(credits),
            "total_credited": total_credited,
            "matched_total": matched_total,
            "matched_pct": (matched_total / total_credited * 100)
                           if total_credited else 0,
            "unmatched_total": total_credited - matched_total,
            "unmatched_count": len(unmatched),
            "debits_total": debits_total,
            "first_date": daily_rows[0]["date"] if daily_rows else "",
            "last_date": daily_rows[-1]["date"] if daily_rows else "",
            "payer_count": len(payments_by_payer),
            "ai_used": any(b.get("method") == "scan reader" for b in banks),
        },
        "matched": matched,
        "unmatched": sorted(unmatched, key=lambda l: -l["credit"]),
        "payments_by_payer": payments_by_payer,
        "daily": daily_rows,
        "ar_link": ar_meta,
        "statement_lines": statement_lines,
    }


def refresh_report(token):
    """Re-run the matching/grouping for a stored statement against the CURRENT
    latest CtP analysis — re-links collections to the newest AR without
    re-uploading the file. Returns the refreshed report, or None."""
    rep = load_report(token)
    if not rep or rep.get("status") != "done":
        return None
    credits = []
    for c in rep.get("matched", []):
        credits.extend(c.get("lines", []))
    credits.extend(rep.get("unmatched", []))
    refreshed = _assemble_report(
        credits, rep.get("statement_lines", []), rep.get("banks", []),
        rep.get("summary", {}).get("debits_total", 0.0),
        token=token, bank_slot=rep.get("bank_slot", ""),
        source=rep.get("source", "statement"), period=rep.get("period", ""))
    save_report(refreshed)
    return refreshed


def sync_cheque_register():
    """New statement lines change which cheques count as presented — re-match
    the whole register and snapshot today's unpresented count so the cheque
    daily graph updates the moment statements are uploaded (not only when
    someone opens the cheque page). Never breaks a statement upload."""
    try:
        from ..services import cheques
        rows, summary = all_statement_lines()
        cheques.refresh_all(rows, summary)
        cheques.record_daily_stats(cheques.register_stats())
    except Exception:  # noqa: BLE001 — best-effort side refresh
        pass


def start_report(files, ai_cfg=None, token=None, bank_slot="", period=""):
    """Kick off the report on a BACKGROUND thread and return its token at once.

    Reading PDF statements can take a minute or more; doing it in
    the request would hit the reverse-proxy timeout (502). The results page
    polls the saved report until ``status`` flips from 'running' to 'done'.

    ``token``/``bank_slot`` pin the report to a configured bank slot so the
    finished statement overrides that bank's previous one."""
    files = list(files)[:MAX_FILES]
    token = token or uuid.uuid4().hex[:12]
    save_report({
        "token": token, "bank_slot": bank_slot, "period": period,
        "status": "running",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": " + ".join(s for _p, s in files if s) or "statement",
        "file_count": len(files),
    })

    def _runner():
        try:
            report = build_report(files, ai_cfg=ai_cfg, token=token,
                                   bank_slot=bank_slot, period=period)
            report["status"] = "done"
            save_report(report)
            sync_cheque_register()
        except Exception as exc:  # noqa: BLE001 — surface, never crash silently
            save_report({
                "token": token, "bank_slot": bank_slot, "period": period,
                "status": "error",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "source": " + ".join(s for _p, s in files if s) or "statement",
                "error": f"{type(exc).__name__}: {exc}",
            })

    threading.Thread(target=_runner, daemon=True).start()
    return token


def delete_report(token):
    if re.fullmatch(r"[0-9a-f]+", token or ""):
        (STORE_DIR / f"{token}.json").unlink(missing_ok=True)


def save_report(report):
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    (STORE_DIR / f"{report['token']}.json").write_text(
        json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report["token"]


def load_report(token):
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return None
    path = STORE_DIR / f"{token}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _line_row(ln):
    """Normalise a stored credit line into a searchable {bank,text,amount,date}."""
    amt = ln.get("credit") or ln.get("amount") or 0
    if isinstance(amt, str):
        amt = bank_statement.parse_amount(amt)
    return {"bank": ln.get("bank", ""),
            "text": ln.get("text") or ln.get("description", ""),
            "amount": round(float(amt or 0), 2), "date": ln.get("date", "")}


def all_statement_lines():
    """Every bank-statement line across ALL stored reports — the single source
    other tools (e.g. cheque matching) search. Returns (rows, summary):
    rows = [{bank, text, amount, date}], summary = [{bank, lines}] per bank.

    Reports saved before ``statement_lines`` existed are reconstructed from the
    credit lines that were kept (matched + unmatched), so old data still works.
    """
    rows = []
    if not STORE_DIR.exists():
        return rows, []
    reports = []
    for path in STORE_DIR.glob("*.json"):
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    reports.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    for rep in reports:
        lines = rep.get("statement_lines")
        if lines is None:                       # legacy report — reconstruct
            lines = [_line_row(ln) for ln in rep.get("unmatched", [])]
            for c in rep.get("matched", []):
                lines += [_line_row(ln) for ln in c.get("lines", [])]
        rows.extend(lines)
    by_bank = {}
    for r in rows:
        by_bank[r.get("bank", "")] = by_bank.get(r.get("bank", ""), 0) + 1
    summary = [{"bank": b or "Unknown bank", "lines": n}
               for b, n in sorted(by_bank.items(), key=lambda kv: -kv[1])]
    return rows, summary


def all_credit_lines():
    """Every CREDIT line across all stored reports, payer preserved —
    [{bank, payer, description, credit, date, text}]. (all_statement_lines()
    merges credits and debits sign-less and drops the payer, so payment
    tracing reads the raw credit lines kept on each report instead.)"""
    out = []
    if not STORE_DIR.exists():
        return out
    reports = []
    for path in STORE_DIR.glob("*.json"):
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    reports.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    for rep in reports:
        lines = list(rep.get("unmatched", []))
        for c in rep.get("matched", []):
            lines += list(c.get("lines", []))
        for ln in lines:
            credit = ln.get("credit", 0)
            if isinstance(credit, str):
                credit = bank_statement.parse_amount(credit)
            credit = round(float(credit or 0), 2)
            if credit <= 0:
                continue
            out.append({"bank": ln.get("bank", ""),
                        "payer": ln.get("payer", ""),
                        "description": ln.get("description", ""),
                        "credit": credit, "date": ln.get("date", ""),
                        "text": ln.get("text", "")})
    return out


def list_reports(limit=10):
    if not STORE_DIR.exists():
        return []
    out = []
    for path in STORE_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({"token": data.get("token", path.stem),
                    "created_at": data.get("created_at", ""),
                    "source": data.get("source", ""),
                    "total": data.get("summary", {}).get("total_credited", 0),
                    "credits": data.get("summary", {}).get("credit_count", 0)})
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out[:limit]
