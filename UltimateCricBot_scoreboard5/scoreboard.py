"""
Scoreboard popup — the "📊 Scoreboard" button under the number pad.

Tapping it shows the live score of a running Team Cricket match as a popup
alert (read-only; nothing is sent to the chat and no state is touched).
Everything is derived from SQLite (team_match_events + team_live_state), so it
also works after a restart.

Also hosts derive_innings() / ball_label(), shared with /history.
callback_data = "sb:<match_id>:p"
"""

from __future__ import annotations

import asyncio
import html
import logging
import re

from telegram import Update
from telegram.ext import ContextTypes

import database as db
from database import is_banned

logger = logging.getLogger(__name__)

SEP = "━━━━━━━━━━━━━━━━━━"
CB_RE = re.compile(r"sb:(\d+):p")
BALL_EMOJI = {"0": "0️⃣", "1": "1️⃣", "2": "2️⃣", "3": "3️⃣", "4": "4️⃣", "5": "5️⃣", "6": "6️⃣", "W": "❌"}


def _limits() -> tuple[int, int]:
    import teamcricket as tc   # lazy: teamcricket never imports this module at import time
    return tc.TEAM_OVERS, tc.BALLS_PER_OVER


def _esc(x) -> str:
    return html.escape(str(x) if x is not None else "", quote=False)


def _is_running(match_id: int) -> bool:
    import teamcricket as tc
    return any(
        g.db_match_id == match_id and g.stage not in ("done", "cancelled")
        for g in tc.TEAM_MATCHES.values()
    )


def _team_label(m: dict, side) -> str:
    return (m.get("team_a_label") if side == "a" else m.get("team_b_label")) or ("Team A" if side == "a" else "Team B")


# ---------------------------------------------------------------------------
# Pure derivation (events -> numbers). No I/O.
# ---------------------------------------------------------------------------

def derive_innings(events: list[dict], innings: int, balls_per_over: int) -> dict:
    """Aggregates one innings from its ball events.

    This engine has no wides/no-balls/byes (every delivery is legal and all
    runs come off the bat), so `extras` is always 0 — but team total is still
    computed from the events themselves, not by summing batter rows.
    """
    evs = [e for e in events if e["innings"] == innings]
    bats: dict[int, dict] = {}
    bowls: dict[int, dict] = {}
    order: list[int] = []
    total = wickets = legal = extras = 0
    for e in evs:
        o = str(e["outcome"])
        sid, bid = e["striker_id"], e["bowler_id"]
        b = bats.setdefault(sid, {"runs": 0, "balls": 0, "fours": 0, "sixes": 0, "out": False, "dismissal": None})
        w = bowls.setdefault(bid, {"balls": 0, "runs": 0, "wickets": 0})
        if sid not in order:
            order.append(sid)
        legal += 1
        b["balls"] += 1
        w["balls"] += 1
        if o == "W":
            wickets += 1
            b["out"] = True
            b["dismissal"] = e.get("dismissal")
            w["wickets"] += 1
        elif o.isdigit():
            r = int(o)
            total += r
            b["runs"] += r
            w["runs"] += r
            if r == 4:
                b["fours"] += 1
            elif r == 6:
                b["sixes"] += 1
    return {
        "events": evs, "bats": bats, "bowls": bowls, "bat_order": order,
        "total": total + extras, "wickets": wickets, "legal": legal, "extras": extras,
    }


def _sr(b: dict) -> str:
    return f"{(b['runs'] / b['balls']) * 100:.1f}" if b["balls"] else "0.0"


def _overs(legal: int, bpo: int) -> str:
    return f"{legal // bpo}.{legal % bpo}"


def _rate(runs: int, legal: int, bpo: int) -> str:
    return f"{runs / (legal / bpo):.2f}" if legal else "0.00"


def ball_label(e: dict) -> str:
    return BALL_EMOJI.get(str(e["outcome"]), str(e["outcome"]))


# ---------------------------------------------------------------------------
# Popup (callback-answer alert) — Telegram allows max 200 chars, plain text
# ---------------------------------------------------------------------------

POPUP_MAX = 200


def _u16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _short(name, n: int = 9) -> str:
    name = str(name or "?")
    return name if len(name) <= n else name[: n - 1] + "…"


def build_popup(snap: dict, running: bool) -> str:
    """Ultra-compact scoreboard for the 'Scoreboard' button under the number
    pad. Plain text, <= 200 UTF-16 units (Telegram's alert limit)."""
    overs_default, bpo = _limits()
    m, live = snap["match"], snap["live"]
    names = {p["user_id"]: p["name"] for p in snap["players"]}
    if live is None:
        return "🏏 Match is being set up — innings not started yet."

    overs_limit = live.get("overs") or overs_default
    inn = live["innings"]
    d = derive_innings(snap["events"], inn, bpo)
    team_size = sum(1 for p in snap["players"] if p["side"] == live["batting_side"] and not p.get("subbed_out"))
    target = live.get("target") if inn == 2 else None
    done = (
        m.get("status") == "completed"
        or (team_size > 0 and d["wickets"] >= team_size - 1)
        or d["legal"] >= overs_limit * bpo
        or (target is not None and d["total"] >= target)
    )

    lines = [f"🏏 {_short(_team_label(m, live['batting_side']), 10)} {d['total']}/{d['wickets']} "
             f"({_overs(d['legal'], bpo)}/{overs_limit})"]
    rr = f"RR {_rate(d['total'], d['legal'], bpo)}"
    if target is not None and not done:
        rr += f" | 🎯{target} need {max(target - d['total'], 0)}"
    lines.append(rr)

    if m.get("status") == "completed":
        lines.append("🏁 Match completed")
    elif m.get("status") == "cancelled":
        lines.append("❌ Match cancelled")
    elif not running:
        lines.append("⏸ Not running")
    elif done:
        lines.append("✅ Innings completed")

    if d["events"]:
        over_no = d["events"][-1]["over_no"] if done else live.get("overs_completed", 0) + 1
        cur = [str(e["outcome"]).replace("W", "❌") for e in d["events"] if e["over_no"] == over_no]
        if cur:
            lines.append(f"Over {over_no}: " + " │ ".join(cur))
        else:  # new over not started yet -> show the one just finished
            prev = [str(e["outcome"]).replace("W", "❌") for e in d["events"] if e["over_no"] == over_no - 1]
            lines.append(f"Last over: " + " │ ".join(prev) if prev else f"Over {over_no}: –")

    if not done and m.get("status") == "active":
        bits = []
        for uid, mark, star in ((live.get("striker_id"), "🟢", "*"), (live.get("non_striker_id"), "⚪", "")):
            b = d["bats"].get(uid) if uid is not None else None
            if uid is None or (b and b["out"]):
                continue
            b = b or {"runs": 0, "balls": 0}
            bits.append(f"{mark}{_short(names.get(uid))}{star} {b['runs']}({b['balls']})")
        if bits:
            lines.append("  ".join(bits))
        bid = live.get("bowler_id")
        if bid is not None:
            w = d["bowls"].get(bid, {"balls": 0, "runs": 0, "wickets": 0})
            lines.append(f"🎯{_short(names.get(bid))} {_overs(w['balls'], bpo)}-{w['runs']}-{w['wickets']}")

    # Drop least important lines first until it fits.
    text = "\n".join(lines)
    while _u16(text) > POPUP_MAX and len(lines) > 2:
        lines.pop()
        text = "\n".join(lines)
    while _u16(text) > POPUP_MAX:
        text = text[:-2]
    return text



async def scoreboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """'📊 Scoreboard' button under the number pad -> read-only alert popup."""
    q = update.callback_query
    try:
        m = CB_RE.fullmatch(q.data or "")
        if not m or q.message is None:
            await q.answer("Invalid action.", show_alert=True)
            return
        match_id = int(m.group(1))
        snap = await asyncio.to_thread(db.get_scoreboard_snapshot, match_id)
        if snap is None:
            await q.answer("This match no longer exists.", show_alert=True)
            return
        if q.message.chat.id != snap["match"]["chat_id"]:
            await q.answer("This match isn't available here.", show_alert=True)
            return
        if is_banned(q.from_user.id):
            await q.answer("🚫 You are banned from playing UltimateCric.", show_alert=True)
            return
        await q.answer(build_popup(snap, _is_running(match_id)), show_alert=True)
    except Exception:
        logger.exception("Scoreboard popup failed")
        try:
            await q.answer("Couldn't load the scoreboard right now.", show_alert=True)
        except Exception:
            pass
