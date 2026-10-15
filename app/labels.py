"""Label artwork, described once in millimetres and drawn two ways:

- `render()` builds the SVG used for the on-screen preview and browser printing.
  Text that might be too long carries data-maxw; static/app.js squeezes it.
- `raster()` draws the same layout as a 1-bit bitmap at the printer's 360 dpi
  for direct printing (see printer.py), squeezing long text the same way.
"""
from __future__ import annotations

import os
from html import escape

import segno

import copy

INK = "#111111"

# Tape colours an admin can choose, with the colour used to draw them on screen.
# "any" means the app doesn't check the cassette colour before printing.
TAPE_FILLS = {
    "yellow": "#f6f06b",
    "white": "#fbfbf8",
    "red": "#e2483b",
    "blue": "#6aa6e8",
    "green": "#7cc47f",
    "clear": "#eef1f4",
    "any": "#fbfbf8",
}

FORMATS = {
    "standard": {
        "name": "Standard",
        "width": 48, "height": 18, "tape_mm": 18, "tape_colour": "yellow",
        "tape": "TZe-641 · 18 mm black on yellow",
        "use": "Servers, NAS, UPS, laptops, desktops",
    },
    "slim": {
        "name": "Slim",
        "width": 38, "height": 12, "tape_mm": 12, "tape_colour": "yellow",
        "tape": "TZe-631 · 12 mm black on yellow",
        "use": "Rack kit front panels, access points",
    },
    "micro": {
        "name": "Micro",
        "width": 22, "height": 6, "tape_mm": 6, "tape_colour": "yellow",
        "tape": "TZe-611 · 6 mm black on yellow",
        "use": "SFPs, PSUs, dongles, spare drives",
    },
    "loan": {
        "name": "Loan",
        "width": 48, "height": 18, "tape_mm": 18, "tape_colour": "red",
        "tape": "TZe-441 · 18 mm black on red",
        "use": "Our loan pool and swap-out spares",
    },
}
for _spec in FORMATS.values():
    _spec["enabled"] = True
BUILT_IN = copy.deepcopy(FORMATS)
EDITABLE = ("enabled", "tape_colour", "tape")


def apply_overrides(overrides: dict) -> None:
    """Reset FORMATS to the built-in set, then apply an admin's saved changes.

    Only `enabled`, `tape_colour` and `tape` (the description) can change:
    sizes are fixed because each layout is drawn for its tape width.
    """
    for key, base in BUILT_IN.items():
        FORMATS[key] = copy.deepcopy(base)
        for field, value in (overrides or {}).get(key, {}).items():
            if field == "tape_colour" and value not in TAPE_FILLS:
                continue
            if field in EDITABLE:
                FORMATS[key][field] = value


def enabled() -> dict:
    return {k: v for k, v in FORMATS.items() if v.get("enabled", True)}


def fill(fmt: str) -> str:
    return TAPE_FILLS.get(FORMATS[fmt]["tape_colour"], TAPE_FILLS["white"])

# SVG font stacks and the matching TTFs used for the bitmap.
FONTS = {
    "display": ("Archivo, 'Helvetica Neue', Arial, sans-serif", 800, "Archivo-Variable.ttf"),
    "mono": ("'JetBrains Mono', Menlo, Consolas, 'DejaVu Sans Mono', monospace", 800, "JetBrainsMono-ExtraBold.ttf"),
    "body": ("'IBM Plex Sans', 'Helvetica Neue', Arial, sans-serif", 500, "IBMPlexSans-Medium.ttf"),
}
FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")


def qr_data(base_url: str, code: str) -> str:
    # Upper case keeps the QR in alphanumeric mode: fewer, larger modules.
    return f"{base_url.rstrip('/')}/{code}".upper()


def default_text() -> dict:
    """Label wording when the caller doesn't pass the organisation's own."""
    from . import settings

    return settings.label_text(dict(settings.DEFAULTS))


def layout(fmt: str, code: str, client_name: str, text: dict = None) -> list:
    """The label as a list of drawing elements, in mm. `fill` is "ink" or "tape".

    `text` is the organisation's wording (settings.label_text): the "managed
    by" line, the loan tag lines and the short mark on micro tags.
    """
    text = text or default_text()
    if fmt in ("standard", "loan"):
        top = text["loan_top"] if fmt == "loan" else client_name
        bottom = text["loan_bottom"] if fmt == "loan" else text["managed"]
        return [
            {"kind": "qr", "x": 2, "y": 2.5, "size": 13},
            {"kind": "text", "x": 17.4, "y": 5.9, "size": 2.7, "font": "display", "body": top, "maxw": 29, "spacing": 0.1},
            {"kind": "text", "x": 17.2, "y": 12.4, "size": 7, "font": "mono", "body": code, "spacing": 0.35},
            {"kind": "text", "x": 17.4, "y": 15.5, "size": 1.72, "font": "body", "body": bottom, "maxw": 29},
        ]
    if fmt == "slim":
        return [
            {"kind": "qr", "x": 1.4, "y": 1.4, "size": 9.2},
            {"kind": "text", "x": 12, "y": 4.3, "size": 2.2, "font": "display", "body": client_name, "maxw": 24.5, "spacing": 0.08},
            {"kind": "text", "x": 11.8, "y": 9.9, "size": 5.3, "font": "mono", "body": code, "spacing": 0.3},
        ]
    if fmt == "micro":
        return [
            {"kind": "rect", "x": 0.9, "y": 0.9, "w": 4.2, "h": 4.2, "rx": 0.5},
            {"kind": "text", "x": 3, "y": 3.85, "size": 2, "font": "display", "body": text["mark"], "anchor": "middle",
             "fill": "tape", "maxw": 3.6},
            {"kind": "text", "x": 6.3, "y": 4.4, "size": 3.9, "font": "mono", "body": code, "spacing": 0.25},
        ]
    raise ValueError(f"unknown format {fmt}")


# ---------- SVG ----------

def _qr_path(data: str, x: float, y: float, size: float) -> str:
    rows = segno.make_qr(data, error="m", boost_error=False).matrix
    c = size / len(rows)
    parts = [f"M{x + k * c:.3f} {y + r * c:.3f}h{c:.3f}v{c:.3f}h{-c:.3f}z"
             for r, row in enumerate(rows) for k, dark in enumerate(row) if dark]
    return f'<path d="{"".join(parts)}" fill="{INK}" shape-rendering="crispEdges"/>'


def render(fmt: str, code: str, client_name: str, base_url: str, css_class: str = "label", text: dict = None) -> str:
    spec = FORMATS[fmt]
    w, h = spec["width"], spec["height"]
    tape = fill(fmt)
    out = [f'<rect width="{w}" height="{h}" fill="{tape}"/>']
    for el in layout(fmt, code, client_name, text):
        colour = tape if el.get("fill") == "tape" else INK
        if el["kind"] == "qr":
            out.append(_qr_path(qr_data(base_url, code), el["x"], el["y"], el["size"]))
        elif el["kind"] == "rect":
            out.append(f'<rect x="{el["x"]}" y="{el["y"]}" width="{el["w"]}" height="{el["h"]}" '
                       f'rx="{el.get("rx", 0)}" fill="{colour}"/>')
        else:
            family, weight, _ = FONTS[el["font"]]
            attrs = [f'x="{el["x"]}"', f'y="{el["y"]}"', f'font-family="{family}"', f'font-weight="{weight}"',
                     f'font-size="{el["size"]}"', f'fill="{colour}"']
            if el.get("spacing"):
                attrs.append(f'letter-spacing="{el["spacing"]}"')
            if el.get("maxw"):
                attrs.append(f'data-maxw="{el["maxw"]}"')
            if el.get("anchor"):
                attrs.append(f'text-anchor="{el["anchor"]}"')
            out.append(f'<text {" ".join(attrs)}>{escape(el["body"])}</text>')
    return (
        f'<svg class="{css_class} fmt-{fmt}" xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {w} {h}" width="{w}mm" height="{h}mm" role="img" '
        f'aria-label="{escape(spec["name"])} tag {code}">{"".join(out)}</svg>'
    )


# ---------- 360 dpi bitmap ----------

DPI = 360
DOTS_PER_MM = DPI / 25.4
_font_cache: dict = {}


def _font(name: str, px: int):
    from PIL import ImageFont

    key = (name, px)
    if key not in _font_cache:
        _, weight, filename = FONTS[name]
        f = ImageFont.truetype(os.path.join(FONT_DIR, filename), px)
        try:
            # Archivo ships as a variable font: pick its weight (and normal width).
            axes = f.get_variation_axes()
            f.set_variation_by_axes([weight if a.get("name", b"") in (b"Weight", "Weight") else a["default"]
                                     for a in axes])
        except OSError:
            pass
        _font_cache[key] = f
    return _font_cache[key]


def _text_image(el: dict):
    """The text as an 8-bit image (255 = ink), already squeezed to maxw."""
    from PIL import Image, ImageDraw

    font = _font(el["font"], max(1, round(el["size"] * DOTS_PER_MM)))
    spacing = el.get("spacing", 0) * DOTS_PER_MM
    ascent, descent = font.getmetrics()
    chars = list(el["body"])
    widths = [font.getlength(ch) for ch in chars]
    natural = sum(widths) + spacing * max(len(chars) - 1, 0)
    img = Image.new("L", (max(1, int(natural + 4)), ascent + descent), 0)
    d = ImageDraw.Draw(img)
    if spacing:
        x = 0.0
        for ch, cw in zip(chars, widths):
            d.text((x, ascent), ch, font=font, fill=255, anchor="ls")
            x += cw + spacing
    else:
        d.text((0, ascent), el["body"], font=font, fill=255, anchor="ls")
    maxw = el.get("maxw")
    if maxw and natural > maxw * DOTS_PER_MM:
        img = img.resize((max(1, round(maxw * DOTS_PER_MM)), img.height), Image.LANCZOS)
        natural = maxw * DOTS_PER_MM
    return img, ascent, natural


def raster(fmt: str, code: str, client_name: str, base_url: str, text: dict = None):
    """1-bit image of the whole label at 360 dpi (black = ink), full tape height."""
    from PIL import Image, ImageDraw

    spec = FORMATS[fmt]
    W = round(spec["width"] * DOTS_PER_MM)
    H = round(spec["height"] * DOTS_PER_MM)
    canvas = Image.new("L", (W, H), 0)          # 0 = no ink, 255 = ink
    d = ImageDraw.Draw(canvas)
    mm = lambda v: round(v * DOTS_PER_MM)
    for el in layout(fmt, code, client_name, text):
        ink = 0 if el.get("fill") == "tape" else 255
        if el["kind"] == "qr":
            rows = segno.make_qr(qr_data(base_url, code), error="m", boost_error=False).matrix
            n = len(rows)
            box = mm(el["size"])
            cell = max(1, box // n)                 # whole dots per module keeps it crisp
            ox = mm(el["x"]) + (box - cell * n) // 2
            oy = mm(el["y"]) + (box - cell * n) // 2
            for r, row in enumerate(rows):
                for k, dark in enumerate(row):
                    if dark:
                        d.rectangle([ox + k * cell, oy + r * cell, ox + (k + 1) * cell - 1, oy + (r + 1) * cell - 1], fill=255)
        elif el["kind"] == "rect":
            d.rounded_rectangle([mm(el["x"]), mm(el["y"]), mm(el["x"] + el["w"]) - 1, mm(el["y"] + el["h"]) - 1],
                                radius=mm(el.get("rx", 0)), fill=ink)
        else:
            img, ascent, width = _text_image(el)
            x = el["x"] * DOTS_PER_MM - (width / 2 if el.get("anchor") == "middle" else 0)
            pos = (round(x), round(el["y"] * DOTS_PER_MM) - ascent)
            if ink:
                canvas.paste(255, pos, img)
            else:
                canvas.paste(0, pos, img)
    return canvas.point(lambda v: 0 if v >= 128 else 255, mode="1")   # black = ink
