"""
Owner-only /giftachievement — give a free achievement to everyone.

  /giftachievement <achievement name>
      e.g.  /giftachievement Early Bird 2026

The bot posts a "🎁 Claim" message to every known chat (every group it is in and
every user who has chatted with it privately — the same list /broadcast uses).
Whoever taps Claim sees a congratulations popup and the achievement is added to
their /myachivements automatically. Each person can claim each gift only once;
banned users can't claim. Claims are saved in the database, so a Claim button
keeps working after a bot restart.

Limits (Telegram's, not the bot's): the bot can only message users who have
started it, and only chats it has already seen a message from.
"""

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter, TelegramError
from telegram.ext import ContextTypes

import database
from database import add_achievement, db_cursor, get_all_known_chats, is_banned
from style import header

logger = logging.getLogger(__name__)

SEND_DELAY_SECONDS = 0.05
MAX_NAME_LEN = 60
_tasks: set = set()  # keeps background broadcast tasks alive until they finish


# ------------------------------------------------------------------ data ----
def _ensure_tables(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS gift_achievements (
            gift_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            created_by INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS gift_claims (
            gift_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            claimed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (gift_id, user_id)
        )
        """
    )


def create_gift(name: str, created_by: int) -> int:
    with db_cursor() as cur:
        _ensure_tables(cur)
        cur.execute("INSERT INTO gift_achievements (name, created_by) VALUES (?, ?)", (name, created_by))
        return cur.lastrowid


def claim_gift(gift_id: int, user_id: int, user_name: str):
    """Returns (status, gift_name): status is 'ok', 'already' or 'missing'.
    The claim record and the achievement are written in ONE transaction, so a
    double-tap can never award the achievement twice."""
    with db_cursor() as cur:
        _ensure_tables(cur)
        cur.execute("SELECT name, created_by FROM gift_achievements WHERE gift_id = ?", (gift_id,))
        gift = cur.fetchone()
        if gift is None:
            return "missing", None
        cur.execute("INSERT OR IGNORE INTO gift_claims (gift_id, user_id) VALUES (?, ?)", (gift_id, user_id))
        if cur.rowcount == 0:
            return "already", gift["name"]
        cur.execute(
            "INSERT INTO achievements (user_id, username, tournament_name, added_by) VALUES (?, ?, ?, ?)",
            (user_id, user_name, gift["name"], gift["created_by"]),
        )
        return "ok", gift["name"]


def _clean_name(raw: str) -> str:
    # existing achievement screens print names inside HTML, so drop HTML characters
    name = "".join(ch for ch in raw if ch not in "<>&")
    return " ".join(name.split())[:MAX_NAME_LEN]


# -------------------------------------------------------------- handlers ----
def _claim_keyboard(gift_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🎁 Claim Achievement", callback_data=f"gift:{gift_id}")]])


async def _send_one(bot, chat_id, text, kb) -> bool:
    for attempt in (1, 2):
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=kb)
            return True
        except RetryAfter as e:
            if attempt == 2:
                return False
            ra = e.retry_after  # int (PTB 21) or timedelta (newer versions)
            await asyncio.sleep((ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)) + 1)
        except TelegramError:
            return False  # bot removed from the group / blocked by the user
        except Exception:
            logger.exception("Gift send to %s failed", chat_id)
            return False
    return False


async def _broadcast_gift(bot, status_msg, gift_id: int, name: str, chats) -> None:
    text = (
        header("🎁 Free Achievement!")
        + f"🏆 <b>{name}</b>\n\n"
        + "Tap the button below to claim it — it will be added to your /myachivements."
    )
    kb = _claim_keyboard(gift_id)
    sent = failed = 0
    for row in chats:
        if await _send_one(bot, row["chat_id"], text, kb):
            sent += 1
        else:
            failed += 1
        await asyncio.sleep(SEND_DELAY_SECONDS)
    try:
        await status_msg.edit_text(
            header("🎁 Gift Sent") + f"🏆 <b>{name}</b>\n✅ Sent: <b>{sent}</b>\n❌ Failed: <b>{failed}</b>",
            parse_mode="HTML",
        )
    except Exception:
        logger.warning("Could not update gift status message", exc_info=True)


async def gift_achievement_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from admin import is_owner  # lazy import, same pattern as game.peek_callback
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⚠️ Only the bot owners can use /giftachievement.")
        return

    parts = (update.message.text or "").split(None, 1)
    name = _clean_name(parts[1]) if len(parts) > 1 else ""
    if not name:
        await update.message.reply_text(
            "Usage: <code>/giftachievement Early Bird 2026</code>\n\n"
            "Sends a Claim button to every group and private chat the bot knows. "
            "Everyone who taps it gets this achievement.",
            parse_mode="HTML",
        )
        return

    chats = get_all_known_chats()
    if not chats:
        await update.message.reply_text("No known chats to send to yet.")
        return

    gift_id = create_gift(name, user.id)
    status_msg = await update.message.reply_text(
        f"🎁 Sending <b>{name}</b> to {len(chats)} chat(s)... I'll update this message when done.",
        parse_mode="HTML",
    )
    # Run in the background so the bot keeps handling matches while it sends.
    task = asyncio.create_task(_broadcast_gift(context.bot, status_msg, gift_id, name, chats))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def gift_claim_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if is_banned(user.id):
        await query.answer("🚫 You are banned from UltimateCric.", show_alert=True)
        return

    gift_id = int(query.data.split(":", 1)[1])
    display = (user.first_name or user.username or str(user.id))[:30]  # popup text is capped at 200 chars
    try:
        status, name = claim_gift(gift_id, user.id, display)
    except Exception:
        logger.exception("Gift claim failed for %s", user.id)
        await query.answer("⚠️ Something went wrong. Please tap again in a moment.", show_alert=True)
        return

    if status == "ok":
        await query.answer(
            f"🎉 Congratulations, {display}!\n\n"
            f"You claimed the \"{name}\" achievement. It's now in your /myachivements. 🏆",
            show_alert=True,
        )
    elif status == "already":
        await query.answer(f"✅ You've already claimed \"{name}\".", show_alert=True)
    else:
        await query.answer("This gift is no longer available.", show_alert=True)
