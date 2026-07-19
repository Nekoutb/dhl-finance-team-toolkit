"""Orange Money monthly statement -> per-correspondant branded PDF receipts.

Pipeline:
  1. parse_for_review() parses the Orange "Relevé de vos opérations" export,
     keeps the SUCCESSFUL collections (credits — drops "Echec" and outgoing
     C2C débits) and groups them by correspondant, pre-filling any remembered
     customer name + AR account number.
  2. The user confirms / adds the name + account for each correspondant.
  3. build_zip() saves those identities, renders one Orange-branded receipt per
     successful transaction into a per-correspondant folder, and bundles the
     lot into a single ZIP for download.

Identities are remembered per correspondant number so the next upload auto-
fills them before generation — the customer directory grows month on month.
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR
from ..services import customers, pdf_writer
from ..services.orange_statement import fmt_xaf, parse_statement

TOOL_SLUG = "orange-cameroun"          # remembered customer NAMES  (key = correspondant)
ACCT_SLUG = "orange-cameroun_acct"     # remembered AR ACCOUNT numbers (key = correspondant)
HISTORY_DIR = DATA_DIR / "orange"      # one JSON summary per uploaded statement


def _safe(text):
    """Filesystem/URL-friendly: keep letters, digits, space, dash, underscore."""
    keep = [c if (c.isalnum() or c in " -_") else "-" for c in str(text or "")]
    return "".join(keep).strip() or "Orange"


def bundle_name(meta):
    """The parent folder / zip stem, e.g. 'Collections Avril 2026 - 658902134'."""
    month = meta.get("month_label") or "Collections"
    account = meta.get("account") or ""
    label = f"Collections {month}" + (f" - {account}" if account else "")
    return _safe(label)


def parse_for_review(path):
    """View model for the review page: statement meta + correspondants (with
    remembered name/account) + the full collection list."""
    data = parse_statement(path)
    rows = []
    for corr, grp in sorted(data["correspondants"].items(),
                            key=lambda kv: (-kv[1]["total"], kv[0])):
        rows.append({
            "correspondant": corr,
            "count": len(grp["transactions"]),
            "total": grp["total"],
            "total_fmt": fmt_xaf(grp["total"]),
            "name": customers.get_name(TOOL_SLUG, corr) or "",
            "account": customers.get_name(ACCT_SLUG, corr) or "",
        })
    return {
        "meta": data["meta"],
        "correspondants": rows,
        "collections": data["collections"],
        "totals": data["totals"],
    }


def build_zip(path, identities, out_zip):
    """Persist the name/account identities, render one branded receipt per
    successful collection into per-correspondant folders, and zip them.

    ``identities`` = {correspondant: {"name": .., "account": ..}}.
    Returns a summary dict (counts, total, month, bundle name).
    """
    data = parse_statement(path)
    meta, collections = data["meta"], data["collections"]

    # 1. remember every identity the user supplied (keyed by correspondant).
    # The review form pre-fills known names/accounts, so a field submitted
    # BLANK means the user cleared it — forget the stored value rather than
    # silently keeping (and re-printing) the stale one.
    for corr, ident in (identities or {}).items():
        name = (ident.get("name") or "").strip()
        account = (ident.get("account") or "").strip()
        if name:
            customers.set_name(TOOL_SLUG, corr, name)
        else:
            customers.delete_name(TOOL_SLUG, corr)
        if account:
            customers.set_name(ACCT_SLUG, corr, account)
        else:
            customers.delete_name(ACCT_SLUG, corr)

    bundle = bundle_name(meta)
    tmp = Path(tempfile.mkdtemp(prefix="orange_"))
    try:
        for tx in collections:
            corr = tx["correspondant"]
            name = customers.get_name(TOOL_SLUG, corr)
            account = customers.get_name(ACCT_SLUG, corr)
            customer = {"name": name, "account": account} if (name or account) else None
            corr_dir = tmp / _safe(corr)
            corr_dir.mkdir(parents=True, exist_ok=True)
            # Guarantee a unique filename so two transactions that share a
            # reference (or a blank/odd reference that normalises the same) can
            # never silently overwrite each other — every receipt is kept.
            base = f"{_safe(corr)}_{_safe(tx['reference'].replace('.', '_'))}"
            fname, n = f"{base}.pdf", 2
            while (corr_dir / fname).exists():
                fname, n = f"{base}_{n}.pdf", n + 1
            pdf_writer.build_orange_receipt(corr_dir / fname, meta=meta, tx=tx,
                                            customer=customer)

        out_zip = Path(out_zip)
        out_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for pdf in sorted(tmp.rglob("*.pdf")):
                zf.write(pdf, arcname=f"{bundle}/{pdf.parent.name}/{pdf.name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return {
        "count": len(collections),
        "correspondant_count": data["totals"]["correspondant_count"],
        "credited": data["totals"]["credited"],
        "credited_fmt": fmt_xaf(data["totals"]["credited"]),
        "month_label": meta.get("month_label", ""),
        "account": meta.get("account", ""),
        "bundle": bundle,
        "zip_name": f"{bundle}.zip",
    }


# --- Upload history (one summary per statement-month) -------------------------
def history_token(meta):
    """Stable 12-hex id per merchant account + statement period, so a
    re-uploaded month REPLACES its history entry instead of duplicating it."""
    raw = (f"orange:{meta.get('account', '')}|{meta.get('period_start', '')}"
           f"|{meta.get('period_end', '')}|{meta.get('month_label', '')}")
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def _write_upload(payload):
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_DIR / f"{payload['token']}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, HISTORY_DIR / f"{payload['token']}.json")


def save_upload_summary(model, source=""):
    """Persist what an upload contained — per-correspondant totals plus the
    individual collections — so previous uploads stay consultable and other
    tools can read the mobile-money payments. Returns the history token."""
    meta = model["meta"]
    token = history_token(meta)
    old = load_upload(token)
    payload = {
        "token": token,
        "uploaded_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": source,
        "meta": {k: meta.get(k, "") for k in
                 ("account", "company", "month_label",
                  "period_start", "period_end")},
        "totals": model["totals"],
        "correspondants": [
            {k: c.get(k, "") for k in
             ("correspondant", "name", "account", "count", "total")}
            for c in model["correspondants"]],
        "collections": [
            {"correspondant": t.get("correspondant", ""),
             "date": t.get("date", ""), "reference": t.get("reference", ""),
             "credit": t.get("credit", 0), "service": t.get("service", "")}
            for t in model["collections"]],
        # keep the generated stamp when the same month is re-uploaded
        "generated_at": (old or {}).get("generated_at", ""),
    }
    _write_upload(payload)
    return token


def mark_generated(token, identities=None):
    """Stamp a history entry as generated and refresh its names/accounts from
    what the user confirmed on the review page. No-op on an unknown token."""
    u = load_upload(token)
    if not u:
        return None
    for c in u.get("correspondants", []):
        ident = (identities or {}).get(c.get("correspondant"))
        if ident is not None:
            c["name"] = (ident.get("name") or "").strip()
            c["account"] = (ident.get("account") or "").strip()
    u["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _write_upload(u)
    return u


def list_uploads(limit=12):
    """Previous uploads, newest first — headline fields only."""
    if not HISTORY_DIR.exists():
        return []
    items = []
    for path in HISTORY_DIR.glob("*.json"):
        try:
            u = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        items.append({
            "token": u.get("token", path.stem),
            "uploaded_at": u.get("uploaded_at", ""),
            "source": u.get("source", ""),
            "month_label": u.get("meta", {}).get("month_label", ""),
            "account": u.get("meta", {}).get("account", ""),
            "count": u.get("totals", {}).get("count", 0),
            "credited": u.get("totals", {}).get("credited", 0),
            "correspondant_count": u.get("totals", {}).get(
                "correspondant_count", 0),
            "generated_at": u.get("generated_at", ""),
        })
    items.sort(key=lambda u: u.get("uploaded_at", ""), reverse=True)
    return items[:limit]


def load_upload(token):
    """One stored upload summary, or None (token is hex-validated)."""
    if not token or not re.fullmatch(r"[0-9a-f]+", str(token)):
        return None
    path = HISTORY_DIR / f"{token}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def delete_upload(token):
    """Remove a processed file from the record (e.g. treated twice) — the
    monthly report and every cross-tool reader recompute without it."""
    if not token or not re.fullmatch(r"[0-9a-f]+", str(token)):
        return False
    path = HISTORY_DIR / f"{token}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def monthly_report():
    """Customer × month pivot of every Orange Money payment on record: one
    row per correspondant — customer name, AR account N°, phone number —
    and, per statement month, the total payments read for them across the
    processed files (a transaction appearing in overlapping files is
    counted once)."""
    uploads = [u for m in list_uploads(limit=1000)
               if (u := load_upload(m["token"]))]
    uploads.sort(key=lambda u: u.get("uploaded_at", ""))
    month_labels = {}                     # "YYYY-MM" -> label ("Avril 2026")
    rows, seen = {}, set()
    for u in uploads:
        meta = u.get("meta", {})
        mkey = (meta.get("period_start") or u.get("uploaded_at", ""))[:7] \
            or "?"
        month_labels.setdefault(mkey, meta.get("month_label") or mkey)
        idents = {c.get("correspondant", ""): c
                  for c in u.get("correspondants", [])}
        for tx in u.get("collections", []):
            corr = str(tx.get("correspondant", ""))
            dedup = (corr, str(tx.get("reference", "")),
                     str(tx.get("date", "")), tx.get("credit", 0))
            if dedup in seen:
                continue
            seen.add(dedup)
            row = rows.setdefault(corr, {
                "correspondant": corr, "name": "", "account": "",
                "months": {}, "total": 0.0})
            ident = idents.get(corr) or {}
            if (ident.get("name") or "").strip():
                row["name"] = ident["name"].strip()
            if (ident.get("account") or "").strip():
                row["account"] = ident["account"].strip()
            amount = float(tx.get("credit") or 0)
            row["months"][mkey] = round(row["months"].get(mkey, 0) + amount, 2)
            row["total"] = round(row["total"] + amount, 2)
    for row in rows.values():             # freshest remembered identity wins
        row["name"] = row["name"] \
            or customers.get_name(TOOL_SLUG, row["correspondant"]) or ""
        row["account"] = row["account"] \
            or customers.get_name(ACCT_SLUG, row["correspondant"]) or ""
    months = sorted(month_labels)
    ordered = sorted(rows.values(), key=lambda r: -r["total"])
    return {"months": [{"key": m, "label": month_labels[m]} for m in months],
            "rows": ordered,
            "grand": {m: round(sum(r["months"].get(m, 0)
                                   for r in rows.values()), 2)
                      for m in months},
            "grand_total": round(sum(r["total"] for r in rows.values()), 2),
            "uploads": len(uploads)}


def build_monthly_xlsx(out_path, report):
    """The customer × month payments pivot as a clean Excel workbook."""
    import xlsxwriter

    wb = xlsxwriter.Workbook(str(out_path))
    ws = wb.add_worksheet("Payments by month")
    f_title = wb.add_format({"bold": True, "font_size": 13,
                             "font_name": "Arial"})
    f_head = wb.add_format({"font_name": "Arial", "font_size": 10,
                            "bold": True, "bg_color": "#0b2545",
                            "font_color": "white", "border": 1})
    f_cell = wb.add_format({"font_name": "Arial", "font_size": 10,
                            "border": 1})
    f_num = wb.add_format({"font_name": "Arial", "font_size": 10,
                           "border": 1, "num_format": "#,##0"})
    f_tot = wb.add_format({"font_name": "Arial", "font_size": 10,
                           "bold": True, "border": 1, "num_format": "#,##0",
                           "bg_color": "#eef3fb"})
    ws.write("A1", "Orange Money — customer payments by month (XAF)",
             f_title)
    headers = ["Customer name", "AR account N°", "Phone number"] + \
        [m["label"] for m in report["months"]] + ["Total"]
    for c, h in enumerate(headers):
        ws.write(2, c, h, f_head)
    ws.set_column(0, 0, 32)
    ws.set_column(1, 2, 16)
    ws.set_column(3, len(headers) - 1, 14)
    r = 3
    for row in report["rows"]:
        ws.write(r, 0, row["name"] or "—", f_cell)
        ws.write(r, 1, row["account"] or "—", f_cell)
        ws.write(r, 2, row["correspondant"], f_cell)
        for c, m in enumerate(report["months"], start=3):
            ws.write_number(r, c, row["months"].get(m["key"], 0), f_num)
        ws.write_number(r, 3 + len(report["months"]), row["total"], f_num)
        r += 1
    ws.write(r, 0, "TOTAL", f_tot)
    ws.write(r, 1, "", f_tot)
    ws.write(r, 2, "", f_tot)
    for c, m in enumerate(report["months"], start=3):
        ws.write_number(r, c, report["grand"].get(m["key"], 0), f_tot)
    ws.write_number(r, 3 + len(report["months"]), report["grand_total"],
                    f_tot)
    ws.freeze_panes(3, 0)
    wb.close()
    return out_path
