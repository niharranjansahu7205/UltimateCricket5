"""
Core hand-cricket game logic and Telegram handlers for UltimateCricBot.

Game state model
----------------
Every match is identified by its own Telegram message_id (globally unique
per chat), NOT by chat_id. This means any number of matches can run at the
same time — including multiple matches inside the SAME group at once.

GAMES: dict[int, dict]   # message_id -> game state

Turn order per ball:
 1. Batter picks a number first (bowler can't pick yet)
 2. Once batter picks, the SAME message is edited to prompt the bowler
 3. Bowler picks -> ball resolves -> the SAME message is edited with the result
    AND the next ball's number-picker in one go (result stays visible until
    the next pick is made — no separate "new message" spam, no flicker)

Match rules:
 - 1st innings: batting continues ball after ball, over after over, with
   NO over limit. It only ends when the batter is OUT (numbers match).
 - 2nd innings (chasing): ends either on OUT, or the INSTANT the batter's
   score reaches/passes the target — that player wins immediately, no need
   to get out.
 - Over display uses proper cricket notation: 0.1, 0.2 ... 0.5, 1.0, 1.1 ...

Modes:
 - "default": normal 1-6 picks
 - "1-3": only numbers 1,2,3 are valid
 - "no5": numbers 1-6 except 5
"""

import asyncio
import logging
import os
import random

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import record_match_result, is_banned

logger = logging.getLogger(__name__)
from style import (
    MEDIA,
    header,
    out_line,
    over_line,
    player_tag,
    result_card,
    scoreboard_line,
    target_line,
)

# message_id -> game state. Using message_id as the key (instead of chat_id)
# is what allows unlimited simultaneous matches, even within one group.
GAMES: dict[int, dict] = {}

BALLS_PER_OVER = 6  # used only for over-notation display (e.g. 1.0, 1.1...), not an innings limit

MODE_NUMBERS = {
    "default": [1, 2, 3, 4, 5, 6],
    "1-3": [1, 2, 3],
    "no5": [1, 2, 3, 4, 6],
}

MODE_LABELS = {
    "default": "Default",
    "1-3": "1-3 Mode",
    "no5": "No 5 Mode",
}


def _new_game(creator_id: int, creator_name: str, chat_id: int) -> dict:
    return {
        "creator_id": creator_id,
        "creator_name": creator_name,
        "opponent_id": None,
        "opponent_name": None,
        "mode": "default",
        "stage": "awaiting_join",  # awaiting_join -> toss -> bat_bowl -> innings1 -> innings2 -> done
        "toss_winner_id": None,
        "batter_id": None,
        "bowler_id": None,
        "innings": 1,
        "ball_no": 0,
        "score": {},  # user_id -> runs
        "picks": {},  # user_id -> chosen number (cleared each ball)
        "turn": "batter",  # "batter" or "bowler" - whose turn it is to pick THIS ball
        "target": None,
        "message_id": None,  # single message that gets edited throughout the match
        "has_media": False,  # True once the message has been converted to a GIF+caption
        "chat_id": chat_id,
    }


def _number_keyboard(mode: str) -> InlineKeyboardMarkup:
    nums = MODE_NUMBERS[mode]
    row1 = nums[: (len(nums) + 1) // 2]
    row2 = nums[(len(nums) + 1) // 2:]
    kb = [
        [InlineKeyboardButton(str(n), callback_data=f"num:{n}") for n in row1],
    ]
    if row2:
        kb.append([InlineKeyboardButton(str(n), callback_data=f"num:{n}") for n in row2])
    return InlineKeyboardMarkup(kb)


def _name(user) -> str:
    return user.first_name or user.username or "Player"


def _format_over(ball_no: int) -> str:
    """Convert a raw ball count into cricket over notation.
    6 balls bowled = completed over -> shown as the next whole number (e.g. 1.0),
    not 0.6. E.g. 0 -> 0.0, 1 -> 0.1, ... 5 -> 0.5, 6 -> 1.0, 7 -> 1.1, ...
    """
    completed_overs, balls_in_current_over = divmod(ball_no, BALLS_PER_OVER)
    if balls_in_current_over == 0 and ball_no > 0:
        return f"{completed_overs}.0"
    return f"{completed_overs}.{balls_in_current_over}"


def _names(game):
    return {game["creator_id"]: game["creator_name"], game["opponent_id"]: game["opponent_name"]}


async def _edit_game_message(context, game, text, reply_markup=None):
    """Update this match's message with plain text (no media).
    If the message is currently showing media (from a previous GIF ball),
    we can't turn it back into a text-only message, so we edit its caption
    instead — the media just stays there under the new text."""
    if game.get("has_media"):
        await context.bot.edit_message_caption(
            chat_id=game["chat_id"],
            message_id=game["message_id"],
            caption=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
    else:
        await context.bot.edit_message_text(
            chat_id=game["chat_id"],
            message_id=game["message_id"],
            text=text,
            parse_mode="HTML",
            reply_markup=reply_markup,
        )


async def _edit_game_message_with_media(context, game, text, media_key, reply_markup=None):
    """Update this match's message, combining the result text and the
    shot/wicket animation into a SINGLE message (GIF + caption together).
    Falls back to a plain text edit if the media file isn't available or
    if the text is too long for a Telegram caption (1024 char limit)."""
    from telegram import InputMediaAnimation

    path = MEDIA.get(media_key)
    if not path or not os.path.isfile(path) or len(text) > 1024:
        await _edit_game_message(context, game, text, reply_markup=reply_markup)
        return

    # Mark has_media BEFORE the call: if this request times out locally but
    # actually succeeded on Telegram's side, the message is now media-type
    # regardless — so any retry/fallback must target it as media, not text.
    was_media_before = game.get("has_media", False)
    game["has_media"] = True

    try:
        with open(path, "rb") as f:
            await context.bot.edit_message_media(
                chat_id=game["chat_id"],
                message_id=game["message_id"],
                media=InputMediaAnimation(media=f, caption=text, parse_mode="HTML"),
                reply_markup=reply_markup,
            )
    except Exception:
        # The edit_message_media call failed/timed out. We can't be sure
        # whether Telegram applied it anyway, so try a caption edit (works
        # if it's now media) and only fall back to a fresh text edit if
        # that also fails AND we know for certain it was text before.
        try:
            await context.bot.edit_message_caption(
                chat_id=game["chat_id"],
                message_id=game["message_id"],
                caption=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except Exception:
            game["has_media"] = was_media_before
            await _edit_game_message(context, game, text, reply_markup=reply_markup)


async def cricket_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "🏏 UltimateCric only works inside <b>groups</b>! Add me to a group to play.",
            parse_mode="HTML",
        )
        return

    user = update.effective_user
    if is_banned(user.id):
        await update.message.reply_text("🚫 You are banned from playing UltimateCric.")
        return

    game = _new_game(user.id, _name(user), chat.id)

    text = (
        header("🏏 UltimateCric Match")
        + f"A new match has been started by {player_tag(_name(user))}!\n\n"
        + "Press <b>Join</b> below to accept the challenge."
    )
    kb = [
        [InlineKeyboardButton("✅ Join Match", callback_data="join:accept")],
        [
            InlineKeyboardButton("1-3 Mode", callback_data="mode:1-3"),
            InlineKeyboardButton("No 5 Mode", callback_data="mode:no5"),
        ],
        [InlineKeyboardButton("Default", callback_data="mode:default")],
    ]
    sent = await update.message.reply_text(
        text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb)
    )
    game["message_id"] = sent.message_id
    GAMES[sent.message_id] = game


async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = GAMES.get(query.message.message_id)
    if not game or game["stage"] != "awaiting_join":
        await query.answer("This match can no longer be configured.", show_alert=True)
        return
    if query.from_user.id != game["creator_id"]:
        await query.answer("Only the match creator can change the mode.", show_alert=True)
        return

    mode = query.data.split(":", 1)[1]
    game["mode"] = mode
    await query.answer(f"Mode set to {MODE_LABELS[mode]}")

    text = (
        header("🏏 UltimateCric Match")
        + f"A new match has been started by {player_tag(game['creator_name'])}!\n"
        + f"Mode: <b>{MODE_LABELS[mode]}</b>\n\n"
        + "Press <b>Join</b> below to accept the challenge."
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=query.message.reply_markup)


async def join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = GAMES.get(query.message.message_id)
    if not game or game["stage"] != "awaiting_join":
        await query.answer("This match is no longer accepting players.", show_alert=True)
        return

    user = query.from_user
    if user.id == game["creator_id"]:
        await query.answer("You can't join your own match!", show_alert=True)
        return

    if is_banned(user.id):
        await query.answer("🚫 You are banned from playing UltimateCric.", show_alert=True)
        return

    game["opponent_id"] = user.id
    game["opponent_name"] = _name(user)
    game["stage"] = "toss"
    game["score"] = {game["creator_id"]: 0, game["opponent_id"]: 0}

    await query.answer("You joined the match!")

    text = (
        header("🏏 UltimateCric Match")
        + f"- {player_tag(game['opponent_name'])} has joined the game!\n\n"
        + f"{player_tag(game['opponent_name'])} choose Head or Tail 🪙"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Head", callback_data="toss:head"),
                InlineKeyboardButton("Tail", callback_data="toss:tail"),
            ]
        ]
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def toss_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = GAMES.get(query.message.message_id)
    if not game or game["stage"] != "toss":
        await query.answer("Toss already done.", show_alert=True)
        return
    if query.from_user.id != game["opponent_id"]:
        await query.answer("Only the joining player calls the toss!", show_alert=True)
        return

    call = query.data.split(":", 1)[1]
    result = random.choice(["head", "tail"])
    winner_id = game["opponent_id"] if call == result else game["creator_id"]
    winner_name = game["opponent_name"] if call == result else game["creator_name"]
    game["toss_winner_id"] = winner_id
    game["stage"] = "bat_bowl"

    await query.answer(f"Toss result: {result.title()}!")

    text = (
        header("🏏 UltimateCric Match")
        + f"{player_tag(winner_name)} won the toss! 🪙\n\n"
        + f"{player_tag(winner_name)} — choose to Bat or Bowl"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🏏 Bat", callback_data="choice:bat"),
                InlineKeyboardButton("🧤 Bowl", callback_data="choice:bowl"),
            ]
        ]
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def bat_bowl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = GAMES.get(query.message.message_id)
    if not game or game["stage"] != "bat_bowl":
        await query.answer("Already decided.", show_alert=True)
        return
    if query.from_user.id != game["toss_winner_id"]:
        await query.answer("Only the toss winner decides this!", show_alert=True)
        return

    choice = query.data.split(":", 1)[1]
    winner_id = game["toss_winner_id"]
    other_id = game["opponent_id"] if winner_id == game["creator_id"] else game["creator_id"]

    if choice == "bat":
        game["batter_id"], game["bowler_id"] = winner_id, other_id
    else:
        game["batter_id"], game["bowler_id"] = other_id, winner_id

    game["stage"] = "innings1"
    game["ball_no"] = 0
    game["picks"] = {}
    game["turn"] = "batter"

    await query.answer("Let's play!")
    await _render_batter_turn(context, game)


def _build_ball_header(game, extra_lines=None):
    names = _names(game)
    over_str = _format_over(game["ball_no"])
    lines = [
        header("🏏 UltimateCric Match"),
        over_line(over_str),
        "",
        scoreboard_line(names[game["batter_id"]], names[game["bowler_id"]]),
    ]
    if game.get("target"):
        lines.append("")
        lines.append(target_line(game["target"]))
    if extra_lines:
        lines.append("")
        lines.extend(extra_lines)
    return lines


async def _render_batter_turn(context, game):
    """Prompt the batter to pick first; bowler has nothing to press yet."""
    names = _names(game)
    lines = _build_ball_header(
        game,
        extra_lines=[f"{player_tag(names[game['batter_id']])} — pick your secret number! 🏏"],
    )
    kb = _number_keyboard(game["mode"])
    await _edit_game_message(context, game, "\n".join(lines), reply_markup=kb)


async def _render_bowler_turn(context, game):
    """Batter has picked. Now prompt the bowler, same message edited."""
    names = _names(game)
    lines = _build_ball_header(
        game,
        extra_lines=[
            f"{player_tag(names[game['batter_id']])} has selected a number,\n"
            f"{player_tag(names[game['bowler_id']])} its your turn to bowl 🧤"
        ],
    )
    kb = _number_keyboard(game["mode"])
    if peek_enabled():
        # TEST MODE ONLY (TEST_PEEK=1): owners get a private "peek" button.
        rows = list(kb.inline_keyboard) + [[InlineKeyboardButton("🔍 Test: peek batter's pick", callback_data="peek")]]
        kb = InlineKeyboardMarkup(rows)
    await _edit_game_message(context, game, "\n".join(lines), reply_markup=kb)


def peek_enabled() -> bool:
    """Developer test switch. OFF unless the env var TEST_PEEK is 1/true/yes."""
    return os.environ.get("TEST_PEEK", "").strip().lower() in ("1", "true", "yes", "on")


async def peek_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows the batter's secret number to a bot OWNER only, as a private popup
    (callback alert — nothing is posted to the chat). For testing the bot.
    Disabled unless TEST_PEEK is on; regular players can never use it."""
    from admin import is_owner  # lazy import
    query = update.callback_query
    if not peek_enabled():
        await query.answer("Test mode is off.", show_alert=True)
        return
    if not is_owner(query.from_user.id):
        await query.answer("Only the bot owner can use this test button.", show_alert=True)
        return
    game = GAMES.get(query.message.message_id)
    if not game or game["stage"] not in ("innings1", "innings2") or game["turn"] != "bowler":
        await query.answer("No batter pick to show right now.", show_alert=True)
        return
    num = game["picks"].get(game["batter_id"])
    if num is None:
        await query.answer("The batter hasn't picked yet.", show_alert=True)
        return
    names = _names(game)
    logger.info("TEST_PEEK used by owner %s (batter pick %s)", query.from_user.id, num)
    await query.answer(f"🔍 TEST ONLY\nBatter {names[game['batter_id']]} picked: {num}", show_alert=True)


REVEAL_SEND_TIMEOUT = 8  # seconds; a slow Telegram call must never stall the match


def _private_group_id():
    """Chat id of the private reveal group from the PRIVATE_GROUP_ID env var.
    Returns None when it is unset/blank/invalid (reveal is then simply skipped)."""
    raw = os.environ.get("PRIVATE_GROUP_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("PRIVATE_GROUP_ID is not a valid chat id (%r); reveal disabled.", raw)
        return None


async def _reveal_batter_pick(context, game, num: int) -> None:
    """Exact Shot Reveal: post the batter's REAL pick (1-6) to the private group.

    Called once per ball, right after the batter picks and BEFORE the bowler's
    turn opens. At most one reveal per ball (guarded by a key that is set before
    any await). Never raises and never changes game state besides that key."""
    group_id = _private_group_id()
    if group_id is None:
        return
    key = (game["innings"], game["ball_no"])
    if game.get("reveal_key") == key:
        return  # already revealed (or attempted) for this ball
    game["reveal_key"] = key

    names = _names(game)
    text = (
        f"Batsman: {names[game['batter_id']]}\n"
        f"Selected Number: {num}\n"
        "⏳ Waiting for the bowler..."
    )
    try:
        await asyncio.wait_for(
            context.bot.send_message(chat_id=group_id, text=text),
            timeout=REVEAL_SEND_TIMEOUT,
        )
    except Exception:
        logger.warning("Exact Shot Reveal could not be sent to PRIVATE_GROUP_ID.", exc_info=True)


async def number_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = GAMES.get(query.message.message_id)
    if not game or game["stage"] not in ("innings1", "innings2"):
        await query.answer("No active ball right now.", show_alert=True)
        return

    user_id = query.from_user.id
    if user_id not in (game["batter_id"], game["bowler_id"]):
        await query.answer("You're not playing this match!", show_alert=True)
        return

    # Enforce strict turn order: batter picks first, then bowler.
    expected_id = game["batter_id"] if game["turn"] == "batter" else game["bowler_id"]
    if user_id != expected_id:
        if game["turn"] == "batter":
            await query.answer("Wait for the batter to pick first!", show_alert=True)
        else:
            await query.answer("Wait, it's the bowler's turn now!", show_alert=True)
        return

    num = int(query.data.split(":", 1)[1])

    # Double-tap guard: once this ball's pick has been revealed it is locked,
    # so the revealed number can never differ from the one that gets scored.
    if game["turn"] == "batter" and game.get("reveal_key") == (game["innings"], game["ball_no"]):
        await query.answer("You already picked. Waiting for the bowler.", show_alert=True)
        return

    game["picks"][user_id] = num

    if game["turn"] == "batter":
        await query.answer(f"You picked {num}. Bowler's turn now.")
        # Exact Shot Reveal: goes out BEFORE the bowler's turn opens.
        await _reveal_batter_pick(context, game, num)
        game["turn"] = "bowler"
        await _render_bowler_turn(context, game)
        return

    # Bowler just picked -> resolve the ball
    await query.answer(f"You picked {num}. Ball resolving...")
    await _resolve_ball(context, game)


async def _resolve_ball(context, game):
    names = _names(game)
    batter_id, bowler_id = game["batter_id"], game["bowler_id"]
    batter_pick = game["picks"][batter_id]
    bowler_pick = game["picks"][bowler_id]
    game["ball_no"] += 1
    game["picks"] = {}
    game["turn"] = "batter"

    over_str = _format_over(game["ball_no"])
    lines = [
        header("🏏 UltimateCric Match"),
        over_line(over_str),
        "",
        scoreboard_line(names[batter_id], names[bowler_id]),
        "",
        f"{player_tag(names[batter_id])} Bat {batter_pick}",
        f"{player_tag(names[bowler_id])} Bowl {bowler_pick}",
        "",
    ]

    is_out = batter_pick == bowler_pick

    if not is_out:
        game["score"][batter_id] = game["score"].get(batter_id, 0) + batter_pick

    total_runs = game["score"].get(batter_id, 0)

    # Which animation to play for this ball: "out" on a wicket, otherwise
    # the run value (1/2/3/4/6) — 5s have no dedicated clip.
    media_key = "out" if is_out else batter_pick

    # "Performance :" section — always shows the batter's running total
    lines.append("<b>Performance :</b>")
    lines.append(f"<i>{player_tag(names[batter_id])} Scored total of {total_runs} Runs</i>")

    if is_out:
        lines.append("")
        lines.append(out_line(names[batter_id]))

    # 1st innings ends only on OUT. 2nd innings (chasing) also ends the
    # instant the target is reached/passed — instant win, no OUT needed.
    target_reached = (
        game["innings"] == 2
        and game.get("target") is not None
        and total_runs >= game["target"]
    )
    innings_over = is_out or target_reached

    if target_reached and not is_out:
        lines.append("")
        lines.append(f"🎯 <b>Target achieved!</b> {player_tag(names[batter_id])} has won the match! 🏆")

    if innings_over and game["innings"] == 1:
        # Set target and flip roles
        game["target"] = total_runs + 1
        lines.append("")
        lines.append(target_line(game["target"]))
        lines.append("")
        lines.append("<b>Next Move :</b>")
        lines.append(
            f"{player_tag(names[bowler_id])} will now Bat, "
            f"{player_tag(names[batter_id])} will now Bowl!"
        )
        lines.append("")
        game["batter_id"], game["bowler_id"] = bowler_id, batter_id
        game["innings"] = 2
        game["ball_no"] = 0

        lines.append(f"{player_tag(names[game['batter_id']])} — pick your secret number! 🏏")
        kb = _number_keyboard(game["mode"])
        await _edit_game_message_with_media(context, game, "\n".join(lines), media_key, reply_markup=kb)
        return

    if innings_over and game["innings"] == 2:
        # Match has ended — straight to the final result.
        await _edit_game_message_with_media(context, game, "\n".join(lines), media_key)
        await _finish_match(context, game)
        return

    # Ball continues in the same innings — show "Next Move" and the number
    # keyboard for the next ball in the SAME edit, so the result stays
    # visible until the batter actually picks their next number.
    lines.append("")
    lines.append("<b>Next Move :</b>")
    lines.append(f"{player_tag(names[batter_id])} Continue your Bat!")
    lines.append("")
    lines.append(f"{player_tag(names[batter_id])} — pick your secret number! 🏏")

    kb = _number_keyboard(game["mode"])
    await _edit_game_message_with_media(context, game, "\n".join(lines), media_key, reply_markup=kb)


async def _finish_match(context, game):
    chat_id = game["chat_id"]
    names = _names(game)
    creator_score = game["score"].get(game["creator_id"], 0)
    opponent_score = game["score"].get(game["opponent_id"], 0)

    text = result_card(
        names[game["creator_id"]], creator_score, names[game["opponent_id"]], opponent_score
    )
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")

    creator_won = creator_score > opponent_score
    opponent_won = opponent_score > creator_score

    record_match_result(chat_id, game["creator_id"], names[game["creator_id"]], creator_score, creator_won)
    record_match_result(chat_id, game["opponent_id"], names[game["opponent_id"]], opponent_score, opponent_won)

    game["stage"] = "done"
    GAMES.pop(game["message_id"], None)


async def cancel_cricket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel a pending/ongoing match. If the user replies to that match's
    message, we cancel that specific one. Otherwise, if they created exactly
    one active match in this chat, we cancel that."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    target_message_id = None
    if update.message.reply_to_message:
        target_message_id = update.message.reply_to_message.message_id

    if target_message_id is not None:
        game = GAMES.get(target_message_id)
        if not game or game["chat_id"] != chat_id:
            await update.message.reply_text("That message isn't an active match.")
            return
        if game["creator_id"] != user_id:
            await update.message.reply_text("Only the match creator can cancel it.")
            return
        GAMES.pop(target_message_id, None)
        await update.message.reply_text("❌ Match cancelled.")
        return

    # No reply given — find this user's own active matches in this chat.
    own_matches = [
        mid for mid, g in GAMES.items()
        if g["chat_id"] == chat_id and g["creator_id"] == user_id
    ]

    if not own_matches:
        await update.message.reply_text(
            "You don't have any active match here. "
            "(Reply to a specific match message with /cancelcricket to cancel it.)"
        )
        return

    if len(own_matches) > 1:
        await update.message.reply_text(
            "You have multiple active matches in this group. "
            "Reply to the specific match's message with /cancelcricket to cancel that one."
        )
        return

    GAMES.pop(own_matches[0], None)
    await update.message.reply_text("❌ Match cancelled.")
