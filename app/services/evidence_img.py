"""Render a single matched line (bank statement or BIT file) into a clean,
legible PNG 'snapshot' for the cheque evidence bundle.

Pure Pillow: Pillow ships a scalable default font (``ImageFont.load_default(
size=...)`` returns a FreeType font since Pillow 10.1), so there is NO external
font-file dependency — the same code renders identically on Windows and the
Linux server.
"""
import unicodedata
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

# Pillow's bundled default font covers ASCII but NOT accented Latin (é, à, ç,
# ô…) or typographic dashes/quotes — those would render as .notdef boxes. All
# text is transliterated to clean ASCII before drawing.
_PUNCT = {"—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"',
          "…": "...", " ": " ", " ": " "}


def _sanitize(text):
    s = str(text)
    for k, v in _PUNCT.items():
        s = s.replace(k, v)
    s = "".join(c for c in unicodedata.normalize("NFKD", s)
                if not unicodedata.combining(c))
    return s.encode("ascii", "replace").decode("ascii")

_ACCENT = (11, 37, 69)          # #0b2545 — DHL-toolkit navy
_INK = (30, 41, 59)
_MUTED = (100, 116, 139)
_HAIR = (226, 232, 240)
_WIDTH = 780
_PAD = 30
_LABEL_W = 230
_LINE_H = 26


def _font(size):
    return ImageFont.load_default(size=size)


def _wrap(draw, text, font, max_w):
    """Greedy word-wrap ``text`` to ``max_w`` pixels; never drops a word."""
    words = str(text).split()
    lines, cur = [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if not cur or draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or ["-"]


def line_card_png(title, rows, subtitle=""):
    """A clean evidence card as PNG bytes.

    ``title``    — header text (white on navy bar).
    ``subtitle`` — small line under the title (optional).
    ``rows``     — list of (label, value) pairs; long values wrap.
    """
    f_title = _font(27)
    f_sub = _font(15)
    f_label = _font(18)
    f_value = _font(20)

    title = _sanitize(title)
    subtitle = _sanitize(subtitle) if subtitle else ""
    scratch = ImageDraw.Draw(Image.new("RGB", (4, 4)))
    val_w = _WIDTH - 2 * _PAD - _LABEL_W - 14
    laid = []
    for label, value in rows:
        text = "-" if value in (None, "") else _sanitize(value)
        vlines = _wrap(scratch, text, f_value, val_w)
        laid.append((_sanitize(label), vlines))

    header_h = 92 if subtitle else 70
    row_heights = [max(_LINE_H + 12, 10 + _LINE_H * len(v)) for _l, v in laid]
    height = header_h + sum(row_heights) + _PAD

    img = Image.new("RGB", (_WIDTH, height), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, _WIDTH, header_h], fill=_ACCENT)
    d.text((_PAD, 20), title, font=f_title, fill="white")
    if subtitle:
        d.text((_PAD, 56), subtitle, font=f_sub, fill=(190, 202, 220))

    y = header_h + 8
    for (label, vlines), rh in zip(laid, row_heights):
        d.text((_PAD, y + 4), str(label), font=f_label, fill=_MUTED)
        vy = y + 2
        for vl in vlines:
            d.text((_PAD + _LABEL_W, vy), vl, font=f_value, fill=_INK)
            vy += _LINE_H
        y += rh
        d.line([(_PAD, y), (_WIDTH - _PAD, y)], fill=_HAIR, width=1)

    d.rectangle([0, 0, _WIDTH - 1, height - 1], outline=_HAIR, width=1)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
