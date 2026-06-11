"""Business logic for the Orange Cameroun Mobile Money -> PDF tool.

Pipeline:
  1. parse_for_review() reads the upload and pre-fills any remembered names.
  2. The user selects as many rows as needed and confirms / adds the name.
  3. build_receipt() saves new names and renders the PDF (tabular for groups).
"""
from pathlib import Path

from ..services import customers, excel_reader, header_assets, pdf_writer

TOOL_SLUG = "orange-cameroun"
MAX_TRANSACTIONS = 10000   # selection is unlimited in practice


def parse_for_review(path):
    """Parse the upload into a view model for the review page."""
    parsed = excel_reader.read_transactions(path)
    key_col = excel_reader.guess_key_column(parsed["header"])

    rows = []
    for row in parsed["rows"]:
        key_value = row["data"].get(key_col, "") if key_col else ""
        rows.append({
            "index": row["index"],
            "data": row["data"],
            "key": key_value,
            # Pre-fill the remembered name -> "identify automatically next time".
            "name": customers.get_name(TOOL_SLUG, key_value) or "",
        })

    return {
        "sheet": parsed["sheet"],
        "header": parsed["header"],
        "metadata": parsed["metadata"],
        "rows": rows,
        "key_col": key_col,
    }


def build_receipt(path, selected_indices, names_map, cfg, out_path):
    """Persist names, render the PDF, and report what was produced."""
    parsed = excel_reader.read_transactions(path)
    key_col = excel_reader.guess_key_column(parsed["header"])
    by_index = {row["index"]: row for row in parsed["rows"]}

    transactions, customer_name = [], None
    for idx in selected_indices[:MAX_TRANSACTIONS]:
        row = by_index.get(idx)
        if not row:
            continue
        key_value = row["data"].get(key_col, "") if key_col else ""

        provided = names_map.get(idx)
        if provided:
            customers.set_name(TOOL_SLUG, key_value, provided)
            customer_name = customer_name or provided
        else:
            customer_name = customer_name or customers.get_name(TOOL_SLUG, key_value)

        transactions.append(row["data"])

    # The document's own header (logos etc.); for PDFs the strip already
    # carries the header text, so skip the text lines to avoid duplication.
    doc_header = header_assets.extract(path)
    is_pdf_strip = doc_header and Path(path).suffix.lower() == ".pdf"
    metadata = [] if is_pdf_strip else parsed["metadata"]

    oc = cfg["orange_cameroun"]
    pdf_writer.build_pdf(
        out_path,
        company=oc["company_label"],
        document_title=oc["document_title"],
        metadata=metadata,
        transactions=transactions,
        customer_name=customer_name,
        header_images=doc_header,
        layout="table" if len(transactions) > 1 else "blocks",
    )
    return {"customer_name": customer_name, "count": len(transactions)}
