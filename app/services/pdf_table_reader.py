"""Read a PDF report's transaction table into the shared grid shape.

Used by the Mobile Money extractor and the Bank Statements tool. Best-effort
until tuned to each provider's real layout: the largest extracted table per
page wins; the text above the first table on page 1 becomes the report
header/metadata lines.
"""


def read_pdf_table(path):
    """PDF -> {sheet, header, metadata, rows} (same shape as excel_reader)."""
    import pdfplumber

    header, rows, metadata = [], [], []
    with pdfplumber.open(str(path)) as pdf:
        for page_no, page in enumerate(pdf.pages):
            tables = page.extract_tables() or []
            best = max(tables, key=len, default=None)
            if page_no == 0:
                text = page.extract_text() or ""
                first_cells = [c for c in (best[0] if best else []) if c]
                marker = str(first_cells[0]).strip() if first_cells else None
                for line in text.splitlines():
                    line = line.strip()
                    if marker and marker in line:
                        break
                    if line:
                        metadata.append(line)
                metadata = metadata[:8]
            if not best:
                continue
            table_header = ["" if c is None else str(c).strip() for c in best[0]]
            if not header:
                header = table_header
            start = 1 if table_header == header or page_no == 0 else 0
            for raw in best[start:]:
                cells = ["" if c is None else str(c).replace("\n", " ").strip()
                         for c in raw]
                if any(cells) and cells != header:
                    rows.append(cells)

    # De-duplicate header names like the excel reader does.
    counts, clean_header = {}, []
    for i, name in enumerate(header):
        name = name or f"Column {i + 1}"
        if name in counts:
            counts[name] += 1
            clean_header.append(f"{name} ({counts[name]})")
        else:
            counts[name] = 1
            clean_header.append(name)

    out_rows = []
    for i, cells in enumerate(rows):
        record = {clean_header[j]: (cells[j] if j < len(cells) else "")
                  for j in range(len(clean_header))}
        out_rows.append({"index": i, "data": record})
    return {"sheet": "PDF report", "header": clean_header,
            "metadata": metadata, "rows": out_rows}
