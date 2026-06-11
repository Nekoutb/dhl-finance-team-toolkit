"""Self-test the CtP analyzer + Excel report against the SAP-style AR sample."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import ctp_rules, xlsx_report
from app.tools import ongoing_ctp as ctp

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "samples" / "ar_breakdown_sample.xlsx"


def main():
    result = ctp.analyze(SAMPLE, as_of=date(2026, 6, 9), default_rank=60)
    print("Mapping:")
    for f, c in result["mapping"].items():
        print(f"   {f:14s} -> {c}")
    s = result["summary"]
    print("\nSummary:", s)

    # Names mapped to the name column, accounts to the number column.
    assert result["mapping"]["customer"] == "Customer Account: Name 1", result["mapping"]
    assert result["mapping"]["account"] == "Customer", result["mapping"]
    assert result["mapping"]["amount"] == "Document Currency Value", result["mapping"]

    # 8 invoices + 2 credits; the total line excluded and verified.
    assert s["invoice_count"] == 8 and s["credit_count"] == 2, s
    tc = result["total_check"]
    print("\nTotal check:", tc)
    assert tc and tc["matches"] and tc["lines"] == 1, tc

    print("\nCustomer risk:")
    for c in result["customers"]:
        print(f"   {c['customer']:18s} acct={c['key']} AR={c['total_ar']:>9,.0f} "
              f"beyond={c['beyond_gctp_pct']:5.1f}% -> {c['status_key'].upper()}"
              f" | next: {c['next_action'][:46]}")
        assert c["customer"].strip() and not c["customer"].isdigit(), \
            f"customer name missing: {c}"

    print("\nControl summary:")
    for g in result["controls_summary"]:
        mode = "AUTO " if g["auto"] else "manual"
        print(f"   [{mode}] {g['invoices']} inv / {g['customers']} cust / "
              f"{g['amount']:>10,.0f} | {g['action'][:56]}")

    # No manual letter-sending controls — dunning is automated.
    actions = [g["action"] for g in result["controls_summary"]]
    assert not any(a.startswith("Send '") for a in actions), actions
    auto_groups = [g for g in result["controls_summary"] if g["auto"]]
    assert auto_groups and s["auto_dunning"] >= 2, (auto_groups, s)
    # Manual actions exclude automated dunning items.
    assert s["actions_due"] == sum(
        g["invoices"] for g in result["controls_summary"]
        if not g["auto"] and g["action"] not in (ctp.WITHIN_TERMS, ctp.CREDIT_ACTION)), s

    # Credits classified, not given dunning actions.
    credits = [i for i in result["invoices"] if i["kind"] == "credit"]
    assert len(credits) == 2 and all(i["action"] == ctp.CREDIT_ACTION for i in credits)

    # Gamma's 24d call is below the XAF threshold (rank 60 -> 750 EUR ≈ 492k XAF).
    suppressed = [i["invoice_no"] for i in result["invoices"] if i["call_suppressed"]]
    assert "90000006" in suppressed, suppressed
    beta_call = next(i for i in result["invoices"] if i["invoice_no"] == "90000004")
    assert not beta_call["call_suppressed"], beta_call  # 950k > threshold

    # Hold comparison: with an empty register, hold/stop customers -> "place".
    hc = ctp.hold_compare(result["customers"])
    print("\nHold compare:", {k: (len(v) if isinstance(v, list) else v)
                              for k, v in hc.items()})
    should_hold = [c for c in result["customers"] if c["status_key"] in ("hold", "stop")
                   and not c["currently_held"]]
    assert len(hc["place_on_hold"]) == len(should_hold)

    # Persistence round-trip (tokens are hex — like the real route's uuids).
    ctp.save_result("ab12cd34ef", result)
    loaded = ctp.load_result("ab12cd34ef")
    assert loaded and loaded["summary"]["invoice_count"] == 8
    (ctp.STORE_DIR / "ab12cd34ef.json").unlink(missing_ok=True)

    out = ROOT / "data" / "outputs" / "selftest_ctp_report.xlsx"
    out.parent.mkdir(parents=True, exist_ok=True)
    xlsx_report.build_ctp_report(out, result, hc)
    print(f"\nExcel report: {out} exists={out.exists()} size={out.stat().st_size}")
    print("\nALL CtP CHECKS PASSED")


if __name__ == "__main__":
    main()
