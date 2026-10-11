"""Owner-only TEST_PEEK tool for solo (1v1) mode."""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin   # noqa: E402
import game    # noqa: E402


def _game(turn="bowler", pick=4):
    g = game._new_game(1, "Alice", -1)
    g.update(stage="innings1", batter_id=1, bowler_id=2, turn=turn, opponent_id=2, opponent_name="Bob",
             message_id=77, picks={1: pick} if pick is not None else {})
    game.GAMES[77] = g
    return g


def _update(uid):
    answers = []
    async def answer(text=None, show_alert=False): answers.append((text, show_alert))
    q = types.SimpleNamespace(answer=answer, from_user=types.SimpleNamespace(id=uid),
                              message=types.SimpleNamespace(message_id=77))
    return types.SimpleNamespace(callback_query=q), answers


def test_peek_is_off_by_default_and_hidden(monkeypatch):
    monkeypatch.delenv("TEST_PEEK", raising=False)
    _game()
    upd, ans = _update(1)
    asyncio.run(game.peek_callback(upd, None))
    assert "off" in ans[-1][0] and "4" not in ans[-1][0]
    assert game.peek_enabled() is False


def test_peek_owner_only_and_private_popup(monkeypatch):
    monkeypatch.setenv("TEST_PEEK", "1")
    monkeypatch.setattr(admin, "OWNER_IDS", {111})
    _game()
    # a normal player (even the bowler himself) cannot peek
    for uid in (2, 5):
        upd, ans = _update(uid)
        asyncio.run(game.peek_callback(upd, None))
        assert "Only the bot owner" in ans[-1][0] and "picked" not in ans[-1][0]
    # owner sees it as an alert popup
    upd, ans = _update(111)
    asyncio.run(game.peek_callback(upd, None))
    text, alert = ans[-1]
    assert alert is True and "picked: 4" in text and "TEST ONLY" in text


def test_peek_only_while_batter_has_picked(monkeypatch):
    monkeypatch.setenv("TEST_PEEK", "1")
    monkeypatch.setattr(admin, "OWNER_IDS", {111})
    _game(turn="batter", pick=None)
    upd, ans = _update(111)
    asyncio.run(game.peek_callback(upd, None))
    assert "No batter pick" in ans[-1][0]
    game.GAMES.pop(77, None)


def test_bowler_keyboard_has_peek_button_only_in_test_mode(monkeypatch):
    sent = {}
    async def fake_edit(context, g, text, reply_markup=None): sent["kb"] = reply_markup
    monkeypatch.setattr(game, "_edit_game_message", fake_edit)
    g = _game()
    for flag, expected in (("", False), ("1", True)):
        monkeypatch.setenv("TEST_PEEK", flag)
        asyncio.run(game._render_bowler_turn(None, g))
        datas = [b.callback_data for row in sent["kb"].inline_keyboard for b in row]
        assert ("peek" in datas) is expected
        assert sum(d.startswith("num:") for d in datas) == len(game.MODE_NUMBERS["default"])
