"""Compute the CtP dashboard metrics from an analysis result.

Operates on the stored/loaded result (dates may be ISO strings), so it can be
recomputed on every dashboard render (e.g. when the small-balance threshold is
changed, or after the hold register / bank matches update).
"""
from datetime import date, timedelta

from . import ctp_rules

DEFAULT_SMALL_THRESHOLD = 100000.0
UPCOMING_BANDS = [(3, "Within 3 days"), (5, "Within 5 days"),
                  (10, "Within 10 days"), (14, "Within 14 days")]


def _pdate(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def month_end(value):
    """Last calendar day of ``value``'s month (e.g. 2026-06-13 -> 2026-06-30)."""
    d = _pdate(value)
    if not d:
        return None
    if d.month == 12:
        return date(d.year, 12, 31)
    return date(d.year, d.month + 1, 1) - timedelta(days=1)


def build(result, threshold=DEFAULT_SMALL_THRESHOLD, top_n=20, as_of=None):
    # ``as_of`` defaults to the analysis date. Pass an override (e.g. the
    # current month-end) to age the receivables against a different reference
    # date: the aging bands AND the "going on hold" projection both follow it,
    # so the team can see how the picture will look at period close. Pure
    # balance/status metrics (unapplied cash, on-hold-negative, below-threshold)
    # are snapshots and come out the same regardless of the reference date.
    as_of = _pdate(as_of) or _pdate(result["as_of"])
    invoices = result["invoices"]
    customers = result["customers"]
    currency = (result.get("currencies") or ["XAF"])[0]

    debits = [i for i in invoices if i["kind"] == "invoice"]
    credits = [i for i in invoices if i["kind"] == "credit"]
    gross_ar = sum(i["amount"] for i in debits)
    net_ar = sum(i["amount"] for i in invoices)

    # --- 1. Aging by DOCUMENT date -----------------------------------------
    cust_aged = {}
    over60 = over90 = 0.0
    # Max days past the DUE date at this as_of, per customer — recomputed here
    # (rather than reusing the stored value) so the "going on hold" projection
    # below tracks whichever reference date we are building for.
    overdue_at = {}
    for i in debits:
        doc = _pdate(i.get("invoice_date"))
        age = (as_of - doc).days if (as_of and doc) else 0
        k = i["account"] or i["customer"]
        due = _pdate(i.get("due_date"))
        od = max(0, (as_of - due).days) if (as_of and due) else 0
        if od > overdue_at.get(k, -1):
            overdue_at[k] = od
        e = cust_aged.setdefault(k, {"customer": i["customer"], "key": k,
                                     "over60": 0.0, "over90": 0.0, "total": 0.0})
        e["total"] += i["amount"]
        if age > 60:
            over60 += i["amount"]
            e["over60"] += i["amount"]
        if age > 90:
            over90 += i["amount"]
            e["over90"] += i["amount"]
    over60_list = sorted((c for c in cust_aged.values() if c["over60"] > 0),
                         key=lambda c: -c["over60"])
    over90_list = sorted((c for c in cust_aged.values() if c["over90"] > 0),
                         key=lambda c: -c["over90"])
    aging = {
        "over60": over60, "over60_pct": (over60 / gross_ar * 100) if gross_ar else 0,
        "over90": over90, "over90_pct": (over90 / gross_ar * 100) if gross_ar else 0,
        "gross_ar": gross_ar,
    }

    # --- 2. Unapplied cash % -----------------------------------------------
    unapplied = -sum(i["amount"] for i in credits)        # positive magnitude
    credit_by_cust = {}
    for i in credits:
        k = i["account"] or i["customer"]
        e = credit_by_cust.setdefault(k, {"customer": i["customer"], "key": k, "amount": 0.0})
        e["amount"] += i["amount"]
    unapplied_block = {
        "amount": unapplied,
        "pct": (unapplied / net_ar * 100) if net_ar else 0,
        "net_ar": net_ar,
        "customers": sorted(credit_by_cust.values(), key=lambda c: c["amount"]),
    }

    # --- 3. On credit hold but net-negative balance ------------------------
    held_negative = sorted(
        (c for c in customers if c.get("currently_held") and c["total_ar"] < 0),
        key=lambda c: c["total_ar"])

    # --- 4. Projected to go on hold within N days --------------------------
    bands = {label: [] for _d, label in UPCOMING_BANDS}
    for c in customers:
        if c.get("currently_held") or c["status_key"] in ("hold", "stop"):
            continue
        stop_day = ctp_rules.stop_credit_day(c.get("plan", ctp_rules.DEFAULT_PLAN))
        if not stop_day:
            continue
        mdo = overdue_at.get(c["key"], c.get("max_days_overdue", 0))
        days_to_hold = stop_day - mdo
        if days_to_hold <= 0 or days_to_hold > UPCOMING_BANDS[-1][0]:
            continue
        row = {**c, "max_days_overdue": mdo, "days_to_hold": days_to_hold,
               "stop_day": stop_day}
        for limit, label in UPCOMING_BANDS:
            if days_to_hold <= limit:
                bands[label].append(row)
                break
    for label in bands:
        bands[label].sort(key=lambda c: c["days_to_hold"])
    upcoming = {"bands": bands,
                "total": sum(len(v) for v in bands.values())}

    # --- 5. Below small-balance threshold (editable) -----------------------
    below = sorted((c for c in customers if 0 < c["total_ar"] <= threshold),
                   key=lambda c: -c["total_ar"])
    below_block = {
        "threshold": threshold,
        "count": len(below),
        "total": sum(c["total_ar"] for c in below),
        "customers": below,
    }

    # --- 6. Top offenders ---------------------------------------------------
    top60 = over60_list[:top_n]
    top90 = over90_list[:top_n]

    return {
        "currency": currency,
        "gross_ar": gross_ar,
        "net_ar": net_ar,
        "aging": aging,
        "over60_customers": over60_list,
        "over90_customers": over90_list,
        "unapplied": unapplied_block,
        "held_negative": held_negative,
        "upcoming": upcoming,
        "below": below_block,
        "top60": top60,
        "top90": top90,
    }
