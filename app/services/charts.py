"""Tiny dependency-free SVG line charts for daily-series panels.

Used by the Electronic Cheque Register (cheques on file vs unpresented) and
the BIT & Cash AR section (open items per day). Returns an inline <svg> string
the templates embed with |safe — no JS, no external chart library, and it
inherits the page's colour scheme (axis text uses currentColor).
"""
from xml.sax.saxutils import escape as _esc

_W, _H = 760, 210
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 44, 14, 26, 30


def _fmt_val(v):
    """A point label: thousands-separated, 1 decimal only when needed."""
    if v is None:
        return ""
    if float(v) == int(v):
        return f"{int(v):,}"
    return f"{float(v):,.1f}"


def line_svg(labels, series, width=_W, height=_H):
    """``labels`` = x-axis labels (dates, oldest first); ``series`` = list of
    {name, color, values} with len(values) == len(labels). Every plotted
    point carries its value label (thinned to ~14 per series on long
    ranges, the latest point always labelled) so the graphs read without
    guessing. Returns SVG markup (empty string when there is nothing to
    plot)."""
    labels = list(labels or [])
    series = [s for s in (series or []) if any(v is not None for v in s["values"])]
    if not labels or not series:
        return ""
    n = len(labels)
    y_max = max((v or 0) for s in series for v in s["values"]) or 1
    y_max = max(y_max, 1)
    plot_w = width - _PAD_L - _PAD_R
    plot_h = height - _PAD_T - _PAD_B

    def x(i):
        return _PAD_L + (plot_w * (i / (n - 1)) if n > 1 else plot_w / 2)

    def y(v):
        return _PAD_T + plot_h - (plot_h * ((v or 0) / y_max))

    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" '
             f'style="max-width:{width}px;display:block;" role="img" '
             'xmlns="http://www.w3.org/2000/svg">']
    # horizontal gridlines + y labels (0, half, max)
    for gv in {0, y_max // 2, y_max}:
        gy = y(gv)
        parts.append(f'<line x1="{_PAD_L}" y1="{gy:.1f}" x2="{width - _PAD_R}" '
                     f'y2="{gy:.1f}" stroke="currentColor" stroke-opacity=".12"/>')
        parts.append(f'<text x="{_PAD_L - 6}" y="{gy + 4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="currentColor" fill-opacity=".55">'
                     f'{int(gv)}</text>')
    # x labels: first, middle, last (avoid clutter)
    idxs = sorted({0, n // 2, n - 1})
    for i in idxs:
        parts.append(f'<text x="{x(i):.1f}" y="{height - 8}" text-anchor="middle" '
                     f'font-size="11" fill="currentColor" fill-opacity=".55">'
                     f'{_esc(str(labels[i]))}</text>')
    # series lines + points + per-point value labels + legend
    label_step = max(1, -(-n // 14))            # ceil: at most ~14 labels
    lx = _PAD_L

    def _val_label(si, i, v):
        if v is None:
            return ""
        if i % label_step and i != n - 1:       # thin long ranges; keep last
            return ""
        tx = min(max(x(i), _PAD_L + 10), width - _PAD_R - 10)
        above = si % 2 == 0                     # alternate per series so two
        ty = y(v) + (-8 if above else 15)       # close lines don't collide
        ty = min(max(ty, _PAD_T + 9), height - _PAD_B - 2)
        return (f'<text x="{tx:.1f}" y="{ty:.1f}" text-anchor="middle" '
                f'font-size="10" font-weight="600" fill="{s["color"]}" '
                f'fill-opacity=".95">{_fmt_val(v)}</text>')

    for si, s in enumerate(series):
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(s["values"]))
        if n == 1:
            i, v = 0, s["values"][0]
            parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="4" '
                         f'fill="{s["color"]}"/>')
            parts.append(_val_label(si, 0, v))
        else:
            parts.append(f'<polyline points="{pts}" fill="none" '
                         f'stroke="{s["color"]}" stroke-width="2.5" '
                         'stroke-linejoin="round" stroke-linecap="round"/>')
            for i, v in enumerate(s["values"]):
                parts.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3" '
                             f'fill="{s["color"]}"/>')
                parts.append(_val_label(si, i, v))
        last = s["values"][-1]
        parts.append(f'<text x="{lx}" y="{_PAD_T - 10}" font-size="12" '
                     f'fill="{s["color"]}" font-weight="600">● '
                     f'{_esc(s["name"])} ({_fmt_val(last or 0)})</text>')
        lx += 9 * (len(s["name"]) + 8)
    parts.append("</svg>")
    return "".join(parts)
