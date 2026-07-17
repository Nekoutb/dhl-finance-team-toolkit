"""One-off: seed data/ctp_daily_stats.json from the CtP analyses already on
record, so the credit-hold and ageing graphs start with history.

Walks stored analyses oldest-first (same-day: the later upload wins, matching
live behaviour) and records each under its created_at (fallback as_of) date.
Idempotent — re-running just rewrites the same points.

Caveat: for customers not covered by a stored master hold flag, the held
count uses TODAY'S manual hold register (the register has no history), so
backfilled points can drift slightly from what the day actually looked like.

Prod: run as www-data (data/ is www-data-owned):
    sudo -u www-data .venv/bin/python scripts/backfill_ctp_stats.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import ongoing_ctp as ctp  # noqa: E402


def main():
    results = []
    for path in sorted(ctp.STORE_DIR.glob("*.json")):
        name = path.name
        if name.endswith("_bank.json") or name in ("master.json",
                                                   "priority.json"):
            continue
        result = ctp.load_result(path.stem)
        if not result or "customers" not in result or "invoices" not in result:
            continue
        day = (result.get("created_at") or "")[:10] \
            or str(result.get("as_of", ""))[:10]
        if not day:
            continue
        results.append((result.get("created_at") or day, day, result))

    results.sort(key=lambda r: r[0])          # oldest first; later upload wins
    days = {}
    for _created, day, result in results:
        ctp.record_daily_stats(result, day=day)
        days[day] = ctp.compute_daily_stats(result)

    if not days:
        print("No stored analyses found — nothing to backfill.")
        return
    print(f"Backfilled {len(days)} day(s) from {len(results)} analysis file(s):")
    for day in sorted(days):
        s = days[day]
        print(f"  {day}: held={s['held']}  >60d={s['pct_over_60']}%  "
              f">90d={s['pct_over_90']}%")
    print("Note: held counts for register-covered customers reflect TODAY'S "
          "hold register (the register keeps no history).")


if __name__ == "__main__":
    main()
