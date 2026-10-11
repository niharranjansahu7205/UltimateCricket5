"""
Tic-Tac-Toe mini-game for UltimateCricBot.

/tictac — starts a match with a Join button, same pattern as /cricket.
The person who ran /tictac gets ❌ and always moves first; whoever
presses Join gets ⭕.

Game state model mirrors game.py: every match is keyed by its own
Telegram message_id, so multiple Tic-Tac-Toe matches can run at once,
even in the same group.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from database import is_banned
from style import header, player_tag

# message_id -> game state
TTT_GAMES: dict[int, dict] = {}

WIN_LINES = [
    (0, 1, 2), (3, 4, 5), (6, 7, 8),  # rows
    (0, 3, 6), (1, 4, 7), (2, 5, 8),  # columns
    (0, 4, 8), (2, 4, 6),             # diagonals
]

CELL_LABELS = ["1", "2", "3", "4", "5", "6", "7", "8", "9"]


def _name(user) -> str:
    return user.first_name or user.username or "Player"


def _new_game(creator_id: int, creator_name: str, chat_id: int) -> dict:
    return {
        "creator_id": creator_id,
        "creator_name": creator_name,
        "opponent_id": None,
        "opponent_name": None,
        "board": [None] * 9,  # None, "X", or "O" per cell
        "turn_id": None,      # whose turn it is right now
        "stage": "awaiting_join",  # awaiting_join -> playing -> done
        "message_id": None,
        "chat_id": chat_id,
    }


def _symbol_for(game, user_id) -> str:
    return "❌" if user_id == game["creator_id"] else "⭕"


def _board_keyboard(board) -> InlineKeyboardMarkup:
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            i = r * 3 + c
            cell = board[i]
            label = cell if cell else CELL_LABELS[i]
            row.append(InlineKeyboardButton(label, callback_data=f"ttt:{i}"))
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _check_winner(board):
    for a, b, c in WIN_LINES:
        if board[a] and board[a] == board[b] == board[c]:
            return board[a]
    return None


async def tictac_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text(
            "⭕ Tic-Tac-Toe only works inside <b>groups</b>! Add me to a group to play.",
            parse_mode="HTML",
        )
        return

    user = update.effective_user
    if is_banned(user.id):
        await update.message.reply_text("🚫 You are banned from playing games with this bot.")
        return

    game = _new_game(user.id, _name(user), chat.id)

    text = (
        header("⭕ Tic-Tac-Toe")
        + f"A new match has been started by {player_tag(_name(user))}! (❌)\n\n"
        + "Press <b>Join</b> below to play as ⭕."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Join Match", callback_data="tttjoin:accept")]])
    sent = await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    game["message_id"] = sent.message_id
    TTT_GAMES[sent.message_id] = game


async def tictac_join_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = TTT_GAMES.get(query.message.message_id)
    if not game or game["stage"] != "awaiting_join":
        await query.answer("This match is no longer accepting players.", show_alert=True)
        return

    user = query.from_user
    if user.id == game["creator_id"]:
        await query.answer("You can't join your own match!", show_alert=True)
        return

    if is_banned(user.id):
        await query.answer("🚫 You are banned from playing games with this bot.", show_alert=True)
        return

    game["opponent_id"] = user.id
    game["opponent_name"] = _name(user)
    game["stage"] = "playing"
    # Creator (❌) always moves first.
    game["turn_id"] = game["creator_id"]

    await query.answer("You joined the match!")
    await _render_board(context, game, joined_text=True)


async def _render_board(context, game, joined_text=False, extra_line=None):
    turn_name = game["creator_name"] if game["turn_id"] == game["creator_id"] else game["opponent_name"]
    turn_symbol = _symbol_for(game, game["turn_id"])

    lines = [header("⭕ Tic-Tac-Toe")]
    if joined_text:
        lines.append(f"- {player_tag(game['opponent_name'])} has joined the game!\n")
    lines.append(f"❌ {player_tag(game['creator_name'])}  vs  ⭕ {player_tag(game['opponent_name'])}")
    lines.append("")
    if extra_line:
        lines.append(extra_line)
        lines.append("")
    lines.append(f"{player_tag(turn_name)}'s turn ({turn_symbol})")

    kb = _board_keyboard(game["board"])
    await context.bot.edit_message_text(
        chat_id=game["chat_id"],
        message_id=game["message_id"],
        text="\n".join(lines),
        parse_mode="HTML",
        reply_markup=kb,
    )


async def tictac_move_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    game = TTT_GAMES.get(query.message.message_id)
    if not game or game["stage"] != "playing":
        await query.answer("No active game right now.", show_alert=True)
        return

    user_id = query.from_user.id
    if user_id not in (game["creator_id"], game["opponent_id"]):
        await query.answer("You're not playing this match!", show_alert=True)
        return
    if user_id != game["turn_id"]:
        await query.answer("It's not your turn!", show_alert=True)
        return

    cell = int(query.data.split(":", 1)[1])
    if game["board"][cell] is not None:
        await query.answer("That cell is already taken!", show_alert=True)
        return

    game["board"][cell] = _symbol_for(game, user_id)
    await query.answer(f"You placed {game['board'][cell]}!")

    winner_symbol = _check_winner(game["board"])
    is_draw = winner_symbol is None and all(c is not None for c in game["board"])

    if winner_symbol:
        winner_name = game["creator_name"] if winner_symbol == "❌" else game["opponent_name"]
        lines = [
            header("⭕ Tic-Tac-Toe"),
            f"❌ {player_tag(game['creator_name'])}  vs  ⭕ {player_tag(game['opponent_name'])}",
            "",
            f"🎉 {player_tag(winner_name)} wins with {winner_symbol}! 🎉",
        ]
        await context.bot.edit_message_text(
            chat_id=game["chat_id"],
            message_id=game["message_id"],
            text="\n".join(lines),
            parse_mode="HTML",
            reply_markup=_board_keyboard(game["board"]),
        )
        game["stage"] = "done"
        TTT_GAMES.pop(game["message_id"], None)
        return

    if is_draw:
        lines = [
            header("⭕ Tic-Tac-Toe"),
            f"❌ {player_tag(game['creator_name'])}  vs  ⭕ {player_tag(game['opponent_name'])}",
            "",
            "🤝 <b>It's a DRAW!</b>",
        ]
        await context.bot.edit_message_text(
            chat_id=game["chat_id"],
            message_id=game["message_id"],
            text="\n".join(lines),
            parse_mode="HTML",
            reply_markup=_board_keyboard(game["board"]),
        )
        game["stage"] = "done"
        TTT_GAMES.pop(game["message_id"], None)
        return

    # Game continues — switch turn.
    game["turn_id"] = (
        game["opponent_id"] if user_id == game["creator_id"] else game["creator_id"]
    )
    await _render_board(context, game)


async def cancel_tictac(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel a pending/ongoing Tic-Tac-Toe match. Same reply-or-lookup
    pattern as /cancelcricket."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    target_message_id = None
    if update.message.reply_to_message:
        target_message_id = update.message.reply_to_message.message_id

    if target_message_id is not None:
        game = TTT_GAMES.get(target_message_id)
        if not game or game["chat_id"] != chat_id:
            await update.message.reply_text("That message isn't an active Tic-Tac-Toe match.")
            return
        if game["creator_id"] != user_id:
            await update.message.reply_text("Only the match creator can cancel it.")
            return
        TTT_GAMES.pop(target_message_id, None)
        await update.message.reply_text("❌ Tic-Tac-Toe match cancelled.")
        return

    own_matches = [
        mid for mid, g in TTT_GAMES.items()
        if g["chat_id"] == chat_id and g["creator_id"] == user_id
    ]

    if not own_matches:
        await update.message.reply_text(
            "You don't have any active Tic-Tac-Toe match here. "
            "(Reply to a specific match message with /canceltictac to cancel it.)"
        )
        return

    if len(own_matches) > 1:
        await update.message.reply_text(
            "You have multiple active Tic-Tac-Toe matches in this group. "
            "Reply to the specific match's message with /canceltictac to cancel that one."
        )
        return

    TTT_GAMES.pop(own_matches[0], None)
    await update.message.reply_text("❌ Tic-Tac-Toe match cancelled.")
