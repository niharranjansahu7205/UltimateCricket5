"""
Match result image card (Team Cricket).

render_result_card(data) -> PNG bytes. Pure drawing with Pillow — no Telegram,
no database — so it is easy to test. All text is cleaned first: characters the
bundled font can't draw (emoji, scripts DejaVu lacks) are dropped instead of
showing as empty boxes. The full, untouched text is always in the normal
result message; the card is the shareable "poster".

data = {
  "key": "K7M3X", "overs": 3,
  "result": "Team A won by 12 run(s)!",  "winner": "a" | "b" | None,
  "teams": {"a": {"label", "score", "wickets", "overs": "2.4",
                  "bat": ("Alice", 45, 20) | None,       # name, runs, balls
                  "bowl": ("Dan", 2, 12) | None},        # name, wickets, runs
            "b": {...}},
  "potm": {"name": "Alice", "line": "45 runs (20b) • 2 wkts"} | None,
}
"""

from __future__ import annotations

import io
import os
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1440
_HERE = os.path.dirname(os.path.abspath(__file__))
_FONT_DIRS = [
    os.path.join(_HERE, "assets", "fonts"),
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/dejavu",
    "/usr/share/fonts/TTF",
    "/Library/Fonts",
]

GOLD = (245, 197, 66)
GOLD_DARK = (176, 130, 20)
WHITE = (255, 255, 255)
GREY = (166, 180, 200)
BLUE = (59, 130, 246)
RED = (239, 68, 68)
BG_TOP = (7, 66, 48)
BG_BOTTOM = (8, 18, 44)


# ---------------------------------------------------------------- fonts ----
def _font_file(bold: bool):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in _FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return None


@lru_cache(maxsize=64)
def _font(bold: bool, size: int):
    p = _font_file(bold)
    if p:
        return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)          # Pillow >= 10.1


def _glyph_bytes(bold: bool, ch: str) -> bytes:
    img = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(img).text((8, 8), ch, font=_font(bold, 32), fill=255)
    return img.tobytes()


@lru_cache(maxsize=4096)
def _has_glyph(bold: bool, ch: str) -> bool:
    """True if the font really draws `ch` (not its empty/.notdef box)."""
    if ch == " ":
        return True
    return _glyph_bytes(bold, ch) != _glyph_bytes(bold, "\U0010FFFF")


def _clean(text, bold: bool = True, fallback: str = "") -> str:
    """Drops characters the font can't render (emoji, unsupported scripts)."""
    out = "".join(ch for ch in str(text or "") if ch.isprintable() and _has_glyph(bold, ch))
    out = " ".join(out.split())
    return out or fallback


def _fit(draw, text: str, bold: bool, max_size: int, max_w: int, min_size: int = 20):
    """Largest font (<= max_size) that fits max_w; ellipsizes as a last resort."""
    size = max_size
    while size >= min_size:
        f = _font(bold, size)
        if draw.textlength(text, font=f) <= max_w:
            return f, text
        size -= 2
    f = _font(bold, min_size)
    while text and draw.textlength(text + "…", font=f) > max_w:
        text = text[:-1]
    return f, (text + "…") if text else ""


# -------------------------------------------------------------- drawing ----
def _gradient_bg() -> Image.Image:
    img = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        t = y / (H - 1)
        d.line([(0, y), (W, y)], fill=tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)) + (255,))
    # soft "stadium" rings + pitch strip
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    cx, cy = W // 2, 700
    for r, a in ((560, 26), (430, 20), (300, 16)):
        od.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, a), width=3)
    od.rounded_rectangle([cx - 70, cy - 560, cx + 70, cy + 560], radius=30, fill=(214, 190, 140, 14))
    return Image.alpha_composite(img, ov)


def _panel(base, box, fill, outline=None, width=0, radius=34, shadow=True):
    x0, y0, x1, y1 = box
    if shadow:
        sh = Image.new("RGBA", base.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).rounded_rectangle([x0, y0 + 10, x1, y1 + 10], radius=radius, fill=(0, 0, 0, 120))
        base = Image.alpha_composite(base, sh.filter(ImageFilter.GaussianBlur(14)))
    ov = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(ov).rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
    return Image.alpha_composite(base, ov)


def _spaced(draw, xy, text, font, fill, spacing, anchor_center=True):
    widths = [draw.textlength(c, font=font) for c in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = xy[0] - total / 2 if anchor_center else xy[0]
    for c, w in zip(text, widths):
        draw.text((x, xy[1]), c, font=font, fill=fill, anchor="lm")
        x += w + spacing


def _crown(draw, cx, cy, s, fill):
    """Little crown icon centred on (cx, cy); s = half-width."""
    pts = [(cx - s, cy + s * 0.55), (cx - s, cy - s * 0.35), (cx - s * 0.5, cy + s * 0.05),
           (cx, cy - s * 0.65), (cx + s * 0.5, cy + s * 0.05), (cx + s, cy - s * 0.35), (cx + s, cy + s * 0.55)]
    draw.polygon(pts, fill=fill)
    draw.rounded_rectangle([cx - s, cy + s * 0.62, cx + s, cy + s * 0.88], radius=6, fill=fill)
    for px, py in ((cx - s, cy - s * 0.35), (cx, cy - s * 0.65), (cx + s, cy - s * 0.35)):
        draw.ellipse([px - 7, py - 7, px + 7, py + 7], fill=fill)


def _ball(draw, cx, cy, r):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(200, 30, 45), outline=(255, 255, 255), width=2)
    draw.arc([cx - r * 0.6, cy - r * 1.3, cx + r * 0.9, cy + r * 1.3], 100, 260, fill=WHITE, width=2)


def _team_panel(base, x0, accent, t, is_winner, tie):
    y0, y1, pw = 340, 870, 480
    box = (x0, y0, x0 + pw, y1)
    base = _panel(base, box, (255, 255, 255, 22), outline=GOLD if is_winner else (255, 255, 255, 60),
                  width=6 if is_winner else 2)
    d = ImageDraw.Draw(base)
    d.rounded_rectangle((x0 + 2, y0 + 2, x0 + pw - 2, y0 + 20), radius=10, fill=accent)
    cx = x0 + pw // 2
    inner = pw - 70

    label = _clean(t.get("label"), True, "Team")
    f, label = _fit(d, label, True, 44, inner, 24)
    d.text((cx, y0 + 88), label, font=f, fill=WHITE, anchor="mm")

    score = f"{t.get('score', 0)}/{t.get('wickets', 0)}"
    f, score = _fit(d, score, True, 140, inner, 60)
    d.text((cx, y0 + 215), score, font=f, fill=GOLD if is_winner else WHITE, anchor="mm")
    d.text((cx, y0 + 300), f"{t.get('overs', '0.0')} overs", font=_font(False, 32), fill=GREY, anchor="mm")

    d.line([(x0 + 40, y0 + 340), (x0 + pw - 40, y0 + 340)], fill=(255, 255, 255, 50), width=2)
    rows = [("TOP BAT", t.get("bat"), lambda b: (b[0], f"{b[1]} ({b[2]})")),
            ("TOP BOWL", t.get("bowl"), lambda b: (b[0], f"{b[1]}/{b[2]}"))]
    y = y0 + 372
    for title, val, fmt in rows:
        d.text((x0 + 40, y), title, font=_font(True, 22), fill=GOLD, anchor="lm")
        if val:
            nm, stat = fmt(val)
            nm = _clean(nm, True, "Player")
            sf = _font(True, 34)
            sw = d.textlength(stat, font=sf)
            d.text((x0 + pw - 40, y + 40), stat, font=sf, fill=WHITE, anchor="rm")
            f, nm = _fit(d, nm, False, 32, int(inner - sw - 20), 20)
            d.text((x0 + 40, y + 40), nm, font=f, fill=WHITE, anchor="lm")
        else:
            d.text((x0 + 40, y + 40), "—", font=_font(False, 32), fill=GREY, anchor="lm")
        y += 80

    if is_winner:
        pill = (cx - 100, y0 - 26, cx + 100, y0 + 24)
        base = _panel(base, pill, GOLD, radius=25, shadow=False)
        d = ImageDraw.Draw(base)
        d.text((cx, y0 - 1), "WINNER", font=_font(True, 28), fill=(60, 40, 0), anchor="mm")
    return base


def render_result_card(data: dict, bot_username: str | None = None) -> bytes:
    base = _gradient_bg()
    d = ImageDraw.Draw(base)

    # header
    _spaced(d, (W // 2, 78), "ULTIMATECRIC", _font(True, 40), GOLD, 10)
    d.text((W // 2, 140), "TEAM CRICKET  •  MATCH RESULT", font=_font(True, 30), fill=WHITE, anchor="mm")
    d.line([(W // 2 - 120, 184), (W // 2 + 120, 184)], fill=GOLD, width=4)
    _ball(d, W // 2 - 330, 140, 14)
    _ball(d, W // 2 + 330, 140, 14)

    # result banner
    result = _clean(data.get("result"), True, "Match completed")
    banner = (60, 215, W - 60, 305)
    base = _panel(base, banner, GOLD, radius=45)
    d = ImageDraw.Draw(base)
    f, result = _fit(d, result, True, 46, banner[2] - banner[0] - 80, 24)
    d.text((W // 2, 260), result, font=f, fill=(55, 36, 0), anchor="mm")

    # teams
    winner = data.get("winner")
    teams = data.get("teams", {})
    base = _team_panel(base, 50, BLUE, teams.get("a", {}), winner == "a", winner is None)
    base = _team_panel(base, 550, RED, teams.get("b", {}), winner == "b", winner is None)
    d = ImageDraw.Draw(base)
    d.ellipse([W // 2 - 32, 605 - 32, W // 2 + 32, 605 + 32], fill=(10, 24, 50), outline=GOLD, width=3)
    d.text((W // 2, 605), "VS", font=_font(True, 26), fill=GOLD, anchor="mm")

    # player of the match
    potm = data.get("potm")
    py0, py1 = 925, 1125
    if potm:
        base = _panel(base, (50, py0, W - 50, py1), (245, 197, 66, 34), outline=GOLD, width=3, radius=40)
        d = ImageDraw.Draw(base)
        _crown(d, 150, py0 + 100, 52, GOLD)
        d.text((250, py0 + 45), "PLAYER OF THE MATCH", font=_font(True, 26), fill=GOLD, anchor="lm")
        name = _clean(potm.get("name"), True, "Player")
        f, name = _fit(d, name, True, 62, W - 50 - 250 - 40, 28)
        d.text((250, py0 + 108), name, font=f, fill=WHITE, anchor="lm")
        line = _clean(potm.get("line"), False)
        f, line = _fit(d, line, False, 30, W - 50 - 250 - 40, 18)
        d.text((250, py0 + 160), line, font=f, fill=GREY, anchor="lm")
    else:
        py1 = 925

    # footer: match key
    key = _clean(data.get("key"), True)
    ky0 = py1 + 50
    if key:
        base = _panel(base, (150, ky0, W - 150, ky0 + 195), (255, 255, 255, 26), outline=(255, 255, 255, 70), width=2, radius=36)
        d = ImageDraw.Draw(base)
        d.text((W // 2, ky0 + 36), "MATCH KEY", font=_font(True, 22), fill=GOLD, anchor="mm")
        _spaced(d, (W // 2, ky0 + 96), key, _font(True, 62), WHITE, 18)
        d.text((W // 2, ky0 + 156), f"Full ball-by-ball:  /history {key}", font=_font(False, 28), fill=GREY, anchor="mm")
    foot = f"{int(data.get('overs', 0))} over match"
    if bot_username:
        foot += f"  •  @{_clean(bot_username, False)}"
    d.text((W // 2, H - 44), foot, font=_font(False, 24), fill=(120, 140, 170), anchor="mm")

    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()
