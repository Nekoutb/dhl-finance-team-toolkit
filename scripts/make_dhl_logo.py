"""Draw the DHL brand mark (yellow field, red italic DHL with speed bars) as
app/static/dhl_logo.png — embedded in the Quick Account Statement PDF/Excel.

Run once on Windows (uses the Arial Bold Italic system font):
    python scripts/make_dhl_logo.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "app" / "static" / "dhl_logo.png"

YELLOW = (255, 204, 0)
RED = (212, 5, 17)

W, H = 640, 160
img = Image.new("RGB", (W, H), YELLOW)
d = ImageDraw.Draw(img)

font = ImageFont.truetype(r"C:\Windows\Fonts\arialbi.ttf", 104)
text = "DHL"
bbox = d.textbbox((0, 0), text, font=font)
tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
tx, ty = (W - tw) // 2 - bbox[0], (H - th) // 2 - bbox[1]
d.text((tx, ty), text, font=font, fill=RED)

# Speed bars either side (three slanted stripes, like the wordmark).
bar_h, gap, slant = 12, 12, 18
mid = H // 2
for i, off in enumerate((-bar_h - gap, 0, bar_h + gap)):
    y0 = mid - bar_h // 2 + off
    # left bars
    d.polygon([(24 + slant, y0), (tx - 36 + slant, y0),
               (tx - 36, y0 + bar_h), (24, y0 + bar_h)], fill=RED)
    # right bars
    d.polygon([(tx + tw + 36 + slant, y0), (W - 24 + slant, y0),
               (W - 24, y0 + bar_h), (tx + tw + 36, y0 + bar_h)], fill=RED)

OUT.parent.mkdir(parents=True, exist_ok=True)
img.save(OUT)
print(f"Wrote {OUT} ({img.size[0]}x{img.size[1]})")
