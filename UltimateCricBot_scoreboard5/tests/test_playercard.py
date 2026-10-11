"""/mycard player card: stats aggregation, image rendering, and the handler.

Run:  python -m pytest -q tests/test_playercard.py
"""
import asyncio
import io
import os
import sys
import types

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db        # noqa: E402
import playercard as pc      # noqa: E402


def _db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    pc._last_used.clear()


def _jpeg(color=(40, 90, 160)):
    b = io.BytesIO()
    Image.new("RGB", (500, 700), color).save(b, "JPEG")
    return b.getvalue()


STATS0 = dict(matches=0, wins=0, losses=0, win_rate=0.0, runs=0, highest=0, wickets=0, potm=0, trophies=0)


def test_stats_combine_solo_and_team_across_groups(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    # solo: two different groups
    db.record_match_result(-1, 7, "Nihar", 30, True)
    db.record_match_result(-2, 7, "Nihar", 50, False)
    # team cricket: one win + one loss, with wickets and a POTM
    db.upsert_team_player_stats(-1, 7, "Nihar", "win", 40, 2, 10, True)
    db.upsert_team_player_stats(-1, 7, "Nihar", "loss", 12, 1, 20, False)
    db.add_achievement(7, "Nihar", "Cup A", 1)
    db.add_achievement(7, "Nihar", "Cup B", 1)
    db.record_match_result(-1, 99, "Other", 500, True)       # someone else: must not leak in
    s = pc.get_card_stats(7)
    assert (s["matches"], s["wins"], s["losses"]) == (4, 2, 2)
    assert s["runs"] == 30 + 50 + 40 + 12
    assert s["highest"] == 50 and s["wickets"] == 3 and s["potm"] == 1 and s["trophies"] == 2
    assert abs(s["win_rate"] - 50.0) < 1e-9


def test_stats_for_unknown_user_are_all_zero(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    assert pc.get_card_stats(12345) == STATS0


def test_tiers():
    assert pc.tier_for(0, 0) == "NEW PLAYER"
    assert pc.tier_for(3, 0) == "ROOKIE" and pc.tier_for(10, 4) == "ROOKIE"
    assert pc.tier_for(10, 5) == "PRO" and pc.tier_for(30, 15) == "ELITE" and pc.tier_for(80, 40) == "LEGEND"


def test_render_is_valid_png_for_photo_no_photo_and_bad_photo():
    for avatar in (_jpeg(), None, b"this is not an image"):
        png = pc.render_player_card({"name": "Nihar", "username": "nihar_dev", "stats": STATS0}, avatar, "Bot")
        img = Image.open(io.BytesIO(png))
        assert img.format == "PNG" and img.size == (pc.W, pc.H)


def test_render_survives_awkward_names():
    for name in ("🏏🔥", "", "A" * 200, "नमस्ते", "<b>x</b> & co"):
        png = pc.render_player_card({"name": name, "username": None, "stats": dict(STATS0, runs=123456789)})
        assert Image.open(io.BytesIO(png)).size == (pc.W, pc.H)


# ------------------------------------------------------------- handler ----
class FakeBot:
    def __init__(self, photo=True, fail=False):
        self.photo, self.fail = photo, fail

    async def get_user_profile_photos(self, user_id, limit=1):
        if self.fail:
            raise RuntimeError("privacy")
        if not self.photo:
            return types.SimpleNamespace(total_count=0, photos=[])
        sizes = [types.SimpleNamespace(file_id="small"), types.SimpleNamespace(file_id="big")]
        return types.SimpleNamespace(total_count=1, photos=[sizes])

    async def get_file(self, file_id):
        assert file_id == "big"                      # largest size is used
        async def dl(): return bytearray(_jpeg())
        return types.SimpleNamespace(download_as_bytearray=dl)


def _update(uid=7):
    out = {"photos": [], "texts": []}
    async def reply_photo(photo, caption=None, parse_mode=None): out["photos"].append((photo, caption))
    async def reply_text(text, **kw): out["texts"].append(text)
    msg = types.SimpleNamespace(reply_photo=reply_photo, reply_text=reply_text)
    user = types.SimpleNamespace(id=uid, first_name="Nihar", username="nihar_dev")
    return types.SimpleNamespace(message=msg, effective_user=user), out


def _run(bot, uid=7):
    upd, out = _update(uid)
    asyncio.run(pc.mycard_command(upd, types.SimpleNamespace(bot=bot)))
    return out


def test_handler_sends_card_with_profile_photo(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    db.record_match_result(-1, 7, "Nihar", 30, True)
    out = _run(FakeBot())
    assert len(out["photos"]) == 1 and out["texts"] == []
    png, caption = out["photos"][0]
    assert Image.open(io.BytesIO(png)).size == (pc.W, pc.H) and "Player Card" in caption
    assert "No matches yet" not in caption


def test_handler_new_player_hint_and_no_photo_or_hidden_photo(monkeypatch, tmp_path):
    for bot in (FakeBot(photo=False), FakeBot(fail=True)):
        _db(monkeypatch, tmp_path)
        out = _run(bot)
        assert len(out["photos"]) == 1 and "/cricket" in out["photos"][0][1]


def test_handler_cooldown_blocks_spam_but_not_other_users(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    assert len(_run(FakeBot())["photos"]) == 1
    second = _run(FakeBot())
    assert second["photos"] == [] and "cooling down" in second["texts"][0]
    assert len(_run(FakeBot(), uid=8)["photos"]) == 1


def test_handler_render_failure_gives_friendly_message(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    def boom(*a, **k): raise RuntimeError("draw failed")
    monkeypatch.setattr(pc, "render_player_card", boom)
    out = _run(FakeBot())
    assert out["photos"] == [] and "Couldn't create your card" in out["texts"][0]
