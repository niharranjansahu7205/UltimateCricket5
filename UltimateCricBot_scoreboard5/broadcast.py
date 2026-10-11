"""
Owner-only /broadcast command.

Usage:
  /broadcast <text>                -> sends a text message to every known
                                       chat (group or private user)
  Reply to a photo with:
  /broadcast <caption>             -> sends that photo with the given
                                       caption to every known chat

"Known chats" = every group and private chat the bot has seen at least one
message from (tracked automatically in known_chats). Sends are done one at
a time with a small delay to stay well under Telegram's rate limits, and
failures (e.g. bot was removed from a group) are skipped without crashing
the broadcast.
"""

import asyncio

from telegram import LinkPreviewOptions, Update
from telegram.ext import ContextTypes
from telegram.error import TelegramError

from admin import is_owner
from database import get_all_known_chats
from style import header

# Small delay between sends to stay safely under Telegram's rate limits
# when broadcasting to many chats.
SEND_DELAY_SECONDS = 0.05


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_owner(user.id):
        await update.message.reply_text("⚠️ Only the bot owners can use /broadcast.")
        return

    replied = update.message.reply_to_message
    photo_file_id = None

    if replied and replied.photo:
        # Largest available resolution is the last item in the list.
        photo_file_id = replied.photo[-1].file_id

    # Pull the raw text after "/broadcast " ourselves instead of using
    # context.args — context.args splits on every whitespace run
    # (including blank lines), which destroys line breaks in the
    # message. A single maxsplit=1 keeps everything after the command
    # intact, newlines and all.
    raw_text = update.message.text or ""
    parts = raw_text.split(None, 1)
    caption_or_text = parts[1].strip() if len(parts) > 1 else ""

    if not photo_file_id and not caption_or_text:
        await update.message.reply_text(
            "Usage:\n"
            "/broadcast <text> — sends plain text. If it contains a bare "
            "t.me/... link (not inside a link tag), a big group/channel "
            "preview card shows automatically below it.\n"
            "Reply to a photo with /broadcast <caption> — sends that photo "
            "instead (no link preview card in this mode — that's a Telegram "
            "limit, not this bot's)."
        )
        return

    chats = get_all_known_chats()
    if not chats:
        await update.message.reply_text("No known chats to broadcast to yet.")
        return

    status_msg = await update.message.reply_text(
        f"📣 Broadcasting to {len(chats)} chat(s)... this may take a moment."
    )

    sent = 0
    failed = 0

    for chat_row in chats:
        target_chat_id = chat_row["chat_id"]
        try:
            if photo_file_id:
                await context.bot.send_photo(
                    chat_id=target_chat_id,
                    photo=photo_file_id,
                    caption=caption_or_text or None,
                    parse_mode="HTML" if caption_or_text else None,
                )
            else:
                # Plain text (no photo reply): if the text contains a bare
                # t.me/... link (not wrapped in an <a> tag), Telegram will
                # auto-fetch that group/channel's own name+photo and show it
                # as a big preview card underneath the message.
                await context.bot.send_message(
                    chat_id=target_chat_id,
                    text=caption_or_text,
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(
                        is_disabled=False,
                        prefer_large_media=True,
                    ),
                )
            sent += 1
        except TelegramError:
            # Bot was likely removed from that group, or blocked by that
            # user — skip it and keep going.
            failed += 1
        await asyncio.sleep(SEND_DELAY_SECONDS)

    await status_msg.edit_text(
        header("📣 Broadcast Complete")
        + f"✅ Sent: <b>{sent}</b>\n"
        + f"❌ Failed: <b>{failed}</b>",
        parse_mode="HTML",
    )
