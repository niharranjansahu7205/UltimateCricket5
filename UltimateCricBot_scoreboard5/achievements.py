"""
Achievement tracking — owner-only /addachivement and /deleteachivement,
plus /myachivements for everyone to check their own.

Achievements are GLOBAL (same everywhere the bot is used, like bans),
not per-group.

/addachivement
    Reply to the user's message:
        /addachivement <tournament name>
    Or name them by username, without a reply:
        /addachivement <username> <tournament name>

/deleteachivement
    Reply to the user's message:
        /deleteachivement
    Or tag them by username, without a reply:
        /deleteachivement <username>
    Shows a button for each of that user's achievements — tap one to
    delete just that achievement.

/myachivements
    Anyone can run this — shows their own achievement list.

Username targeting only works for a user the bot has already seen send
a message somewhere (tracked automatically in the background). If the
username isn't found, reply to one of their messages instead.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from admin import is_owner
from database import (
    add_achievement,
    delete_achievement_by_id,
    find_user_by_username,
    get_achievement_by_id,
    get_achievements,
)
from style import header


def _display_name(user) -> str:
    return user.first_name or user.username or str(user.id)


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resolve (user_id, display_name, remaining_args) from a reply, or a
    leading username argument (with or without '@'). If neither works,
    returns (None, None, context.args)."""
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        return target.id, _display_name(target), list(context.args)

    if context.args:
        candidate = context.args[0].strip()
        if candidate.isdigit():
            return int(candidate), candidate, list(context.args[1:])

        uname = candidate.lstrip("@")
        row = find_user_by_username(uname)
        if row:
            name = row["first_name"] or row["username"] or str(row["user_id"])
            return row["user_id"], name, list(context.args[1:])

    return None, None, list(context.args)


def _delete_keyboard(rows) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"🗑️ {row['tournament_name']}", callback_data=f"delach:{row['id']}")]
        for row in rows
    ]
    return InlineKeyboardMarkup(buttons)


async def add_achievement_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⚠️ Only the bot owners can use /addachivement.")
        return

    target_id, target_name, rest = await _resolve_target(update, context)
    if target_id is None:
        await update.message.reply_text(
            "Usage: reply to a user's message with /addachivement <tournament name>, "
            "or /addachivement <username> <tournament name>.\n\n"
            "(Username only works if the bot has already seen that user send a "
            "message somewhere — reply to one of their messages if it's not found.)"
        )
        return

    tournament_name = " ".join(rest).strip()
    if not tournament_name:
        await update.message.reply_text(
            "Please include the tournament name too, e.g.:\n"
            "<code>/addachivement Summer Cup 2026</code>",
            parse_mode="HTML",
        )
        return

    add_achievement(target_id, target_name, tournament_name, user.id)
    await update.message.reply_text(
        header("🏆 Achievement Added")
        + f"<b>{target_name}</b> has been awarded <b>{tournament_name}</b>!",
        parse_mode="HTML",
    )


async def delete_achievement_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⚠️ Only the bot owners can use /deleteachivement.")
        return

    target_id, target_name, _rest = await _resolve_target(update, context)
    if target_id is None:
        await update.message.reply_text(
            "Usage: reply to a user's message with /deleteachivement, "
            "or /deleteachivement <username>."
        )
        return

    rows = get_achievements(target_id)
    if not rows:
        await update.message.reply_text(f"{target_name} has no achievements to delete.")
        return

    await update.message.reply_text(
        header("🗑️ Delete Achievement") + f"Tap one to remove it from <b>{target_name}</b>:",
        parse_mode="HTML",
        reply_markup=_delete_keyboard(rows),
    )


async def delete_achievement_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user

    if not is_owner(user.id):
        await query.answer("⚠️ Only bot owners can do this.", show_alert=True)
        return

    achievement_id = int(query.data.split(":", 1)[1])
    row = get_achievement_by_id(achievement_id)
    if row is None:
        await query.answer("Already deleted.", show_alert=True)
        return

    target_user_id = row["user_id"]
    target_name = row["username"] or str(target_user_id)
    tournament_name = row["tournament_name"]

    delete_achievement_by_id(achievement_id)
    await query.answer(f"Deleted: {tournament_name}")

    remaining = get_achievements(target_user_id)
    if remaining:
        await query.edit_message_text(
            header("🗑️ Delete Achievement") + f"Tap one to remove it from <b>{target_name}</b>:",
            parse_mode="HTML",
            reply_markup=_delete_keyboard(remaining),
        )
    else:
        await query.edit_message_text(
            header("🗑️ Achievement Removed")
            + f"<b>{target_name}</b> has no achievements left.",
            parse_mode="HTML",
        )


async def my_achievements_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = get_achievements(user.id)

    if not rows:
        await update.message.reply_text("You don't have any achievements yet. 🏏")
        return

    lines = [header("🏆 Your Achievements")]
    for row in rows:
        lines.append(f"• {row['tournament_name']}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
