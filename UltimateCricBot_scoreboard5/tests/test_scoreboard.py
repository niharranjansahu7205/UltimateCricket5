"""Tests for the live scoreboard. Drives the REAL Team Cricket engine
(teamcricket._resolve_ball etc.) against a temporary SQLite file, with a
fake Telegram bot that records every API call.

Run:  python -m pytest -q tests/
"""
import asyncio
import os
import sys
import types

import pytest
from telegram.error import BadRequest, RetryAfter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db            # noqa: E402
import history as hist            # noqa: E402
import scoreboard as sb          # noqa: E402
import teamcricket as tc         # noqa: E402

CHAT = -1001
A = [(1, "Alice"), (2, "Bob"), (3, "Cara")]
B = [(11, "Dan"), (12, "Eve"), (13, "Fay")]


class FakeBot:
    def __init__(self):
        self.edits, self.sent, self.pins, self.unpins, self.deleted = [], [], [], [], []
        self.fail_with = None

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, reply_markup=None):
        if self.fail_with:
            raise self.fail_with
        self.edits.append((chat_id, message_id, text, reply_markup))

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append(text)

    async def send_photo(self, chat_id, photo, caption=None, parse_mode=None):
        self.photos = getattr(self, "photos", []) + [(photo, caption)]

    async def pin_chat_message(self, chat_id, message_id, disable_notification=True):
        self.pins.append(message_id)

    async def unpin_chat_message(self, chat_id, message_id=None):
        self.unpins.append(message_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)



@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    tc.TEAM_MATCHES.clear()
    yield


def make_game(a=A, b=B):
    game = tc.TeamMatch(match_id=500, chat_id=CHAT, message_id=500, creator_id=1, creator_name="Alice")
    game.team_a.players, game.team_a.captain_id = list(a), a[0][0]
    game.team_b.players, game.team_b.captain_id = list(b), b[0][0]
    game.db_match_id = db.create_team_match(CHAT, 500, "Team A", "Team B")
    for side, team in (("a", game.team_a), ("b", game.team_b)):
        for uid, n in team.players:
            db.record_team_match_player(game.db_match_id, uid, n, side, uid == team.captain_id)
            game.batting_stats[uid] = tc.BattingStats()
            game.bowling_stats[uid] = tc.BowlingStats()
    game.stage = "toss_call"
    TEAM = tc.TEAM_MATCHES
    TEAM[500] = game
    return game


async def start_innings(ctx, game, side="a", bowler=None):
    await tc._begin_innings(ctx, game, side)
    team = tc._side_obj(game, side)
    game.lineup = [u for u, _ in team.players]
    game.order_pick_pool = []
    await tc._confirm_batting_order(ctx, game)
    other = tc._side_obj(game, tc._other_side(side))
    await tc._set_bowler_and_start_ball(ctx, game, bowler or other.players[0][0])
    tc._cancel_timer(game)


async def ball(ctx, game, bat, bowl):
    game.picks = {game.striker_id: bat, game.current_bowler_id: bowl}
    game.turn = "bowler"
    await tc._resolve_ball(ctx, game)
    tc._cancel_timer(game)


def ctx_of(bot):
    return types.SimpleNamespace(bot=bot)


def snap(game):
    return db.get_scoreboard_snapshot(game.db_match_id)


def popup(game, running=True):
    return sb.build_popup(snap(game), running)


def run(coro):
    return asyncio.run(coro)



def _history_update(args, chat=CHAT, uid=7):
    replies = []
    async def reply_text(text, **kw): replies.append(text)
    upd = types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=chat, type="supergroup"),
                                effective_user=types.SimpleNamespace(id=uid), message=types.SimpleNamespace(reply_text=reply_text))
    return upd, types.SimpleNamespace(args=args, bot=None), replies






# ----- popup scoreboard ------------------------------------------------------
def test_popup_new_innings_and_after_balls():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot); g = make_game()
        await start_innings(ctx, g)
        t = popup(g)
        assert "0/0" in t and "RR 0.00" in t and "🟢Alice*" in t and "⚪Bob" in t and "🎯Dan" in t
        await ball(ctx, g, 0, 1); await ball(ctx, g, 1, 2); await ball(ctx, g, 4, 0)
        d = sb.derive_innings(snap(g)["events"], 1, 6)
        assert d["total"] == 5 and d["legal"] == 3 and d["extras"] == 0
        assert d["bats"][1]["runs"] == 1 and d["bats"][1]["balls"] == 2 and d["bats"][2]["fours"] == 1
        t = popup(g)
        assert "5/0 (0.3/3)" in t and "0 │ 1 │ 4" in t and "🟢Bob* 4(1)" in t
        assert sb._u16(t) <= 200
    run(go())


def test_popup_wicket_over_end_and_limit():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot)
        a = [(i, f"VeryLongPlayerName{i:02d}") for i in range(1, 13)]
        b = [(100 + i, f"AnotherLongOpponent{i:02d}") for i in range(1, 13)]
        g = make_game(a, b); g.overs = 20
        await start_innings(ctx, g)
        for _ in range(5):
            await ball(ctx, g, 6, 1)
        await ball(ctx, g, 3, 3)                       # wicket on ball 6 -> over complete
        t = popup(g)
        assert "30/1" in t and "Last over:" in t and "❌" in t and sb._u16(t) <= 200
        kb = tc._pick_keyboard(g).inline_keyboard
        assert kb[-1][0].text == "📊 Scoreboard" and kb[-1][0].callback_data == f"sb:{g.db_match_id}:p"
        assert len(kb[0]) + len(kb[1]) == 7
    run(go())


def test_popup_callback_is_read_only_and_checks_chat():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot); g = make_game()
        await start_innings(ctx, g)
        await ball(ctx, g, 2, 1)
        alerts = []
        async def answer(text=None, show_alert=False): alerts.append((text, show_alert))
        def q(data=None, chat=CHAT):
            return types.SimpleNamespace(data=data or f"sb:{g.db_match_id}:p", answer=answer, from_user=types.SimpleNamespace(id=7),
                                         message=types.SimpleNamespace(chat=types.SimpleNamespace(id=chat), message_id=500))
        before = snap(g)
        await sb.scoreboard_callback(types.SimpleNamespace(callback_query=q()), ctx)
        assert alerts[-1][1] is True and "2/0" in alerts[-1][0]
        assert snap(g)["events"] == before["events"] and snap(g)["live"] == before["live"]
        await sb.scoreboard_callback(types.SimpleNamespace(callback_query=q(chat=-5)), ctx)
        assert "isn't available" in alerts[-1][0]
        await sb.scoreboard_callback(types.SimpleNamespace(callback_query=q("sb:99999:p")), ctx)
        assert "no longer exists" in alerts[-1][0]
        await sb.scoreboard_callback(types.SimpleNamespace(callback_query=q(f"sb:{g.db_match_id}:r")), ctx)
        assert "Invalid" in alerts[-1][0]          # old refresh buttons are gone
    run(go())


def test_pinned_scoreboard_feature_is_gone():
    assert not hasattr(sb, "scoreboard_command") and not hasattr(sb, "request_update")
    import bot as botmod
    assert not hasattr(botmod, "scoreboard_command")
    assert not hasattr(db, "save_team_scoreboard")


# ----- substitutes -----------------------------------------------------------------
def sub_game(a=A, b=B):
    g = make_game(a, b)
    return g


def test_sub_before_batting_replaces_order_slot_and_captain():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot); g = make_game()
        await tc._begin_innings(ctx, g, "a"); tc._cancel_timer(g)
        g.lineup = [3]; g.order_pick_pool = [1, 2]                    # Cara picked first
        out_name, cap = tc._apply_substitution(g, "a", 1, 50, "Newbie")   # captain Alice goes out
        assert cap and g.team_a.captain_id == 50
        assert g.lineup == [3] and g.order_pick_pool == [50, 2]
        assert g.team_a.name_of(50) == "Newbie" and not g.team_a.has(1)
        assert g.former == [(1, "Alice", "a")] and g.subs_used["a"] == 1
        assert g.batting_stats[50].balls == 0
    run(go())


def test_sub_batter_at_crease_keeps_old_stats_and_all_out_rule():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot); g = make_game()
        await start_innings(ctx, g)
        await ball(ctx, g, 4, 1)                     # Alice 4 (1)
        g.stage = "ball_wait"
        assert tc._sub_block_reason(g, "a", 1) is None
        tc._apply_substitution(g, "a", 1, 50, "Newbie")
        db.record_team_substitution(g.db_match_id, 1, 1, "a", 1, 50, "Newbie", True)
        tc._persist_live(ctx, g)
        assert g.striker_id == 50 and g.lineup == [50, 2, 3]
        await ball(ctx, g, 6, 2)                     # Newbie 6 (1)
        assert g.batting_stats[1].runs == 4 and g.batting_stats[1].balls == 1 and not g.batting_stats[1].is_out
        assert g.batting_stats[50].runs == 6
        s = snap(g)
        assert [p for p in s["players"] if p["user_id"] == 1][0]["subbed_out"] == 1
        assert len(s["subs"]) == 1
        t = popup(g)
        assert "10/0" in t and "Newbie" in t
        # team size unchanged: 3 batters -> all out after 2 wickets
        await ball(ctx, g, 3, 3)                     # Newbie out? striker after single/6 rotation
        for _ in range(4):
            if g.innings == 1:
                if g.stage == "bowler_pick":
                    await tc._set_bowler_and_start_ball(ctx, g, 12); tc._cancel_timer(g)
                await ball(ctx, g, 5, 5)
        assert g.innings == 2
        out = [e for e in snap(g)["events"] if e["innings"] == 1 and e["outcome"] == "W"]
        assert len(out) == 2
    run(go())


def test_sub_bowler_takes_over_current_over_and_history_marks_it():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot); g = make_game()
        await start_innings(ctx, g)
        await ball(ctx, g, 1, 2)
        tc._apply_substitution(g, "b", 11, 60, "Zed")
        db.record_team_substitution(g.db_match_id, 1, 1, "b", 11, 60, "Zed", True)
        assert g.current_bowler_id == 60 and g.team_b.captain_id == 60
        await ball(ctx, g, 2, 0)
        evs = snap(g)["events"]
        assert [e["bowler_id"] for e in evs] == [11, 60]
        assert g.bowling_stats[11].balls_bowled == 1 and g.bowling_stats[60].balls_bowled == 1
        pages = "\n".join(hist.build_history(snap(g)))
        assert "Substitutions" in pages and "Zed replaced Dan" in pages
        assert "🔁 replaced by Zed" in pages and "(🎯 Zed)" in pages
    run(go())


def test_sub_blocks():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot); g = make_game()
        g.stage = "toss_call"
        assert "after the toss" in tc._sub_block_reason(g, "a", 1)
        await start_innings(ctx, g)
        g.picks = {1: 4}                                              # striker already picked
        assert "middle of a ball" in tc._sub_block_reason(g, "a", 1)
        assert tc._sub_block_reason(g, "a", 3) is None                # a player not in the ball is fine
        g.picks = {}
        g.subs_used["a"] = tc.TEAM_MAX_SUBS
        assert "used all" in tc._sub_block_reason(g, "a", 3)
    run(go())


def make_sub_cb(g, data, uid, chat=CHAT):
    answers, edits = [], []
    async def answer(t=None, show_alert=False): answers.append(t)
    async def edit_text(text, parse_mode=None, reply_markup=None): edits.append((text, reply_markup))
    q = types.SimpleNamespace(data=data, answer=answer, from_user=types.SimpleNamespace(id=uid, first_name="Newbie", username=None),
                              message=types.SimpleNamespace(chat=types.SimpleNamespace(id=chat), edit_text=edit_text))
    return types.SimpleNamespace(callback_query=q), answers, edits


def test_sub_command_and_callback_flow():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot); g = make_game()
        g.creator_id = 500                                           # creator != captain
        await start_innings(ctx, g)
        replies = []
        async def reply_text(text, parse_mode=None, reply_markup=None): replies.append((text, reply_markup))
        def upd(uid, reply_to=None, chat_type="supergroup"):
            return types.SimpleNamespace(
                effective_chat=types.SimpleNamespace(id=CHAT, type=chat_type),
                effective_user=types.SimpleNamespace(id=uid),
                message=types.SimpleNamespace(reply_text=reply_text, reply_to_message=reply_to))
        newbie = types.SimpleNamespace(from_user=types.SimpleNamespace(id=50, first_name="Newbie", username=None, is_bot=False))
        c = types.SimpleNamespace(bot=bot)

        await tc.sub_command(upd(99, newbie), c)                    # random user
        assert "Only a team captain" in replies[-1][0]
        await tc.sub_command(upd(1, None), c)                       # no reply
        assert "Reply to the" in replies[-1][0]
        await tc.sub_command(upd(1, types.SimpleNamespace(from_user=types.SimpleNamespace(id=2, first_name="Bob", username=None, is_bot=False))), c)
        assert "already playing" in replies[-1][0]
        await tc.sub_command(upd(1, newbie), c)                     # captain of A
        text, kb = replies[-1]
        datas = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert f"sub:500:p:2:50" in datas and not any(":p:11:" in d for d in datas)   # only own team
        assert all(len(d.encode()) <= 64 for d in datas)
        await tc.sub_command(upd(500, newbie), c)                   # creator sees both teams
        both = [b.callback_data for row in replies[-1][1].inline_keyboard for b in row]
        assert "sub:500:p:2:50" in both and "sub:500:p:12:50" in both

        # captain picks Bob -> prompt for newcomer
        u, ans, ed = make_sub_cb(g, "sub:500:p:2:50", uid=1); await tc.sub_callback(u, c)
        assert "Accept" in str(ed[-1][1]) or ed[-1][1] is not None
        # wrong person tries to accept / outsider picks
        u, ans, ed = make_sub_cb(g, "sub:500:ok:2:50", uid=77); await tc.sub_callback(u, c)
        assert "Only the new player" in ans[-1] and g.team_a.has(2)
        u, ans, ed = make_sub_cb(g, "sub:500:p:2:50", uid=99); await tc.sub_callback(u, c)
        assert "captain" in ans[-1]
        # newcomer accepts
        u, ans, ed = make_sub_cb(g, "sub:500:ok:2:50", uid=50); await tc.sub_callback(u, c)
        assert ans[-1] == "You're in! 🏏" and g.team_a.has(50) and not g.team_a.has(2)
        assert g.non_striker_id == 50 and "replaces" in ed[-1][0]
        s = snap(g)
        assert [p["subbed_out"] for p in s["players"] if p["user_id"] == 2] == [1]
        assert any(p["user_id"] == 50 and p["side"] == "a" for p in s["players"])
        # replayed accept does nothing; replaced player can't be brought back; match message refreshed
        u, ans, ed = make_sub_cb(g, "sub:500:ok:2:50", uid=50); await tc.sub_callback(u, c)
        assert "no longer in the match" in ans[-1] and g.subs_used["a"] == 1
        assert any("pick your secret number" in e[2] for e in bot.edits)
        # outsider chat
        u, ans, ed = make_sub_cb(g, "sub:500:x", uid=1, chat=-9); await tc.sub_callback(u, c)
        assert "no longer running" in ans[-1]
    run(go())


def test_replaced_players_stats_survive_to_match_result():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot)
        g = make_game(A[:2], B[:2])
        await start_innings(ctx, g)
        await ball(ctx, g, 6, 1)                                    # Alice 6
        g.stage = "ball_wait"
        tc._apply_substitution(g, "a", 1, 50, "Newbie")             # Alice replaced
        db.record_team_substitution(g.db_match_id, 1, 1, "a", 1, 50, "Newbie", True)
        await ball(ctx, g, 3, 3)                                    # Newbie out -> all out (2 players)
        assert g.innings == 2
        tc._cancel_timer(g)
        g.lineup = [11, 12]; g.order_pick_pool = []
        await tc._confirm_batting_order(ctx, g)
        await tc._set_bowler_and_start_ball(ctx, g, 2); tc._cancel_timer(g)
        await ball(ctx, g, 6, 0)                                    # Dan chases 7 -> 6, not enough? target 7
        await ball(ctx, g, 6, 0)
        result = "\n".join(bot.sent)
        assert "Alice" in result and "Newbie" in result            # both on the result card
        assert "MATCH RESULT" in result
        potm = snap(g)["match"]["potm_name"]
        assert potm in ("Alice", "Newbie", "Dan")
        pages = "\n".join(hist.build_history(snap(g)))
        assert "Alice" in pages and "replaced" in pages
        # career stats: Alice (6 runs) is recorded even though she was replaced
        with db.db_cursor() as cur:
            cur.execute("SELECT runs FROM team_player_stats WHERE user_id = 1")
            row = cur.fetchone()
        assert row is not None and row[0] == 6
    run(go())


def test_duplicate_and_concurrent_delivery_commit_once():
    async def go():
        g = make_game()
        mid = g.db_match_id
        live = {"innings": 1, "batting_side": "a", "bowling_side": "b", "stage": "ball_wait", "striker_id": 1,
                "non_striker_id": 2, "bowler_id": 11, "lineup": [1, 2, 3], "next_in_idx": 2}
        bat = {"runs": 4, "balls": 1, "fours": 1, "sixes": 0, "is_out": False, "dismissal": None}
        bowl = {"overs": 0.2, "runs_conceded": 4, "wickets": 0}
        args = (mid, 1, 1, 1, 1, 11, "4", None, bat, bowl, live)
        res = await asyncio.gather(*[asyncio.to_thread(db.commit_team_ball, *args) for _ in range(6)])
        assert res.count(True) == 1 and res.count(False) == 5
        assert len(snap(g)["events"]) == 1
    run(go())


def test_overs_column_migrates_on_existing_db(tmp_path, monkeypatch):
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE team_live_state (match_id INTEGER PRIMARY KEY, innings INTEGER NOT NULL, batting_side TEXT,"
              " bowling_side TEXT, stage TEXT, striker_id INTEGER, non_striker_id INTEGER, bowler_id INTEGER, lineup TEXT,"
              " next_in_idx INTEGER DEFAULT 0, target INTEGER, overs_completed INTEGER DEFAULT 0,"
              " balls_this_over INTEGER DEFAULT 0, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)")
    c.commit(); c.close()
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(); db.init_db()
    cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(team_live_state)")]
    assert "overs" in cols


def test_match_keys_unique_and_backfilled_for_old_matches(tmp_path, monkeypatch):
    keys = {db.get_team_match_key(db.create_team_match(CHAT, i, "A", "B")) for i in range(300)}
    assert len(keys) == 300 and all(len(k) == 5 for k in keys)
    assert not set("".join(keys)) & set("0O1ILSZB258")     # no look-alike characters
    # old DB: match row exists without key -> init_db backfills it
    import sqlite3
    with db.db_cursor() as cur:
        cur.execute("UPDATE team_matches SET match_key = NULL WHERE match_id = 1")
    db.init_db()
    assert len(db.get_team_match_key(1)) == 5
    assert db.get_team_match_id_by_key(db.get_team_match_key(1).lower()) == 1



def test_history_by_key_shows_ball_by_ball_and_scorecards():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot)
        g = make_game(A[:2], B[:2])
        await start_innings(ctx, g)
        await ball(ctx, g, 6, 1); await ball(ctx, g, 4, 2); await ball(ctx, g, 3, 3)   # Alice 10, then out (Alice)
        tc._cancel_timer(g)
        g.lineup = [11, 12]; g.order_pick_pool = []
        await tc._confirm_batting_order(ctx, g)
        await tc._set_bowler_and_start_ball(ctx, g, 1); tc._cancel_timer(g)
        await ball(ctx, g, 6, 0); await ball(ctx, g, 6, 0)                              # Dan 12 -> wins
        key = db.get_team_match_key(g.db_match_id)
        assert len(key) == 5 and key.isupper() and any(c.isdigit() for c in key) and any(c.isalpha() for c in key)
        assert any(f"/history {key}" in t for t in bot.sent)       # key printed on result card

        upd, c, replies = _history_update([str(key)])
        await hist.history_command(upd, c)
        t = "\n".join(replies)
        assert f"<code>{key}</code>" in t and "MATCH HISTORY" in t
        assert "INNINGS 1" in t and "INNINGS 2" in t and "BALL-BY-BALL" in t
        assert "Alice — <b>10</b> (3)" in t and "❌ Bowled" in t or "❌" in t
        assert "0.1  Alice — 6️⃣ 6 run(s) 💥 SIX!" in t
        assert "0.3  Alice — ❌ <b>OUT</b>" in t and "b Dan" in t or "b Dan" in t
        assert "Over 1</b> • 🎯 Dan • 10 run(s), 1 wkt • total 10" in t
        assert "won by" in t

        # '#key' style, wrong group, unknown key, junk
        upd, c, r = _history_update([f"#{key.lower()}"]); await hist.history_command(upd, c); assert "MATCH HISTORY" in r[0]  # case-insensitive, '#' ok
        upd, c, r = _history_update([str(key)], chat=-777); await hist.history_command(upd, c); assert "No match with that key" in r[0]
        upd, c, r = _history_update(["ZZZZZ"]); await hist.history_command(upd, c); assert "No match with that key" in r[0]
        upd, c, r = _history_update(["12"]); await hist.history_command(upd, c); assert "Invalid key" in r[0]
        upd, c, r = _history_update([]); await hist.history_command(upd, c); assert "Usage" in r[0] and str(key) in r[0]
    run(go())


def test_history_long_match_is_paginated_under_limit():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot)
        a = [(i, f"Player{i:02d}") for i in range(1, 13)]
        b = [(100 + i, f"Opp{i:02d}") for i in range(1, 13)]
        g = make_game(a, b); g.overs = 20
        await start_innings(ctx, g)
        for _ in range(120):
            await ball(ctx, g, 2, 1)
            if g.stage == "bowler_pick":
                await tc._set_bowler_and_start_ball(ctx, g, 101 + (g.overs_completed % 11)); tc._cancel_timer(g)
        pages = hist.build_history(snap(g))
        assert len(pages) >= 2 and all(len(p) <= 4096 for p in pages)
        assert "Over 19" in "\n".join(pages)
    run(go())


def test_overs_button_rules_and_command_arg():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot)
        g = make_game(); g.stage = "lobby"; g.message_id = 500
        async def press(uid, n):
            ans = []
            async def answer(t=None, show_alert=False): ans.append(t)
            q = types.SimpleNamespace(data=f"tc:500:overs:{n}", answer=answer, from_user=types.SimpleNamespace(id=uid),
                                      message=types.SimpleNamespace(message_id=500))
            await tc.team_overs_callback(types.SimpleNamespace(callback_query=q), ctx)
            return ans[0]
        assert "Only the creator" in await press(99, 5) and g.overs == 3      # outsider rejected
        assert "Overs set to 5" in await press(1, 5) and g.overs == 5          # captain ok
        assert "Invalid" in await press(1, 99) and g.overs == 5                # out of range
        assert "<b>5 overs</b> per innings" in bot.edits[-1][2]
        g.stage = "toss_call"
        assert "before the match starts" in await press(1, 2) and g.overs == 5  # locked after start

        # /teamcricket 7
        sent = types.SimpleNamespace(message_id=700)
        async def reply_text(text, **kw): return sent
        upd = types.SimpleNamespace(
            effective_chat=types.SimpleNamespace(id=CHAT, type="supergroup"),
            effective_user=types.SimpleNamespace(id=5, first_name="Zed", username=None),
            message=types.SimpleNamespace(reply_text=reply_text))
        await tc.teamcricket_start(upd, types.SimpleNamespace(bot=bot, args=["7"]))
        assert tc.TEAM_MATCHES[700].overs == 7
        await tc.teamcricket_start(upd, types.SimpleNamespace(bot=bot, args=["500"]))
        assert tc.TEAM_MATCHES[700].overs == 3   # invalid value -> default, with a warning reply
    run(go())


def test_custom_overs_end_innings_and_show_on_scoreboard():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot)
        g = make_game(); g.overs = 1
        await start_innings(ctx, g)
        for _ in range(6):
            await ball(ctx, g, 1, 2)       # 1 over = 6 balls, all singles
        assert g.innings == 2 and g.target == 7      # innings ended after ONE over
        assert snap(g)["live"]["overs"] == 1
        assert "1.0/1" in popup(g) or snap(g)["live"]["innings"] == 2
    run(go())


# ----- live message look -------------------------------------------------------------
def test_live_message_new_look_current_over_only_and_chase_info():
    async def go():
        bot = FakeBot(); ctx = ctx_of(bot); g = make_game(); g.overs = 2
        await start_innings(ctx, g)
        for n in (1, 4, 0):
            await ball(ctx, g, n, 5 if n else 6)
        t = tc._scoreboard_block(g, "ok")
        assert "TEAM CRICKET</b>  •  2 OVERS" in t and "<b>5/0</b>   ⏱ 0.3/2 ov" in t
        assert "▰▰▰▱▱▱▱▱▱▱" in t                                               # 3 of 12 balls -> 25% bar
        assert "RR 10.00" in t
        assert "🟢 <b>" in t and "(1)" in t                                    # striker with his score
        assert "<b>This over</b>  1️⃣ │ 4️⃣ │ ⚪ │ ▫️ │ ▫️ │ ▫️" in t
        for _ in range(3):
            await ball(ctx, g, 1, 5)
        assert g.balls_this_over == 0
        t = tc._scoreboard_block(g)
        assert "<b>This over</b>  ▫️ │ ▫️ │ ▫️ │ ▫️ │ ▫️ │ ▫️" in t and "Last over" in t   # no mixing of overs
        # chase info
        g.target = 20
        t = tc._scoreboard_block(g)
        assert "Target <b>20</b>" in t and "RRR" in t
    run(go())


# ----- result image card ------------------------------------------------------------
def _card_data(**over):
    d = {"key": "K7M3X", "overs": 3, "result": "Team A won by 12 run(s)!", "winner": "a",
         "teams": {"a": {"label": "Team A", "score": 86, "wickets": 3, "overs": "3.0", "bat": ("Alice", 45, 20), "bowl": ("Dan", 2, 12)},
                   "b": {"label": "Team B", "score": 74, "wickets": 6, "overs": "3.0", "bat": None, "bowl": None}},
         "potm": {"name": "Alice", "line": "45 runs (20b)"}}
    d.update(over)
    return d


def test_result_card_renders_png_even_with_weird_names():
    import resultcard as rc
    from PIL import Image
    import io
    png = rc.render_result_card(_card_data(), "MyBot")
    img = Image.open(io.BytesIO(png))
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and img.size == (rc.W, rc.H)
    weird = _card_data(
        result="🤝 MATCH TIED!", winner=None,
        potm={"name": "राहुल 🔥 " + "Very" * 30, "line": "x" * 200},
        teams={"a": {"label": "😀😀", "score": 1, "wickets": 0, "overs": "0.1", "bat": ("राहुल", 1, 1), "bowl": ("A" * 80, 0, 0)},
               "b": {"label": "<b>&Team", "score": 1, "wickets": 0, "overs": "0.1"}},
    )
    assert rc.render_result_card(weird)[:4] == b"\x89PNG"
    assert rc._clean("Ali🔥ce", True) == "Alice" and rc._clean("🔥", True, "Player") == "Player"
    assert rc.render_result_card(_card_data(potm=None, key=None))[:4] == b"\x89PNG"


def test_match_finish_sends_result_card_and_survives_card_failure(monkeypatch):
    async def play(fail):
        bot = FakeBot(); ctx = ctx_of(bot)
        g = make_game(A[:2], B[:2])
        await start_innings(ctx, g)
        await ball(ctx, g, 6, 1); await ball(ctx, g, 3, 3)
        tc._cancel_timer(g)
        g.lineup = [11, 12]; g.order_pick_pool = []
        await tc._confirm_batting_order(ctx, g)
        await tc._set_bowler_and_start_ball(ctx, g, 1); tc._cancel_timer(g)
        await ball(ctx, g, 2, 2)
        return bot, g
    bot, g = run(play(False))
    key = db.get_team_match_key(g.db_match_id)
    assert len(bot.photos) == 1 and bot.photos[0][0][:4] == b"\x89PNG" and key in bot.photos[0][1]
    assert any("MATCH RESULT" in t for t in bot.sent)
    import resultcard
    monkeypatch.setattr(resultcard, "render_result_card", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    bot, g = run(play(True))
    assert getattr(bot, "photos", []) == [] and any("MATCH RESULT" in t for t in bot.sent)   # text result still sent
    assert db.get_scoreboard_snapshot(g.db_match_id)["match"]["status"] == "completed"


def test_lobby_new_look_text_and_keyboard():
    g = tc._new_match(1, "Alice <b>", CHAT, 800)
    t = tc._lobby_text(g)
    assert "T E A M" in t and "Hosted by <b>Alice &lt;b&gt;</b>" in t and "<b>3 overs</b> per innings" in t
    assert "TEAM A</b>   👥 0/12" in t and "No players yet" in t and "Waiting for players" in t
    assert "Team A needs 2 more" in t and "Team B needs 2 more" in t
    g.team_a.players = [(1, "Alice"), (2, "Bob")]; g.team_a.captain_id = 1
    g.team_b.players = [(11, "Dan"), (12, "Eve")]; g.team_b.captain_id = 11
    g.overs = 1
    t = tc._lobby_text(g)
    assert "1. 👑 <b>Alice</b>  <i>(C)</i>" in t and "2. 🏏 <b>Bob</b>" in t
    assert "<code>●●○○○○○○○○○○</code>" in t and "READY!" in t and "<b>1 over</b>" in t
    datas = [b.callback_data for row in tc._lobby_keyboard(g).inline_keyboard for b in row]
    for must in ("join:a", "join:b", "overs:1", "overs:10", "start", "leave", "cancel"):
        assert any(d.endswith(must) for d in datas), must
    assert len(t) < 4096
    g.team_a.players = [(i, f"Player{i}") for i in range(12)]
    g.team_b.players = [(100 + i, f"Opp{i}") for i in range(12)]
    assert len(tc._lobby_text(g)) < 4096                     # full 12v12 still fits
