"""CtP Portal: analyse an invoice-level AR breakdown against the
Collections Treatment Plan and produce the controls to apply.

Pipeline: read file -> map columns -> classify lines (invoice / credit /
control-total) -> per-invoice control -> per-customer risk -> control-type
summary -> credit-hold comparison vs the register.

Column mapping is auto-detected and tuned to the real SAP FBL5N-style export
("AR DETAILS CM"): Customer = account number, "Customer Account: Name" = name,
"Document Currency Value" = amount, "Net Due Date" = due date. The file ends
with a control-total line where the amount is populated but most other fields
are empty — detected, excluded from the analysis, and used to validate that
the line items sum to the file's own total.
"""
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from ..config import DATA_DIR
from ..services import ctp_rules, excel_reader, holds

TOOL_SLUG = "ongoing-ctp-monitoring"
STORE_DIR = DATA_DIR / "ctp"

CREDIT_ACTION = "Credit / payment on account — allocate to invoices"
WITHIN_TERMS = "Within terms — monitor"

# Logical field -> header fragments (lowercased), most specific first.
FIELD_CANDIDATES = {
    "customer": ["customer account: name", "customer name", "account name",
                 "client name", "debtor name", "name of customer", "name"],
    "account": ["customer number", "customer no", "customer code", "bp number",
                "account number", "account no", "customer", "account", "compte"],
    "invoice_no": ["invoice number", "invoice no", "billing document",
                   "document number", "invoice", "document no", "facture",
                   "doc no", "reference"],
    "invoice_date": ["invoice date", "billing date", "document date",
                     "date facture", "doc date"],
    "due_date": ["net due date", "due date", "payment due", "net due", "due",
                 "echeance", "échéance"],
    "doc_type": ["document type", "charge type", "invoice type", "doc type",
                 "category", "type"],
    "amount": ["document currency value", "open amount", "outstanding amount",
               "amount in eur", "outstanding", "balance", "amount due",
               "amount", "montant", "value", "open"],
    "currency": ["currency key", "currency", "curr", "devise"],
    "clerk": ["accounting clerk", "clerk", "collector"],
    "segment": ["sales segment", "treatment plan", "customer group", "segment",
                "gctp", "ctp"],
    "country_rank": ["country rank", "rank", "tier"],
    "credit_limit": ["credit limit", "cr limit", "crlimit", "limit"],
    "annual_billed": ["annual billed", "annual billing", "annual sales",
                      "yearly billed"],
    "payment_term": ["payment terms", "payment term", "term days", "net days",
                     "terms"],
}


def _map_columns(header):
    used, mapping = set(), {}
    lowered = {h: h.lower() for h in header}
    for field, candidates in FIELD_CANDIDATES.items():
        for cand in candidates:
            match = next((h for h in header if h not in used and cand in lowered[h]), None)
            if match:
                mapping[field] = match
                used.add(match)
                break
    return mapping


def _mostly_numeric(values):
    vals = [str(v).strip() for v in values if v not in (None, "")]
    if not vals:
        return False
    numeric = sum(1 for v in vals if re.fullmatch(r"\d+(\.0)?", v))
    return numeric / len(vals) >= 0.6


def _post_validate(mapping, rows):
    """If 'customer' landed on a numeric column while 'account' got the text
    one (or no name column at all), swap so names are names."""
    sample = rows[:200]
    cust_vals = [r["data"].get(mapping["customer"]) for r in sample] \
        if "customer" in mapping else []
    acct_vals = [r["data"].get(mapping["account"]) for r in sample] \
        if "account" in mapping else []
    if cust_vals and _mostly_numeric(cust_vals) and acct_vals \
            and not _mostly_numeric(acct_vals):
        mapping["customer"], mapping["account"] = mapping["account"], mapping["customer"]
    return mapping


def _to_date(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip().split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d-%m-%Y",
                "%Y/%m/%d", "%d %b %Y", "%d-%b-%Y", "%d %B %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s).date()
    except ValueError:
        return None


def _to_amount(value):
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[^0-9,.\-]", "", str(value))
    if s.count(",") == 1 and s.count(".") == 0:   # decimal comma
        s = s.replace(",", ".")
    else:                                          # comma as thousands sep
        s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _norm_key(value):
    s = str(value or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _is_total_candidate(row_data, g, amount):
    """A possible control-total line: amount populated, identifying fields
    empty — 'a line on which most of the other data fields are empty'.
    Confirmed later: it only counts as the total if the remaining lines
    actually sum to it (otherwise it is a real, unassigned AR line)."""
    if not amount:
        return False
    identifying = [g("customer"), g("account"), g("invoice_no"), g("due_date"),
                   g("invoice_date")]
    if any(v not in (None, "") for v in identifying):
        # an explicit "total" label still counts
        return any("total" in str(v).lower() for v in row_data.values()
                   if isinstance(v, str))
    return True


TB_AMOUNT_CANDIDATES = ["closing balance", "ending balance", "balance",
                        "solde", "amount", "montant", "total", "ytd"]


def parse_trial_balance(path):
    """Read the AR trial balance and find its total receivables figure.

    Data-driven: a line whose amount equals the sum of all the others is the
    file's own total and is preferred; otherwise the lines are summed.
    """
    parsed = excel_reader.read_transactions(path)
    header = parsed["header"]
    lowered = {h: h.lower() for h in header}
    amount_col = None
    for cand in TB_AMOUNT_CANDIDATES:
        amount_col = next((h for h in header if cand in lowered[h]), None)
        if amount_col:
            break
    amounts = []
    for row in parsed["rows"]:
        if amount_col:
            value = _to_amount(row["data"].get(amount_col))
        else:  # no obvious column: use the right-most numeric cell
            nums = [_to_amount(v) for v in row["data"].values()
                    if isinstance(v, (int, float))]
            value = nums[-1] if nums else 0.0
        if value:
            amounts.append(value)

    grand = sum(amounts)
    explicit_total = None
    for a in amounts:
        if abs((grand - a) - a) <= max(1.0, abs(a) * 0.0001):
            explicit_total = a
            break
    total = explicit_total if explicit_total is not None else grand
    return {
        "total": total,
        "explicit_total": explicit_total is not None,
        "lines": len(amounts) - (1 if explicit_total is not None else 0),
        "amount_col": amount_col or "(right-most numeric)",
    }


def analyze(path, as_of=None, default_rank=51, master=None, tb=None):
    """``master`` is the parsed AR master file (ar_master.parse_master output):
    supplies each customer's segment/plan, balance and credit-hold position.
    ``tb`` is parse_trial_balance() output — drives the FIRST analysis: total
    AR per the transaction file vs the AR trial balance."""
    as_of = as_of or date.today()
    master_customers = (master or {}).get("customers", {})
    parsed = excel_reader.read_transactions(path)
    header = parsed["header"]
    mapping = _post_validate(_map_columns(header), parsed["rows"])

    def getter(data):
        return lambda field: data.get(mapping[field]) if field in mapping else None

    # Pass 1: split rows into line items and total-row candidates.
    line_rows, candidates = [], []
    for row in parsed["rows"]:
        g = getter(row["data"])
        amount = _to_amount(g("amount"))
        if _is_total_candidate(row["data"], g, amount):
            candidates.append((row, amount))
        elif amount or g("customer") not in (None, "") \
                or g("invoice_no") not in (None, ""):
            line_rows.append(row)
        # else: fully blank filler line — skip

    # Pass 2: a candidate is a real control total only if everything else in
    # the file sums to it; otherwise it's a genuine (unassigned) AR line.
    grand_sum = sum(_to_amount(getter(r["data"])("amount")) for r in line_rows) \
        + sum(a for _r, a in candidates)
    control_totals = []
    for row, amount in candidates:
        tolerance = max(1.0, abs(amount) * 0.0001)
        if abs((grand_sum - amount) - amount) <= tolerance:
            control_totals.append(amount)
        else:
            line_rows.append(row)

    invoices = []
    for row in line_rows:
        g = getter(row["data"])
        amount = _to_amount(g("amount"))
        kind = "credit" if amount < 0 else "invoice"
        inv_date = _to_date(g("invoice_date"))
        due = _to_date(g("due_date"))
        doc_type = g("doc_type")
        if due is None and inv_date is not None:
            due = inv_date + timedelta(days=ctp_rules.term_days_for(doc_type))

        # Plan: the master file's segment wins; else any segment column in the
        # transaction file; else the default (Telesales).
        account_key = _norm_key(g("account"))
        m = master_customers.get(account_key)
        if m and m.get("plan"):
            plan = m["plan"]
        else:
            plan = ctp_rules.resolve_plan(g("segment"))
        rank = g("country_rank") or default_rank
        currency = str(g("currency") or "").strip()

        if kind == "credit":
            days_overdue, action, owner, suppressed = 0, CREDIT_ACTION, "Cash Application", False
        else:
            days_overdue = max(0, (as_of - due).days) if due else 0
            step = ctp_rules.action_due(plan, days_overdue)
            action, owner = (step[1], step[2]) if step else (WITHIN_TERMS, "—")
            suppressed = False
            if step and ctp_rules.is_call_action(action):
                threshold = ctp_rules.call_value_threshold(rank, currency)
                suppressed = threshold is not None and amount < threshold

        cl_raw = g("credit_limit")
        ab_raw = g("annual_billed")
        invoices.append({
            "kind": kind,
            "customer": str(g("customer") or "").strip()
            or account_key or "(Unassigned)",
            "account": account_key,
            "invoice_no": _norm_key(g("invoice_no")),
            "invoice_date": inv_date,
            "due_date": due,
            "doc_type": str(g("doc_type") or "").strip(),
            "amount": amount,
            "currency": currency,
            "clerk": str(g("clerk") or "").strip(),
            "plan": plan,
            "plan_label": ctp_rules.PLAN_LABELS[plan],
            "rank": rank,
            "days_overdue": days_overdue,
            "action": action,
            "owner": owner,
            "call_suppressed": suppressed,
            "credit_limit": _to_amount(cl_raw) if cl_raw not in (None, "") else None,
            "annual_billed": _to_amount(ab_raw) if ab_raw not in (None, "") else None,
        })

    customers = _rollup_customers(invoices)

    # Attach master-file data per customer + reconcile balances.
    reconciliation = None
    if master_customers:
        seen = set()
        mismatches, matched = [], 0
        for c in customers:
            m = master_customers.get(c["key"])
            if not m:
                continue
            seen.add(c["key"])
            c["master_balance"] = m["balance"]
            c["master_hold"] = m["on_hold"]
            c["master_segment"] = m["segment"]
            diff = round(c["total_ar"] - m["balance"], 2)
            if abs(diff) <= max(1.0, abs(m["balance"]) * 0.0001):
                matched += 1
            else:
                mismatches.append({
                    "key": c["key"], "customer": c["customer"],
                    "transactions": c["total_ar"], "master": m["balance"],
                    "difference": diff,
                })
        only_master = [
            {"key": k, "customer": m["name"], "master": m["balance"]}
            for k, m in master_customers.items()
            if k not in seen and abs(m["balance"]) > 0.005
        ]
        only_tx = [{"key": c["key"], "customer": c["customer"],
                    "transactions": c["total_ar"]}
                   for c in customers if c["key"] not in master_customers]
        mismatches.sort(key=lambda x: -abs(x["difference"]))
        reconciliation = {
            "matched": matched,
            "mismatches": mismatches,
            "only_in_master": sorted(only_master, key=lambda x: -abs(x["master"])),
            "only_in_transactions": sorted(only_tx, key=lambda x: -abs(x["transactions"])),
        }

    line_sum = sum(i["amount"] for i in invoices)
    file_total = sum(control_totals) if control_totals else None
    total_check = None
    if file_total is not None:
        tolerance = max(1.0, abs(file_total) * 0.0001)
        total_check = {
            "file_total": file_total,
            "line_sum": line_sum,
            "matches": abs(line_sum - file_total) <= tolerance,
            "lines": len(control_totals),
        }

    # FIRST analysis: transaction-file total vs the AR trial balance.
    tb_check = None
    if tb:
        tolerance = max(1.0, abs(tb["total"]) * 0.0001)
        tb_check = {
            "tb_total": tb["total"],
            "tx_total": line_sum,
            "difference": round(line_sum - tb["total"], 2),
            "matches": abs(line_sum - tb["total"]) <= tolerance,
            "tb_lines": tb["lines"],
            "explicit_total": tb["explicit_total"],
        }

    return {
        "as_of": as_of,
        "mapping": mapping,
        "header": header,
        "default_rank": default_rank,
        "invoices": invoices,
        "customers": customers,
        "controls_summary": _controls_summary(invoices),
        "summary": _summary(invoices, customers),
        "total_check": total_check,
        "tb_check": tb_check,
        "reconciliation": reconciliation,
        "master_used": bool(master_customers),
        "master_stats": (master or {}).get("stats") if master_customers else None,
        "unmapped": [f for f in FIELD_CANDIDATES if f not in mapping],
        "currencies": sorted({i["currency"] for i in invoices if i["currency"]}),
    }


def _rollup_customers(invoices):
    groups = {}
    for inv in invoices:
        groups.setdefault(inv["account"] or inv["customer"], []).append(inv)

    out = []
    for key, items in groups.items():
        debits = [i for i in items if i["kind"] == "invoice"]
        credits = [i for i in items if i["kind"] == "credit"]
        plan = items[0]["plan"]
        grace = ctp_rules.GRACE_DAYS.get(plan, 28)
        net_total = sum(i["amount"] for i in items)
        gross_debits = sum(i["amount"] for i in debits)
        overdue = sum(i["amount"] for i in debits if i["days_overdue"] > 0)
        beyond = sum(i["amount"] for i in debits if i["days_overdue"] > grace)
        base = gross_debits if gross_debits > 0 else 0.0
        beyond_pct = (beyond / base * 100) if base else 0.0

        credit_limit = next((i["credit_limit"] for i in items if i["credit_limit"]), None)
        if credit_limit is None:
            annual = next((i["annual_billed"] for i in items if i["annual_billed"]), None)
            credit_limit = ctp_rules.credit_limit_from_billing(annual) if annual else None
        overrun_pct = ((net_total - credit_limit) / credit_limit * 100) if credit_limit else None

        status_key, status = ctp_rules.risk_control(beyond_pct, overrun_pct)
        worst = max(debits, key=lambda i: i["days_overdue"]) if debits else items[0]
        clerk = next((i["clerk"] for i in items if i["clerk"]), "")
        out.append({
            "key": key,
            "customer": items[0]["customer"],
            "clerk": clerk,
            "plan": plan,
            "plan_label": ctp_rules.PLAN_LABELS[plan],
            "invoice_count": len(debits),
            "credit_count": len(credits),
            "total_ar": net_total,
            "overdue": overdue,
            "credits_total": sum(i["amount"] for i in credits),
            "beyond_gctp_pct": beyond_pct,
            "credit_limit": credit_limit,
            "overrun_pct": overrun_pct,
            "max_days_overdue": worst["days_overdue"],
            "status_key": status_key,
            "status": status,
            "next_action": worst["action"],
            "owner": worst["owner"],
        })

    order = {"stop": 0, "hold": 1, "watch": 2, "ok": 3}
    out.sort(key=lambda c: (order[c["status_key"]], -c["max_days_overdue"]))
    return out


def _controls_summary(invoices):
    """One row per control type: how many invoices/customers/value it covers."""
    timeline_rank = {WITHIN_TERMS: -1}
    for plan_steps in ctp_rules.TIMELINES.values():
        for day, action, _owner in plan_steps:
            timeline_rank.setdefault(action, day)
    timeline_rank[CREDIT_ACTION] = 9999

    groups = {}
    for i in invoices:
        g = groups.setdefault(i["action"], {
            "action": i["action"], "owner": i["owner"],
            "invoices": 0, "customers": set(), "amount": 0.0, "suppressed": 0,
        })
        g["invoices"] += 1
        g["customers"].add(i["account"] or i["customer"])
        g["amount"] += i["amount"]
        g["suppressed"] += 1 if i["call_suppressed"] else 0

    out = []
    for action, g in groups.items():
        g["customers"] = len(g["customers"])
        g["rank"] = timeline_rank.get(action, 5000)
        g["auto"] = ctp_rules.is_auto_action(action)
        out.append(g)
    out.sort(key=lambda g: g["rank"])
    return out


def _summary(invoices, customers):
    debits = [i for i in invoices if i["kind"] == "invoice"]
    credits = [i for i in invoices if i["kind"] == "credit"]
    return {
        "invoice_count": len(debits),
        "credit_count": len(credits),
        "customer_count": len(customers),
        "total_ar": sum(i["amount"] for i in invoices),
        "overdue_total": sum(i["amount"] for i in debits if i["days_overdue"] > 0),
        "credits_total": sum(i["amount"] for i in credits),
        # Manual actions only — automated dunning letters are the system's job.
        "actions_due": sum(1 for i in debits if i["action"] != WITHIN_TERMS
                           and not ctp_rules.is_auto_action(i["action"])),
        "auto_dunning": sum(1 for i in debits
                            if ctp_rules.is_auto_action(i["action"])),
        "calls_suppressed": sum(1 for i in debits if i["call_suppressed"]),
        "status_counts": dict(Counter(c["status_key"] for c in customers)),
    }


# --- Credit-hold comparison (policy state vs master file / register) ---------
def hold_compare(customers):
    """Compare required hold state (per §2.5 matrix) vs the current position.

    The master file's hold column is authoritative for customers it covers;
    the manual register covers the rest.
    """
    held_keys, held_names = holds.lookup()
    place, release, correct, clear = [], [], [], []
    for c in customers:
        if c.get("master_hold") is not None:
            current = bool(c["master_hold"])
            c["hold_source"] = "master file"
        else:
            current = _norm_key(c["key"]) in held_keys \
                or c["customer"].strip().lower() in held_names
            c["hold_source"] = "register"
        c["currently_held"] = current
        # Canonical credit-stop status carried into every report.
        c["credit_stop_key"], c["credit_stop"] = ctp_rules.credit_stop_status(
            c["status_key"], current)
        should = c["status_key"] in ("hold", "stop")
        if should and not current:
            place.append(c)
        elif current and not should:
            release.append(c)
        elif should and current:
            correct.append(c)
        else:
            clear.append(c)
    return {
        "place_on_hold": place,
        "review_release": release,
        "correctly_held": correct,
        "no_hold_needed": len(clear),
        "register_size": len(held_keys),
    }


# --- Persistence so results can be revisited & hold toggles re-render --------
def _serialize(result):
    out = dict(result)
    out["as_of"] = result["as_of"].isoformat()
    out["invoices"] = [
        {**i, "invoice_date": i["invoice_date"].isoformat() if i["invoice_date"] else None,
         "due_date": i["due_date"].isoformat() if i["due_date"] else None}
        for i in result["invoices"]
    ]
    return out


def save_result(token, result):
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    data = _serialize(result)
    (STORE_DIR / f"{token}.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8")


def load_result(token):
    if not token or not re.fullmatch(r"[0-9a-f]+", token):
        return None
    path = STORE_DIR / f"{token}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


MASTER_PATH = STORE_DIR / "master.json"


def save_master(parsed_master, source=""):
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    payload = dict(parsed_master)
    payload["source"] = source
    payload["uploaded_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    MASTER_PATH.write_text(json.dumps(payload, ensure_ascii=False),
                           encoding="utf-8")
    return payload


def load_master():
    if not MASTER_PATH.exists():
        return None
    try:
        return json.loads(MASTER_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# --- Priority customers (~40 key accounts) -----------------------------------
# A standing list of customer keys (account numbers / names) the team watches
# most closely. The ⭐ filter on EVERY analysis (past and future) recomputes
# the whole dashboard/controls for these customers only.
PRIORITY_PATH = STORE_DIR / "priority.json"


def load_priority():
    if not PRIORITY_PATH.exists():
        return {"keys": [], "updated_at": ""}
    try:
        data = json.loads(PRIORITY_PATH.read_text(encoding="utf-8"))
        data.setdefault("keys", [])
        return data
    except (json.JSONDecodeError, OSError):
        return {"keys": [], "updated_at": ""}


def save_priority(keys):
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"keys": sorted({str(k).strip() for k in keys if str(k).strip()}),
               "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    PRIORITY_PATH.write_text(json.dumps(payload, ensure_ascii=False),
                             encoding="utf-8")
    return payload


def toggle_priority(key):
    """Add/remove one customer key. Returns True if it is now priority."""
    key = str(key or "").strip()
    if not key:
        return False
    keys = set(load_priority()["keys"])
    now_on = key not in keys
    (keys.add if now_on else keys.discard)(key)
    save_priority(keys)
    return now_on


def priority_set():
    return set(load_priority()["keys"])


def filter_result_priority(result, keys):
    """A copy of an analysis narrowed to the priority customers — controls
    and summaries are recomputed so every dashboard section reflects only
    those customers."""
    keys = {str(k) for k in keys}
    customers = [c for c in result["customers"] if str(c["key"]) in keys]
    invoices = [i for i in result["invoices"]
                if str(i.get("account") or i.get("customer")) in keys]
    out = dict(result)
    out["customers"] = customers
    out["invoices"] = invoices
    out["controls_summary"] = _controls_summary(invoices)
    out["summary"] = _summary(invoices, customers)
    return out


def list_results(limit=10):
    """Recent stored analyses, newest first, for the upload page."""
    if not STORE_DIR.exists():
        return []
    out = []
    for path in STORE_DIR.glob("*.json"):
        if path.stem.endswith("_bank") or path.name == "master.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append({
            "token": path.stem,
            "created_at": data.get("created_at", ""),
            "source": data.get("source", ""),
            "as_of": data.get("as_of", ""),
            "invoices": data.get("summary", {}).get("invoice_count", 0),
            "customers": data.get("summary", {}).get("customer_count", 0),
        })
    out.sort(key=lambda r: r["created_at"], reverse=True)
    return out[:limit]


def delete_result(token):
    """Remove a stored analysis (and its bank matches / Excel report)."""
    if not re.fullmatch(r"[0-9a-f]+", token or ""):
        return
    (STORE_DIR / f"{token}.json").unlink(missing_ok=True)
    (STORE_DIR / f"{token}_bank.json").unlink(missing_ok=True)
    from ..config import OUTPUT_DIR
    (OUTPUT_DIR / f"CtP_controls_{token[:8]}.xlsx").unlink(missing_ok=True)


def delete_master():
    MASTER_PATH.unlink(missing_ok=True)
