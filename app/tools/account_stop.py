"""Account Stop — every account currently on credit stop, with the payment
traces found for it across the three money sources on file:

  * Bank payments  — credit lines on the uploaded bank statements whose
    payer/narration matches the customer name.
  * Cheque payments — cheques on the electronic register whose client name
    matches, with the bank the cheque cleared in (when presented).
  * Mobile money   — Orange Money collections from the saved uploads, joined
    on the customer's AR account number (or the remembered name).

"On credit stop" = the customer's ACTUAL state is held (master-file hold flag
where present, else the manual hold register) — the same `currently_held`
shown across the CtP reports.
"""
from ..services import bank_statement, cheques
from . import bank
from . import ongoing_ctp as ctp
from . import orange_cameroun as orange

TOOL_SLUG = "account-stop"

# Disclosure page: precision beats recall. 0.90 accepts a narration only
# when every distinctive word of the customer name appears in it (or the
# strings are near-identical) — generic-word coincidences that pass the
# default 0.86 fuzzy bar (e.g. a payer matched to an unrelated customer
# sharing one common word) are rejected.
MATCH_RATIO = 0.90


def _norm_acct(value):
    s = str(value or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _latest_stopped():
    """(stopped customers, ar_meta) from the latest CtP analysis on record."""
    recent = ctp.list_results(limit=1)
    if not recent:
        return [], None
    result = ctp.load_result(recent[0]["token"])
    if not result:
        return [], None
    customers = result.get("customers", [])
    ctp.hold_compare(customers)
    stopped = [c for c in customers if c.get("currently_held")]
    return stopped, {"token": recent[0]["token"],
                     "as_of": str(result.get("as_of", "")),
                     "source": result.get("source", "")}


def _ar_entry(c):
    return {"key": str(c.get("key", "")), "customer": c.get("customer", ""),
            "total_ar": c.get("total_ar", 0) or 0,
            "overdue": c.get("overdue", 0) or 0,
            "credit_stop": c.get("credit_stop", ""),
            "hold_source": c.get("hold_source", ""),
            "bank": [], "cheques": [], "momo": []}


def build_view():
    """The whole page model: one entry per stopped account (sorted by overdue,
    biggest first), each carrying every matched payment per source."""
    stopped, ar_meta = _latest_stopped()
    accounts = {str(c.get("key", "")): _ar_entry(c) for c in stopped}
    counts = {"stopped": len(stopped), "bank_lines": 0, "orange_uploads": 0}
    if not stopped:
        return {"ar": ar_meta, "accounts": [], "counts": counts}

    # --- Bank payments: each credit line vs the stopped customers -----------
    credit_lines = bank.all_credit_lines()
    counts["bank_lines"] = len(credit_lines)
    seen_bank = set()
    for ln in credit_lines:
        narration = f"{ln.get('payer', '')} {ln.get('description', '')}".strip()
        if not narration:
            continue
        hit = bank_statement.best_customer_for(narration, stopped,
                                               min_ratio=MATCH_RATIO)
        if not hit:
            continue
        entry = accounts[str(hit[0].get("key", ""))]
        name = ln.get("payer") or (ln.get("description") or "")[:60]
        dedup = (entry["key"], ln.get("bank", ""), ln.get("date", ""),
                 round(ln.get("credit", 0), 2), name)
        if dedup in seen_bank:                   # overlapping statement extracts
            continue
        seen_bank.add(dedup)
        entry["bank"].append({
            "name": name, "amount": ln.get("credit", 0),
            "bank": ln.get("bank", ""), "date": ln.get("date", ""),
            "reference": (ln.get("text") or ln.get("description") or "")[:90]})

    # --- Cheque payments: register client names vs the stopped customers ----
    memo = {}
    for r in cheques.register_rows():
        name = (r.get("customer") or "").strip()
        if r.get("status") != "done" or not name or r.get("duplicate_of"):
            continue
        mkey = name.lower()
        if mkey not in memo:
            hit = bank_statement.best_customer_for(name, stopped,
                                                   min_ratio=MATCH_RATIO)
            memo[mkey] = str(hit[0].get("key", "")) if hit else None
        if not memo[mkey]:
            continue
        cleared = r.get("cleared") or {}
        accounts[memo[mkey]]["cheques"].append({
            "name": name, "amount": r.get("amount"),
            "cheque_number": r.get("cheque_number", ""),
            "bank": cleared.get("bank") or "not cleared yet",
            "date": cleared.get("date") or ""})

    # --- Mobile money: Orange uploads joined on AR account (else name) ------
    by_acct = {_norm_acct(k): v for k, v in accounts.items()}
    seen_momo = set()
    for meta in orange.list_uploads(limit=24):
        u = orange.load_upload(meta["token"])
        if not u:
            continue
        counts["orange_uploads"] += 1
        tx_by_corr = {}
        for tx in u.get("collections", []):
            tx_by_corr.setdefault(tx.get("correspondant", ""), []).append(tx)
        for c in u.get("correspondants", []):
            entry = by_acct.get(_norm_acct(c.get("account")))
            if entry is None and (c.get("name") or "").strip():
                hit = bank_statement.best_customer_for(
                    c["name"], stopped, min_ratio=MATCH_RATIO)
                entry = accounts[str(hit[0].get("key", ""))] if hit else None
            if entry is None:
                continue
            for tx in tx_by_corr.get(c.get("correspondant", ""), []):
                dedup = (c.get("correspondant", ""), tx.get("reference", ""),
                         tx.get("date", ""), tx.get("credit", 0))
                if dedup in seen_momo:           # same month re-uploaded twice
                    continue
                seen_momo.add(dedup)
                entry["momo"].append({
                    "operator": "Orange Money",
                    "name": c.get("name") or c.get("correspondant", ""),
                    "amount": tx.get("credit", 0),
                    "date": tx.get("date", "")})

    ordered = sorted(accounts.values(),
                     key=lambda a: -(a.get("overdue") or 0))
    return {"ar": ar_meta, "accounts": ordered, "counts": counts}
