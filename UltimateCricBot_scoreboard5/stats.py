"""
/mystats and /leaderboard commands — premium formatted stat cards.
"""

from telegram import Update
from telegram.ext import ContextTypes

from database import get_leaderboard, get_player_stats
from style import header


async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("📊 Stats are tracked per group. Use this inside a group!")
        return

    user = update.effective_user
    row = get_player_stats(chat.id, user.id)

    if not row or row["matches_played"] == 0:
        await update.message.reply_text(
            f"📊 {user.first_name}, you haven't played any matches in this group yet. "
            "Type /cricket to start one!"
        )
        return

    win_rate = (row["matches_won"] / row["matches_played"]) * 100
    avg_runs = row["total_runs"] / row["matches_played"]

    text = (
        header(f"📊 Stats — {user.first_name}")
        + f"🏏 Matches Played: <b>{row['matches_played']}</b>\n"
        + f"🏆 Won: <b>{row['matches_won']}</b>\n"
        + f"💔 Lost: <b>{row['matches_lost']}</b>\n"
        + f"📈 Win Rate: <b>{win_rate:.1f}%</b>\n"
        + f"🔥 Highest Score: <b>{row['highest_score']}</b>\n"
        + f"⚡ Avg Runs/Match: <b>{avg_runs:.1f}</b>"
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def leaderboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.message.reply_text("🏆 Leaderboard is per group. Use this inside a group!")
        return

    rows = get_leaderboard(chat.id, limit=10)
    if not rows:
        await update.message.reply_text("No matches played yet in this group. Be the first — /cricket!")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = [header("🏆 UltimateCric Leaderboard")]
    for i, row in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i + 1}."
        name = row["username"] or "Player"
        lines.append(
            f"{medal} <b>{name}</b> — {row['matches_won']}W / {row['matches_played']}P "
            f"(🔥 best {row['highest_score']})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
