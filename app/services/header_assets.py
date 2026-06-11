"""Pull the document's own header — logos and header imagery — for receipts.

- .xlsx/.xlsm : embedded images (openpyxl) anchored near the top of the sheet
                (the provider's logo as placed in the file).
- .pdf        : the top strip of page 1, above the transaction table, rendered
                to an image — captures logo + header text exactly as printed.
- .xls        : legacy format exposes no images — text header only.

Returns a list of {"bytes": png/jpeg bytes, "width": px, "height": px}.
Never raises: header decoration must not break receipt generation.
"""
from pathlib import Path

MAX_IMAGES = 3


def _xlsx_images(path):
    import openpyxl

    out = []
    workbook = openpyxl.load_workbook(path)  # read_only can't see drawings
    sheet = workbook.active
    for img in getattr(sheet, "_images", [])[:MAX_IMAGES]:
        try:
            row = getattr(getattr(img.anchor, "_from", None), "row", 0)
            if row > 15:          # only images that belong to the header area
                continue
            data = img._data()
            width = getattr(img, "width", 0) or 0
            height = getattr(img, "height", 0) or 0
            out.append({"bytes": data, "width": width, "height": height})
        except Exception:  # noqa: BLE001 - skip any broken drawing
            continue
    workbook.close()
    return out


def _pdf_header_strip(path):
    import pdfplumber

    with pdfplumber.open(str(path)) as pdf:
        page = pdf.pages[0]
        tables = page.find_tables()
        top = min((t.bbox[1] for t in tables), default=None)
        if top is None or top < 24:   # no table, or no room above it
            return []
        strip = page.crop((0, 0, page.width, top))
        image = strip.to_image(resolution=150)
        from io import BytesIO
        buf = BytesIO()
        image.original.save(buf, format="PNG")
        return [{"bytes": buf.getvalue(),
                 "width": image.original.width,
                 "height": image.original.height}]


def extract(path):
    """Header images for the given source document (best effort)."""
    suffix = Path(path).suffix.lower()
    try:
        if suffix in (".xlsx", ".xlsm"):
            return _xlsx_images(path)
        if suffix == ".pdf":
            return _pdf_header_strip(path)
    except Exception:  # noqa: BLE001
        return []
    return []
