"""Read a bank statement and fuzzy-match customer names against its narration.

Flags customers that still have open receivables but whose name (or a close
variant) appears on the bank statement — a strong sign a payment was made but
not yet applied to the account.
"""
import re
from difflib import SequenceMatcher

from . import excel_reader

NARRATION_CANDIDATES = ["description", "narration", "details", "detail",
                        "libelle", "libellé", "label", "wording", "motif",
                        "beneficiaire", "bénéficiaire", "remitter", "payer",
                        "counterparty", "object", "objet", "reference", "text",
                        "name", "sender"]
AMOUNT_CANDIDATES = ["credit amount", "credit", "amount", "montant", "value",
                     "deposit"]
DATE_CANDIDATES = ["value date", "posting date", "operation date",
                   "transaction date", "date"]

# Legal forms / geography / filler dropped before matching.
_STOPWORDS = {
    "sarl", "sa", "s.a", "sas", "snc", "ets", "ltd", "plc", "inc", "gmbh",
    "limited", "company", "co", "cie", "group", "groupe", "holding", "holdings",
    "cameroun", "cameroon", "cmr", "the", "and", "of", "du", "de", "des", "la",
    "le", "les", "et", "pour", "international", "intl", "sarlu", "eurl",
}


def _norm(text):
    text = str(text or "").upper()
    text = re.sub(r"[^A-Z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text):
    return [t for t in _norm(text).split() if len(t) >= 4 and t.lower() not in _STOPWORDS]


def parse_amount(value):
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^0-9,.\-]", "", str(value))
    if s.count(",") == 1 and s.count(".") == 0:
        s = s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def read_bank(path):
    """Return {lines, metadata, columns} from a bank statement (Excel or PDF).

    Each line: {date, description, amount (raw), credit (float >0 when money
    came IN), debit (float)}. Credits drive the collection-activity reports.
    """
    from pathlib import Path as _P
    if _P(path).suffix.lower() == ".pdf":
        from .pdf_table_reader import read_pdf_table
        parsed = read_pdf_table(path)
    else:
        parsed = excel_reader.read_transactions(path)
    header = parsed["header"]
    lowered = {h: h.lower() for h in header}

    def pick(cands, exclude=()):
        for cand in cands:
            for h in header:
                if h not in exclude and cand in lowered[h]:
                    return h
        return None

    desc_col = pick(NARRATION_CANDIDATES)
    credit_col = pick(["credit amount", "credit", "deposit", "montant credit"])
    debit_col = pick(["debit amount", "debit", "withdrawal", "montant debit"],
                     exclude={credit_col})
    amt_col = credit_col or pick(AMOUNT_CANDIDATES)
    date_col = pick(DATE_CANDIDATES)

    lines = []
    for row in parsed["rows"]:
        data = row["data"]
        desc = str(data.get(desc_col) or "").strip() if desc_col else \
            " ".join(str(v) for v in data.values() if isinstance(v, str))
        if not desc:
            continue
        if credit_col:
            credit = parse_amount(data.get(credit_col))
            debit = parse_amount(data.get(debit_col)) if debit_col else 0.0
        else:
            raw = parse_amount(data.get(amt_col)) if amt_col else 0.0
            credit, debit = (raw, 0.0) if raw > 0 else (0.0, -raw)
        date_raw = data.get(date_col) if date_col else ""
        date_str = str(date_raw or "")[:10]
        # Full row text (every cell) so callers can search for references such
        # as a cheque number that may live in a column other than the narration.
        row_text = " ".join(str(v) for v in data.values() if v not in (None, ""))
        lines.append({
            "description": desc,
            "amount": data.get(amt_col) if amt_col else "",
            "credit": credit,
            "debit": debit,
            "date": date_str,
            "text": row_text,
        })
    return {"lines": lines, "metadata": parsed.get("metadata", []),
            "desc_col": desc_col, "amount_col": amt_col,
            "credit_col": credit_col, "date_col": date_col}


def best_customer_for(description, customers, min_ratio=0.86):
    """Match one bank narration line to the most similar AR customer name."""
    desc_norm = _norm(description)
    if not desc_norm:
        return None
    best = None
    for c in customers:
        name_norm = _norm(c["customer"])
        if not name_norm:
            continue
        name_tokens = _tokens(c["customer"])
        ratio = SequenceMatcher(None, name_norm, desc_norm).ratio()
        tokens_present = name_tokens and all(t in desc_norm for t in name_tokens)
        distinctive = any(len(t) >= 5 for t in name_tokens)
        score = max(ratio, 0.9) if (tokens_present and distinctive) else ratio
        if score >= min_ratio and (best is None or score > best[1]):
            best = (c, round(score, 2))
    return best


def match_customers(customers, bank, min_ratio=0.86):
    """Match open-AR customers to bank narration lines.

    A match needs either a high overall similarity ratio, or all of the
    customer's distinctive tokens present in the narration (with at least one
    token of length >= 5 to avoid spurious short-token hits).
    """
    lines = bank["lines"]
    norm_lines = [(ln, _norm(ln["description"])) for ln in lines]

    matches = []
    for c in customers:
        if c["total_ar"] <= 0:                     # only customers still owing
            continue
        name_norm = _norm(c["customer"])
        name_tokens = _tokens(c["customer"])
        if not name_norm:
            continue
        best = None
        for ln, desc_norm in norm_lines:
            if not desc_norm:
                continue
            ratio = SequenceMatcher(None, name_norm, desc_norm).ratio()
            tokens_present = name_tokens and all(t in desc_norm for t in name_tokens)
            distinctive = any(len(t) >= 5 for t in name_tokens)
            score = ratio
            if tokens_present and distinctive:
                score = max(score, 0.9)
            if score >= min_ratio and (best is None or score > best["score"]):
                best = {"line": ln, "score": round(score, 2)}
        if best:
            matches.append({
                "customer": c["customer"], "key": c["key"],
                "total_ar": c["total_ar"], "currently_held": c.get("currently_held", False),
                "score": best["score"],
                "bank_description": best["line"]["description"],
                "bank_amount": best["line"]["amount"],
                "bank_date": best["line"]["date"],
            })
    matches.sort(key=lambda m: (-m["score"], -m["total_ar"]))
    return matches
