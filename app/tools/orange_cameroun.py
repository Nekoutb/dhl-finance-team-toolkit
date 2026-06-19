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
import shutil
import tempfile
import zipfile
from pathlib import Path

from ..services import customers, pdf_writer
from ..services.orange_statement import fmt_xaf, parse_statement

TOOL_SLUG = "orange-cameroun"          # remembered customer NAMES  (key = correspondant)
ACCT_SLUG = "orange-cameroun_acct"     # remembered AR ACCOUNT numbers (key = correspondant)


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
