"""
SQLite storage layer for UltimateCricBot.
Keeps per-user, per-chat stats: matches played, won, lost, runs scored, highest score.
Designed to persist on a Railway volume mounted at DB_PATH.
"""

import json
import logging
import os
import sqlite3
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "ultimatecric.db")


def log_db_path_diagnostics():
    """Logs exactly what DB_PATH resolved to and whether that file already
    exists — call this once at startup. If stats keep resetting after every
    deploy, check this log line first: it will immediately show whether
    DB_PATH is pointing at the volume path you expect, and whether the
    database file from the PREVIOUS run is still there (proof the volume
    is actually persisting) or missing (proof it isn't)."""
    exists = os.path.isfile(DB_PATH)
    size = os.path.getsize(DB_PATH) if exists else 0
    logger.info(
        "DB_PATH=%r | file exists=%s | size=%d bytes%s",
        DB_PATH,
        exists,
        size,
        " (fresh/empty — data was NOT preserved from before this restart!)"
        if exists and size == 0
        else "",
    )
    if not exists:
        logger.warning(
            "No existing database file found at DB_PATH=%r on startup. "
            "If this is not the very first deploy, your volume is not "
            "actually persisting data across restarts — check that a "
            "Volume is attached to THIS service and mounted at the exact "
            "directory DB_PATH points into.",
            DB_PATH,
        )


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL mode lets reads and writes happen concurrently without blocking
    # each other (important for Team Cricket, which can fire several
    # writes per ball across simultaneous matches). busy_timeout makes
    # SQLite retry for up to 5s instead of raising "database is locked"
    # immediately if two writes do briefly collide.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db_cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS player_stats (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                matches_played INTEGER DEFAULT 0,
                matches_won INTEGER DEFAULT 0,
                matches_lost INTEGER DEFAULT 0,
                total_runs INTEGER DEFAULT 0,
                highest_score INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                banned_by INTEGER,
                banned_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS known_chats (
                chat_id INTEGER PRIMARY KEY,
                chat_type TEXT NOT NULL,  -- 'group', 'supergroup', or 'private'
                title TEXT,               -- group name, or user's display name for private chats
                last_seen TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS known_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,       -- Telegram @username, no leading @ (may be NULL)
                first_name TEXT,
                last_seen TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                tournament_name TEXT NOT NULL,
                added_by INTEGER,
                added_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # -------------------------------------------------------------
        # Team Cricket (/teamcricket) tables
        # -------------------------------------------------------------
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS team_matches (
                match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                finished_at TEXT,
                team_a_label TEXT,
                team_b_label TEXT,
                team_a_score INTEGER,
                team_a_wickets INTEGER,
                team_a_overs REAL,
                team_b_score INTEGER,
                team_b_wickets INTEGER,
                team_b_overs REAL,
                result TEXT,
                winner_side TEXT,            -- 'a', 'b', or NULL for a tie
                potm_user_id INTEGER,
                potm_name TEXT,
                status TEXT DEFAULT 'active'  -- active / completed / cancelled
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_team_matches_chat ON team_matches(chat_id)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS team_match_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT,
                side TEXT,                   -- 'a' or 'b'
                is_captain INTEGER DEFAULT 0,
                runs INTEGER DEFAULT 0,
                balls_faced INTEGER DEFAULT 0,
                fours INTEGER DEFAULT 0,
                sixes INTEGER DEFAULT 0,
                is_out INTEGER DEFAULT 0,
                dismissal TEXT,
                overs_bowled REAL DEFAULT 0,
                runs_conceded INTEGER DEFAULT 0,
                wickets_taken INTEGER DEFAULT 0,
                FOREIGN KEY (match_id) REFERENCES team_matches(match_id)
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tmp_match ON team_match_players(match_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tmp_user ON team_match_players(user_id)")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS team_player_stats (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT,
                matches INTEGER DEFAULT 0,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                ties INTEGER DEFAULT 0,
                runs INTEGER DEFAULT 0,
                wickets INTEGER DEFAULT 0,
                highest_score INTEGER DEFAULT 0,
                best_bowling_wickets INTEGER DEFAULT 0,
                best_bowling_runs INTEGER DEFAULT 0,
                potm_awards INTEGER DEFAULT 0,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS team_match_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                innings INTEGER,
                over_no INTEGER,
                ball_no INTEGER,
                striker_id INTEGER,
                bowler_id INTEGER,
                outcome TEXT,                -- '0'..'6' or 'W'
                dismissal TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tme_match ON team_match_events(match_id)")

        # -------------------------------------------------------------
        # Live scoreboard (/scoreboard) — additive migrations only.
        # -------------------------------------------------------------
        # One row per match holding the *live* crease/bowler state that
        # team_match_events alone can't tell us (who's on strike, batting
        # order, current bowler, target). Written in the SAME transaction
        # as each delivery, so a refresh can never see a half-applied ball.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS team_live_state (
                match_id INTEGER PRIMARY KEY,   -- team_matches.match_id
                innings INTEGER NOT NULL,
                batting_side TEXT,
                bowling_side TEXT,
                stage TEXT,
                striker_id INTEGER,
                non_striker_id INTEGER,
                bowler_id INTEGER,
                lineup TEXT,                    -- JSON list of user_ids in batting order
                next_in_idx INTEGER DEFAULT 0,
                target INTEGER,
                overs_completed INTEGER DEFAULT 0,
                balls_this_over INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        # Per-match over limit (added after the first release -> ALTER for existing DBs).
        try:
            cur.execute("ALTER TABLE team_live_state ADD COLUMN overs INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists
        # Mid-match substitutions (/sub): the replaced player stays in
        # team_match_players (his balls are in the history) but is flagged.
        try:
            cur.execute("ALTER TABLE team_match_players ADD COLUMN subbed_out INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS team_match_subs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                innings INTEGER NOT NULL,
                legal_balls INTEGER NOT NULL,   -- balls of the batting side's innings already bowled
                side TEXT NOT NULL,
                out_user_id INTEGER NOT NULL,
                in_user_id INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tms_match ON team_match_subs(match_id)")

        # Each delivery must be recorded exactly once. Old databases could
        # in theory already hold duplicates, so never let this crash startup.
        try:
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_tme_delivery "
                "ON team_match_events(match_id, innings, over_no, ball_no)"
            )
        except sqlite3.IntegrityError:
            logger.warning("uq_tme_delivery not created: duplicate deliveries already exist in team_match_events")

        # Public match key (5 chars, letters+digits) used by /history.
        try:
            cur.execute("ALTER TABLE team_matches ADD COLUMN match_key TEXT")
        except sqlite3.OperationalError:
            pass  # already exists
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_team_matches_key ON team_matches(match_key)")
        # Older matches (played before keys existed) get one too.
        cur.execute("SELECT match_id FROM team_matches WHERE match_key IS NULL")
        for (old_id,) in cur.fetchall():
            cur.execute("UPDATE team_matches SET match_key = ? WHERE match_id = ?",
                        (_new_match_key(cur), old_id))


def upsert_player(chat_id: int, user_id: int, username: str):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO player_stats (chat_id, user_id, username)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET username=excluded.username
            """,
            (chat_id, user_id, username),
        )


def record_match_result(chat_id: int, user_id: int, username: str, runs: int, won: bool):
    upsert_player(chat_id, user_id, username)
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE player_stats
            SET matches_played = matches_played + 1,
                matches_won = matches_won + ?,
                matches_lost = matches_lost + ?,
                total_runs = total_runs + ?,
                highest_score = MAX(highest_score, ?)
            WHERE chat_id = ? AND user_id = ?
            """,
            (1 if won else 0, 0 if won else 1, runs, runs, chat_id, user_id),
        )


def get_player_stats(chat_id: int, user_id: int):
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM player_stats WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        return cur.fetchone()


def get_leaderboard(chat_id: int, limit: int = 10):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM player_stats
            WHERE chat_id = ?
            ORDER BY matches_won DESC, total_runs DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Global ban list (owner-only feature, applies bot-wide across all groups)
# ---------------------------------------------------------------------------

def ban_user(user_id: int, username: str, banned_by: int):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO banned_users (user_id, username, banned_by)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                banned_by=excluded.banned_by,
                banned_at=CURRENT_TIMESTAMP
            """,
            (user_id, username, banned_by),
        )


def unban_user(user_id: int) -> bool:
    """Returns True if the user was actually banned (and is now removed)."""
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
        existed = cur.fetchone() is not None
        cur.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
        return existed


def is_banned(user_id: int) -> bool:
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
        return cur.fetchone() is not None


def get_banned_users():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM banned_users ORDER BY banned_at DESC")
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Known chats (every group + private chat the bot has seen) — used for /broadcast
# ---------------------------------------------------------------------------

def record_known_chat(chat_id: int, chat_type: str, title: str):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO known_chats (chat_id, chat_type, title)
            VALUES (?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                chat_type=excluded.chat_type,
                title=excluded.title,
                last_seen=CURRENT_TIMESTAMP
            """,
            (chat_id, chat_type, title),
        )


def get_all_known_chats():
    with db_cursor() as cur:
        cur.execute("SELECT * FROM known_chats")
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Known users (every user the bot has ever seen a message from) — lets
# owner-only commands target someone by @username instead of only by reply
# or numeric ID.
# ---------------------------------------------------------------------------

def record_known_user(user_id: int, username: str | None, first_name: str | None):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO known_users (user_id, username, first_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_seen=CURRENT_TIMESTAMP
            """,
            (user_id, username, first_name),
        )


def find_user_by_username(username: str):
    """Look up a previously-seen user by @username (case-insensitive,
    no leading @). Returns the known_users row, or None if never seen."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM known_users WHERE username = ? COLLATE NOCASE",
            (username,),
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Achievements (owner-only feature, global — same across every group)
# ---------------------------------------------------------------------------

def add_achievement(user_id: int, username: str, tournament_name: str, added_by: int):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO achievements (user_id, username, tournament_name, added_by)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, username, tournament_name, added_by),
        )


def get_achievements(user_id: int):
    with db_cursor() as cur:
        cur.execute(
            "SELECT * FROM achievements WHERE user_id = ? ORDER BY added_at DESC",
            (user_id,),
        )
        return cur.fetchall()


def delete_achievements(user_id: int, tournament_name: str | None = None) -> int:
    """Delete achievement(s) for a user. If tournament_name is given, only
    the matching one (case-insensitive) is deleted; otherwise every
    achievement recorded for the user is deleted. Returns rows deleted."""
    with db_cursor() as cur:
        if tournament_name:
            cur.execute(
                "DELETE FROM achievements WHERE user_id = ? AND tournament_name = ? COLLATE NOCASE",
                (user_id, tournament_name),
            )
        else:
            cur.execute("DELETE FROM achievements WHERE user_id = ?", (user_id,))
        return cur.rowcount


def get_achievement_by_id(achievement_id: int):
    with db_cursor() as cur:
        cur.execute("SELECT * FROM achievements WHERE id = ?", (achievement_id,))
        return cur.fetchone()


def delete_achievement_by_id(achievement_id: int) -> bool:
    """Returns True if the achievement existed (and is now removed)."""
    with db_cursor() as cur:
        cur.execute("SELECT 1 FROM achievements WHERE id = ?", (achievement_id,))
        existed = cur.fetchone() is not None
        cur.execute("DELETE FROM achievements WHERE id = ?", (achievement_id,))
        return existed


# ---------------------------------------------------------------------------
# Team Cricket (/teamcricket) — match history, per-player stats, ball events
# ---------------------------------------------------------------------------

# No 0/O, 1/I/L, 5/S, 2/Z, 8/B -> keys are easy to read out and type.
_KEY_ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"
KEY_LEN = 5


def _new_match_key(cur) -> str:
    """Random 5-char key like 'K7M3X' that is not used by any match yet."""
    import secrets
    while True:
        # always mix letters and digits so every key looks like a "key"
        key = "".join(secrets.choice(_KEY_ALPHABET) for _ in range(KEY_LEN))
        if any(c.isalpha() for c in key) and any(c.isdigit() for c in key):
            cur.execute("SELECT 1 FROM team_matches WHERE match_key = ?", (key,))
            if cur.fetchone() is None:
                return key


def create_team_match(chat_id: int, message_id: int, team_a_label: str, team_b_label: str) -> int:
    """Creates the match row (with its public key) and returns its new match_id."""
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO team_matches (chat_id, message_id, team_a_label, team_b_label, status, match_key)
            VALUES (?, ?, ?, ?, 'active', ?)
            """,
            (chat_id, message_id, team_a_label, team_b_label, _new_match_key(cur)),
        )
        return cur.lastrowid


def get_team_match_key(match_id: int) -> str | None:
    with db_cursor() as cur:
        cur.execute("SELECT match_key FROM team_matches WHERE match_id = ?", (match_id,))
        row = cur.fetchone()
        return row["match_key"] if row else None


def get_team_match_id_by_key(key: str) -> int | None:
    with db_cursor() as cur:
        cur.execute("SELECT match_id FROM team_matches WHERE match_key = ?", (key.upper(),))
        row = cur.fetchone()
        return row["match_id"] if row else None


def record_team_match_player(match_id: int, user_id: int, name: str, side: str, is_captain: bool):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO team_match_players (match_id, user_id, name, side, is_captain)
            VALUES (?, ?, ?, ?, ?)
            """,
            (match_id, user_id, name, side, 1 if is_captain else 0),
        )


def update_team_player_batting(
    match_id: int, user_id: int, runs: int, balls: int, fours: int, sixes: int,
    is_out: bool, dismissal: str | None,
):
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE team_match_players
            SET runs = ?, balls_faced = ?, fours = ?, sixes = ?, is_out = ?, dismissal = ?
            WHERE match_id = ? AND user_id = ?
            """,
            (runs, balls, fours, sixes, 1 if is_out else 0, dismissal, match_id, user_id),
        )


def update_team_player_bowling(match_id: int, user_id: int, overs: float, runs_conceded: int, wickets: int):
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE team_match_players
            SET overs_bowled = ?, runs_conceded = ?, wickets_taken = ?
            WHERE match_id = ? AND user_id = ?
            """,
            (overs, runs_conceded, wickets, match_id, user_id),
        )


def record_team_match_event(
    match_id: int, innings: int, over_no: int, ball_no: int,
    striker_id: int, bowler_id: int, outcome: str, dismissal: str | None,
):
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO team_match_events
            (match_id, innings, over_no, ball_no, striker_id, bowler_id, outcome, dismissal)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (match_id, innings, over_no, ball_no, striker_id, bowler_id, outcome, dismissal),
        )


def finish_team_match(
    match_id: int,
    team_a_score: int, team_a_wickets: int, team_a_overs: float,
    team_b_score: int, team_b_wickets: int, team_b_overs: float,
    result: str, winner_side: str | None,
    potm_user_id: int | None, potm_name: str | None,
):
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE team_matches
            SET finished_at = CURRENT_TIMESTAMP,
                team_a_score = ?, team_a_wickets = ?, team_a_overs = ?,
                team_b_score = ?, team_b_wickets = ?, team_b_overs = ?,
                result = ?, winner_side = ?,
                potm_user_id = ?, potm_name = ?,
                status = 'completed'
            WHERE match_id = ?
            """,
            (
                team_a_score, team_a_wickets, team_a_overs,
                team_b_score, team_b_wickets, team_b_overs,
                result, winner_side, potm_user_id, potm_name, match_id,
            ),
        )


def cancel_team_match_row(match_id: int):
    with db_cursor() as cur:
        cur.execute("UPDATE team_matches SET status = 'cancelled' WHERE match_id = ?", (match_id,))


def upsert_team_player_stats(
    chat_id: int, user_id: int, name: str,
    outcome: str,  # 'win' | 'loss' | 'tie'
    runs: int, wickets: int, runs_conceded: int, is_potm: bool,
):
    """Single idempotent-ish upsert of one player's Team Cricket stats for
    one completed match. outcome MUST be exactly one of 'win'/'loss'/'tie'
    — a tie is its own bucket and never increments losses."""
    win_inc = 1 if outcome == "win" else 0
    loss_inc = 1 if outcome == "loss" else 0
    tie_inc = 1 if outcome == "tie" else 0

    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO team_player_stats (chat_id, user_id, name, matches, wins, losses, ties, runs, wickets, highest_score, potm_awards)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                name = excluded.name,
                matches = matches + 1,
                wins = wins + ?,
                losses = losses + ?,
                ties = ties + ?,
                runs = runs + ?,
                wickets = wickets + ?,
                highest_score = MAX(highest_score, ?),
                potm_awards = potm_awards + ?
            """,
            (
                chat_id, user_id, name, win_inc, loss_inc, tie_inc, runs, wickets, runs, 1 if is_potm else 0,
                win_inc, loss_inc, tie_inc, runs, wickets, runs, 1 if is_potm else 0,
            ),
        )

        if wickets > 0:
            cur.execute(
                """
                UPDATE team_player_stats
                SET best_bowling_wickets = ?, best_bowling_runs = ?
                WHERE chat_id = ? AND user_id = ?
                AND (? > best_bowling_wickets
                     OR (? = best_bowling_wickets AND ? < best_bowling_runs))
                """,
                (wickets, runs_conceded, chat_id, user_id, wickets, wickets, runs_conceded),
            )


def get_team_leaderboard(chat_id: int, limit: int = 10):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM team_player_stats
            WHERE chat_id = ?
            ORDER BY wins DESC, runs DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        return cur.fetchall()


def get_recent_team_matches(chat_id: int, limit: int = 5):
    with db_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM team_matches
            WHERE chat_id = ? AND status = 'completed'
            ORDER BY finished_at DESC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Live scoreboard support
# ---------------------------------------------------------------------------

def _upsert_live_state(cur, match_id: int, st: dict):
    cur.execute(
        """
        INSERT INTO team_live_state
            (match_id, innings, batting_side, bowling_side, stage, striker_id, non_striker_id,
             bowler_id, lineup, next_in_idx, target, overs_completed, balls_this_over, overs, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(match_id) DO UPDATE SET
            innings = excluded.innings, batting_side = excluded.batting_side,
            bowling_side = excluded.bowling_side, stage = excluded.stage,
            striker_id = excluded.striker_id, non_striker_id = excluded.non_striker_id,
            bowler_id = excluded.bowler_id, lineup = excluded.lineup,
            next_in_idx = excluded.next_in_idx, target = excluded.target,
            overs_completed = excluded.overs_completed, balls_this_over = excluded.balls_this_over, overs = excluded.overs,
            updated_at = CURRENT_TIMESTAMP
        """,
        (
            match_id, st["innings"], st.get("batting_side"), st.get("bowling_side"), st.get("stage"),
            st.get("striker_id"), st.get("non_striker_id"), st.get("bowler_id"),
            json.dumps(st.get("lineup") or []), st.get("next_in_idx", 0), st.get("target"),
            st.get("overs_completed", 0), st.get("balls_this_over", 0), st.get("overs"),
        ),
    )


def save_team_live_state(match_id: int, st: dict):
    """Persist crease/bowler state outside of a delivery (innings start,
    batting order confirmed, bowler chosen...)."""
    with db_cursor() as cur:
        _upsert_live_state(cur, match_id, st)


def commit_team_ball(
    match_id: int, innings: int, over_no: int, ball_no: int,
    striker_id: int, bowler_id: int, outcome: str, dismissal: str | None,
    batting: dict, bowling: dict, live_state: dict,
) -> bool:
    """Records ONE delivery atomically: event row + batter row + bowler row +
    live state, all in a single transaction. Returns False (and writes
    nothing) if this exact delivery was already recorded, so a double
    callback can never score a ball twice."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR IGNORE INTO team_match_events
            (match_id, innings, over_no, ball_no, striker_id, bowler_id, outcome, dismissal)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (match_id, innings, over_no, ball_no, striker_id, bowler_id, outcome, dismissal),
        )
        if cur.rowcount == 0:
            conn.rollback()
            logger.warning("Duplicate delivery ignored: match=%s inn=%s over=%s ball=%s",
                           match_id, innings, over_no, ball_no)
            return False
        cur.execute(
            """
            UPDATE team_match_players
            SET runs = ?, balls_faced = ?, fours = ?, sixes = ?, is_out = ?, dismissal = ?
            WHERE match_id = ? AND user_id = ?
            """,
            (batting["runs"], batting["balls"], batting["fours"], batting["sixes"],
             1 if batting["is_out"] else 0, batting["dismissal"], match_id, striker_id),
        )
        cur.execute(
            """
            UPDATE team_match_players
            SET overs_bowled = ?, runs_conceded = ?, wickets_taken = ?
            WHERE match_id = ? AND user_id = ?
            """,
            (bowling["overs"], bowling["runs_conceded"], bowling["wickets"], match_id, bowler_id),
        )
        _upsert_live_state(cur, match_id, live_state)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_scoreboard_snapshot(match_id: int) -> dict | None:
    """Everything a scoreboard render needs, read inside ONE read transaction
    (WAL snapshot) so it is internally consistent even while a ball is being
    committed. Pure read — never mutates match state."""
    conn = get_connection()
    try:
        conn.execute("BEGIN")
        m = conn.execute("SELECT * FROM team_matches WHERE match_id = ?", (match_id,)).fetchone()
        if m is None:
            return None
        live = conn.execute("SELECT * FROM team_live_state WHERE match_id = ?", (match_id,)).fetchone()
        players = conn.execute(
            "SELECT user_id, name, side, is_captain, subbed_out FROM team_match_players WHERE match_id = ? ORDER BY id",
            (match_id,),
        ).fetchall()
        events = conn.execute(
            "SELECT innings, over_no, ball_no, striker_id, bowler_id, outcome, dismissal "
            "FROM team_match_events WHERE match_id = ? ORDER BY innings, over_no, ball_no",
            (match_id,),
        ).fetchall()
        subs = conn.execute(
            "SELECT innings, legal_balls, side, out_user_id, in_user_id FROM team_match_subs "
            "WHERE match_id = ? ORDER BY id", (match_id,),
        ).fetchall()
        live_d = dict(live) if live else None
        if live_d:
            live_d["lineup"] = json.loads(live_d.get("lineup") or "[]")
        return {
            "match": dict(m),
            "live": live_d,
            "players": [dict(p) for p in players],
            "events": [dict(e) for e in events],
            "subs": [dict(x) for x in subs],
        }
    finally:
        conn.rollback()
        conn.close()



def record_team_substitution(match_id: int, innings: int, legal_balls: int, side: str,
                             out_uid: int, in_uid: int, in_name: str, in_is_captain: bool):
    """One transaction: flag the outgoing player, add the incoming one, log the swap."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        cur.execute(
            "UPDATE team_match_players SET subbed_out = 1, is_captain = 0 WHERE match_id = ? AND user_id = ?",
            (match_id, out_uid),
        )
        cur.execute(
            "INSERT INTO team_match_players (match_id, user_id, name, side, is_captain) VALUES (?, ?, ?, ?, ?)",
            (match_id, in_uid, in_name, side, 1 if in_is_captain else 0),
        )
        cur.execute(
            "INSERT INTO team_match_subs (match_id, innings, legal_balls, side, out_user_id, in_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (match_id, innings, legal_balls, side, out_uid, in_uid),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
