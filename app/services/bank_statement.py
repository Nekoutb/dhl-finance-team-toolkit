"""Read a bank statement and fuzzy-match customer names against its narration.

Flags customers that still have open receivables but whose name (or a close
variant) appears on the bank statement — a strong sign a payment was made but
not yet applied to the account.
"""
import re
import unicodedata
from difflib import SequenceMatcher

from . import excel_reader

NARRATION_CANDIDATES = ["description", "narration", "details", "detail",
                        "libelle", "label", "wording", "motif",
                        "beneficiaire", "remitter", "payer", "donneur",
                        "counterparty", "object", "objet", "reference", "text",
                        "name", "sender"]
AMOUNT_CANDIDATES = ["credit amount", "credit", "amount", "montant", "value",
                     "deposit"]
DATE_CANDIDATES = ["value date", "posting date", "operation date",
                   "date operation", "date valeur", "transaction date", "date"]
# Running-balance / closing-balance column. Kept distinct from amount columns
# so the last row's value can be read as the statement's closing balance.
BALANCE_CANDIDATES = ["running balance", "closing balance", "ending balance",
                      "new balance", "account balance", "balance c/f",
                      "solde courant", "solde du compte", "solde apres",
                      "solde final", "nouveau solde", "solde", "balance"]


def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", str(s or ""))
                   if not unicodedata.combining(c))


# Payer / originator: many statements print it after "Donneur d'ordre".
_PAYER_RE = re.compile(
    r"donneur\s*d\s*['’ʼ` ]?\s*ordre\s*[:\-]*\s*(.+?)(?:\s{2,}|[\r\n]|$)",
    re.IGNORECASE)


_FR_MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5,
              "juin": 6, "juillet": 7, "aout": 8, "septembre": 9,
              "octobre": 10, "novembre": 11, "decembre": 12}
_EN_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
              "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def normalize_date(value):
    """Best-effort ISO yyyy-mm-dd. Returns the ORIGINAL text (not truncated)
    when it can't be parsed, so dates stay readable and group correctly."""
    s = str(value or "").strip()
    if not s:
        return ""
    if re.match(r"\d{4}-\d{2}-\d{2}", s):                 # already ISO
        return s[:10]
    m = re.match(r"^(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})$", s)
    if m:
        a, b, c = m.groups()
        try:
            if len(a) == 4:                               # yyyy/mm/dd
                y, mo, d = int(a), int(b), int(c)
            else:                                         # dd/mm/yyyy (FR)
                d, mo, y = int(a), int(b), int(c)
                y += 2000 if y < 100 else 0
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
        except ValueError:
            pass
    m = re.match(r"^(\d{1,2})\s+([A-Za-zÀ-ÿ]+)\.?\s+(\d{4})$", s)  # 10 juin 2026
    if m:
        d, mon, y = m.groups()
        mon = _strip_accents(mon.lower())
        mo = _FR_MONTHS.get(mon) or _EN_MONTHS.get(mon[:3])
        if mo:
            return f"{int(y):04d}-{mo:02d}-{int(d):02d}"
    return s


def extract_payer(text):
    """The payer/originator named after 'Donneur d'ordre' (else empty)."""
    m = _PAYER_RE.search(str(text or ""))
    if not m:
        return ""
    name = m.group(1).strip(" .:-")
    # When read from a whole row, a trailing amount can follow the payer name —
    # drop it (e.g. "ACME LOGISTICS 1500000" -> "ACME LOGISTICS").
    name = re.sub(r"\s+[-+]?\d[\d\s.,]*$", "", name).strip(" .:-")
    return name

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
    # Accent-insensitive header matching so French columns (Crédit, Débit,
    # Libellé, Opération…) are detected the same as their ASCII forms.
    lowered = {h: _strip_accents(h.lower()) for h in header}

    def pick(cands, exclude=()):
        for cand in cands:
            cand = _strip_accents(cand.lower())
            for h in header:
                if h not in exclude and cand in lowered[h]:
                    return h
        return None

    desc_col = pick(NARRATION_CANDIDATES)
    credit_col = pick(["credit amount", "credit", "deposit", "montant credit",
                       "encaissement", "versement", "entree", "recette"])
    debit_col = pick(["debit amount", "debit", "withdrawal", "montant debit",
                      "retrait", "sortie", "decaissement"],
                     exclude={credit_col})
    amt_col = credit_col or pick(AMOUNT_CANDIDATES)
    date_col = pick(DATE_CANDIDATES)
    # Balance column — never re-use an amount/credit/debit column for it.
    bal_col = pick(BALANCE_CANDIDATES,
                   exclude={c for c in (credit_col, debit_col, amt_col) if c})

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
        date_str = normalize_date(date_raw)
        # Full row text (every cell) so callers can search for references such
        # as a cheque number that may live in a column other than the narration.
        row_text = " ".join(str(v) for v in data.values() if v not in (None, ""))
        # Running balance for this row — None (not 0.0) when the cell is blank,
        # so the closing balance is the last row that actually carries one.
        balance = None
        if bal_col:
            raw_bal = data.get(bal_col)
            if str(raw_bal or "").strip():
                balance = parse_amount(raw_bal)
        lines.append({
            "description": desc,
            "amount": data.get(amt_col) if amt_col else "",
            "credit": credit,
            "debit": debit,
            "date": date_str,
            "balance": balance,
            "text": row_text,
            # Payer/originator (after "Donneur d'ordre") — prefer the narration
            # (clean), fall back to the whole row if the label sits elsewhere.
            "payer": extract_payer(desc) or extract_payer(row_text),
        })
    # Closing balance = the balance on the LATEST-DATED row (statements can be
    # exported newest-first, where the last file row is the OPENING balance —
    # file order alone would pick the wrong end). Ties on the same day keep the
    # later file row; rows without parseable dates fall back to file order.
    dated = [(ln["date"], i) for i, ln in enumerate(lines)
             if ln.get("balance") is not None
             and re.fullmatch(r"\d{4}-\d{2}-\d{2}", ln.get("date") or "")]
    if dated:
        closing_balance = lines[max(dated)[1]]["balance"]
    else:
        closing_balance = next(
            (ln["balance"] for ln in reversed(lines)
             if ln.get("balance") is not None), None)
    return {"lines": lines, "metadata": parsed.get("metadata", []),
            "desc_col": desc_col, "amount_col": amt_col,
            "credit_col": credit_col, "date_col": date_col,
            "balance_col": bal_col, "closing_balance": closing_balance}


def best_customer_for(description, customers, min_ratio=0.86):
    """Match one bank narration line to the most similar AR customer name.

    The expensive fuzzy ratio is only computed when at least one of the
    customer's distinctive tokens already overlaps the narration — otherwise a
    ratio of >= 0.86 is effectively impossible, so it is skipped (keeps the tool
    fast even with hundreds of customers x hundreds of lines).
    """
    desc_norm = _norm(description)
    if not desc_norm:
        return None
    best = None
    for c in customers:
        name_tokens = _tokens(c["customer"])
        if not name_tokens:
            continue
        distinctive = any(len(t) >= 5 for t in name_tokens)
        if all(t in desc_norm for t in name_tokens) and distinctive:
            score = 0.9
        elif any(t in desc_norm for t in name_tokens):
            score = SequenceMatcher(None, _norm(c["customer"]), desc_norm).ratio()
        else:
            continue
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
    # Match against the payer (originator) AND the narration text together.
    norm_lines = [(ln, _norm((ln.get("payer") or "") + " " + ln["description"]))
                  for ln in lines]
    norm_lines = [(ln, t) for ln, t in norm_lines if t]

    matches = []
    for c in customers:
        if c["total_ar"] <= 0:                     # only customers still owing
            continue
        name_norm = _norm(c["customer"])
        name_tokens = _tokens(c["customer"])
        if not name_norm or not name_tokens:
            continue
        distinctive = any(len(t) >= 5 for t in name_tokens)
        best = None
        for ln, desc_norm in norm_lines:
            # Cheap token check first; only run the costly fuzzy ratio when a
            # token already overlaps (a no-overlap ratio can't reach min_ratio).
            if all(t in desc_norm for t in name_tokens) and distinctive:
                score = 0.9
            elif any(t in desc_norm for t in name_tokens):
                score = SequenceMatcher(None, name_norm, desc_norm).ratio()
            else:
                continue
            if score >= min_ratio and (best is None or score > best["score"]):
                best = {"line": ln, "score": round(score, 2)}
        if best:
            matches.append({
                "customer": c["customer"], "key": c["key"],
                "total_ar": c["total_ar"], "currently_held": c.get("currently_held", False),
                "critical": c.get("critical", False),
                "score": best["score"],
                "bank": best["line"].get("bank", ""),
                "bank_description": best["line"]["description"],
                "bank_amount": best["line"]["amount"],
                "bank_date": best["line"]["date"],
            })
    matches.sort(key=lambda m: (-m["score"], -m["total_ar"]))
    return matches
