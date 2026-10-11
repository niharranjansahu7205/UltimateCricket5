"""Owner-only /backup and /restore (DB moves between deployments).

Run:  python -m pytest -q tests/test_backup.py
"""
import asyncio
import os
import shutil
import sqlite3
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import admin      # noqa: E402
import backup     # noqa: E402
import database as db   # noqa: E402

OWNER = 111


def _env(monkeypatch, tmp_path, name="live.db"):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / name))
    monkeypatch.setattr(admin, "OWNER_IDS", {OWNER})
    db.init_db()


def _populate():
    db.record_match_result(-1, 7, "Nihar", 40, True)
    db.record_match_result(-1, 8, "Bob", 10, False)
    db.add_achievement(7, "Nihar", "Cup", 1)


def _msg(uid=OWNER, chat="private", reply_doc=None):
    out = {"docs": [], "texts": []}

    async def reply_document(document, filename=None, caption=None):
        out["docs"].append((document.read(), filename, caption))

    async def reply_text(text, **kw):
        out["texts"].append(text)

    reply = types.SimpleNamespace(document=reply_doc) if reply_doc is not None else None
    m = types.SimpleNamespace(reply_document=reply_document, reply_text=reply_text, reply_to_message=reply)
    return types.SimpleNamespace(message=m, effective_user=types.SimpleNamespace(id=uid),
                                 effective_chat=types.SimpleNamespace(type=chat)), out


class FakeBot:
    def __init__(self, src_path):
        self.src = src_path

    async def get_file(self, file_id):
        async def dl(path): shutil.copy(self.src, path)
        return types.SimpleNamespace(download_to_drive=dl)


def _run(fn, upd, bot=None, args=()):
    asyncio.run(fn(upd, types.SimpleNamespace(bot=bot, args=list(args))))


def test_backup_sends_valid_db_and_restore_roundtrip_on_fresh_deployment(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path, "old_account.db")
    _populate()
    upd, out = _msg()
    _run(backup.backup_command, upd)
    data, fname, caption = out["docs"][0]
    assert fname.startswith("ultimatecric_backup_") and "2 solo player records" in caption
    backup_file = tmp_path / "received.db"
    backup_file.write_bytes(data)

    # brand-new deployment: empty database
    _env(monkeypatch, tmp_path, "new_account.db")
    assert db.get_player_stats(-1, 7) is None
    doc = types.SimpleNamespace(file_id="f", file_size=len(data))
    bot = FakeBot(str(backup_file))

    upd, out = _msg(reply_doc=doc)
    _run(backup.restore_command, upd, bot)                 # no "yes": preview only
    assert "REPLACES" in out["texts"][0] and db.get_player_stats(-1, 7) is None

    upd, out = _msg(reply_doc=doc)
    _run(backup.restore_command, upd, bot, ["yes"])
    assert "Restore complete" in out["texts"][-1]
    row = db.get_player_stats(-1, 7)
    assert row["total_runs"] == 40 and row["matches_won"] == 1
    assert len(db.get_achievements(7)) == 1


def test_restore_keeps_before_restore_copy(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    db.record_match_result(-1, 1, "Old", 5, True)
    other = tmp_path / "other.db"
    sqlite3.connect(other).close()
    shutil.copy(db.DB_PATH, other)
    c = sqlite3.connect(other); c.execute("DELETE FROM player_stats"); c.commit(); c.close()
    doc = types.SimpleNamespace(file_id="f", file_size=10)
    upd, out = _msg(reply_doc=doc)
    _run(backup.restore_command, upd, FakeBot(str(other)), ["yes"])
    assert db.get_player_stats(-1, 1) is None                      # replaced
    old = sqlite3.connect(db.DB_PATH + ".before_restore")
    assert old.execute("SELECT COUNT(*) FROM player_stats").fetchone()[0] == 1   # previous data kept
    old.close()


def test_non_owner_and_group_chat_are_refused(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _populate()
    for fn in (backup.backup_command, backup.restore_command):
        upd, out = _msg(uid=5)
        _run(fn, upd)
        assert out["docs"] == [] and "Only the bot owner" in out["texts"][0]
        upd, out = _msg(chat="supergroup")
        _run(fn, upd)
        assert out["docs"] == [] and "private chat" in out["texts"][0]


def test_bad_files_are_rejected_and_data_untouched(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _populate()
    junk = tmp_path / "junk.db"; junk.write_bytes(b"definitely not sqlite" * 50)
    wrong = tmp_path / "wrong.db"
    c = sqlite3.connect(wrong); c.execute("CREATE TABLE x(a)"); c.commit(); c.close()
    doc = types.SimpleNamespace(file_id="f", file_size=100)
    for bad in (junk, wrong):
        upd, out = _msg(reply_doc=doc)
        _run(backup.restore_command, upd, FakeBot(str(bad)), ["yes"])
        assert out["texts"][0].startswith("❌")
        assert db.get_player_stats(-1, 7)["total_runs"] == 40


def test_restore_needs_a_replied_file_and_size_limit(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    upd, out = _msg()
    _run(backup.restore_command, upd)
    assert "reply to that file" in out["texts"][0]
    upd, out = _msg(reply_doc=types.SimpleNamespace(file_id="f", file_size=backup.MAX_RESTORE_BYTES + 1))
    _run(backup.restore_command, upd)
    assert "too large" in out["texts"][0]


def test_backup_with_no_database_file(monkeypatch, tmp_path):
    monkeypatch.setattr(admin, "OWNER_IDS", {OWNER})
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "missing.db"))
    upd, out = _msg()
    _run(backup.backup_command, upd)
    assert out["docs"] == [] and "nothing to back up" in out["texts"][0]
