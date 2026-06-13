"""Bank Statements — collection activity reports.

Upload a bank statement; credited balances are treated as collections. The
narration text is matched against customer names to identify who paid, and —
when a CtP analysis exists — each payer is linked to their receivables
position (net AR, overdue, risk status, coverage of the payment).

Reports persist under data/bank/<token>.json so they can be revisited.
"""
import json
import re
import uuid
from datetime import datetime

from ..config import DATA_DIR
from ..services import bank_statement
from . import ongoing_ctp as ctp

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


def build_report(files):
    """``files`` = list of (path, source_name) — up to MAX_FILES statements,
    combined into one collection report with the bank identified per file."""
    if not isinstance(files, (list, tuple)):
        files = [(files, "")]
    files = list(files)[:MAX_FILES]
    customers, ar_meta = _latest_ar_customers()

    credits, debits_total, banks, metadata = [], 0.0, [], []
    statement_lines = []        # every line (credit + debit) for cross-tool search
    for path, source in files:
        parsed = bank_statement.read_bank(path)
        bank_name = detect_bank(parsed["metadata"], source)
        file_credits = [dict(ln, bank=bank_name)
                        for ln in parsed["lines"] if ln["credit"] > 0]
        credits.extend(file_credits)
        debits_total += sum(ln["debit"] for ln in parsed["lines"])
        for ln in parsed["lines"]:
            amt = ln["credit"] if ln["credit"] > 0 else (
                ln["debit"] if ln["debit"] > 0
                else bank_statement.parse_amount(ln["amount"]))
            statement_lines.append({
                "bank": bank_name,
                "text": ln.get("text") or ln["description"],
                "amount": round(amt, 2), "date": ln["date"]})
        banks.append({
            "bank": bank_name, "source": source,
            "credit_count": len(file_credits),
            "total_credited": sum(ln["credit"] for ln in file_credits),
        })
        metadata.extend(parsed["metadata"][:4])

    by_customer, unmatched = {}, []
    for ln in credits:
        best = bank_statement.best_customer_for(ln["description"], customers) \
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
        "token": uuid.uuid4().hex[:12],
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": " + ".join(s for _p, s in files if s) or "statement",
        "banks": banks,
        "metadata": metadata,
        "summary": {
            "statement_count": len(files),
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
        },
        "matched": matched,
        "unmatched": sorted(unmatched, key=lambda l: -l["credit"]),
        "daily": daily_rows,
        "ar_link": ar_meta,
        "statement_lines": statement_lines,
    }


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
