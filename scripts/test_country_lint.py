"""COUNTRY-HARDCODING LINT — baseline-locked (v12 phase 0).

The multi-tenant migration moves Cameroon's specifics into per-country config.
That takes several releases, so today's occurrences cannot simply be banned —
instead they are SNAPSHOTTED as a baseline. The build then fails when a *new*
one appears, so the hardcoding cannot grow back faster than we remove it.

  python scripts/test_country_lint.py             # fail on NEW occurrences
  python scripts/test_country_lint.py --bless     # re-record after removing
                                                  # some (the count must DROP)

The baseline is a per-file count, not line numbers, so ordinary edits don't
churn it. Adding a hardcoded value to a file fails; removing them is expected
and the baseline is re-recorded downward.
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "samples" / "golden" / "country_lint_baseline.json"
BLESS = "--bless" in sys.argv

# What must eventually live in per-country config. Each entry: name -> regex.
PATTERNS = {
    "XAF":            r"\bXAF\b",
    "CM01":           r"\bCM01\b",
    "cameroon":       r"(?i)\bcamero(?:on|un)\b",
    "cfa_peg":        r"655\.957",
    "momo_merchant":  r"\b675153953\b",
    "eneo_vat":       r"\b19\.25\b",
    "zero_dec_money": r'"\{:,\.0f\}"',
    "xlsx_money_fmt": r'"#,##0"',
}

# Files that are ALLOWED to keep these forever.
EXEMPT = (
    "app/i18n/",             # language packs (future)
    "app/country.py",        # the per-country accessor (future)
    "data/",
    "samples/",
    "design-explorations/",
)

SCAN_DIRS = ("app",)
SCAN_EXT = (".py", ".html", ".js")


def scan():
    """{relative_path: {pattern_name: count}} for every non-exempt file."""
    found = {}
    for d in SCAN_DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if not p.is_file() or p.suffix not in SCAN_EXT:
                continue
            rel = p.relative_to(ROOT).as_posix()
            if any(rel.startswith(x) for x in EXEMPT):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            counts = {}
            for name, rx in PATTERNS.items():
                n = len(re.findall(rx, text))
                if n:
                    counts[name] = n
            if counts:
                found[rel] = counts
    return found


now = scan()

if BLESS or not BASELINE.exists():
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps(now, indent=1, sort_keys=True),
                        encoding="utf-8")
    total = sum(sum(c.values()) for c in now.values())
    print(f"BASELINE RECORDED — {total} occurrence(s) across {len(now)} file(s)")
    print(f"  -> {BASELINE.relative_to(ROOT)}")
    sys.exit(0)

base = json.loads(BASELINE.read_text(encoding="utf-8"))
regressions, improvements = [], []

for rel, counts in sorted(now.items()):
    for name, n in sorted(counts.items()):
        was = base.get(rel, {}).get(name, 0)
        if n > was:
            regressions.append(f"{rel}: {name} {was} -> {n}  (+{n - was})")
        elif n < was:
            improvements.append(f"{rel}: {name} {was} -> {n}")
for rel, counts in sorted(base.items()):
    for name, was in sorted(counts.items()):
        if name not in now.get(rel, {}):
            improvements.append(f"{rel}: {name} {was} -> 0  (cleared)")

total_now = sum(sum(c.values()) for c in now.values())
total_base = sum(sum(c.values()) for c in base.values())
print(f"country-specific literals: {total_now} now / {total_base} at baseline "
      f"({len(now)} file(s))")

if improvements:
    print(f"\n  {len(improvements)} improvement(s) — re-record with --bless:")
    for i in improvements[:15]:
        print("   ", i)

if regressions:
    print(f"\n[FAIL] {len(regressions)} NEW hardcoded country value(s):")
    for r in regressions:
        print("   ", r)
    print("\n  These must come from per-country config, not a literal.")
    print("  See the v12 plan: app/country.py accessor + Jinja money()/cur().")
    sys.exit(1)

print("\n[OK ] no new hardcoded country values")
