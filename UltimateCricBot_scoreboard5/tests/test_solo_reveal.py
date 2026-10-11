"""Exact Shot Reveal for solo (1v1 /cricket) mode.

Drives the REAL game.number_callback with a fake Telegram bot that records
every API call in order, so we can prove: the reveal carries the exact number
the batter picked, goes out BEFORE the bowler's turn opens, is sent once per
ball, fails safely, and leaves scoring / wickets / flow untouched.

Run:  python -m pytest -q tests/test_solo_reveal.py
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db   # noqa: E402
import game              # noqa: E402

PRIVATE = -100999
MATCH_CHAT = -100111
MID = 501


class FakeBot:
    def __init__(self, send_error=None, send_delay=0):
        self.log = []          # ordered ("send"|"edit", chat_id, text)
        self.send_error = send_error
        self.send_delay = send_delay

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        if chat_id == PRIVATE:
            if self.send_delay:
                await asyncio.sleep(self.send_delay)
            if self.send_error:
                raise self.send_error
        self.log.append(("send", chat_id, text))

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, reply_markup=None):
        self.log.append(("edit", chat_id, text))

    edit_message_caption = None  # media is absent in tests -> text path only

    def reveals(self):
        return [t for k, c, t in self.log if k == "send" and c == PRIVATE]


def _ctx(bot):
    return types.SimpleNamespace(bot=bot)


def _new_match(bot=None):
    g = game._new_game(1, "Nihar", MATCH_CHAT)
    g.update(opponent_id=2, opponent_name="Bob", stage="innings1", batter_id=1, bowler_id=2,
             turn="batter", message_id=MID, score={1: 0, 2: 0}, picks={})
    game.GAMES[MID] = g
    return g


def _tap(ctx, uid, n):
    answers = []

    async def answer(text=None, show_alert=False):
        answers.append(text)

    q = types.SimpleNamespace(
        data=f"num:{n}", answer=answer,
        from_user=types.SimpleNamespace(id=uid),
        message=types.SimpleNamespace(message_id=MID),
    )
    asyncio.run(game.number_callback(types.SimpleNamespace(callback_query=q), ctx))
    return answers


def _setup(monkeypatch, tmp_path, group=str(PRIVATE)):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    if group is None:
        monkeypatch.delenv("PRIVATE_GROUP_ID", raising=False)
    else:
        monkeypatch.setenv("PRIVATE_GROUP_ID", group)
    game.GAMES.clear()


def test_reveal_has_exact_number_and_precedes_bowler_turn(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bot = FakeBot()
    g = _new_match()
    _tap(_ctx(bot), 1, 4)
    assert bot.reveals() == ["Batsman: Nihar\nSelected Number: 4\n⏳ Waiting for the bowler..."]
    kinds = [(k, c) for k, c, _ in bot.log]
    # reveal first, THEN the bowler prompt edit in the match chat
    assert kinds[0] == ("send", PRIVATE) and kinds[1] == ("edit", MATCH_CHAT)
    assert "its your turn to bowl" in bot.log[1][2]
    assert g["turn"] == "bowler" and g["picks"] == {1: 4}


def test_every_number_1_to_6_is_revealed_exactly(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    for n in range(1, 7):
        bot = FakeBot()
        _new_match()
        _tap(_ctx(bot), 1, n)
        assert bot.reveals()[0].splitlines()[1] == f"Selected Number: {n}"


def test_bowler_pick_sends_no_reveal_and_scoring_unchanged(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bot = FakeBot()
    g = _new_match()
    _tap(_ctx(bot), 1, 6)
    _tap(_ctx(bot), 2, 2)          # bowler picks -> 6 runs
    assert len(bot.reveals()) == 1
    assert g["score"][1] == 6 and g["ball_no"] == 1 and g["turn"] == "batter"
    _tap(_ctx(bot), 1, 3)
    _tap(_ctx(bot), 2, 3)          # same number -> OUT, innings flips
    assert g["innings"] == 2 and g["target"] == 7 and g["batter_id"] == 2
    assert len(bot.reveals()) == 2


def _play(bot, picks):
    """Play a scripted whole match; return (final texts of match edits, scores)."""
    g = _new_match()
    for batter_pick, bowler_pick in picks:
        if MID not in game.GAMES:
            break
        _tap(_ctx(bot), g["batter_id"], batter_pick)
        _tap(_ctx(bot), g["bowler_id"], bowler_pick)
    edits = [t for k, c, t in bot.log if c == MATCH_CHAT]
    return edits, dict(g["score"]), g["stage"]


def test_full_match_identical_with_and_without_reveal(monkeypatch, tmp_path):
    script = [(4, 1), (6, 2), (5, 3), (2, 2),      # P1 out on 2nd... innings 1 ends
              (1, 5), (3, 2), (6, 6), (4, 1), (5, 5)]
    _setup(monkeypatch, tmp_path, group=None)
    off = _play(FakeBot(), script)
    _setup(monkeypatch, tmp_path, group=str(PRIVATE))
    bot_on = FakeBot()
    on = _play(bot_on, script)
    assert off == on                                   # texts, scores, stage all equal
    assert off[2] == "done"
    # one reveal per batter pick actually made, none extra
    batter_picks = len([1 for k, c, t in bot_on.log if c == MATCH_CHAT and "its your turn to bowl" in t])
    assert len(bot_on.reveals()) == batter_picks


def test_second_innings_reveals_new_batter_name(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bot = FakeBot()
    g = _new_match()
    _tap(_ctx(bot), 1, 2); _tap(_ctx(bot), 2, 2)       # P1 out
    _tap(_ctx(bot), 2, 5)                              # Bob bats now
    assert bot.reveals()[-1].startswith("Batsman: Bob\nSelected Number: 5")


def test_missing_or_invalid_config_skips_silently(monkeypatch, tmp_path):
    for val in (None, "", "   ", "not-a-number"):
        _setup(monkeypatch, tmp_path, group=val)
        bot = FakeBot()
        g = _new_match()
        _tap(_ctx(bot), 1, 4)
        assert bot.reveals() == [] and g["turn"] == "bowler"
        assert any("its your turn to bowl" in t for _, _, t in bot.log)


def test_send_failure_never_blocks_game(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bot = FakeBot(send_error=RuntimeError("Forbidden: bot was kicked"))
    g = _new_match()
    _tap(_ctx(bot), 1, 3)
    assert g["turn"] == "bowler"
    assert any("its your turn to bowl" in t for _, _, t in bot.log)
    _tap(_ctx(bot), 2, 1)
    assert g["score"][1] == 3


def test_hanging_send_times_out_and_game_continues(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(game, "REVEAL_SEND_TIMEOUT", 0.05)
    bot = FakeBot(send_delay=5)
    g = _new_match()
    _tap(_ctx(bot), 1, 3)
    assert g["turn"] == "bowler"


def test_no_duplicate_reveal_for_same_ball_but_next_ball_reveals(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bot = FakeBot()
    g = _new_match()
    ctx = _ctx(bot)
    asyncio.run(game._reveal_batter_pick(ctx, g, 4))
    asyncio.run(game._reveal_batter_pick(ctx, g, 4))
    assert len(bot.reveals()) == 1
    g["ball_no"] += 1
    asyncio.run(game._reveal_batter_pick(ctx, g, 2))
    assert len(bot.reveals()) == 2


def test_double_tap_race_cannot_change_revealed_number_or_resend(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bot = FakeBot(send_delay=0.05)
    g = _new_match()
    ctx = _ctx(bot)

    async def go():
        def mk(n, uid):
            async def answer(text=None, show_alert=False): pass
            q = types.SimpleNamespace(data=f"num:{n}", answer=answer,
                                      from_user=types.SimpleNamespace(id=uid),
                                      message=types.SimpleNamespace(message_id=MID))
            return types.SimpleNamespace(callback_query=q)
        # batter taps 4 then (while reveal is in flight) taps 6; bowler jumps in too
        await asyncio.gather(game.number_callback(mk(4, 1), ctx),
                             game.number_callback(mk(6, 1), ctx),
                             game.number_callback(mk(2, 2), ctx))
    asyncio.run(go())
    assert len(bot.reveals()) == 1 and "Selected Number: 4" in bot.reveals()[0]
    assert g["picks"] == {1: 4}          # bowler's early tap rejected, 2nd batter tap locked out
    assert g["turn"] == "bowler"


def test_other_modes_and_stages_unaffected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    bot = FakeBot()
    g = _new_match()
    g["stage"] = "toss"
    _tap(_ctx(bot), 1, 4)                  # rejected: no active ball
    assert bot.reveals() == []
    # reveal helper only lives in game.py's solo handler
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "teamcricket.py"), encoding="utf-8") as f:
        assert "_reveal_batter_pick" not in f.read()
