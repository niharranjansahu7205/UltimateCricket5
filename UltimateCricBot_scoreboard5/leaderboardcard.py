"""
/globaltop — premium Global Leaderboard card (PNG): Top 10 players across ALL
groups, Solo + Team Cricket combined, ranked by wins then runs (same rule as the
per-group /leaderboard). Everyone in the Top 10 gets their own Telegram profile
picture (big for the podium, small in the list); players whose photo is hidden or
missing get a clean initial-letter avatar. Banned users are left out.

The finished card is cached for CACHE_SECONDS so spamming the command doesn't hit
Telegram's API ten times per request. The existing /leaderboard is untouched.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import time

from PIL import Image, ImageDraw, ImageFilter
from telegram import Update
from telegram.ext import ContextTypes

from database import db_cursor
from playercard import _circle_avatar, _fetch_avatar
from resultcard import (
    GOLD, GREY, WHITE, W, H,
    _ball, _clean, _crown, _fit, _font, _gradient_bg, _panel, _spaced,
)

logger = logging.getLogger(__name__)

TOP_N = 10
CACHE_SECONDS = 120
AVATAR_TIMEOUT = 8
SILVER, BRONZE = (203, 213, 225), (205, 127, 50)
_cache: dict = {"t": 0.0, "png": None}


# ------------------------------------------------------------------ data ----
def get_global_top(limit: int = TOP_N) -> list[dict]:
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT user_id, SUM(w) AS wins, SUM(runs) AS runs, SUM(m) AS matches, MAX(nm) AS nm FROM (
                SELECT user_id, matches_won AS w, total_runs AS runs, matches_played AS m, username AS nm FROM player_stats
                UNION ALL
                SELECT user_id, wins, runs, matches, name FROM team_player_stats
            )
            WHERE user_id NOT IN (SELECT user_id FROM banned_users)
            GROUP BY user_id HAVING SUM(m) > 0
            ORDER BY wins DESC, runs DESC, user_id ASC LIMIT ?
            """,
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        for r in rows:
            cur.execute("SELECT first_name, username FROM known_users WHERE user_id = ?", (r["user_id"],))
            k = cur.fetchone()
            r["name"] = (k["first_name"] or k["username"]) if k and (k["first_name"] or k["username"]) else (r.pop("nm") or "Player")
            r.pop("nm", None)
    return rows


# --------------------------------------------------------------- drawing ----
def _ringed_avatar(base, img, cx, cy, s, ring, glow=False):
    if glow:
        g = Image.new("RGBA", base.size, (0, 0, 0, 0))
        r = s // 2 + 24
        ImageDraw.Draw(g).ellipse([cx - r, cy - r, cx + r, cy + r], fill=ring + (130,))
        base = Image.alpha_composite(base, g.filter(ImageFilter.GaussianBlur(24)))
    d = ImageDraw.Draw(base)
    r = s // 2 + 9
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ring)
    d.ellipse([cx - r + 6, cy - r + 6, cx + r - 6, cy + r - 6], fill=(8, 18, 44))
    base.alpha_composite(img, (cx - s // 2, cy - s // 2))
    return base


def render_leaderboard_card(rows: list[dict], avatars: dict | None = None) -> bytes:
    """rows: [{user_id, name, wins, runs, matches}] best first (max 10).
    avatars: {user_id: jpeg/png bytes | None}."""
    avatars = avatars or {}
    base = _gradient_bg()
    d = ImageDraw.Draw(base)
    _spaced(d, (W // 2, 70), "ULTIMATECRIC", _font(True, 38), GOLD, 10)
    d.text((W // 2, 128), "GLOBAL LEADERBOARD", font=_font(True, 40), fill=WHITE, anchor="mm")
    d.line([(W // 2 - 140, 170), (W // 2 + 140, 170)], fill=GOLD, width=4)
    _ball(d, W // 2 - 400, 128, 14)
    _ball(d, W // 2 + 400, 128, 14)
    base = _panel(base, (40, 200, W - 40, 1340), (255, 255, 255, 18), outline=GOLD, width=5, radius=48)

    def pic(i, size):
        r = rows[i]
        return _circle_avatar(avatars.get(r["user_id"]), _clean(r["name"], True, "?"), size)

    # podium: 2nd, 1st, 3rd (only the spots that exist)
    spots = [(1, 225, 470, 190, SILVER), (0, 540, 420, 240, GOLD), (2, 855, 470, 190, BRONZE)]
    for idx, cx, cy, s, col in spots:
        if idx >= len(rows):
            continue
        r = rows[idx]
        base = _ringed_avatar(base, pic(idx, s), cx, cy, s, col, glow=(idx == 0))
        d = ImageDraw.Draw(base)
        if idx == 0:
            _crown(d, cx, cy - s // 2 - 44, 30, GOLD)
        d.ellipse([cx - 24, cy + s // 2 - 18, cx + 24, cy + s // 2 + 30], fill=col)
        d.text((cx, cy + s // 2 + 5), str(idx + 1), font=_font(True, 30), fill=(40, 28, 0), anchor="mm")
        f, nm = _fit(d, _clean(r["name"], True, "Player"), True, 44 if idx == 0 else 36, 270, 22)
        d.text((cx, cy + s // 2 + 56), nm, font=f, fill=WHITE, anchor="mm")
        d.text((cx, cy + s // 2 + 100), f"{r['wins']} WINS", font=_font(True, 28), fill=col, anchor="mm")
        d.text((cx, cy + s // 2 + 134), f"{r['runs']} runs", font=_font(False, 24), fill=GREY, anchor="mm")

    # ranks 4..10 as a list
    if len(rows) > 3:
        y0 = 835
        for x, t in ((690, "WINS"), (800, "RUNS"), (935, "WIN %")):
            d.text((x, y0), t, font=_font(True, 20), fill=GOLD, anchor="mm")
        y = y0 + 22
        for i in range(3, len(rows)):
            r = rows[i]
            base = _panel(base, (70, y, W - 70, y + 58), (255, 255, 255, 22),
                          outline=(255, 255, 255, 60), width=2, radius=29, shadow=False)
            base = _ringed_avatar(base, pic(i, 44), 160, y + 29, 44, (90, 105, 130))
            d = ImageDraw.Draw(base)
            d.text((105, y + 29), str(i + 1), font=_font(True, 28), fill=GOLD, anchor="mm")
            f, nm = _fit(d, _clean(r["name"], True, "Player"), True, 30, 360, 20)
            d.text((215, y + 29), nm, font=f, fill=WHITE, anchor="lm")
            wr = (r["wins"] / r["matches"] * 100) if r["matches"] else 0
            d.text((690, y + 29), str(r["wins"]), font=_font(True, 28), fill=WHITE, anchor="mm")
            d.text((800, y + 29), str(r["runs"]), font=_font(False, 26), fill=GREY, anchor="mm")
            d.text((935, y + 29), f"{wr:.0f}%", font=_font(False, 26), fill=GREY, anchor="mm")
            y += 68

    foot = "Top 10  •  all groups  •  ranked by wins, then runs"
    d.text((W // 2, H - 50), foot, font=_font(False, 24), fill=(120, 140, 170), anchor="mm")
    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


# --------------------------------------------------------------- handler ----
async def _avatar_or_none(bot, user_id):
    try:
        return await asyncio.wait_for(_fetch_avatar(bot, user_id), timeout=AVATAR_TIMEOUT)
    except Exception:
        return None


async def globaltop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    caption = "🏆 <b>UltimateCric Global Leaderboard</b>\nTop players across all groups"
    if _cache["png"] and time.monotonic() - _cache["t"] < CACHE_SECONDS:
        await update.message.reply_photo(photo=_cache["png"], caption=caption, parse_mode="HTML")
        return
    try:
        rows = get_global_top()
        if not rows:
            await update.message.reply_text("No ranked players yet — play a match with /cricket first! 🏏")
            return
        pics = await asyncio.gather(*[_avatar_or_none(context.bot, r["user_id"]) for r in rows])
        avatars = {r["user_id"]: p for r, p in zip(rows, pics)}
        png = await asyncio.to_thread(render_leaderboard_card, rows, avatars)
    except Exception:
        logger.exception("Could not build /globaltop")
        await update.message.reply_text("⚠️ Couldn't create the leaderboard right now. Please try again later.")
        return
    _cache.update(t=time.monotonic(), png=png)
    await update.message.reply_photo(photo=png, caption=caption, parse_mode="HTML")
