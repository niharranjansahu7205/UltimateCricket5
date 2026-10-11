"""/globaltop Global Leaderboard card: ranking, avatars for all Top 10, handler.

Run:  python -m pytest -q tests/test_leaderboardcard.py
"""
import asyncio
import io
import os
import sys
import types

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db            # noqa: E402
import leaderboardcard as lb     # noqa: E402


def _db(monkeypatch, tmp_path):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "t.db"))
    db.init_db()
    lb._cache.update(t=0.0, png=None)


def _solid(color):
    b = io.BytesIO()
    Image.new("RGB", (400, 400), color).save(b, "PNG")
    return b.getvalue()


def _row(i, wins=10, runs=100, matches=20):
    return {"user_id": 1000 + i, "name": f"P{i}", "wins": wins, "runs": runs, "matches": matches}


def _win(n, user, chat=-1):
    for _ in range(n):
        db.record_match_result(chat, user, f"U{user}", 10, True)


def test_ranking_combines_solo_team_and_groups_with_runs_tiebreak(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    _win(3, 1, -1); _win(2, 1, -2)                         # user 1: 5 solo wins over two groups
    _win(4, 2, -1)
    db.upsert_team_player_stats(-1, 2, "U2", "win", 5, 0, 0, False)      # user 2: 4 solo + 1 team = 5 wins, fewer runs
    _win(5, 3, -1)
    for _ in range(3): db.record_match_result(-1, 3, "U3", 40, True)     # user 3 more runs -> beats user 1
    _win(1, 4, -1)
    top = lb.get_global_top()
    assert [r["user_id"] for r in top] == [3, 1, 2, 4]     # 8 wins; then 5 wins (50 runs) > 5 wins (45 runs); then 1 win
    wins = [r["wins"] for r in top]
    assert wins == sorted(wins, reverse=True)
    assert top[0]["user_id"] == 3 and top[-1]["user_id"] == 4
    assert {r["user_id"]: r["wins"] for r in top}[1] == 5


def test_limit_ten_banned_excluded_and_names(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    for u in range(1, 14):
        _win(u, u)
    db.ban_user(13, "Bad", 1)                                # best player banned -> gone
    db.record_known_user(12, "uname12", "Twelve")
    top = lb.get_global_top()
    assert len(top) == 10 and all(r["user_id"] != 13 for r in top)
    assert top[0]["user_id"] == 12 and top[0]["name"] == "Twelve"      # known_users name wins
    assert top[1]["name"] == "U11"                                      # falls back to stored name


def test_empty_database_has_no_rows(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    assert lb.get_global_top() == []


def test_every_top10_profile_picture_is_drawn(monkeypatch=None):
    colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255),
              (0, 255, 255), (255, 128, 0), (128, 0, 255), (0, 128, 128), (250, 250, 250)]
    rows = [_row(i, wins=50 - i) for i in range(10)]
    avatars = {r["user_id"]: _solid(colors[i]) for i, r in enumerate(rows)}
    img = Image.open(io.BytesIO(lb.render_leaderboard_card(rows, avatars))).convert("RGB")
    assert img.size == (lb.W, lb.H)
    spots = {0: (540, 420), 1: (225, 470), 2: (855, 470)}          # podium centres
    for i in range(10):
        xy = spots.get(i) or (160, 857 + 68 * (i - 3) + 29)        # list-row avatar centres
        got = img.getpixel(xy)
        assert max(abs(a - b) for a, b in zip(got, colors[i])) <= 3, (i, got, colors[i])


def test_render_handles_any_number_of_players_and_missing_or_bad_photos():
    for n in (1, 2, 3, 4, 7, 10):
        rows = [_row(i, wins=30 - i) for i in range(n)]
        png = lb.render_leaderboard_card(rows, {1000: b"not an image"})   # others: no photo at all
        assert Image.open(io.BytesIO(png)).size == (lb.W, lb.H)
    png = lb.render_leaderboard_card([dict(_row(0), name="🏏🔥"), dict(_row(1), name="A" * 120)])
    assert Image.open(io.BytesIO(png)).size == (lb.W, lb.H)


# ------------------------------------------------------------- handler ----
class FakeBot:
    def __init__(self, broken=()):
        self.asked, self.broken = [], set(broken)

    async def get_user_profile_photos(self, user_id, limit=1):
        self.asked.append(user_id)
        if user_id in self.broken:
            raise RuntimeError("privacy")
        return types.SimpleNamespace(total_count=1, photos=[[types.SimpleNamespace(file_id=str(user_id))]])

    async def get_file(self, file_id):
        async def dl(): return bytearray(_solid((10, 120, 200)))
        return types.SimpleNamespace(download_as_bytearray=dl)


def _run(bot):
    out = {"photos": [], "texts": []}

    async def reply_photo(photo, caption=None, parse_mode=None): out["photos"].append((photo, caption))
    async def reply_text(text, **kw): out["texts"].append(text)
    upd = types.SimpleNamespace(message=types.SimpleNamespace(reply_photo=reply_photo, reply_text=reply_text))
    asyncio.run(lb.globaltop_command(upd, types.SimpleNamespace(bot=bot)))
    return out


def test_handler_fetches_photo_for_all_ten_and_survives_a_hidden_one(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    for u in range(1, 13):
        _win(u, u)
    bot = FakeBot(broken={5})
    out = _run(bot)
    assert len(bot.asked) == 10 and len(out["photos"]) == 1 and out["texts"] == []
    assert Image.open(io.BytesIO(out["photos"][0][0])).size == (lb.W, lb.H)


def test_handler_caches_card_to_protect_telegram_api(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    _win(2, 1); _win(1, 2)
    bot = FakeBot()
    _run(bot)
    asked = len(bot.asked)
    out = _run(bot)
    assert len(bot.asked) == asked and len(out["photos"]) == 1       # served from cache


def test_handler_empty_and_failure_messages(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path)
    assert "No ranked players" in _run(FakeBot())["texts"][0]
    _win(1, 1)
    def boom(*a, **k): raise RuntimeError("draw failed")
    monkeypatch.setattr(lb, "render_leaderboard_card", boom)
    out = _run(FakeBot())
    assert out["photos"] == [] and "Couldn't create" in out["texts"][0]
