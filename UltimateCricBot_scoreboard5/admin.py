"""
Owner-only moderation: /banuser and /unbanuser.

Bans are GLOBAL — a banned user is blocked from starting or playing
matches in every group the bot is in, not just the group the ban command
was run in.

Owner IDs are configured via the OWNER_IDS environment variable, a
comma-separated list of Telegram numeric user IDs. There's no limit on
how many you can add — 1, 3, 10, whatever you need:
    OWNER_IDS=111111111,222222222,333333333,444444444

Falls back to the hardcoded OWNER_IDS_FALLBACK list below if the env var
isn't set — replace the placeholder zeros with your real owner IDs (add
or remove entries freely, the list can be any length) so the commands
work even without setting the environment variable.
"""

import os

from telegram import Update
from telegram.ext import ContextTypes

from database import ban_user, get_banned_users, is_banned, unban_user
from style import header

# Replace these with your owners' real numeric Telegram user IDs.
# Add or remove entries as needed — any number of owners is fine.
OWNER_IDS_FALLBACK = {0, 0, 0}


def _load_owner_ids() -> set[int]:
    raw = os.environ.get("OWNER_IDS", "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids or OWNER_IDS_FALLBACK


OWNER_IDS = _load_owner_ids()


def is_owner(user_id: int) -> bool:
    return user_id in OWNER_IDS


def _target_name(user) -> str:
    return user.first_name or user.username or str(user.id)


async def _resolve_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Figure out who the command is targeting: from a reply, or from a
    user_id argument. Returns (user_id, display_name) or (None, None)."""
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        return target.id, _target_name(target)

    if context.args:
        arg = context.args[0].strip()
        if arg.isdigit():
            user_id = int(arg)
            # We may not have a display name for a raw ID — that's fine.
            return user_id, str(user_id)

    return None, None


async def ban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⚠️ Only the bot owners can use /banuser.")
        return

    target_id, target_name = await _resolve_target(update, context)
    if target_id is None:
        await update.message.reply_text(
            "Usage: reply to a user's message with /banuser, "
            "or /banuser <user_id>."
        )
        return

    if is_owner(target_id):
        await update.message.reply_text("You can't ban another owner.")
        return

    ban_user(target_id, target_name, user.id)
    await update.message.reply_text(
        header("🚫 User Banned")
        + f"<b>{target_name}</b> (<code>{target_id}</code>) has been banned "
        "from playing UltimateCric in every group.",
        parse_mode="HTML",
    )


async def unban_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⚠️ Only the bot owners can use /unbanuser.")
        return

    target_id, target_name = await _resolve_target(update, context)
    if target_id is None:
        await update.message.reply_text(
            "Usage: reply to a user's message with /unbanuser, "
            "or /unbanuser <user_id>."
        )
        return

    was_banned = unban_user(target_id)
    if was_banned:
        await update.message.reply_text(
            header("✅ User Unbanned")
            + f"<b>{target_name}</b> (<code>{target_id}</code>) can now play again.",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(f"{target_name} wasn't banned.")


async def banlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⚠️ Only the bot owners can use /banlist.")
        return

    rows = get_banned_users()
    if not rows:
        await update.message.reply_text("No users are currently banned.")
        return

    lines = [header("🚫 Banned Users")]
    for row in rows:
        name = row["username"] or str(row["user_id"])
        lines.append(f"• <b>{name}</b> (<code>{row['user_id']}</code>)")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
