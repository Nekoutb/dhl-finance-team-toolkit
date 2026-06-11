"""Write the CtP analysis to a styled multi-sheet Excel workbook."""
from pathlib import Path

import pandas as pd


def _d(value):
    if not value:
        return ""
    return value if isinstance(value, str) else value.isoformat()


def _cust_frame(rows):
    return pd.DataFrame([{
        "Customer": c["customer"],
        "Account": c["key"],
        "Clerk": c.get("clerk", ""),
        "Treatment plan": c["plan_label"],
        "Invoices": c["invoice_count"],
        "Credits": c.get("credit_count", 0),
        "Total AR (net)": round(c["total_ar"], 2),
        "Overdue": round(c["overdue"], 2),
        "% beyond GCTP": round(c["beyond_gctp_pct"], 1),
        "Max days overdue": c["max_days_overdue"],
        "Risk control": c["status"],
        "Currently on hold": "Yes" if c.get("currently_held") else "No",
        "Hold source": c.get("hold_source", ""),
        "Master balance": c.get("master_balance", ""),
        "Segment (master)": c.get("master_segment", ""),
        "Next action": c["next_action"],
        "Owner": c["owner"],
    } for c in rows])


def build_ctp_report(out_path, result, hold_cmp=None):
    summary = result["summary"]
    status_labels = {"stop": "Credit stop", "hold": "Credit hold",
                     "watch": "Watch list", "ok": "Within terms"}

    controls = pd.DataFrame([{
        "Control / action": g["action"],
        "Executed by": g["owner"],
        "Automated": "Yes (system dunning)" if g.get("auto") else "No",
        "Invoices": g["invoices"],
        "Customers": g["customers"],
        "Amount": round(g["amount"], 2),
        "Calls suppressed": g["suppressed"] or "",
    } for g in result["controls_summary"]])

    customers = _cust_frame(result["customers"])

    invoices = pd.DataFrame([{
        "Customer": i["customer"],
        "Account": i["account"],
        "Type": "Credit/Payment" if i["kind"] == "credit" else "Invoice",
        "Document": i["invoice_no"],
        "Document date": _d(i["invoice_date"]),
        "Due date": _d(i["due_date"]),
        "Doc type": i["doc_type"],
        "Amount": round(i["amount"], 2),
        "Currency": i["currency"],
        "Clerk": i.get("clerk", ""),
        "Days overdue": i["days_overdue"],
        "Control / action": i["action"],
        "Owner": i["owner"],
        "Call suppressed": "Yes" if i["call_suppressed"] else "",
    } for i in result["invoices"]])

    tc = result.get("total_check")
    tb = result.get("tb_check")
    overview_rows = [
        ("As-of date", _d(result["as_of"])),
    ]
    if tb:
        overview_rows += [
            ("FIRST ANALYSIS — transaction file vs AR trial balance", ""),
            ("  Total AR per transaction file", round(tb["tx_total"], 2)),
            ("  Total AR per trial balance", round(tb["tb_total"], 2)),
            ("  Difference", round(tb["difference"], 2)),
            ("  Agreement", "AGREES" if tb["matches"] else "DISAGREES — investigate"),
            ("", ""),
        ]
    overview_rows += [
        ("Invoices analysed", summary["invoice_count"]),
        ("Credits / payments on account", summary["credit_count"]),
        ("Customers", summary["customer_count"]),
        ("Total AR (net)", round(summary["total_ar"], 2)),
        ("Overdue total", round(summary["overdue_total"], 2)),
        ("Credits total", round(summary["credits_total"], 2)),
        ("Manual actions due", summary["actions_due"]),
        ("Items in automated dunning (letters by system)", summary.get("auto_dunning", 0)),
        ("Calls suppressed (below value threshold)", summary["calls_suppressed"]),
        ("Currencies", ", ".join(result["currencies"]) or "n/a"),
    ]
    if tc:
        overview_rows += [
            ("", ""),
            ("File control total (total line in file)", round(tc["file_total"], 2)),
            ("Sum of line items", round(tc["line_sum"], 2)),
            ("Control total check", "MATCHES" if tc["matches"] else "MISMATCH — investigate"),
        ]
    overview_rows += [("", ""), ("Customers by risk control", "")]
    overview_rows += [(f"  {status_labels.get(k, k)}", v)
                      for k, v in summary["status_counts"].items()]
    if hold_cmp:
        overview_rows += [
            ("", ""),
            ("Credit-hold register comparison", ""),
            ("  To PLACE on credit hold (policy requires, not held)",
             len(hold_cmp["place_on_hold"])),
            ("  To REVIEW for release (held, policy does not require)",
             len(hold_cmp["review_release"])),
            ("  Correctly on hold", len(hold_cmp["correctly_held"])),
            ("  Customers on the hold register", hold_cmp["register_size"]),
        ]
    overview = pd.DataFrame(overview_rows, columns=["Metric", "Value"])

    sheets = [("Summary", overview), ("Control summary", controls)]
    rec = result.get("reconciliation")
    if rec:
        rec_rows = (
            [{"Type": "Balance mismatch", "Customer": x["customer"], "Account": x["key"],
              "Per transactions": x["transactions"], "Per master": x["master"],
              "Difference": x["difference"]} for x in rec["mismatches"]]
            + [{"Type": "Only in master", "Customer": x["customer"], "Account": x["key"],
                "Per transactions": "", "Per master": x["master"], "Difference": ""}
               for x in rec["only_in_master"]]
            + [{"Type": "Only in transactions", "Customer": x["customer"], "Account": x["key"],
                "Per transactions": x["transactions"], "Per master": "", "Difference": ""}
               for x in rec["only_in_transactions"]]
        )
        if not rec_rows:
            rec_rows = [{"Type": f"All {rec['matched']} customers reconcile — no exceptions"}]
        sheets.append(("Master reconciliation", pd.DataFrame(rec_rows)))
    if hold_cmp:
        place = _cust_frame(hold_cmp["place_on_hold"])
        release = _cust_frame(hold_cmp["review_release"])
        place.insert(0, "Exception", "Place on credit hold")
        release.insert(0, "Exception", "Review for release")
        exceptions = pd.concat([place, release], ignore_index=True) \
            if len(place) + len(release) else pd.DataFrame(
                [{"Exception": "None — register matches policy"}])
        sheets.append(("Hold exceptions", exceptions))
    sheets += [("Customer risk", customers), ("Controls by invoice", invoices)]

    out_path = Path(out_path)
    with pd.ExcelWriter(out_path, engine="xlsxwriter") as xl:
        book = xl.book
        header_fmt = book.add_format({
            "bold": True, "font_color": "white", "bg_color": "#0b2545",
            "border": 1, "valign": "vcenter"})
        for sheet_name, frame in sheets:
            frame.to_excel(xl, sheet_name=sheet_name, index=False)
            ws = xl.sheets[sheet_name]
            for col_idx, col in enumerate(frame.columns):
                width = max(len(str(col)),
                            *(frame[col].astype(str).map(len).tolist() or [0]))
                ws.set_column(col_idx, col_idx, min(max(width + 2, 10), 46))
                ws.write(0, col_idx, col, header_fmt)
            ws.freeze_panes(1, 0)
    return out_path
