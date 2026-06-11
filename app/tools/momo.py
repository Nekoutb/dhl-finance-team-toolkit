"""Mobile Money payments (Orange / MTN) -> per-transaction PDF receipts.

The provider report arrives as Excel OR PDF. Both are normalised to the same
shape the Excel reader produces: {header, metadata (report header lines),
rows}. The user picks one transaction or a whole group; the PDF receipt
carries the report header + every selected line. Third-party names are
remembered per account/number (namespace "momo" in the customers store) and
auto-filled on the next upload.
"""
import json
import re
from pathlib import Path

from ..config import DATA_DIR
from ..services import customers, excel_reader, pdf_writer

TOOL_SLUG = "momo"
ACCT_SLUG = "momo_acct"          # remembered account numbers (per third party)
SETTINGS_PATH = DATA_DIR / "momo_settings.json"

PROVIDERS = {
    "orange": "Orange Money — Orange Cameroun S.A.",
    "mtn": "MTN Mobile Money — MTN Cameroon Ltd",
}
GENERIC_PROVIDER = "Mobile Money provider"

# Logo marks rendered on the receipt, per provider.
BRANDS = {
    "orange": {"bg": "#FF7900", "fg": "#FFFFFF", "word": "orange"},
    "mtn": {"bg": "#FFCC00", "fg": "#00304B", "word": "MTN"},
}


def provider_brand(provider_label):
    lowered = (provider_label or "").lower()
    for key, brand in BRANDS.items():
        if key in lowered:
            return brand
    return {"bg": "#3e63dd", "fg": "#FFFFFF", "word": "MoMo"}


def load_settings():
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_settings(data):
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                             encoding="utf-8")


def transaction_reference(row_data, header):
    """The short reference the receipt file name starts with: the final
    segment of the transaction ID (e.g. BM260417.1426.C00598 -> C00598)."""
    tx_col = next((h for h in header
                   if "transaction" in h.lower() or "trans id" in h.lower()), None)
    value = str(row_data.get(tx_col) or "").strip() if tx_col else ""
    if not value:  # fall back to any dotted reference-looking cell
        value = next((str(v) for v in row_data.values()
                      if isinstance(v, str) and v.count(".") >= 2), "")
    segment = value.split(".")[-1].strip() if value else ""
    ref = re.sub(r"[^A-Za-z0-9]", "", segment)
    return ref[:8] if ref else "receipt"


def detect_provider(metadata, filename=""):
    """Return a provider label from the report header lines / file name."""
    haystack = " ".join(metadata).lower() + " " + str(filename).lower()
    for key, label in PROVIDERS.items():
        if key in haystack:
            return label
    return GENERIC_PROVIDER


def read_report(path):
    """Excel or PDF -> the common {header, metadata, rows} shape."""
    if Path(path).suffix.lower() == ".pdf":
        from ..services.pdf_table_reader import read_pdf_table
        return read_pdf_table(path)
    return excel_reader.read_transactions(path)


def parse_for_review(path, filename=""):
    parsed = read_report(path)
    key_col = excel_reader.guess_key_column(parsed["header"])
    rows = []
    for row in parsed["rows"]:
        key_value = row["data"].get(key_col, "") if key_col else ""
        rows.append({
            "index": row["index"],
            "data": row["data"],
            "key": key_value,
            "name": customers.get_name(TOOL_SLUG, key_value) or "",
            "account": customers.get_name(ACCT_SLUG, key_value) or "",
        })
    return {
        "sheet": parsed["sheet"],
        "header": parsed["header"],
        "metadata": parsed["metadata"],
        "rows": rows,
        "key_col": key_col,
        "provider": detect_provider(parsed["metadata"], filename),
    }


def build_receipt(path, selected_indices, names_map, cfg, out_dir, filename="",
                  accounts_map=None):
    """Persist new third-party names + account numbers, then render the
    receipt PDF. The file name STARTS with the transaction reference (final
    segment of the first selected transaction ID, e.g. C00598)."""
    accounts_map = accounts_map or {}
    parsed = read_report(path)
    key_col = excel_reader.guess_key_column(parsed["header"])
    by_index = {row["index"]: row for row in parsed["rows"]}
    provider = detect_provider(parsed["metadata"], filename)

    transactions, first_name, first_account, ref = [], None, None, None
    for idx in selected_indices:
        row = by_index.get(idx)
        if not row:
            continue
        key_value = row["data"].get(key_col, "") if key_col else ""
        provided = (names_map.get(idx) or "").strip()
        if provided:
            customers.set_name(TOOL_SLUG, key_value, provided)
        provided_acct = (accounts_map.get(idx) or "").strip()
        if provided_acct:
            customers.set_name(ACCT_SLUG, key_value, provided_acct)
        name = provided or customers.get_name(TOOL_SLUG, key_value)
        account = provided_acct or customers.get_name(ACCT_SLUG, key_value)
        first_name = first_name or name
        first_account = first_account or account
        if ref is None:
            ref = transaction_reference(row["data"], parsed["header"])
        transactions.append(row["data"])

    ref = ref or "receipt"
    suffix = "" if len(transactions) <= 1 else f"_plus{len(transactions) - 1}"
    out_path = Path(out_dir) / f"{ref}{suffix}_MobileMoney.pdf"

    # Pull the document's own header (logos etc.). A PDF header strip already
    # contains the header text, so skip the text metadata to avoid duplication.
    from ..services import header_assets
    doc_header = header_assets.extract(path)
    is_pdf_strip = doc_header and Path(path).suffix.lower() == ".pdf"
    metadata = [] if is_pdf_strip else parsed["metadata"]

    title = "Mobile Money Transaction Receipt" if len(transactions) == 1 \
        else f"Mobile Money Transactions — {len(transactions)} selected"
    pdf_writer.build_pdf(
        out_path,
        company=provider,
        document_title=title,
        metadata=metadata,
        transactions=transactions,
        customer_name=first_name,
        customer_account=first_account,
        brand=provider_brand(provider),
        header_images=doc_header,
        layout="table" if len(transactions) > 1 else "blocks",
    )
    return {"customer_name": first_name, "customer_account": first_account,
            "count": len(transactions), "provider": provider,
            "reference": ref, "pdf_path": out_path}
