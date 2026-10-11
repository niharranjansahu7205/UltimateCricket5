"""/giftachievement: broadcast a Claim button; claiming adds the achievement.

Run:  python -m pytest -q tests/test_gift.py
"""
import asyncio
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin            # noqa: E402
import database as db   # noqa: E402
import gift             # noqa: E402
from telegram.error import RetryAfter, TelegramError   # noqa: E402

OWNER = 111


def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(admin, "OWNER_IDS", {OWNER})
    monkeypatch.setattr(gift, "SEND_DELAY_SECONDS", 0)
    db.init_db()


class FakeBot:
    def __init__(self, fail_chats=(), flaky_chat=None):
        self.sent, self.fail_chats, self.flaky_chat, self._flaked = [], set(fail_chats), flaky_chat, False

    async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        if chat_id in self.fail_chats:
            raise TelegramError("Forbidden: bot was kicked")
        if chat_id == self.flaky_chat and not self._flaked:
            self._flaked = True
            raise RetryAfter(0)
        self.sent.append((chat_id, text, reply_markup))


def _send_gift(bot, text="/giftachievement Early Bird 2026", uid=OWNER):
    out = {"texts": [], "status": []}

    async def go():
        async def reply_text(t, **kw):
            out["texts"].append(t)
            async def edit_text(t2, **k): out["status"].append(t2)
            return types.SimpleNamespace(edit_text=edit_text)
        upd = types.SimpleNamespace(
            message=types.SimpleNamespace(text=text, reply_text=reply_text),
            effective_user=types.SimpleNamespace(id=uid))
        await gift.gift_achievement_command(upd, types.SimpleNamespace(bot=bot))
        if gift._tasks:
            await asyncio.gather(*list(gift._tasks))
    asyncio.run(go())
    return out


def _claim(gift_id, uid, first="Bob", username=None):
    answers = []

    async def answer(text=None, show_alert=False): answers.append((text, show_alert))
    q = types.SimpleNamespace(data=f"gift:{gift_id}", answer=answer,
                              from_user=types.SimpleNamespace(id=uid, first_name=first, username=username))
    asyncio.run(gift.gift_claim_callback(types.SimpleNamespace(callback_query=q), None))
    return answers[-1]


def _chats():
    db.record_known_chat(-100, "supergroup", "G1")
    db.record_known_chat(-200, "group", "G2")
    db.record_known_chat(5, "private", "Alice")      # a user's DM
    db.record_known_chat(6, "private", "Bob")


def test_sends_claim_button_to_every_group_and_dm(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path); _chats()
    bot = FakeBot()
    out = _send_gift(bot)
    assert sorted(c for c, _, _ in bot.sent) == [-200, -100, 5, 6]
    for _, text, kb in bot.sent:
        assert "Early Bird 2026" in text
        btn = kb.inline_keyboard[0][0]
        assert btn.callback_data.startswith("gift:")
    assert "Sent: <b>4</b>" in out["status"][-1] and "Failed: <b>0</b>" in out["status"][-1]


def test_failed_chats_are_skipped_and_counted_and_retryafter_retries(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path); _chats()
    bot = FakeBot(fail_chats={-200}, flaky_chat=5)
    out = _send_gift(bot)
    assert sorted(c for c, _, _ in bot.sent) == [-100, 5, 6]
    assert "Sent: <b>3</b>" in out["status"][-1] and "Failed: <b>1</b>" in out["status"][-1]


def test_claim_popup_adds_achievement_once(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path); _chats()
    _send_gift(FakeBot())
    gid = 1
    text, alert = _claim(gid, 42, "Nihar")
    assert alert is True and "Congratulations" in text and "Early Bird 2026" in text and len(text) <= 200
    rows = db.get_achievements(42)
    assert [r["tournament_name"] for r in rows] == ["Early Bird 2026"]
    text, alert = _claim(gid, 42, "Nihar")                       # double tap / second click
    assert "already claimed" in text and len(db.get_achievements(42)) == 1
    _claim(gid, 43, "Bob")                                       # someone else can still claim
    assert len(db.get_achievements(43)) == 1


def test_two_gifts_are_independent_and_unknown_gift_is_safe(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path); _chats()
    _send_gift(FakeBot(), "/giftachievement Gift One")
    _send_gift(FakeBot(), "/giftachievement Gift Two")
    _claim(1, 42); _claim(2, 42)
    assert sorted(r["tournament_name"] for r in db.get_achievements(42)) == ["Gift One", "Gift Two"]
    text, alert = _claim(999, 42)
    assert "no longer available" in text and len(db.get_achievements(42)) == 2


def test_banned_user_cannot_claim(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path); _chats()
    _send_gift(FakeBot())
    db.ban_user(77, "Bad", OWNER)
    text, _ = _claim(1, 77)
    assert "banned" in text and db.get_achievements(77) == []


def test_owner_only_usage_and_name_validation(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path); _chats()
    bot = FakeBot()
    assert "Only the bot owners" in _send_gift(bot, uid=5)["texts"][0] and bot.sent == []
    assert "Usage" in _send_gift(bot, "/giftachievement")["texts"][0] and bot.sent == []
    _send_gift(bot, "/giftachievement <b>Hacker</b> & Co " + "x" * 100)
    _claim(1, 42)
    stored = db.get_achievements(42)[0]["tournament_name"]
    assert "<" not in stored and ">" not in stored and "&" not in stored and len(stored) <= gift.MAX_NAME_LEN


def test_long_names_keep_popup_under_200_chars(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path); _chats()
    _send_gift(FakeBot(), "/giftachievement " + "N" * 200)
    text, _ = _claim(1, 42, "L" * 100)
    assert len(text) <= 200


def test_claims_survive_restore_style_missing_tables(monkeypatch, tmp_path):
    """Tables are created lazily, so a freshly restored/old DB still works."""
    _env(monkeypatch, tmp_path)
    assert gift.create_gift("X", OWNER) == 1
    assert gift.claim_gift(1, 9, "Z") == ("ok", "X")
