"""
Match history — /history <key>

Every Team Cricket match gets a 5-character key (letters+digits, e.g. K7M3X; shown on the
scoreboard, in the final result card and in /teammatches). `/history <key>`
replays that match from SQLite: result, full batting/bowling scorecards, and
every ball (who faced it, who bowled it, runs / wicket).

Everything is derived from team_match_events + team_match_players, so it works
for finished matches long after the bot restarted. Read-only.
Only usable in the group where the match was played.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database as db
from database import is_banned
from scoreboard import SEP, ball_label, derive_innings

logger = logging.getLogger(__name__)

CHUNK = 3800          # stay well under Telegram's 4096-char limit
MAX_MESSAGES = 8      # hard cap so one /history can never flood a group
BPO = 6
KEY_RE = re.compile(r"[#\s]*([A-Za-z0-9]{5})")


def _e(x) -> str:
    return html.escape(str(x) if x is not None else "", quote=False)


def split_lines(lines: list[str], limit: int = CHUNK) -> list[str]:
    """Packs whole lines into messages <= limit chars (lines are self-contained
    HTML, so a split never leaves a tag open)."""
    out, cur, size = [], [], 0
    for ln in lines:
        ln = ln[: limit - 10]
        if size + len(ln) + 1 > limit and cur:
            out.append("\n".join(cur))
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def _innings_meta(snap: dict) -> list[tuple[int, str | None]]:
    """[(innings_no, batting_side)] for innings that have balls."""
    side_of = {p["user_id"]: p["side"] for p in snap["players"]}
    res = []
    for inn in sorted({e["innings"] for e in snap["events"]}):
        first = next(e for e in snap["events"] if e["innings"] == inn)
        res.append((inn, side_of.get(first["striker_id"])))
    return res


def build_history(snap: dict) -> list[str]:
    """Returns the history as a list of messages (HTML)."""
    m = snap["match"]
    key = m.get("match_key") or m["match_id"]
    names = {p["user_id"]: p["name"] for p in snap["players"]}
    nm = lambda uid: _e(names.get(uid, "?"))  # noqa: E731
    label = lambda side: _e(m["team_a_label"] if side == "a" else m["team_b_label"])  # noqa: E731
    status = m.get("status")

    head = [f"📜 <b>MATCH HISTORY</b>  🔑 <code>{key}</code>", SEP]
    head.append(f"🟦 <b>{_e(m['team_a_label'])}</b> vs 🟥 <b>{_e(m['team_b_label'])}</b>")
    if m.get("created_at"):
        head.append(f"🗓 {_e(m['created_at'])} UTC")
    if status == "completed":
        head.append(f"🏁 {_e(m.get('result') or 'Completed')}")
        if m.get("potm_name"):
            head.append(f"👑 Player of the Match: <b>{_e(m['potm_name'])}</b>")
    elif status == "cancelled":
        head.append("❌ Match was cancelled")
    else:
        head.append("🔴 Match not finished — showing balls played so far")

    replaced = {x["out_user_id"]: x["in_user_id"] for x in snap.get("subs", [])}
    if snap.get("subs"):
        head += ["", "🔁 <b>Substitutions</b>"]
        for x in snap["subs"]:
            bpo_balls = x["legal_balls"]
            head.append(f"• Innings {x['innings']}, after {bpo_balls // BPO}.{bpo_balls % BPO} ov — "
                        f"{nm(x['in_user_id'])} replaced {nm(x['out_user_id'])} ({label(x['side'])})")

    meta = _innings_meta(snap)
    if not meta:
        return ["\n".join(head + ["", "<i>No balls were bowled in this match.</i>"])]

    # ---- scorecards ---------------------------------------------------------
    card = []
    derived = {}
    for inn, side in meta:
        d = derive_innings(snap["events"], inn, BPO)
        derived[inn] = d
        # who got each batter out
        out_by = {}
        for e in d["events"]:
            if str(e["outcome"]) == "W":
                out_by[e["striker_id"]] = (e["dismissal"], e["bowler_id"])
        card += ["", SEP, f"🏏 <b>INNINGS {inn} — {label(side)}</b>: <b>{d['total']}/{d['wickets']}</b> "
                     f"({d['legal'] // BPO}.{d['legal'] % BPO} ov)", ""]
        card.append("<b>Batting</b>")
        for uid in d["bat_order"]:
            b = d["bats"][uid]
            sr = f"{b['runs'] / b['balls'] * 100:.1f}" if b["balls"] else "0.0"
            if b["out"]:
                how, bw = out_by.get(uid, ("out", None))
                status_txt = f"❌ {_e(how)} b {nm(bw)}"
            else:
                status_txt = "not out"
            if uid in replaced and not b["out"]:
                status_txt = f"🔁 replaced by {nm(replaced[uid])}"
            card.append(f"• {nm(uid)} — <b>{b['runs']}</b> ({b['balls']}) 4s:{b['fours']} 6s:{b['sixes']} "
                        f"SR {sr} — {status_txt}")
        card.append("➕ Extras: 0")
        card += ["", "<b>Bowling</b>"]
        for uid, w in d["bowls"].items():
            eco = f"{w['runs'] / (w['balls'] / BPO):.2f}" if w["balls"] else "0.00"
            card.append(f"• {nm(uid)} — {w['balls'] // BPO}.{w['balls'] % BPO} ov, {w['runs']} R, "
                        f"{w['wickets']} W (Econ {eco})" + (f" 🔁 replaced by {nm(replaced[uid])}" if uid in replaced else ""))

    # ---- ball by ball ---------------------------------------------------------
    bb = ["", SEP, "🎯 <b>BALL-BY-BALL</b>"]
    for inn, side in meta:
        d = derived[inn]
        bb += ["", f"━ <b>Innings {inn} — {label(side)}</b> ━"]
        overs: dict[int, list[dict]] = {}
        for e in d["events"]:
            overs.setdefault(e["over_no"], []).append(e)
        running = 0
        for over_no in sorted(overs):
            evs = overs[over_no]
            runs = sum(int(e["outcome"]) for e in evs if str(e["outcome"]).isdigit())
            wk = sum(1 for e in evs if str(e["outcome"]) == "W")
            running += runs
            bb.append("")
            bb.append(f"<b>Over {over_no}</b> • 🎯 {nm(evs[0]['bowler_id'])} • {runs} run(s)"
                      + (f", {wk} wkt" if wk else "") + f" • total {running}")
            for e in evs:
                ball = f"{over_no - 1}.{e['ball_no']}"
                if e["bowler_id"] != evs[0]["bowler_id"]:
                    ball += f" (🎯 {nm(e['bowler_id'])})"   # bowler changed mid-over by a substitution
                if str(e["outcome"]) == "W":
                    bb.append(f"{ball}  {nm(e['striker_id'])} — ❌ <b>OUT</b> ({_e(e['dismissal'])}) b {nm(e['bowler_id'])}")
                else:
                    r = str(e["outcome"])
                    extra = " 🔥 FOUR!" if r == "4" else " 💥 SIX!" if r == "6" else ""
                    bb.append(f"{ball}  {nm(e['striker_id'])} — {ball_label(e)} {r} run(s){extra}")

    return split_lines(head + card + bb)


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, msg, user = update.effective_chat, update.message, update.effective_user
    if is_banned(user.id):
        await msg.reply_text("🚫 You are banned from playing UltimateCric.")
        return

    if not context.args:
        rows = await asyncio.to_thread(db.get_recent_team_matches, chat.id, 5)
        lines = ["📜 <b>MATCH HISTORY</b>", "", "Usage: <code>/history &lt;key&gt;</code>  (e.g. <code>/history K7M3X</code>)"]
        if rows:
            lines += ["", "<b>Recent matches here:</b>"]
            for r in rows:
                lines.append(f"🔑 <code>{r['match_key'] or r['match_id']}</code> — {_e(r['team_a_label'])} {r['team_a_score']}/{r['team_a_wickets']} "
                             f"vs {_e(r['team_b_label'])} {r['team_b_score']}/{r['team_b_wickets']}")
        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    m = KEY_RE.fullmatch(" ".join(context.args).strip())
    if not m:
        await msg.reply_text("❌ Invalid key. A key is 5 letters/digits, e.g. <code>/history K7M3X</code>", parse_mode=ParseMode.HTML)
        return
    match_id = await asyncio.to_thread(db.get_team_match_id_by_key, m.group(1))
    key = m.group(1).upper()
    snap = await asyncio.to_thread(db.get_scoreboard_snapshot, match_id) if match_id else None
    # Same message for "doesn't exist" and "other group" so keys can't be probed.
    if snap is None or snap["match"]["chat_id"] != chat.id:
        await msg.reply_text("❌ No match with that key in this group. Check /history for recent keys.")
        return

    try:
        pages = build_history(snap)
    except Exception:
        logger.exception("History render failed for match %s", key)
        await msg.reply_text("⚠️ Couldn't build that history right now. Please try again.")
        return

    shown = pages[:MAX_MESSAGES]
    for i, page in enumerate(shown, 1):
        suffix = f"\n\n<i>Page {i}/{len(pages)}</i>" if len(pages) > 1 else ""
        try:
            await msg.reply_text(page + suffix, parse_mode=ParseMode.HTML)
        except TelegramError:
            logger.exception("History send failed (match %s page %s)", key, i)
            return
        if i < len(shown):
            await asyncio.sleep(0.4)   # gentle on group rate limits
    if len(pages) > MAX_MESSAGES:
        await msg.reply_text(f"…history is very long; showing the first {MAX_MESSAGES} pages.")
