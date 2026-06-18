"""Parse the AR customer MASTER file.

One row per customer: account number, name, sales segment, total receivable
balance, credit-hold position, plus general information columns (kept as-is
for display). Total rows (balance populated, identifying fields empty, or an
explicit "total" label) are excluded the same way as in the transaction file.
"""
import re

from . import ctp_rules, excel_reader

FIELD_CANDIDATES = {
    # NAME is resolved before ACCOUNT on purpose: the broad "customer"
    # account-candidate is a *substring* of "Customer Name", so if account ran
    # first it would grab the name column, leave the real "Customer"
    # (account-number) column unmapped, and key every customer by name. Names
    # never match the transaction file's account-number keys, so the credit-
    # hold register comes back empty. Claim the name column first.
    "name": ["customer account: name", "customer name", "account name",
             "client name", "debtor name", "name"],
    "account": ["customer number", "customer no", "customer code", "bp number",
                "account number", "account no", "customer", "account", "compte"],
    "segment": ["sales segment", "customer segment", "treatment plan",
                "customer group", "segment", "gctp", "ctp", "channel"],
    "balance": ["receivable balance", "receivables balance", "ar balance",
                "total receivable", "open balance", "outstanding balance",
                "total due", "balance", "amount", "solde"],
    # "Account stop/open" is the customer trial balance's credit-hold column:
    # an "X" = on credit hold (account stopped), blank = open (not on hold).
    "hold": ["account stop/open", "account stop / open", "account stop",
             "stop/open", "stop / open", "credit hold position", "credit hold",
             "on hold", "hold position", "credit stop", "credit block",
             "dunning block", "blocked", "block", "hold"],
    "critical": ["critical customer", "critical customers", "critical",
                 "key customer", "strategic customer", "vip"],
    "payment_term": ["payment terms", "payment term", "terms"],
    "credit_limit": ["credit limit", "cr limit", "limit"],
}

_YES_WORDS = {"x", "y", "yes", "oui", "true", "1", "critical", "vip", "key"}


def _is_yes(value):
    """Strict yes/true reading for flag columns like 'Critical customers'."""
    return str(value if value is not None else "").strip().lower() in _YES_WORDS

_TRUE_WORDS = {"x", "y", "yes", "oui", "true", "1", "on hold", "hold", "held",
               "blocked", "block", "stop", "credit stop", "active"}
_FALSE_WORDS = {"", "n", "no", "non", "false", "0", "-", "none", "off",
                "not held", "no hold", "released", "open", "o"}


def _norm_key(value):
    s = str(value or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _to_amount(value):
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


def parse_hold_flag(value):
    """Truthy interpretation of a credit-hold cell. None = no hold column."""
    s = str(value if value is not None else "").strip().lower()
    if s in _FALSE_WORDS:
        return False
    if s in _TRUE_WORDS:
        return True
    # Any other non-empty text (e.g. a hold reason) counts as held.
    return bool(s)


def _map_columns(header):
    used, mapping = set(), {}
    lowered = {h: h.lower().strip() for h in header}
    for field, candidates in FIELD_CANDIDATES.items():
        match = None
        # Pass 1 — exact header match, so a bare "Customer" (account number)
        # column is claimed by `account` rather than shadowed by the substring
        # hiding inside "Customer Name".
        for cand in candidates:
            match = next((h for h in header
                          if h not in used and lowered[h] == cand), None)
            if match:
                break
        # Pass 2 — substring match (handles "Customer No.", "Crédit", "Solde…").
        if not match:
            for cand in candidates:
                match = next((h for h in header
                              if h not in used and cand in lowered[h]), None)
                if match:
                    break
        if match:
            mapping[field] = match
            used.add(match)
    return mapping


def parse_master(path):
    """Return {mapping, customers: {key: {...}}, info_columns, stats}."""
    parsed = excel_reader.read_transactions(path)
    header = parsed["header"]
    mapping = _map_columns(header)
    # Columns not mapped to a known field = general information, kept for display.
    info_columns = [h for h in header if h not in mapping.values()]

    customers = {}
    skipped_totals = 0
    for row in parsed["rows"]:
        data = row["data"]
        g = lambda f: data.get(mapping[f]) if f in mapping else None  # noqa: E731
        account = _norm_key(g("account"))
        name = str(g("name") or "").strip()
        balance = _to_amount(g("balance"))

        # Total line: a balance with no customer identity, or a "total" label.
        if not account and not name:
            if balance:
                skipped_totals += 1
            continue
        if any("total" in str(v).lower() for v in (account, name) if v):
            skipped_totals += 1
            continue

        key = account or name
        segment_raw = str(g("segment") or "").strip()
        hold_raw = g("hold")
        customers[key] = {
            "account": account,
            "name": name or account,
            "segment": segment_raw,
            "plan": ctp_rules.resolve_plan(segment_raw) if segment_raw else None,
            "balance": balance,
            "critical": _is_yes(g("critical")) if "critical" in mapping else False,
            "on_hold": parse_hold_flag(hold_raw) if "hold" in mapping else None,
            "hold_raw": str(hold_raw if hold_raw is not None else "").strip(),
            "credit_limit": _to_amount(g("credit_limit")) if g("credit_limit") not in (None, "") else None,
            "info": {h: ("" if data.get(h) is None else str(data.get(h)))
                     for h in info_columns},
        }

    return _finish(mapping, customers, info_columns, skipped_totals)


def combine(parsed_list):
    """Merge several parsed master files into one (later files win on clashes)."""
    customers, info_columns, skipped = {}, [], 0
    mapping = {}
    for parsed in parsed_list:
        customers.update(parsed["customers"])
        for col in parsed["info_columns"]:
            if col not in info_columns:
                info_columns.append(col)
        skipped += parsed["stats"].get("skipped_totals", 0)
        mapping = parsed["mapping"] or mapping
    return _finish(mapping, customers, info_columns, skipped)


def _finish(mapping, customers, info_columns, skipped_totals):
    plans = [c["plan"] for c in customers.values() if c["plan"]]
    stats = {
        "customers": len(customers),
        "with_segment": sum(1 for c in customers.values() if c["segment"]),
        "with_hold_flag": sum(1 for c in customers.values() if c["on_hold"]),
        "with_critical": sum(1 for c in customers.values() if c.get("critical")),
        "has_hold_column": "hold" in mapping,
        "has_segment_column": "segment" in mapping,
        "has_critical_column": "critical" in mapping,
        "total_balance": sum(c["balance"] for c in customers.values()),
        "skipped_totals": skipped_totals,
        "plan_counts": {p: plans.count(p) for p in set(plans)},
    }
    return {"mapping": mapping, "customers": customers,
            "info_columns": info_columns, "stats": stats}
