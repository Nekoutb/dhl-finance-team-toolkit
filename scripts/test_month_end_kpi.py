"""Test the month-end KPI line on the CtP dashboard.

The dashboard shows two rows of the same six metrics: one aged to the analysis
date ("today") and one aged to the current month-end. This checks that:
  * month_end() returns the last calendar day of any month (incl. Dec + leap),
  * the today row is byte-identical to the pre-change behaviour (invariant),
  * aging (AR>60 / AR>90) and the going-on-hold projection shift forward when
    aged to month-end, while pure balance metrics stay put.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import ctp_dashboard, ctp_rules  # noqa: E402

TLS = ctp_rules.PLAN_TELESALES  # stop-credit at 25 days overdue


def _result():
    """A small synthetic analysis, as_of 13 Jun 2026 (month-end = 30 Jun)."""
    return {
        "as_of": "2026-06-13",
        "currencies": ["XAF"],
        "invoices": [
            # crosses the 60-day line only by month-end (54d today, 71d at ME)
            {"kind": "invoice", "amount": 1_000_000, "invoice_date": "2026-04-20",
             "due_date": "2026-05-05", "account": "A", "customer": "Alpha"},
            # always > 90 days
            {"kind": "invoice", "amount": 1_000_000, "invoice_date": "2026-01-01",
             "due_date": "2026-01-16", "account": "B", "customer": "Bravo"},
            # > 60 today, crosses the 90-day line only by month-end
            {"kind": "invoice", "amount": 1_000_000, "invoice_date": "2026-03-28",
             "due_date": "2026-04-12", "account": "D", "customer": "Delta"},
            # 5d overdue today (20d to hold) -> 22d at ME (3d to hold => band)
            {"kind": "invoice", "amount": 2_000_000, "invoice_date": "2026-05-25",
             "due_date": "2026-06-08", "account": "HOLD", "customer": "Hold Co"},
            # unapplied cash (date-independent metric)
            {"kind": "credit", "amount": -300_000, "invoice_date": "2026-06-01",
             "due_date": "2026-06-01", "account": "A", "customer": "Alpha"},
        ],
        "customers": [
            {"key": "A", "customer": "Alpha", "total_ar": 700_000,
             "currently_held": False, "status_key": "ok", "plan": TLS, "max_days_overdue": 39},
            {"key": "B", "customer": "Bravo", "total_ar": 1_000_000,
             "currently_held": False, "status_key": "ok", "plan": TLS, "max_days_overdue": 148},
            {"key": "D", "customer": "Delta", "total_ar": 1_000_000,
             "currently_held": False, "status_key": "ok", "plan": TLS, "max_days_overdue": 62},
            {"key": "HOLD", "customer": "Hold Co", "total_ar": 2_000_000,
             "currently_held": False, "status_key": "ok", "plan": TLS, "max_days_overdue": 5},
            # currently held + negative -> the "On hold & negative" metric
            {"key": "NEG", "customer": "Neg Co", "total_ar": -50_000,
             "currently_held": True, "status_key": "ok", "plan": TLS, "max_days_overdue": 0},
            # below the 100k threshold
            {"key": "SMALL", "customer": "Small Co", "total_ar": 40_000,
             "currently_held": False, "status_key": "ok", "plan": TLS, "max_days_overdue": 0},
        ],
    }


def _approx(a, b, tol=0.05):
    return abs(a - b) <= tol


def main():
    # --- 1. month_end() incl. December rollover and leap February -----------
    assert ctp_dashboard.month_end("2026-06-13") == date(2026, 6, 30)
    assert ctp_dashboard.month_end("2025-12-05") == date(2025, 12, 31)
    assert ctp_dashboard.month_end("2024-02-10") == date(2024, 2, 29)   # leap
    assert ctp_dashboard.month_end("2026-02-10") == date(2026, 2, 28)
    assert ctp_dashboard.month_end(date(2026, 1, 31)) == date(2026, 1, 31)
    assert ctp_dashboard.month_end(None) is None
    print("ok: month_end() handles every month incl. Dec + leap years")

    r = _result()
    today = ctp_dashboard.build(r)                                   # default
    me = ctp_dashboard.build(r, as_of=ctp_dashboard.month_end(r["as_of"]))

    # --- 2. invariant: omitting as_of == passing the analysis date ----------
    explicit_today = ctp_dashboard.build(r, as_of="2026-06-13")
    assert today["aging"] == explicit_today["aging"]
    assert today["upcoming"]["total"] == explicit_today["upcoming"]["total"]
    print("ok: today row unchanged — default build == build(as_of=analysis date)")

    # --- 3. aging shares move forward at month-end --------------------------
    # today: over60 = B+D = 2.0M of 5.0M gross = 40%; over90 = B = 20%
    assert _approx(today["aging"]["over60_pct"], 40.0), today["aging"]
    assert _approx(today["aging"]["over90_pct"], 20.0), today["aging"]
    # month-end: over60 = A+B+D = 3.0M = 60%; over90 = B+D = 2.0M = 40%
    assert _approx(me["aging"]["over60_pct"], 60.0), me["aging"]
    assert _approx(me["aging"]["over90_pct"], 40.0), me["aging"]
    print("ok: AR>60 40%->60% and AR>90 20%->40% when aged to month-end")

    # --- 4. going-on-hold projection grows at month-end ---------------------
    assert today["upcoming"]["total"] == 0, today["upcoming"]
    assert me["upcoming"]["total"] == 1, me["upcoming"]      # Hold Co trips it
    names = [c["customer"] for band in me["upcoming"]["bands"].values() for c in band]
    assert "Hold Co" in names, names
    print("ok: 'Going on hold' 0 today -> 1 at month-end (Hold Co crosses)")

    # --- 5. pure balance/status metrics are identical on both lines ---------
    assert _approx(today["unapplied"]["pct"], me["unapplied"]["pct"])
    assert len(today["held_negative"]) == len(me["held_negative"]) == 1
    assert today["below"]["count"] == me["below"]["count"] == 1
    print("ok: unapplied cash / on-hold-negative / below-threshold unchanged")

    # --- 6. projected top-offender LISTS (the dashboard's new section) ------
    t60 = {c["key"] for c in today["over60_customers"]}
    t90 = {c["key"] for c in today["over90_customers"]}
    m60 = {c["key"] for c in me["over60_customers"]}
    m90 = {c["key"] for c in me["over90_customers"]}
    assert t60 == {"B", "D"} and t90 == {"B"}, (t60, t90)
    assert m60 == {"A", "B", "D"} and m90 == {"B", "D"}, (m60, m90)
    # the "new at month-end" delta the template flags: projected minus today
    assert m60 - t60 == {"A"}, "Alpha becomes an over-60 offender only at ME"
    assert m90 - t90 == {"D"}, "Delta becomes an over-90 offender only at ME"
    print("ok: projected offender lists + new-at-month-end delta (A / D)")

    print("\nALL MONTH-END KPI TESTS PASSED")


if __name__ == "__main__":
    main()
