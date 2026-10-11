"""
Team Cricket — /teamcricket

A full team-vs-team cricket mode (2-12 players per side), built as a
separate, self-contained module from the 1v1 /cricket engine (game.py)
and the earlier simple /teammatch prototype (teammode.py, now removed).
Nothing here touches game.py, tictactoe.py, achievements.py, admin.py,
broadcast.py, or the existing player_stats table — it has its own tables
(see database.py's "Team Cricket" section).

Flow
----
/teamcricket -> lobby (join/leave, captains auto-assigned on first join)
             -> toss (captain of each team; auto coin flip + captain call)
             -> winning captain picks Bat/Bowl
             -> batting captain builds batting order (tap players in order)
             -> bowling captain picks the over's bowler (no 2 overs running)
             -> ball-by-ball: same secret-number fairness as /cricket —
                striker picks a hidden number (0-6) first, then the
                bowler picks one too; matching numbers = wicket, otherwise
                the striker's number is the runs scored
             -> innings ends on target reached / N-1 wickets down / overs used
             -> 2nd innings mirrors the first, chasing the target
             -> match result, scorecards, Player of the Match, stats saved

Design notes
------------
- State is a single @dataclass (TeamMatch) per match, keyed by match_id
  in TEAM_MATCHES — not scattered globals, so many matches run at once
  safely (point 29 of the spec).
- Every mutating callback acquires the match's own asyncio.Lock before
  touching state, so two near-simultaneous taps can't double-process one
  ball / double-join a slot / double-start a match (point 31).
- Every callback_data is namespaced with "tc:<match_id>:..." and checked
  against the match's real state before anything happens (point 30).
- Captain-required waits (toss call, bat/bowl, batting order, bowler
  pick) and player-required waits (striker/bowler ball confirm) auto-
  resolve after TURN_TIMEOUT_SECONDS via an internal stage_token so a
  stuck match never blocks the group forever (point 32).
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import random
import re
from dataclasses import dataclass, field
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import database as db
from database import is_banned

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (all the "don't hard-code this" knobs from the spec)
# ---------------------------------------------------------------------------

TEAM_MIN_PLAYERS = 2
TEAM_MAX_PLAYERS = 12
TEAM_MAX_SUBS = 3                   # substitutions allowed per team per match
TEAM_OVERS = 3                      # default overs per innings
TEAM_OVERS_MIN = 1
TEAM_OVERS_MAX = 20
TEAM_OVERS_QUICK = [1, 2, 3, 5, 10]  # one-tap buttons in the lobby (any 1-20 via /teamcricket <overs>)
BALLS_PER_OVER = 6
TURN_TIMEOUT_SECONDS = 60

# Ball mechanic: same secret-number-pick-and-match fairness as /cricket
# (game.py) — striker picks a secret number first, then the bowler picks
# one too, both hidden from each other. If they MATCH -> wicket. If not,
# the striker's number is the runs scored. 0 is included here (unlike
# solo /cricket's 1-6) so dot balls are a pickable outcome too.
PICK_NUMBERS = [0, 1, 2, 3, 4, 5, 6]

DISMISSAL_TYPES = ["Bowled", "Caught", "LBW", "Run Out", "Stumped"]
DISMISSAL_WEIGHTS = [0.35, 0.35, 0.15, 0.10, 0.05]

RUN_EMOJI = {0: "⚪", 1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣", "W": "❌"}


def _esc(text) -> str:
    """HTML-escape any dynamic/user-supplied text before it goes into an
    HTML-parse-mode message — names can contain '<', '&', etc."""
    return html.escape(str(text) if text is not None else "", quote=False)


def _tag(name) -> str:
    return f"<b>{_esc(name)}</b>"


# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------

@dataclass
class BattingStats:
    runs: int = 0
    balls: int = 0
    fours: int = 0
    sixes: int = 0
    is_out: bool = False
    dismissal: Optional[str] = None

    @property
    def strike_rate(self) -> float:
        return round((self.runs / self.balls) * 100, 1) if self.balls else 0.0


@dataclass
class BowlingStats:
    balls_bowled: int = 0
    runs_conceded: int = 0
    wickets: int = 0

    @property
    def overs_display(self) -> str:
        overs, balls = divmod(self.balls_bowled, BALLS_PER_OVER)
        return f"{overs}.{balls}"

    @property
    def economy(self) -> float:
        overs_bowled = self.balls_bowled / BALLS_PER_OVER
        return round(self.runs_conceded / overs_bowled, 2) if overs_bowled else 0.0


@dataclass
class TeamSide:
    label: str
    players: list[tuple[int, str]] = field(default_factory=list)  # (user_id, name), join order
    captain_id: Optional[int] = None

    def has(self, user_id: int) -> bool:
        return any(uid == user_id for uid, _ in self.players)

    def name_of(self, user_id: int) -> Optional[str]:
        for uid, name in self.players:
            if uid == user_id:
                return name
        return None


@dataclass
class TeamMatch:
    match_id: int
    chat_id: int
    message_id: Optional[int] = None
    creator_id: int = 0
    creator_name: str = ""

    overs: int = TEAM_OVERS             # overs per innings for THIS match

    stage: str = "lobby"
    # lobby -> toss_call -> bat_bowl -> order_a / order_b (batting order build)
    # -> bowler_pick -> ball_wait -> (innings switch repeats order/bowler/ball)
    # -> done

    team_a: TeamSide = field(default_factory=lambda: TeamSide("Team A"))
    team_b: TeamSide = field(default_factory=lambda: TeamSide("Team B"))

    toss_winner_side: Optional[str] = None
    batting_side: Optional[str] = None
    bowling_side: Optional[str] = None
    innings: int = 1
    target: Optional[int] = None

    # Per-innings batting state
    lineup: list[int] = field(default_factory=list)       # chosen batting order (user_ids)
    order_pick_pool: list[int] = field(default_factory=list)  # remaining user_ids left to pick into lineup
    striker_id: Optional[int] = None
    non_striker_id: Optional[int] = None
    next_in_idx: int = 0

    current_bowler_id: Optional[int] = None
    last_over_bowler_id: Optional[int] = None
    balls_this_over: int = 0
    overs_completed: int = 0

    team_scores: dict = field(default_factory=lambda: {"a": 0, "b": 0})
    team_wickets: dict = field(default_factory=lambda: {"a": 0, "b": 0})
    team_balls: dict = field(default_factory=lambda: {"a": 0, "b": 0})  # legal balls faced this innings

    batting_stats: dict = field(default_factory=dict)  # user_id -> BattingStats (whole match)
    bowling_stats: dict = field(default_factory=dict)  # user_id -> BowlingStats (whole match)
    ball_history: dict = field(default_factory=lambda: {1: [], 2: []})  # innings -> [emoji,...]

    picks: dict = field(default_factory=dict)  # user_id -> secret number (0-6) chosen this ball
    turn: str = "batter"  # "batter" or "bowler" — whose secret pick we're waiting on this ball

    # Substitutions: players who were replaced mid-match (kept for stats / scorecards)
    former: list = field(default_factory=list)            # [(user_id, name, side)]
    subs_used: dict = field(default_factory=lambda: {"a": 0, "b": 0})

    db_match_id: Optional[int] = None
    result_text: Optional[str] = None
    winner_side: Optional[str] = None  # 'a' / 'b' / None (tie)

    stage_token: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer_task: Optional[asyncio.Task] = None


# message_id -> TeamMatch (the lobby message IS the match's permanent message)
TEAM_MATCHES: dict[int, TeamMatch] = {}


def _side_all_players(game: TeamMatch, side: str) -> list[tuple[int, str]]:
    """Current players of a side + anyone who was substituted out of it."""
    return list(_side_obj(game, side).players) + [(u, n) for u, n, sd in game.former if sd == side]


def _side_obj(game: TeamMatch, side: str) -> TeamSide:
    return game.team_a if side == "a" else game.team_b


def _other_side(side: str) -> str:
    return "b" if side == "a" else "a"


def _name(user) -> str:
    return user.first_name or user.username or "Player"


def _bump_token(game: TeamMatch) -> int:
    game.stage_token += 1
    return game.stage_token


def _cancel_timer(game: TeamMatch):
    t = game.timer_task
    if t and not t.done():
        t.cancel()
    game.timer_task = None


def _live_state(game: "TeamMatch", stage: Optional[str] = None) -> dict:
    """Snapshot of the crease/bowler state for the live scoreboard."""
    return {
        "innings": game.innings,
        "batting_side": game.batting_side,
        "bowling_side": game.bowling_side,
        "stage": stage or game.stage,
        "striker_id": game.striker_id,
        "non_striker_id": game.non_striker_id,
        "bowler_id": game.current_bowler_id,
        "lineup": list(game.lineup),
        "next_in_idx": game.next_in_idx,
        "target": game.target,
        "overs_completed": game.overs_completed,
        "balls_this_over": game.balls_this_over,
        "overs": game.overs,
    }


def _persist_live(context, game: "TeamMatch", stage: Optional[str] = None):
    """Saves live state (non-delivery transitions). Must never break the match flow."""
    if not game.db_match_id:
        return
    try:
        db.save_team_live_state(game.db_match_id, _live_state(game, stage))
    except Exception:
        logger.exception("Could not persist live state for match %s", game.match_id)


def _schedule_timeout(context, game: TeamMatch, token: int, on_timeout, seconds: int = TURN_TIMEOUT_SECONDS):
    """Schedules on_timeout(context, game) to run after `seconds`, but only
    if the match's stage_token still matches `token` when it fires (i.e.
    nothing else resolved this wait in the meantime)."""
    async def _runner():
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        if game.stage_token != token:
            return  # stale — state already moved on, do nothing
        try:
            async with game.lock:
                if game.stage_token != token:
                    return
                await on_timeout(context, game)
        except Exception:
            logger.exception("Team Cricket timeout handler failed for match %s", game.match_id)

    _cancel_timer(game)
    game.timer_task = asyncio.create_task(_runner())


def _roll_dismissal() -> str:
    return random.choices(DISMISSAL_TYPES, weights=DISMISSAL_WEIGHTS, k=1)[0]


def _pick_keyboard(game: TeamMatch) -> InlineKeyboardMarkup:
    """7 buttons (0-6) for whoever's turn it is to pick — striker first,
    then the bowler, same as /cricket. Only the expected picker's tap
    (checked server-side) actually registers."""
    mid = game.match_id
    row1 = [InlineKeyboardButton(str(n), callback_data=f"tc:{mid}:pick:{n}") for n in PICK_NUMBERS[:4]]
    row2 = [InlineKeyboardButton(str(n), callback_data=f"tc:{mid}:pick:{n}") for n in PICK_NUMBERS[4:]]
    rows = [row1, row2]
    if game.db_match_id:
        # Popup scoreboard (read-only) — handled by scoreboard.py
        rows.append([InlineKeyboardButton("📊 Scoreboard", callback_data=f"sb:{game.db_match_id}:p")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Lobby
# ---------------------------------------------------------------------------

def _new_match(creator_id: int, creator_name: str, chat_id: int, match_id: int) -> TeamMatch:
    return TeamMatch(match_id=match_id, chat_id=chat_id, creator_id=creator_id, creator_name=creator_name)


def _roster_text(team: TeamSide) -> str:
    if not team.players:
        return "   <i>No players yet — tap Join below 👇</i>"
    lines = []
    for i, (uid, name) in enumerate(team.players, 1):
        badge = "👑" if uid == team.captain_id else "🏏"
        cap = "  <i>(C)</i>" if uid == team.captain_id else ""
        lines.append(f"   {i}. {badge} {_tag(name)}{cap}")
    return "\n".join(lines)


def _slots_bar(count: int, width: int = 12) -> str:
    """●●●○○○ style team-size meter (TEAM_MAX_PLAYERS slots)."""
    filled = max(0, min(width, count))
    return "●" * filled + "○" * (width - filled)


def _lobby_text(game: TeamMatch) -> str:
    na, nb = len(game.team_a.players), len(game.team_b.players)
    need_a, need_b = max(0, TEAM_MIN_PLAYERS - na), max(0, TEAM_MIN_PLAYERS - nb)
    if need_a == 0 and need_b == 0:
        status = "🟢 <b>READY!</b> A captain can press <b>Start Match</b> 🚀"
    else:
        waits = []
        if need_a:
            waits.append(f"{_esc(game.team_a.label)} needs {need_a} more")
        if need_b:
            waits.append(f"{_esc(game.team_b.label)} needs {need_b} more")
        status = "🟡 <b>Waiting for players</b> — " + " • ".join(waits)

    overs_word = "over" if game.overs == 1 else "overs"
    lines = [
        "🏏 <b>T E A M   C R I C K E T</b> 🏏",
        "━━━━━━━━━━━━━━━━━━━",
        f"🎙 Hosted by <b>{_esc(game.creator_name)}</b>",
        f"⏱ <b>{game.overs} {overs_word}</b> per innings  •  🎲 Toss next",
        "━━━━━━━━━━━━━━━━━━━",
        "",
        f"🟦 <b>{_esc(game.team_a.label).upper()}</b>   👥 {na}/{TEAM_MAX_PLAYERS}",
        f"   <code>{_slots_bar(na)}</code>",
        _roster_text(game.team_a),
        "",
        "━━━━━━  ⚔️ <b>VS</b> ⚔️  ━━━━━━",
        "",
        f"🟥 <b>{_esc(game.team_b.label).upper()}</b>   👥 {nb}/{TEAM_MAX_PLAYERS}",
        f"   <code>{_slots_bar(nb)}</code>",
        _roster_text(game.team_b),
        "",
        "━━━━━━━━━━━━━━━━━━━",
        status,
        f"<i>👑 First to join a team becomes its captain. Need {TEAM_MIN_PLAYERS}+ per team to start.</i>",
    ]
    return "\n".join(lines)


def _lobby_keyboard(game: TeamMatch) -> InlineKeyboardMarkup:
    mid = game.match_id
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🟦 Join Team A", callback_data=f"tc:{mid}:join:a"),
                InlineKeyboardButton("🟥 Join Team B", callback_data=f"tc:{mid}:join:b"),
            ],
            [
                InlineKeyboardButton(
                    (f"✅ {n}" if n == game.overs else str(n)) + " ov", callback_data=f"tc:{mid}:overs:{n}"
                )
                for n in TEAM_OVERS_QUICK
            ],
            [InlineKeyboardButton("🚀 START MATCH", callback_data=f"tc:{mid}:start")],
            [
                InlineKeyboardButton("🚪 Leave", callback_data=f"tc:{mid}:leave"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"tc:{mid}:cancel"),
            ],
        ]
    )


async def teamcricket_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "🏏 Team Cricket only works inside <b>groups</b>! Add me to a group to play.",
            parse_mode="HTML",
        )
        return

    user = update.effective_user
    if is_banned(user.id):
        await update.message.reply_text("🚫 You are banned from playing UltimateCric.")
        return

    # Reserve a message first so we have a message_id to use as match_id.
    sent = await update.message.reply_text("🏏 Setting up the pitch… 🌱")
    match_id = sent.message_id
    game = _new_match(user.id, _name(user), chat.id, match_id)
    if context.args:
        try:
            n = int(context.args[0])
        except ValueError:
            n = None
        if n is not None and TEAM_OVERS_MIN <= n <= TEAM_OVERS_MAX:
            game.overs = n
        else:
            await update.message.reply_text(
                f"⚠️ Overs must be a number from {TEAM_OVERS_MIN} to {TEAM_OVERS_MAX}. Using {game.overs} overs.")
    TEAM_MATCHES[match_id] = game

    await context.bot.edit_message_text(
        chat_id=chat.id,
        message_id=sent.message_id,
        text=_lobby_text(game),
        parse_mode="HTML",
        reply_markup=_lobby_keyboard(game),
    )
    game.message_id = sent.message_id


def _parse_cb(data: str):
    """callback_data is always 'tc:<match_id>:<action>[:<extra>]'."""
    parts = data.split(":")
    match_id = int(parts[1])
    action = parts[2]
    extra = parts[3] if len(parts) > 3 else None
    return match_id, action, extra


async def _get_match_for_callback(query) -> Optional[TeamMatch]:
    try:
        match_id, _action, _extra = _parse_cb(query.data)
    except (IndexError, ValueError):
        await query.answer("Invalid action.", show_alert=True)
        return None
    game = TEAM_MATCHES.get(match_id)
    if not game:
        await query.answer("This match no longer exists.", show_alert=True)
        return None
    if query.message.message_id != game.message_id:
        # A callback from a stale/forwarded copy of an old message.
        await query.answer("This match is no longer active here.", show_alert=True)
        return None
    return game


async def team_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = await _get_match_for_callback(query)
    if not game:
        return
    _mid, _action, side = _parse_cb(query.data)

    if game.stage != "lobby":
        await query.answer("This match is no longer accepting players.", show_alert=True)
        return

    user = query.from_user
    if is_banned(user.id):
        await query.answer("🚫 You are banned from playing UltimateCric.", show_alert=True)
        return

    async with game.lock:
        if game.team_a.has(user.id) or game.team_b.has(user.id):
            await query.answer("You've already joined a team in this match!", show_alert=True)
            return

        team = _side_obj(game, side)
        if len(team.players) >= TEAM_MAX_PLAYERS:
            await query.answer(f"{team.label} is full ({TEAM_MAX_PLAYERS}/{TEAM_MAX_PLAYERS})!", show_alert=True)
            return

        team.players.append((user.id, _name(user)))
        if team.captain_id is None:
            team.captain_id = user.id  # first player to join a team becomes its captain

        await query.answer(f"Joined {team.label}!" + (" You're the captain! 👑" if team.captain_id == user.id else ""))
        await context.bot.edit_message_text(
            chat_id=game.chat_id, message_id=game.message_id,
            text=_lobby_text(game), parse_mode="HTML", reply_markup=_lobby_keyboard(game),
        )


async def team_overs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = await _get_match_for_callback(query)
    if not game:
        return
    _mid, _action, n_str = _parse_cb(query.data)
    if game.stage != "lobby":
        await query.answer("Overs can only be changed before the match starts.", show_alert=True)
        return
    uid = query.from_user.id
    if uid not in (game.creator_id, game.team_a.captain_id, game.team_b.captain_id):
        await query.answer("Only the creator or a team captain can change overs.", show_alert=True)
        return
    try:
        n = int(n_str)
    except (TypeError, ValueError):
        n = 0
    if not (TEAM_OVERS_MIN <= n <= TEAM_OVERS_MAX):
        await query.answer("Invalid overs.", show_alert=True)
        return
    async with game.lock:
        if game.stage != "lobby":
            await query.answer("Overs can only be changed before the match starts.", show_alert=True)
            return
        if n == game.overs:
            await query.answer(f"Already {n} over(s).")
            return
        game.overs = n
        await query.answer(f"Overs set to {n}.")
        await context.bot.edit_message_text(
            chat_id=game.chat_id, message_id=game.message_id,
            text=_lobby_text(game), parse_mode="HTML", reply_markup=_lobby_keyboard(game),
        )


async def team_leave_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = await _get_match_for_callback(query)
    if not game:
        return

    if game.stage != "lobby":
        await query.answer("You can't leave after the match has started.", show_alert=True)
        return

    user = query.from_user
    async with game.lock:
        for side_key, team in (("a", game.team_a), ("b", game.team_b)):
            if team.has(user.id):
                team.players = [(uid, n) for uid, n in team.players if uid != user.id]
                if team.captain_id == user.id:
                    team.captain_id = team.players[0][0] if team.players else None
                await query.answer(f"Left {team.label}.")
                await context.bot.edit_message_text(
                    chat_id=game.chat_id, message_id=game.message_id,
                    text=_lobby_text(game), parse_mode="HTML", reply_markup=_lobby_keyboard(game),
                )
                return

        await query.answer("You haven't joined a team yet!", show_alert=True)


async def team_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = await _get_match_for_callback(query)
    if not game:
        return

    user_id = query.from_user.id
    is_authorized = (
        user_id == game.creator_id
        or user_id == game.team_a.captain_id
        or user_id == game.team_b.captain_id
    )
    if not is_authorized:
        await query.answer("Only the creator or a team captain can cancel this.", show_alert=True)
        return

    game.stage = "cancelled"
    _bump_token(game)
    _cancel_timer(game)
    if game.db_match_id:
        db.cancel_team_match_row(game.db_match_id)
    TEAM_MATCHES.pop(game.message_id, None)
    await query.answer("Match cancelled.")
    await context.bot.edit_message_text(
        chat_id=game.chat_id, message_id=game.message_id,
        text="❌ <b>Team Cricket match cancelled.</b>", parse_mode="HTML",
    )


async def team_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = await _get_match_for_callback(query)
    if not game:
        return

    if game.stage != "lobby":
        await query.answer("This match has already started.", show_alert=True)
        return

    user_id = query.from_user.id
    if user_id not in (game.team_a.captain_id, game.team_b.captain_id):
        await query.answer("Only a team captain can start the match.", show_alert=True)
        return

    async with game.lock:
        if game.stage != "lobby":
            await query.answer("This match has already started.", show_alert=True)
            return
        if len(game.team_a.players) < TEAM_MIN_PLAYERS or len(game.team_b.players) < TEAM_MIN_PLAYERS:
            await query.answer(
                f"Both teams need at least {TEAM_MIN_PLAYERS} players before starting!", show_alert=True
            )
            return

        _cancel_timer(game)
        game.db_match_id = db.create_team_match(game.chat_id, game.message_id, game.team_a.label, game.team_b.label)
        for side, team in (("a", game.team_a), ("b", game.team_b)):
            for uid, pname in team.players:
                db.record_team_match_player(game.db_match_id, uid, pname, side, uid == team.captain_id)
                game.batting_stats[uid] = BattingStats()
                game.bowling_stats[uid] = BowlingStats()

        game.stage = "toss_call"
        token = _bump_token(game)
        await query.answer("Starting the match!")
        await _render_toss_call(context, game)
        _schedule_timeout(context, game, token, _timeout_toss_call)


# ---------------------------------------------------------------------------
# Toss
# ---------------------------------------------------------------------------

async def _render_toss_call(context, game: TeamMatch):
    mid = game.match_id
    text = (
        "🏏 <b>TOSS TIME</b>\n\n"
        f"🪙 {_tag(game.team_a.label)}'s captain {_tag(game.team_a.name_of(game.team_a.captain_id))} calls it:"
    )
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🪙 Heads", callback_data=f"tc:{mid}:tosscall:heads"),
            InlineKeyboardButton("🪙 Tails", callback_data=f"tc:{mid}:tosscall:tails"),
        ]]
    )
    await context.bot.edit_message_text(
        chat_id=game.chat_id, message_id=game.message_id, text=text, parse_mode="HTML", reply_markup=kb
    )


async def _timeout_toss_call(context, game: TeamMatch):
    await _resolve_toss(context, game, "heads")


async def team_toss_call_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = await _get_match_for_callback(query)
    if not game:
        return
    _mid, _action, call = _parse_cb(query.data)

    if game.stage != "toss_call":
        await query.answer("Toss already done.", show_alert=True)
        return
    if query.from_user.id != game.team_a.captain_id:
        await query.answer(f"Only {game.team_a.label}'s captain calls the toss!", show_alert=True)
        return

    async with game.lock:
        if game.stage != "toss_call":
            await query.answer("Toss already done.", show_alert=True)
            return
        _cancel_timer(game)
        await query.answer(f"You called {call.title()}!")
        await _resolve_toss(context, game, call)


async def _resolve_toss(context, game: TeamMatch, call: str):
    result = random.choice(["heads", "tails"])
    winner_side = "a" if call == result else "b"
    game.toss_winner_side = winner_side
    game.stage = "bat_bowl"
    token = _bump_token(game)

    winner_team = _side_obj(game, winner_side)
    text = (
        "🏏 <b>TOSS RESULT</b>\n\n"
        f"🪙 It's <b>{result.title()}</b>!\n\n"
        f"{_tag(winner_team.label)} won the toss! 🎉\n\n"
        f"{_tag(winner_team.name_of(winner_team.captain_id))} (captain) — choose to Bat or Bowl first"
    )
    mid = game.match_id
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🏏 Bat First", callback_data=f"tc:{mid}:batbowl:bat"),
            InlineKeyboardButton("🎯 Bowl First", callback_data=f"tc:{mid}:batbowl:bowl"),
        ]]
    )
    await context.bot.edit_message_text(
        chat_id=game.chat_id, message_id=game.message_id, text=text, parse_mode="HTML", reply_markup=kb
    )
    _schedule_timeout(context, game, token, _timeout_bat_bowl)


async def _timeout_bat_bowl(context, game: TeamMatch):
    await _begin_innings(context, game, game.toss_winner_side)


async def team_batbowl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = await _get_match_for_callback(query)
    if not game:
        return
    _mid, _action, choice = _parse_cb(query.data)

    if game.stage != "bat_bowl":
        await query.answer("Already decided.", show_alert=True)
        return

    winner_side = game.toss_winner_side
    winner_team = _side_obj(game, winner_side)
    if query.from_user.id != winner_team.captain_id:
        await query.answer("Only the toss-winning captain decides this!", show_alert=True)
        return

    async with game.lock:
        if game.stage != "bat_bowl":
            await query.answer("Already decided.", show_alert=True)
            return
        _cancel_timer(game)
        batting_side = winner_side if choice == "bat" else _other_side(winner_side)
        await query.answer("Let's play!")
        await _begin_innings(context, game, batting_side)


# ---------------------------------------------------------------------------
# Batting order
# ---------------------------------------------------------------------------

async def _begin_innings(context, game: TeamMatch, batting_side: str):
    game.batting_side = batting_side
    game.bowling_side = _other_side(batting_side)
    game.lineup = []
    team = _side_obj(game, batting_side)
    game.order_pick_pool = [uid for uid, _ in team.players]
    game.stage = "order_pick"
    token = _bump_token(game)
    game.striker_id = game.non_striker_id = game.current_bowler_id = None
    game.next_in_idx = 0
    game.balls_this_over = game.overs_completed = 0
    _persist_live(context, game)
    await _render_order_pick(context, game)
    _schedule_timeout(context, game, token, _timeout_order_pick)


def _order_pick_keyboard(game: TeamMatch) -> InlineKeyboardMarkup:
    mid = game.match_id
    team = _side_obj(game, game.batting_side)
    rows = []
    row = []
    for uid in game.order_pick_pool:
        name = team.name_of(uid)
        row.append(InlineKeyboardButton(f"➕ {name}", callback_data=f"tc:{mid}:orderpick:{uid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def _render_order_pick(context, game: TeamMatch):
    team = _side_obj(game, game.batting_side)
    captain_name = team.name_of(team.captain_id)
    lines = [
        f"🟦 <b>{_esc(team.label)} BATTING ORDER</b>",
        "",
        f"{_tag(captain_name)} (captain) — tap players in the order they should bat:",
        "",
    ]
    if game.lineup:
        lines.append("<b>Order so far:</b>")
        for i, uid in enumerate(game.lineup, 1):
            lines.append(f"{i}. {_tag(team.name_of(uid))}")
        lines.append("")
    await context.bot.edit_message_text(
        chat_id=game.chat_id, message_id=game.message_id,
        text="\n".join(lines), parse_mode="HTML", reply_markup=_order_pick_keyboard(game),
    )


async def _timeout_order_pick(context, game: TeamMatch):
    # Auto-fill the remaining order in join order so the match isn't stuck.
    game.lineup.extend(game.order_pick_pool)
    game.order_pick_pool = []
    await _confirm_batting_order(context, game)


async def team_order_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = await _get_match_for_callback(query)
    if not game:
        return
    _mid, _action, uid_str = _parse_cb(query.data)
    picked_uid = int(uid_str)

    if game.stage != "order_pick":
        await query.answer("Batting order already set.", show_alert=True)
        return

    team = _side_obj(game, game.batting_side)
    if query.from_user.id != team.captain_id:
        await query.answer(f"Only {team.label}'s captain sets the batting order!", show_alert=True)
        return

    async with game.lock:
        if game.stage != "order_pick" or picked_uid not in game.order_pick_pool:
            await query.answer("That player is no longer available to pick.", show_alert=True)
            return

        game.order_pick_pool.remove(picked_uid)
        game.lineup.append(picked_uid)
        _bump_token(game)  # any progress resets the inactivity clock
        await query.answer(f"Added {team.name_of(picked_uid)}!")

        if game.order_pick_pool:
            await _render_order_pick(context, game)
            token = _bump_token(game)
            _schedule_timeout(context, game, token, _timeout_order_pick)
        else:
            _cancel_timer(game)
            await _confirm_batting_order(context, game)


async def _confirm_batting_order(context, game: TeamMatch):
    game.striker_id = game.lineup[0]
    game.non_striker_id = game.lineup[1] if len(game.lineup) > 1 else None
    game.next_in_idx = 2
    game.team_wickets[game.batting_side] = 0
    game.team_scores[game.batting_side] = 0
    game.team_balls[game.batting_side] = 0
    game.balls_this_over = 0
    game.overs_completed = 0
    game.current_bowler_id = None
    game.last_over_bowler_id = None
    game.picks = {}
    game.turn = "batter"

    game.stage = "bowler_pick"
    token = _bump_token(game)
    _persist_live(context, game)
    await _render_bowler_pick(context, game)
    _schedule_timeout(context, game, token, _timeout_bowler_pick)


# ---------------------------------------------------------------------------
# Bowler selection (every over)
# ---------------------------------------------------------------------------

def _eligible_bowlers(game: TeamMatch) -> list[tuple[int, str]]:
    team = _side_obj(game, game.bowling_side)
    return [(uid, name) for uid, name in team.players if uid != game.last_over_bowler_id]


def _bowler_pick_keyboard(game: TeamMatch) -> InlineKeyboardMarkup:
    mid = game.match_id
    rows, row = [], []
    for uid, name in _eligible_bowlers(game):
        row.append(InlineKeyboardButton(f"🎯 {name}", callback_data=f"tc:{mid}:bowlerpick:{uid}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def _render_bowler_pick(context, game: TeamMatch):
    bowl_team = _side_obj(game, game.bowling_side)
    captain_name = bowl_team.name_of(bowl_team.captain_id)
    over_no = game.overs_completed + 1
    lines = [
        _scoreboard_block(game),
        "",
        f"{_tag(captain_name)} (captain) — pick the bowler for Over {over_no}:",
    ]
    await context.bot.edit_message_text(
        chat_id=game.chat_id, message_id=game.message_id,
        text="\n".join(lines), parse_mode="HTML", reply_markup=_bowler_pick_keyboard(game),
    )


async def _timeout_bowler_pick(context, game: TeamMatch):
    eligible = _eligible_bowlers(game)
    chosen = eligible[0][0] if eligible else _side_obj(game, game.bowling_side).players[0][0]
    await _set_bowler_and_start_ball(context, game, chosen)


async def team_bowler_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = await _get_match_for_callback(query)
    if not game:
        return
    _mid, _action, uid_str = _parse_cb(query.data)
    picked_uid = int(uid_str)

    if game.stage != "bowler_pick":
        await query.answer("Bowler already chosen for this over.", show_alert=True)
        return

    bowl_team = _side_obj(game, game.bowling_side)
    if query.from_user.id != bowl_team.captain_id:
        await query.answer(f"Only {bowl_team.label}'s captain picks the bowler!", show_alert=True)
        return

    async with game.lock:
        if game.stage != "bowler_pick":
            await query.answer("Bowler already chosen for this over.", show_alert=True)
            return
        if picked_uid == game.last_over_bowler_id:
            await query.answer("That bowler just bowled the previous over!", show_alert=True)
            return
        if not bowl_team.has(picked_uid):
            await query.answer("That player isn't on the bowling team.", show_alert=True)
            return

        _cancel_timer(game)
        await query.answer(f"{bowl_team.name_of(picked_uid)} will bowl this over.")
        await _set_bowler_and_start_ball(context, game, picked_uid)


async def _set_bowler_and_start_ball(context, game: TeamMatch, bowler_id: int):
    game.current_bowler_id = bowler_id
    game.picks = {}
    game.turn = "batter"
    game.stage = "ball_wait"
    token = _bump_token(game)
    _persist_live(context, game)
    await _render_striker_pick(context, game)
    _schedule_timeout(context, game, token, _timeout_ball_wait)


# ---------------------------------------------------------------------------
# Ball-by-ball
# ---------------------------------------------------------------------------

def _striker_name(game: TeamMatch) -> str:
    return _side_obj(game, game.batting_side).name_of(game.striker_id) or "?"


def _non_striker_name(game: TeamMatch) -> Optional[str]:
    if game.non_striker_id is None:
        return None
    return _side_obj(game, game.batting_side).name_of(game.non_striker_id)


def _bowler_name(game: TeamMatch) -> str:
    return _side_obj(game, game.bowling_side).name_of(game.current_bowler_id) or "?"


def _over_display(game: TeamMatch, side: str) -> str:
    balls = game.team_balls[side]
    overs, rem = divmod(balls, BALLS_PER_OVER)
    return f"{overs}.{rem}/{game.overs}"


def _progress_bar(used: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▱" * width
    filled = max(0, min(width, int(width * used / total + 0.5)))
    return "▰" * filled + "▱" * (width - filled)


def _this_over_strip(game: TeamMatch) -> str:
    """Balls of the CURRENT over only, with ▫️ for balls still to come."""
    hist = game.ball_history.get(game.innings, [])
    cur = hist[-game.balls_this_over:] if game.balls_this_over else []
    slots = cur + ["▫️"] * max(0, BALLS_PER_OVER - len(cur))
    return " │ ".join(slots)


def _last_over_strip(game: TeamMatch) -> str:
    """The previous completed over (shown while the new over has no balls yet)."""
    hist = game.ball_history.get(game.innings, [])
    if game.balls_this_over or len(hist) < BALLS_PER_OVER:
        return ""
    return " │ ".join(hist[-BALLS_PER_OVER:])


def _scoreboard_block(game: TeamMatch, last_ball_text: Optional[str] = None) -> str:
    side = game.batting_side
    bat_team = _side_obj(game, side)
    score = game.team_scores[side]
    wkts = game.team_wickets[side]
    balls = game.team_balls[side]
    max_balls = game.overs * BALLS_PER_OVER
    icon = "🟦" if side == "a" else "🟥"

    lines = [
        f"🏏 <b>TEAM CRICKET</b>  •  {game.overs} OVERS",
        "━━━━━━━━━━━━━━━━━━",
        f"{icon} <b>{_esc(bat_team.label)}</b>  <i>(Innings {game.innings})</i>",
        f"<b>{score}/{wkts}</b>   ⏱ {_over_display(game, side)} ov",
        f"{_progress_bar(balls, max_balls)}  ⚡ RR {score / (balls / BALLS_PER_OVER):.2f}" if balls
        else f"{_progress_bar(0, max_balls)}  ⚡ RR 0.00",
    ]
    if game.target is not None:
        need = game.target - score
        left = max_balls - balls
        if need > 0 and left > 0:
            rrr = need / (left / BALLS_PER_OVER)
            lines += ["", f"🎯 Target <b>{game.target}</b>  •  need <b>{need}</b> from <b>{left}</b> ball(s)  •  RRR {rrr:.2f}"]
        else:
            lines += ["", f"🎯 Target <b>{game.target}</b>"]

    lines += ["━━━━━━━━━━━━━━━━━━"]
    st = game.batting_stats.get(game.striker_id)
    lines.append(f"🟢 <b>{_esc(_striker_name(game))}</b> *"
                 + (f"  {st.runs} ({st.balls})" if st else ""))
    ns = _non_striker_name(game)
    if ns:
        nst = game.batting_stats.get(game.non_striker_id)
        lines.append(f"⚪ {_esc(ns)}" + (f"  {nst.runs} ({nst.balls})" if nst else ""))
    if game.current_bowler_id is not None:
        bw = game.bowling_stats.get(game.current_bowler_id)
        lines.append(f"🎯 <b>{_esc(_bowler_name(game))}</b>"
                     + (f"  {bw.overs_display}-{bw.runs_conceded}-{bw.wickets}" if bw else ""))

    lines += ["", f"<b>This over</b>  {_this_over_strip(game)}"]
    last = _last_over_strip(game)
    if last:
        lines.append(f"<i>Last over</i>  {last}")
    if last_ball_text:
        lines += ["", f"<b>Last ball:</b> {last_ball_text}"]
    lines.append("━━━━━━━━━━━━━━━━━━")
    return "\n".join(lines)


async def _render_striker_pick(context, game: TeamMatch):
    text = _scoreboard_block(game) + f"\n\n{_tag(_striker_name(game))} — pick your secret number! 🏏"
    await context.bot.edit_message_text(
        chat_id=game.chat_id, message_id=game.message_id,
        text=text, parse_mode="HTML", reply_markup=_pick_keyboard(game),
    )


async def _render_bowler_pick_number(context, game: TeamMatch, last_ball_text: Optional[str] = None):
    text = (
        _scoreboard_block(game, last_ball_text)
        + f"\n\n{_tag(_striker_name(game))} has selected a number,\n"
        + f"{_tag(_bowler_name(game))} its your turn to bowl 🎯"
    )
    await context.bot.edit_message_text(
        chat_id=game.chat_id, message_id=game.message_id,
        text=text, parse_mode="HTML", reply_markup=_pick_keyboard(game),
    )


async def _timeout_ball_wait(context, game: TeamMatch):
    # Whoever hasn't picked yet gets a random number auto-picked for them,
    # so a non-responding player can never stall the match forever.
    if game.striker_id not in game.picks:
        game.picks[game.striker_id] = random.choice(PICK_NUMBERS)
    if game.current_bowler_id not in game.picks:
        game.picks[game.current_bowler_id] = random.choice(PICK_NUMBERS)
    game.turn = "bowler"
    await _resolve_ball(context, game)


async def team_number_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = await _get_match_for_callback(query)
    if not game:
        return
    _mid, _action, num_str = _parse_cb(query.data)

    if game.stage != "ball_wait":
        await query.answer("No active ball right now.", show_alert=True)
        return

    user_id = query.from_user.id
    if user_id not in (game.striker_id, game.current_bowler_id):
        await query.answer("You're not the striker or bowler for this ball!", show_alert=True)
        return

    expected_id = game.striker_id if game.turn == "batter" else game.current_bowler_id
    if user_id != expected_id:
        hint = "Wait for the striker to pick first!" if game.turn == "batter" else "Wait, it's the bowler's turn now!"
        await query.answer(hint, show_alert=True)
        return

    async with game.lock:
        if game.stage != "ball_wait" or user_id != (game.striker_id if game.turn == "batter" else game.current_bowler_id):
            await query.answer("No active ball right now.", show_alert=True)
            return

        num = int(num_str)
        game.picks[user_id] = num

        if game.turn == "batter":
            _bump_token(game)
            game.turn = "bowler"
            await query.answer(f"You picked {num}. Bowler's turn now.")
            await _render_bowler_pick_number(context, game)
            token = _bump_token(game)
            _schedule_timeout(context, game, token, _timeout_ball_wait)
        else:
            _cancel_timer(game)
            await query.answer(f"You picked {num}. Ball resolving...")
            await _resolve_ball(context, game)


async def _resolve_ball(context, game: TeamMatch):
    striker_id = game.striker_id
    bowler_id = game.current_bowler_id
    bat_side = game.batting_side

    striker_pick = game.picks.get(striker_id, 0)
    bowler_pick = game.picks.get(bowler_id, 0)
    # Same fairness rule as /cricket: matching secret numbers = wicket.
    # Otherwise the striker's picked number is the runs scored this ball.
    outcome = "W" if striker_pick == bowler_pick else striker_pick

    game.picks = {}
    game.turn = "batter"
    game.balls_this_over += 1
    game.team_balls[bat_side] += 1

    striker_stats = game.batting_stats[striker_id]
    bowler_stats = game.bowling_stats[bowler_id]
    striker_stats.balls += 1
    bowler_stats.balls_bowled += 1

    is_wicket = outcome == "W"
    last_ball_text = ""
    dismissal = None

    if is_wicket:
        dismissal = _roll_dismissal()
        striker_stats.is_out = True
        striker_stats.dismissal = dismissal
        bowler_stats.wickets += 1
        game.team_wickets[bat_side] += 1
        last_ball_text = f"❌ <b>WICKET!</b> {_tag(_striker_name(game))} is OUT ({dismissal})"
    else:
        runs = outcome
        game.team_scores[bat_side] += runs
        striker_stats.runs += runs
        bowler_stats.runs_conceded += runs
        if runs == 4:
            striker_stats.fours += 1
            last_ball_text = "🔥 <b>FOUR!</b>"
        elif runs == 6:
            striker_stats.sixes += 1
            last_ball_text = "💥 <b>SIX!</b>"
        elif runs == 0:
            last_ball_text = "⚪ Dot ball."
        else:
            last_ball_text = f"➕ {runs} run(s)."

    game.ball_history.setdefault(game.innings, []).append(RUN_EMOJI[outcome])

    # Delivery identity (captured BEFORE the over counters roll over below).
    ball_over_no = game.overs_completed + 1
    ball_no = game.balls_this_over

    # --- wicket / all-out handling (dynamic N-1 rule) -------------------
    all_out = False
    if is_wicket:
        team_size = len(game.lineup)
        if game.team_wickets[bat_side] >= team_size - 1:
            all_out = True
        else:
            game.striker_id = game.lineup[game.next_in_idx]
            game.next_in_idx += 1
    else:
        # Strike changes on ODD runs (1, 3, 5) — not on 0/2/4/6.
        if outcome % 2 == 1 and game.non_striker_id is not None:
            game.striker_id, game.non_striker_id = game.non_striker_id, game.striker_id

    # --- end-of-over: bowler rotates, strike also changes ends ----------
    over_completed = game.balls_this_over >= BALLS_PER_OVER
    if over_completed:
        game.balls_this_over = 0
        game.overs_completed += 1
        game.last_over_bowler_id = bowler_id
        if game.non_striker_id is not None and not all_out:
            game.striker_id, game.non_striker_id = game.non_striker_id, game.striker_id

    # --- innings/match end checks ----------------------------------------
    total = game.team_scores[bat_side]
    target_reached = game.target is not None and total >= game.target
    overs_used_up = game.team_balls[bat_side] >= game.overs * BALLS_PER_OVER
    innings_over = all_out or target_reached or overs_used_up

    # --- persist the delivery atomically (event + batter + bowler + live state)
    if game.db_match_id:
        try:
            db.commit_team_ball(
                game.db_match_id, game.innings, ball_over_no, ball_no,
                striker_id, bowler_id, str(outcome), dismissal,
                {
                    "runs": striker_stats.runs, "balls": striker_stats.balls,
                    "fours": striker_stats.fours, "sixes": striker_stats.sixes,
                    "is_out": striker_stats.is_out, "dismissal": striker_stats.dismissal,
                },
                {
                    "overs": round(bowler_stats.balls_bowled / BALLS_PER_OVER, 1),
                    "runs_conceded": bowler_stats.runs_conceded, "wickets": bowler_stats.wickets,
                },
                _live_state(
                    game,
                    "innings_end" if innings_over else ("bowler_pick" if over_completed else "ball_wait"),
                ),
            )
        except Exception:
            logger.exception("Could not persist ball for match %s", game.match_id)

    if target_reached and not is_wicket:
        last_ball_text += f"\n\n🎯 <b>Target achieved!</b> {_tag(_side_obj(game, bat_side).label)} wins! 🏆"
    elif all_out:
        last_ball_text += f"\n\n🏏 <b>All out!</b> {_tag(_side_obj(game, bat_side).label)} innings ends."
    elif overs_used_up:
        last_ball_text += f"\n\n⏱️ <b>Overs complete!</b> {_tag(_side_obj(game, bat_side).label)} innings ends."

    if innings_over and game.innings == 1:
        await _end_first_innings(context, game, last_ball_text)
        return

    if innings_over and game.innings == 2:
        await context.bot.edit_message_text(
            chat_id=game.chat_id, message_id=game.message_id,
            text=_scoreboard_block(game, last_ball_text), parse_mode="HTML",
        )
        await _finish_match(context, game)
        return

    # Ball continues — need a new bowler if the over just completed.
    if over_completed:
        game.stage = "bowler_pick"
        token = _bump_token(game)
        await context.bot.edit_message_text(
            chat_id=game.chat_id, message_id=game.message_id,
            text=_scoreboard_block(game, last_ball_text), parse_mode="HTML",
        )
        await _render_bowler_pick(context, game)
        _schedule_timeout(context, game, token, _timeout_bowler_pick)
        return

    game.stage = "ball_wait"
    game.turn = "batter"
    token = _bump_token(game)
    text = _scoreboard_block(game, last_ball_text) + f"\n\n{_tag(_striker_name(game))} — pick your secret number! 🏏"
    await context.bot.edit_message_text(
        chat_id=game.chat_id, message_id=game.message_id,
        text=text, parse_mode="HTML", reply_markup=_pick_keyboard(game),
    )
    _schedule_timeout(context, game, token, _timeout_ball_wait)


# ---------------------------------------------------------------------------
# Innings transition / match end
# ---------------------------------------------------------------------------

async def _end_first_innings(context, game: TeamMatch, last_ball_text: str):
    total = game.team_scores[game.batting_side]
    game.target = total + 1
    game.innings = 2

    first_innings_card = _innings_scorecard_text(game, game.batting_side)
    summary = (
        _scoreboard_block(game, last_ball_text)
        + "\n\n"
        + first_innings_card
        + f"\n\n🎯 <b>Target for {_tag(_side_obj(game, game.bowling_side).label)}:</b> {game.target}"
    )
    await context.bot.edit_message_text(
        chat_id=game.chat_id, message_id=game.message_id, text=summary, parse_mode="HTML",
    )

    new_batting_side = game.bowling_side
    await _begin_innings(context, game, new_batting_side)


def _innings_scorecard_text(game: TeamMatch, side: str) -> str:
    team = _side_obj(game, side)
    lines = [f"🟦 <b>{_esc(team.label)} BATTING</b>"]
    for uid, name in _side_all_players(game, side):
        st = game.batting_stats.get(uid)
        if not st or (st.balls == 0 and not st.is_out):
            continue
        status = st.dismissal if st.is_out else "Not Out"
        extra = []
        if st.fours:
            extra.append(f"{st.fours}x4")
        if st.sixes:
            extra.append(f"{st.sixes}x6")
        extra_text = f" — {' '.join(extra)}" if extra else ""
        lines.append(f"• {_tag(name)} — {st.runs} ({st.balls}) {extra_text} [{status}]")

    bowl_team = _side_obj(game, _other_side(side))
    bowler_lines = []
    for uid, name in _side_all_players(game, _other_side(side)):
        bw = game.bowling_stats.get(uid)
        if not bw or bw.balls_bowled == 0:
            continue
        bowler_lines.append(
            f"• {_tag(name)} — {bw.overs_display} ov, {bw.runs_conceded} R, {bw.wickets} W (Econ {bw.economy})"
        )
    if bowler_lines:
        lines.append("")
        lines.append(f"🎯 <b>{_esc(bowl_team.label)} BOWLING</b>")
        lines.extend(bowler_lines)

    return "\n".join(lines)


def _compute_player_of_match(game: TeamMatch) -> tuple[Optional[int], Optional[str]]:
    best_uid, best_score, best_name = None, -1.0, None
    all_players = _side_all_players(game, "a") + _side_all_players(game, "b")
    for uid, name in all_players:
        bat = game.batting_stats.get(uid, BattingStats())
        bowl = game.bowling_stats.get(uid, BowlingStats())
        score = bat.runs * 1.0 + bat.fours * 0.5 + bat.sixes * 1.0 + bowl.wickets * 20.0
        if bowl.wickets > 0 and bowl.economy:
            score += max(0.0, (8 - bowl.economy))  # reward economical bowling
        if score > best_score:
            best_score, best_uid, best_name = score, uid, name
    return best_uid, best_name


def _plain(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text or ""))


def _result_card_data(game: TeamMatch, potm_uid, potm_name, mkey) -> dict:
    """Plain-data description of the match for resultcard.render_result_card."""
    teams = {}
    for side in ("a", "b"):
        players = _side_all_players(game, side)
        bats = [(n, game.batting_stats.get(u)) for u, n in players]
        bats = [(n, b) for n, b in bats if b and b.balls > 0]
        bowls = [(n, game.bowling_stats.get(u)) for u, n in players]
        bowls = [(n, w) for n, w in bowls if w and w.balls_bowled > 0]
        best_bat = max(bats, key=lambda x: (x[1].runs, -x[1].balls), default=None)
        best_bowl = max(bowls, key=lambda x: (x[1].wickets, -x[1].runs_conceded), default=None)
        balls = game.team_balls[side]
        teams[side] = {
            "label": _side_obj(game, side).label,
            "score": game.team_scores[side],
            "wickets": game.team_wickets[side],
            "overs": f"{balls // BALLS_PER_OVER}.{balls % BALLS_PER_OVER}",
            "bat": (best_bat[0], best_bat[1].runs, best_bat[1].balls) if best_bat else None,
            "bowl": (best_bowl[0], best_bowl[1].wickets, best_bowl[1].runs_conceded) if best_bowl else None,
        }
    potm = None
    if potm_uid is not None:
        pb = game.batting_stats.get(potm_uid, BattingStats())
        pw = game.bowling_stats.get(potm_uid, BowlingStats())
        bits = [f"{pb.runs} runs ({pb.balls}b)"]
        if pb.fours or pb.sixes:
            bits.append(f"{pb.fours}x4 {pb.sixes}x6")
        if pw.wickets:
            bits.append(f"{pw.wickets} wkts")
        potm = {"name": potm_name, "line": " • ".join(bits)}
    return {
        "key": mkey, "overs": game.overs, "result": _plain(game.result_text),
        "winner": game.winner_side, "teams": teams, "potm": potm,
    }


async def _send_result_card(context, game: TeamMatch, potm_uid, potm_name, mkey):
    """Shareable image card. Purely cosmetic: any failure is logged and ignored."""
    try:
        from resultcard import render_result_card
        png = await asyncio.to_thread(
            render_result_card, _result_card_data(game, potm_uid, potm_name, mkey), os.environ.get("BOT_USERNAME"),
        )
        caption = f"{game.result_text}\n\n🔑 Match Key: <code>{mkey}</code>"
        await context.bot.send_photo(chat_id=game.chat_id, photo=png, caption=caption, parse_mode="HTML")
    except Exception:
        logger.exception("Could not send result card for match %s", game.match_id)


async def _finish_match(context, game: TeamMatch):
    score_a, wkts_a = game.team_scores["a"], game.team_wickets["a"]
    score_b, wkts_b = game.team_scores["b"], game.team_wickets["b"]
    overs_a = round(game.team_balls["a"] / BALLS_PER_OVER, 1)
    overs_b = round(game.team_balls["b"] / BALLS_PER_OVER, 1)

    if score_a > score_b:
        game.winner_side = "a"
        margin = score_a - score_b
        game.result_text = f"🏆 {_tag(game.team_a.label)} won by {margin} run(s)!"
    elif score_b > score_a:
        game.winner_side = "b"
        wickets_in_hand = len(game.team_b.players) - 1 - wkts_b
        game.result_text = f"🏆 {_tag(game.team_b.label)} won by {max(wickets_in_hand, 0)} wicket(s)!"
    else:
        game.winner_side = None
        game.result_text = "🤝 <b>MATCH TIED!</b>"

    potm_uid, potm_name = _compute_player_of_match(game)

    lines = [
        "🏆 <b>MATCH RESULT</b>",
        "",
        _innings_scorecard_text(game, "a"),
        "",
        _innings_scorecard_text(game, "b"),
        "",
        f"🟦 {_tag(game.team_a.label)}: {score_a}/{wkts_a} ({overs_a} ov)",
        f"🟥 {_tag(game.team_b.label)}: {score_b}/{wkts_b} ({overs_b} ov)",
        "",
        game.result_text,
    ]
    if potm_uid is not None:
        pm_bat = game.batting_stats.get(potm_uid, BattingStats())
        pm_bowl = game.bowling_stats.get(potm_uid, BowlingStats())
        lines.append("")
        lines.append("🏆 <b>PLAYER OF THE MATCH</b>")
        lines.append(f"👑 {_tag(potm_name)}")
        detail = [f"{pm_bat.runs} runs ({pm_bat.balls}b)"]
        if pm_bowl.wickets:
            detail.append(f"{pm_bowl.wickets} wkts")
        lines.append(", ".join(detail))

    mkey = None
    if game.db_match_id:
        mkey = db.get_team_match_key(game.db_match_id) or game.db_match_id
        lines += ["", f"🔑 Match Key: <code>{mkey}</code> — full ball-by-ball: <code>/history {mkey}</code>"]
    await _send_result_card(context, game, potm_uid, potm_name, mkey)
    await context.bot.send_message(chat_id=game.chat_id, text="\n".join(lines), parse_mode="HTML")

    if game.db_match_id:
        db.finish_team_match(
            game.db_match_id, score_a, wkts_a, overs_a, score_b, wkts_b, overs_b,
            html.unescape(game.result_text).replace("<b>", "").replace("</b>", ""),
            game.winner_side, potm_uid, potm_name,
        )
        for side, team in (("a", game.team_a), ("b", game.team_b)):
            if game.winner_side is None:
                outcome = "tie"
            elif side == game.winner_side:
                outcome = "win"
            else:
                outcome = "loss"
            for uid, pname in _side_all_players(game, side):
                bat = game.batting_stats.get(uid, BattingStats())
                bowl = game.bowling_stats.get(uid, BowlingStats())
                if uid in {u for u, _n, _s in game.former} and not (bat.balls or bowl.balls_bowled):
                    continue  # replaced before doing anything -> no career entry
                db.upsert_team_player_stats(
                    game.chat_id, uid, pname, outcome,
                    bat.runs, bowl.wickets, bowl.runs_conceded, uid == potm_uid,
                )

    game.stage = "done"
    _bump_token(game)  # invalidate any pending timeout for this match
    _cancel_timer(game)
    TEAM_MATCHES.pop(game.message_id, None)



# ---------------------------------------------------------------------------
# Substitutes  (/sub — reply to the new player's message)
# ---------------------------------------------------------------------------
#
# Flow: captain (or match creator) replies to the incoming player's message
# with /sub -> picks WHO is replaced -> the incoming player taps Accept.
# The incoming player takes over the slot: batting position / crease spot /
# bowling duty. The replaced player's runs, balls and wickets stay with HIM
# (scorecard + /history show both); the newcomer starts from zero. Team size
# is unchanged, so the all-out rule is unchanged.

SUB_STAGES = ("order_pick", "bowler_pick", "ball_wait")
_SUB_CB_RE = re.compile(r"sub:(\d+):(?:(p|ok):(\d+):(\d+)|(x))")


def _sub_games_for(chat_id: int, user_id: int) -> list[TeamMatch]:
    return [
        g for g in TEAM_MATCHES.values()
        if g.chat_id == chat_id and g.db_match_id and g.stage != "lobby"
        and user_id in (g.creator_id, g.team_a.captain_id, g.team_b.captain_id)
    ]


def _is_in_any_match(user_id: int, chat_id: int) -> bool:
    return any(
        g.chat_id == chat_id and (g.team_a.has(user_id) or g.team_b.has(user_id))
        for g in TEAM_MATCHES.values()
    )


def _sub_authorized(game: TeamMatch, user_id: int, side: str) -> bool:
    return user_id == game.creator_id or user_id == _side_obj(game, side).captain_id


def _sub_block_reason(game: TeamMatch, side: str, out_uid: int) -> Optional[str]:
    """Why this substitution can't happen right now (None = fine)."""
    if game.stage not in SUB_STAGES:
        return "Substitutes can only be made after the toss, between balls."
    if game.subs_used[side] >= TEAM_MAX_SUBS:
        return f"{_side_obj(game, side).label} has used all {TEAM_MAX_SUBS} substitutions."
    if game.stage == "ball_wait" and game.picks and out_uid in (game.striker_id, game.current_bowler_id):
        return "That player is in the middle of a ball — wait for it to finish."
    return None


def _apply_substitution(game: TeamMatch, side: str, out_uid: int, in_uid: int, in_name: str):
    team = _side_obj(game, side)
    out_name = team.name_of(out_uid)
    team.players = [(in_uid, in_name) if uid == out_uid else (uid, n) for uid, n in team.players]
    became_captain = team.captain_id == out_uid
    if became_captain:
        team.captain_id = in_uid
    game.former.append((out_uid, out_name, side))
    game.subs_used[side] += 1
    game.batting_stats[in_uid] = BattingStats()
    game.bowling_stats[in_uid] = BowlingStats()

    # Batting: take over the order slot (only if it is still to come, or he is at the crease).
    for i, uid in enumerate(game.lineup):
        if uid == out_uid and (i >= game.next_in_idx or out_uid in (game.striker_id, game.non_striker_id)):
            game.lineup[i] = in_uid
    game.order_pick_pool = [in_uid if uid == out_uid else uid for uid in game.order_pick_pool]
    if game.striker_id == out_uid:
        game.striker_id = in_uid
    if game.non_striker_id == out_uid:
        game.non_striker_id = in_uid
    # Bowling: take over the current over.
    if game.current_bowler_id == out_uid:
        game.current_bowler_id = in_uid
    return out_name, became_captain


async def _rerender_after_sub(context, game: TeamMatch):
    if game.stage == "order_pick":
        await _render_order_pick(context, game)
    elif game.stage == "bowler_pick":
        await _render_bowler_pick(context, game)
    elif game.stage == "ball_wait":
        if game.turn == "batter":
            await _render_striker_pick(context, game)
        else:
            await _render_bowler_pick_number(context, game)


async def sub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat, msg, user = update.effective_chat, update.message, update.effective_user
    if chat.type not in ("group", "supergroup"):
        await msg.reply_text("🔁 /sub works inside groups, during a /teamcricket match.")
        return
    if is_banned(user.id):
        await msg.reply_text("🚫 You are banned from playing UltimateCric.")
        return
    games = _sub_games_for(chat.id, user.id)
    if not games:
        await msg.reply_text("Only a team captain (or the match creator) of a running Team Cricket match can use /sub.")
        return
    game = games[0]

    rep = msg.reply_to_message
    if not rep or not rep.from_user:
        await msg.reply_text("↩️ Reply to the <b>new player's message</b> with /sub to bring them in.", parse_mode="HTML")
        return
    inc = rep.from_user
    if inc.is_bot:
        await msg.reply_text("🤖 Bots can't play cricket!")
        return
    if is_banned(inc.id):
        await msg.reply_text("🚫 That user is banned from playing UltimateCric.")
        return
    if _is_in_any_match(inc.id, chat.id):
        await msg.reply_text("That player is already playing in a match here.")
        return
    if inc.id in {u for u, _n, _s in game.former}:
        await msg.reply_text("That player was already substituted out of this match and can't come back.")
        return
    if game.stage not in SUB_STAGES:
        await msg.reply_text("Substitutes can only be made after the toss, between balls. Try again in a moment.")
        return

    sides = [sd for sd in ("a", "b") if _sub_authorized(game, user.id, sd)]
    rows, row = [], []
    for sd in sides:
        if game.subs_used[sd] >= TEAM_MAX_SUBS:
            continue
        icon = "🟦" if sd == "a" else "🟥"
        for uid, name in _side_obj(game, sd).players:
            row.append(InlineKeyboardButton(f"{icon} {name}"[:30], callback_data=f"sub:{game.match_id}:p:{uid}:{inc.id}"))
            if len(row) == 2:
                rows.append(row); row = []
    if row:
        rows.append(row)
    if not rows:
        await msg.reply_text(f"All {TEAM_MAX_SUBS} substitutions have been used.")
        return
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"sub:{game.match_id}:x")])
    await msg.reply_text(
        f"🔁 <b>SUBSTITUTION</b>\n\nNew player: {_tag(_name(inc))}\n"
        f"Who will {_tag(_name(inc))} replace?  <i>({TEAM_MAX_SUBS} subs per team)</i>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows),
    )


async def sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    m = _SUB_CB_RE.fullmatch(q.data or "")
    if not m:
        await q.answer("Invalid action.", show_alert=True)
        return
    game = TEAM_MATCHES.get(int(m.group(1)))
    if not game or game.chat_id != q.message.chat.id or game.stage in ("lobby", "done", "cancelled"):
        await q.answer("This match is no longer running.", show_alert=True)
        return
    uid = q.from_user.id

    async def edit_prompt(text, markup=None):
        try:
            await q.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        except TelegramError:
            logger.info("Could not edit substitution prompt", exc_info=True)

    if m.group(5) == "x":                                   # cancel
        if uid not in (game.creator_id, game.team_a.captain_id, game.team_b.captain_id):
            await q.answer("Only the creator or a captain can cancel this.", show_alert=True)
            return
        await q.answer("Cancelled.")
        await edit_prompt("🔁 Substitution cancelled.")
        return

    step, out_uid, in_uid = m.group(2), int(m.group(3)), int(m.group(4))
    side = "a" if game.team_a.has(out_uid) else "b" if game.team_b.has(out_uid) else None
    if side is None:
        await q.answer("That player is no longer in the match.", show_alert=True)
        return

    if step == "p":                                         # captain picked who goes out
        if not _sub_authorized(game, uid, side):
            await q.answer(f"Only {_side_obj(game, side).label}'s captain (or the creator) can do this.", show_alert=True)
            return
        reason = _sub_block_reason(game, side, out_uid)
        if reason:
            await q.answer(reason, show_alert=True)
            return
        if _is_in_any_match(in_uid, game.chat_id):
            await q.answer("That player is already in a match.", show_alert=True)
            return
        out_name = _side_obj(game, side).name_of(out_uid)
        await q.answer("Waiting for the new player to accept…")
        await edit_prompt(
            f"🔁 <b>SUBSTITUTION</b>\n\n{_tag(out_name)} → <b>new player</b> in {_esc(_side_obj(game, side).label)}.\n"
            f"The new player must tap ✅ Accept to join.",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Accept & Join", callback_data=f"sub:{game.match_id}:ok:{out_uid}:{in_uid}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"sub:{game.match_id}:x"),
            ]]),
        )
        return

    # step == "ok": only the incoming player can accept
    if uid != in_uid:
        await q.answer("Only the new player can accept this.", show_alert=True)
        return
    if is_banned(uid):
        await q.answer("🚫 You are banned from playing UltimateCric.", show_alert=True)
        return
    async with game.lock:
        if game.stage in ("lobby", "done", "cancelled") or not _side_obj(game, side).has(out_uid):
            await q.answer("This substitution is no longer valid.", show_alert=True)
            return
        reason = _sub_block_reason(game, side, out_uid)
        if reason:
            await q.answer(reason, show_alert=True)
            return
        if _is_in_any_match(uid, game.chat_id) or uid in {u for u, _n, _s in game.former}:
            await q.answer("You can't join this match.", show_alert=True)
            return

        legal = game.team_balls.get(game.batting_side, 0) if game.batting_side else 0
        in_name = _name(q.from_user)
        out_name, became_captain = _apply_substitution(game, side, out_uid, uid, in_name)
        try:
            db.record_team_substitution(
                game.db_match_id, game.innings, legal, side, out_uid, uid, in_name, became_captain,
            )
        except Exception:
            logger.exception("Could not persist substitution for match %s", game.match_id)
        _persist_live(context, game)

        await q.answer("You're in! 🏏")
        await edit_prompt(
            f"✅ <b>SUBSTITUTION</b>\n\n{_tag(in_name)} replaces {_tag(out_name)} in "
            f"{_esc(_side_obj(game, side).label)}"
            + (" and is the new captain 👑" if became_captain else "")
            + f".\n<i>{out_name}'s runs/wickets so far stay on the scorecard.</i>"
        )
        try:
            await _rerender_after_sub(context, game)
        except TelegramError:
            logger.exception("Could not refresh match message after substitution")

# ---------------------------------------------------------------------------
# /teamleaderboard, /teammatches, /teamhelp
# ---------------------------------------------------------------------------

async def team_leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    rows = db.get_team_leaderboard(chat.id, limit=10)
    if not rows:
        await update.message.reply_text("No Team Cricket matches played in this group yet. Try /teamcricket!")
        return

    lines = ["🏆 <b>TEAM CRICKET LEADERBOARD</b>", ""]
    for i, row in enumerate(rows, 1):
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
        lines.append(
            f"{medal} {_tag(row['name'])} — {row['wins']}W {row['losses']}L {row['ties']}T "
            f"({row['runs']} runs, {row['wickets']} wkts)"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def team_matches_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    rows = db.get_recent_team_matches(chat.id, limit=5)
    if not rows:
        await update.message.reply_text("No completed Team Cricket matches in this group yet.")
        return

    lines = ["📜 <b>RECENT TEAM CRICKET MATCHES</b>", ""]
    for row in rows:
        lines.append(
            f"🔑 <code>{row['match_key'] or row['match_id']}</code> • {_tag(row['team_a_label'])} {row['team_a_score']}/{row['team_a_wickets']} vs "
            f"{_tag(row['team_b_label'])} {row['team_b_score']}/{row['team_b_wickets']}\n"
            f"  {_esc(row['result'] or '')}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def team_help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🏏 <b>TEAM CRICKET HELP</b>\n\n"
        "• Create a match with /teamcricket\n"
        f"• Minimum {TEAM_MIN_PLAYERS} players per team\n"
        f"• Maximum {TEAM_MAX_PLAYERS} players per team\n"
        f"• {TEAM_OVERS} overs per innings by default — choose {TEAM_OVERS_MIN}-{TEAM_OVERS_MAX} in the lobby or with /teamcricket &lt;overs&gt;\n"
        f"• {BALLS_PER_OVER} legal balls per over\n"
        "• Runs: 0–6 (5 runs supported!)\n"
        "• Real strike rotation (odd runs + end of over)\n"
        "• Wickets and dynamic all-out (N-1 wickets)\n"
        "• Toss, batting order, and bowler selection by captains\n"
        "• Live scoreboard, scorecards, and Player of the Match\n"
        f"• /sub — replace a player mid-match (captain: reply to the new player's message; max {TEAM_MAX_SUBS} per team)\n"
        "• /history &lt;key&gt; — full ball-by-ball history of a match\n"
        "• /teamleaderboard — top players in this group\n"
        "• /teammatches — recent matches in this group"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ---------------------------------------------------------------------------
# Error isolation wrapper — never let a bad callback crash the bot
# ---------------------------------------------------------------------------

def safe_callback(handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await handler(update, context)
        except Exception:
            logger.exception("Team Cricket callback failed: %s", handler.__name__)
            try:
                await update.callback_query.answer(
                    "Something went wrong with that action. Please try again.", show_alert=True
                )
            except Exception:
                pass
    wrapped.__name__ = handler.__name__
    return wrapped


team_join_callback = safe_callback(team_join_callback)
team_overs_callback = safe_callback(team_overs_callback)
team_leave_callback = safe_callback(team_leave_callback)
team_cancel_callback = safe_callback(team_cancel_callback)
team_start_callback = safe_callback(team_start_callback)
team_toss_call_callback = safe_callback(team_toss_call_callback)
team_batbowl_callback = safe_callback(team_batbowl_callback)
team_order_pick_callback = safe_callback(team_order_pick_callback)
team_bowler_pick_callback = safe_callback(team_bowler_pick_callback)
team_number_callback = safe_callback(team_number_callback)
sub_callback = safe_callback(sub_callback)
