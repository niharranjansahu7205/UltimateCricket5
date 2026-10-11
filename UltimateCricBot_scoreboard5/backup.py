"""
Owner-only /backup and /restore — move the whole database between deployments
(e.g. when you redeploy on a new Railway account) without losing player data.

  /backup                       -> bot sends you the database file (private chat)
  send that file to the bot, then reply to it with:
  /restore                      -> shows what is inside the file (nothing changes yet)
  /restore yes                  -> replaces the current data with the file

Uses SQLite's own backup API (safe while the bot is running, never a
half-written copy). Before a restore the current database is kept next to it as
"<DB_PATH>.before_restore". Both commands work in a private chat with the bot
only — the file contains every player's data.
"""

import asyncio
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import ContextTypes

import database

logger = logging.getLogger(__name__)

MAX_RESTORE_BYTES = 20 * 1024 * 1024  # Telegram's download limit for bots
_COUNT_TABLES = (
    ("player_stats", "solo player records"),
    ("team_player_stats", "team player records"),
    ("team_matches", "team matches"),
    ("known_users", "known users"),
    ("known_chats", "known chats"),
    ("achievements", "achievements"),
    ("banned_users", "banned users"),
)


def _snapshot(src_path: str, dest_path: str) -> None:
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dest_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def inspect_db(path: str) -> dict:
    """Validates a candidate backup file; returns {label: row_count}.
    Raises ValueError if it isn't a healthy UltimateCricBot database."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("The file is corrupted.")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "player_stats" not in tables:
                raise ValueError("This doesn't look like an UltimateCricBot database.")
            return {
                label: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t, label in _COUNT_TABLES if t in tables
            }
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        raise ValueError("That file is not a valid SQLite database.") from e


def restore_from(path: str) -> None:
    """Replaces the live database with `path` (keeps a .before_restore copy)."""
    live = database.DB_PATH
    if os.path.isfile(live):
        _snapshot(live, live + ".before_restore")
    _snapshot(path, live)
    database.init_db()  # adds any tables/columns newer than the backup


def _summary(counts: dict) -> str:
    return "\n".join(f"• {n} {label}" for label, n in counts.items() if n) or "• (empty database)"


async def _guard(update: Update) -> bool:
    """True if the caller may proceed (owner, in a private chat)."""
    from admin import is_owner  # lazy import, same pattern as game.peek_callback
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("🚫 Only the bot owner can use this command.")
        return False
    if update.effective_chat.type != "private":
        await update.message.reply_text("🔒 Use this command in a private chat with me — the file holds all player data.")
        return False
    return True


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    live = database.DB_PATH
    if not os.path.isfile(live):
        await update.message.reply_text("⚠️ No database file found yet — nothing to back up.")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, f"ultimatecric_backup_{stamp}.db")
        try:
            await asyncio.to_thread(_snapshot, live, out)
            counts = await asyncio.to_thread(inspect_db, out)
            with open(out, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=os.path.basename(out),
                    caption="💾 UltimateCric backup\n" + _summary(counts)
                            + "\n\nTo restore on a new deployment: send this file to the bot, "
                              "reply to it with /restore, then /restore yes",
                )
        except Exception:
            logger.exception("Backup failed")
            await update.message.reply_text("⚠️ Backup failed — check the bot logs.")


async def restore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    replied = update.message.reply_to_message
    doc = getattr(replied, "document", None) if replied else None
    if doc is None:
        await update.message.reply_text(
            "Send me the backup file first, then reply to that file with /restore."
        )
        return
    if doc.file_size and doc.file_size > MAX_RESTORE_BYTES:
        await update.message.reply_text("⚠️ File is too large (Telegram limits bot downloads to 20 MB).")
        return

    confirmed = [a.lower() for a in (context.args or [])] == ["yes"]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "incoming.db")
        try:
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(path)
            counts = await asyncio.to_thread(inspect_db, path)
        except ValueError as e:
            await update.message.reply_text(f"❌ {e}")
            return
        except Exception:
            logger.exception("Restore: could not download/read the file")
            await update.message.reply_text("⚠️ Couldn't read that file. Please send it again.")
            return

        if not confirmed:
            await update.message.reply_text(
                "📦 This backup contains:\n" + _summary(counts)
                + "\n\n⚠️ Restoring REPLACES the data currently in the bot "
                  "(a copy is kept as .before_restore).\nReply to the file with <code>/restore yes</code> to continue.",
                parse_mode="HTML",
            )
            return
        try:
            await asyncio.to_thread(restore_from, path)
        except Exception:
            logger.exception("Restore failed")
            await update.message.reply_text("⚠️ Restore failed — check the bot logs. Your previous data is kept as .before_restore.")
            return
    await update.message.reply_text("✅ Restore complete:\n" + _summary(counts))
