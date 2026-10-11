"""
UltimateCricBot - Premium Hand Cricket Mini Game for Telegram
Framework: python-telegram-bot v20+
Storage: SQLite
Deploy: Railway
"""

import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # loads .env into os.environ if python-dotenv is installed and a .env file exists
except ImportError:
    pass  # fine in production (Railway) — env vars come from the platform instead

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from database import init_db, record_known_chat, record_known_user, log_db_path_diagnostics
from game import (
    cricket_start,
    join_callback,
    toss_callback,
    bat_bowl_callback,
    number_callback,
    peek_callback,
    mode_callback,
    cancel_cricket,
)
from history import history_command
from playercard import mycard_command
from leaderboardcard import globaltop_command
from scoreboard import scoreboard_callback
from stats import mystats_command, leaderboard_command
from admin import ban_user_command, unban_user_command, banlist_command
from backup import backup_command, restore_command
from broadcast import broadcast_command
from gift import gift_achievement_command, gift_claim_callback
from achievements import (
    add_achievement_command,
    delete_achievement_callback,
    delete_achievement_command,
    my_achievements_command,
)
from tictactoe import (
    cancel_tictac,
    tictac_join_callback,
    tictac_move_callback,
    tictac_start,
)
from teamcricket import (
    team_batbowl_callback,
    team_bowler_pick_callback,
    team_cancel_callback,
    team_help_command,
    team_join_callback,
    team_leave_callback,
    team_leaderboard_command,
    team_matches_command,
    team_order_pick_callback,
    team_overs_callback,
    team_number_callback,
    sub_callback,
    sub_command,
    team_start_callback,
    team_toss_call_callback,
    teamcricket_start,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "UltimatecricketBot")
OFFICIAL_GROUP_LINK = os.environ.get("OFFICIAL_GROUP_LINK", "https://t.me/UltimatecricketBot")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💬 Join our official bot group", url=OFFICIAL_GROUP_LINK)],
            [
                InlineKeyboardButton(
                    "➕ Add me in your group",
                    url=f"https://t.me/{BOT_USERNAME}?startgroup=true",
                )
            ],
        ]
    )
    await update.message.reply_text(
        "🏏 <b>Welcome to UltimateCric!</b> 🏏\n\n"
        "The most premium hand-cricket experience on Telegram.\n\n"
        "➤ Add me to a group\n"
        "➤ Type <code>/cricket</code> to start a match\n"
        "➤ Type <code>/mystats</code> to see your record\n"
        "➤ Type <code>/leaderboard</code> to see top players\n\n"
        "<i>Only works inside groups — challenge your friends!</i> 🔥",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏏 <b>UltimateCric — How to Play</b>\n\n"
        "<b>/cricket</b> — Start a new match in this group\n"
        "<b>/mystats</b> — View your personal stats\n"
        "<b>/mycard</b> — Get your premium player card image\n"
        "<b>/globaltop</b> — Global Top 10 leaderboard card\n"
        "<b>/leaderboard</b> — Top players in this group\n"
        "<b>/cancelcricket</b> — Cancel a pending match you created\n"
        "<b>/myachivements</b> — View your tournament achievements\n"
        "<b>/tictac</b> — Start a Tic-Tac-Toe match in this group\n"
        "<b>/canceltictac</b> — Cancel a pending Tic-Tac-Toe match you created\n"
        "<b>/teamcricket</b> — Start a full Team Cricket match (2-12 players/team)\n"
        "<b>/teamleaderboard</b> — Top players in Team Cricket (this group)\n"
        "<b>/teammatches</b> — Recent Team Cricket matches in this group\n"
        "<b>/teamhelp</b> — Full Team Cricket rules\n"
        "<b>/sub</b> — Replace a player mid-match (captain: reply to the new player's message)\n"
        "<b>/history &lt;key&gt;</b> — Full ball-by-ball history of a finished Team Cricket match\n\n"
        "<u>Rules:</u>\n"
        "• Two players pick a number secretly each ball — batter picks first, then bowler\n"
        "• If numbers <b>don't match</b> → runs added to batter's score\n"
        "• If numbers <b>match</b> → batter is OUT 🎯\n"
        "• 1st innings: batting continues until OUT (no over limit)\n"
        "• 2nd innings (chasing): the moment the target is reached, that player "
        "wins immediately — no need to get out!\n\n"
        "<u>Owner-only:</u> /banuser, /unbanuser, /banlist, /broadcast, "
        "/addachivement, /deleteachivement, /giftachievement, /backup, /restore",
        parse_mode="HTML",
    )


async def track_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silently records every chat (group or private) the bot sees a message
    from, so /broadcast knows where to send to. Also records the sending
    user's username, so owner-only commands like /addachivement can target
    someone by @username instead of only by reply. Runs before all other
    handlers and never blocks/replies — just logs and passes through."""
    chat = update.effective_chat
    if chat is None:
        return
    if chat.type == "private":
        user = update.effective_user
        title = (user.first_name if user else None) or "Unknown"
    else:
        title = chat.title or "Unknown Group"
    record_known_chat(chat.id, chat.type, title)

    user = update.effective_user
    if user is not None:
        record_known_user(user.id, user.username, user.first_name)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Catches any exception not already handled inside a specific command
    or callback, so one bad update (old button, Telegram hiccup, etc.)
    never crashes the whole bot process."""
    logger.exception("Unhandled error while processing update: %s", update, exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is not set.")

    init_db()
    log_db_path_diagnostics()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )

    # Runs before everything else, on every kind of update, to keep the
    # known_chats table up to date for /broadcast.
    app.add_handler(MessageHandler(filters.ALL, track_chat), group=-1)

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cricket", cricket_start))
    app.add_handler(CommandHandler("cancelcricket", cancel_cricket))
    app.add_handler(CommandHandler("mystats", mystats_command))
    app.add_handler(CommandHandler("mycard", mycard_command))
    app.add_handler(CommandHandler("globaltop", globaltop_command))
    app.add_handler(CommandHandler("leaderboard", leaderboard_command))
    app.add_handler(CommandHandler("banuser", ban_user_command))
    app.add_handler(CommandHandler("unbanuser", unban_user_command))
    app.add_handler(CommandHandler("banlist", banlist_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("giftachievement", gift_achievement_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CommandHandler("restore", restore_command))
    app.add_handler(CommandHandler("addachivement", add_achievement_command))
    app.add_handler(CommandHandler("deleteachivement", delete_achievement_command))
    app.add_handler(CommandHandler("myachivements", my_achievements_command))
    app.add_handler(CommandHandler("tictac", tictac_start))
    app.add_handler(CommandHandler("canceltictac", cancel_tictac))
    app.add_handler(CommandHandler("teamcricket", teamcricket_start))
    app.add_handler(CommandHandler("teamleaderboard", team_leaderboard_command))
    app.add_handler(CommandHandler("teammatches", team_matches_command))
    app.add_handler(CommandHandler("teamhelp", team_help_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("sub", sub_command))

    app.add_handler(CallbackQueryHandler(mode_callback, pattern=r"^mode:"))
    app.add_handler(CallbackQueryHandler(join_callback, pattern=r"^join:"))
    app.add_handler(CallbackQueryHandler(toss_callback, pattern=r"^toss:"))
    app.add_handler(CallbackQueryHandler(bat_bowl_callback, pattern=r"^choice:"))
    app.add_handler(CallbackQueryHandler(number_callback, pattern=r"^num:"))
    app.add_handler(CallbackQueryHandler(peek_callback, pattern=r"^peek$"))  # owner-only test tool (TEST_PEEK=1)
    app.add_handler(CallbackQueryHandler(delete_achievement_callback, pattern=r"^delach:"))
    app.add_handler(CallbackQueryHandler(gift_claim_callback, pattern=r"^gift:\d+$"))
    app.add_handler(CallbackQueryHandler(tictac_join_callback, pattern=r"^tttjoin:"))
    app.add_handler(CallbackQueryHandler(tictac_move_callback, pattern=r"^ttt:"))

    # Team Cricket — every callback_data is "tc:<match_id>:<action>[:<extra>]"
    app.add_handler(CallbackQueryHandler(team_join_callback, pattern=r"^tc:\d+:join:"))
    app.add_handler(CallbackQueryHandler(team_overs_callback, pattern=r"^tc:\d+:overs:\d+$"))
    app.add_handler(CallbackQueryHandler(team_leave_callback, pattern=r"^tc:\d+:leave$"))
    app.add_handler(CallbackQueryHandler(team_cancel_callback, pattern=r"^tc:\d+:cancel$"))
    app.add_handler(CallbackQueryHandler(team_start_callback, pattern=r"^tc:\d+:start$"))
    app.add_handler(CallbackQueryHandler(team_toss_call_callback, pattern=r"^tc:\d+:tosscall:"))
    app.add_handler(CallbackQueryHandler(team_batbowl_callback, pattern=r"^tc:\d+:batbowl:"))
    app.add_handler(CallbackQueryHandler(team_order_pick_callback, pattern=r"^tc:\d+:orderpick:"))
    app.add_handler(CallbackQueryHandler(team_bowler_pick_callback, pattern=r"^tc:\d+:bowlerpick:"))
    app.add_handler(CallbackQueryHandler(team_number_callback, pattern=r"^tc:\d+:pick:"))

    # Scoreboard popup (button under the number pad) — callback_data is "sb:<match_id>:p"
    app.add_handler(CallbackQueryHandler(sub_callback, pattern=r"^sub:\d+:(p:\d+:\d+|ok:\d+:\d+|x)$"))
    app.add_handler(CallbackQueryHandler(scoreboard_callback, pattern=r"^sb:\d+:p$"))

    app.add_error_handler(global_error_handler)

    logger.info("UltimateCricBot starting (polling mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
