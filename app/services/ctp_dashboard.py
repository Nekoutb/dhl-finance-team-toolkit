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
    # Per-customer credit-stop status (set by hold_compare) so even the reduced
    # offender/unapplied rows below can carry it into the dashboard.
    by_key = {c["key"]: c for c in customers}

    def _flags(k):
        """Status flags carried onto even the reduced offender/unapplied rows
        so every report can show credit-hold + critical + credit-stop."""
        c = by_key.get(k) or {}
        return {"credit_stop": c.get("credit_stop", ""),
                "credit_stop_key": c.get("credit_stop_key", "ok"),
                "currently_held": c.get("currently_held", False),
                "critical": c.get("critical", False)}

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
                                     "over60": 0.0, "over90": 0.0, "total": 0.0,
                                     **_flags(k)})
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
        e = credit_by_cust.setdefault(k, {"customer": i["customer"], "key": k, "amount": 0.0,
                                          **_flags(k)})
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

    # --- Per-customer aging (over60/over90) helper for the sections below ---
    def _aged(c):
        a = cust_aged.get(c["key"], {})
        gross = a.get("total") or c.get("gross_ar") or 0.0
        o60, o90 = a.get("over60", 0.0), a.get("over90", 0.0)
        return o60, o90, (o60 / gross * 100 if gross else 0.0), \
            (o90 / gross * 100 if gross else 0.0)

    # --- 7. Legal recovery: customers handed to third-party legal ----------
    legal_rows = []
    for c in customers:
        if not c.get("legal"):
            continue
        o60, o90, _p60, _p90 = _aged(c)
        legal_rows.append({**c, "over60": o60, "over90": o90})
    legal_rows.sort(key=lambda c: -c["total_ar"])
    legal_block = {
        "customers": legal_rows, "count": len(legal_rows),
        "total_ar": sum(c["total_ar"] for c in legal_rows),
        "over60": sum(c["over60"] for c in legal_rows),
        "over90": sum(c["over90"] for c in legal_rows),
        "critical_count": sum(1 for c in legal_rows if c.get("critical")),
    }

    # --- 8. Critical customer analysis (master "Critical" = Yes) -----------
    critical_rows = []
    for c in customers:
        if not c.get("critical"):
            continue
        o60, o90, p60, p90 = _aged(c)
        mdo = overdue_at.get(c["key"], c.get("max_days_overdue", 0))
        stop_day = c.get("stop_day") or ctp_rules.stop_credit_day(
            c.get("plan", ctp_rules.DEFAULT_PLAN))
        critical_rows.append({
            **c, "over60": o60, "over90": o90, "pct_over60": p60,
            "pct_over90": p90, "max_days_overdue": mdo, "stop_day": stop_day,
            "days_to_stop": (stop_day - mdo) if stop_day else None})
    critical_rows.sort(key=lambda c: -c["total_ar"])
    critical_block = {
        "customers": critical_rows, "count": len(critical_rows),
        "total_ar": sum(c["total_ar"] for c in critical_rows),
        "held": sum(1 for c in critical_rows if c.get("currently_held")),
    }

    # --- 9. Duty account analysis (invoices with a "D0…" id) ---------------
    duty_invs = []
    duty_total = duty_over60 = duty_over90 = duty_overdue = 0.0
    for i in debits:
        if not i.get("duty"):
            continue
        doc = _pdate(i.get("invoice_date"))
        age = (as_of - doc).days if (as_of and doc) else 0
        amt = i["amount"]
        duty_total += amt
        if i.get("days_overdue", 0) > 0:
            duty_overdue += amt
        o60, o90 = age > 60, age > 90
        duty_over60 += amt if o60 else 0
        duty_over90 += amt if o90 else 0
        cust = by_key.get(i["account"] or i["customer"]) or {}
        duty_invs.append({
            "customer": i["customer"], "account": i["account"],
            "invoice_no": i.get("invoice_no") or i.get("reference"),
            "due_date": i.get("due_date"), "amount": amt,
            "days_overdue": i.get("days_overdue", 0), "action": i.get("action"),
            "currently_held": cust.get("currently_held", False),
            "critical": cust.get("critical", False),
            "over60": o60, "over90": o90})
    duty_invs.sort(key=lambda r: -r["amount"])
    duty_block = {
        "invoices": duty_invs, "count": len(duty_invs), "total": duty_total,
        "overdue": duty_overdue, "over60": duty_over60, "over90": duty_over90,
        "over60_pct": (duty_over60 / duty_total * 100) if duty_total else 0,
        "over90_pct": (duty_over90 / duty_total * 100) if duty_total else 0,
        "customers": len({(r["account"] or r["customer"]) for r in duty_invs}),
    }

    # --- 6. Top offenders ---------------------------------------------------
    top60 = over60_list[:top_n]
    top90 = over90_list[:top_n]

    return {
        "legal": legal_block,
        "critical": critical_block,
        "duty": duty_block,
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
