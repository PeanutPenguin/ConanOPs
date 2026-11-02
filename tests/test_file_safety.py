"""1.0.18: state files survive crashes and odd encodings."""
import json
import os

import fileio
import ini_utils
import models
import session_tracker


def test_atomic_write_replaces_and_leaves_no_temp_files(tmp_path):
    p = tmp_path / "a" / "x.txt"
    fileio.write_text_atomic(str(p), "one")
    fileio.write_text_atomic(str(p), "two")
    assert p.read_text(encoding="utf-8") == "two"
    assert os.listdir(p.parent) == ["x.txt"]


def test_atomic_write_falls_back_when_the_file_is_held_open(tmp_path, monkeypatch):
    p = tmp_path / "x.txt"
    p.write_text("old", encoding="utf-8")

    def locked(src, dst):
        raise PermissionError("in use")
    monkeypatch.setattr(fileio.os, "replace", locked)
    fileio.write_text_atomic(str(p), "new")
    assert p.read_text(encoding="utf-8") == "new" and os.listdir(tmp_path) == ["x.txt"]


def test_utf16_ini_from_unreal_is_read_and_written_back_as_utf16(tmp_path):
    p = tmp_path / "Engine.ini"
    p.write_bytes("[OnlineSubsystemSteam]\r\nServerName=Chüdville\r\n".encode("utf-16"))
    assert ini_utils.read_known_keys(str(p), {"ServerName": "OnlineSubsystemSteam"}) == {"ServerName": "Chüdville"}
    ini_utils.apply_known_keys(str(p), {"ServerPassword": ("OnlineSubsystemSteam", "pw")})
    raw = p.read_bytes()
    assert raw.startswith(b"\xff\xfe")
    text = raw.decode("utf-16")
    assert "ServerName=Chüdville" in text and "ServerPassword=pw" in text and text.count("[OnlineSubsystemSteam]") == 1


def test_byte_order_mark_doesnt_hide_the_first_section(tmp_path):
    p = tmp_path / "Game.ini"
    p.write_bytes(b"\xef\xbb\xbf[RconPlugin]\r\nRconEnabled=0\r\n")
    assert ini_utils.read_known_keys(str(p), {"RconEnabled": "RconPlugin"}) == {"RconEnabled": "0"}
    ini_utils.apply_known_keys(str(p), {"RconEnabled": ("RconPlugin", "1")})
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf") and raw.count(b"[RconPlugin]") == 1 and b"RconEnabled=1" in raw


def test_damaged_config_falls_back_to_the_previous_save(tmp_path):
    path = str(tmp_path / "config.json")
    cfg = models.AppConfig()
    cfg.add_server("Chudville")
    cfg.save(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"servers": [')  # cut off mid-write
    loaded = models.AppConfig.load(path)
    assert [s.name for s in loaded.servers] == ["Chudville"]
    assert os.path.exists(path + ".damaged")


def test_unreadable_config_without_a_copy_starts_fresh(tmp_path):
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\x00\x00")
    assert models.AppConfig.load(path).servers == []


def test_damaged_player_history_doesnt_block_the_server(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text('{"sessions": [{"name": "a", "start": "2026-', encoding="utf-8")
    t = session_tracker.SessionTracker(str(path))
    assert t.sessions == []
    t.player_joined("Oran")
    assert json.loads(path.read_text(encoding="utf-8"))["sessions"][0]["name"] == "Oran"
    assert (tmp_path / "sessions.json.damaged").exists()


def test_rcon_rejects_a_reply_that_isnt_rcon():
    import socket
    import struct
    import threading
    import pytest
    import rcon
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        c, _ = srv.accept()
        c.recv(1024)
        c.sendall(struct.pack("<i", 0x7FFFFFF0))  # an absurd length
        c.close()
    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(rcon.RconError):
        rcon.send_command("127.0.0.1", srv.getsockname()[1], "pw", "listplayers", timeout=2)
    srv.close()


# ------------------------------------------------------------ backups --

def _server_with_save(tmp_path):
    saved = tmp_path / "srv" / "ConanSandbox" / "Saved"
    saved.mkdir(parents=True)
    (saved / "game.db").write_bytes(b"world")
    return str(tmp_path / "srv"), saved


def test_restore_never_writes_outside_the_saved_folder(tmp_path):
    import zipfile
    import backup_manager
    install, saved = _server_with_save(tmp_path)
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("Saved/game.db", b"restored")
        zf.writestr("Saved/../../../escaped.ini", b"x")
        zf.writestr("x/Saved/../../../../escaped2.db", b"x")
    entry = backup_manager.BackupEntry(path=str(evil), when=None, trigger="imported", size_bytes=0)
    backup_manager.restore_backup(install, entry)
    assert (saved / "game.db").read_bytes() == b"restored"
    assert not any(p.name.startswith("escaped") for p in tmp_path.rglob("*"))


def test_a_failed_backup_leaves_nothing_that_looks_like_a_backup(tmp_path, monkeypatch):
    import zipfile
    import pytest
    import backup_manager
    install, _saved = _server_with_save(tmp_path)
    dest = tmp_path / "backups"
    real_write = zipfile.ZipFile.write

    def disk_full(self, *a, **k):
        real_write(self, *a, **k)
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(zipfile.ZipFile, "write", disk_full)
    with pytest.raises(OSError):
        backup_manager.create_backup(install, str(dest), "scheduled")
    assert backup_manager.list_backups(str(dest)) == [] and list(dest.iterdir()) == []


def test_two_backups_in_the_same_second_both_survive(tmp_path):
    import backup_manager
    install, _saved = _server_with_save(tmp_path)
    a = backup_manager.create_backup(install, str(tmp_path / "b"), "manual")
    b = backup_manager.create_backup(install, str(tmp_path / "b"), "manual")
    assert a.path != b.path and len(backup_manager.list_backups(str(tmp_path / "b"))) == 2
    assert {e.trigger for e in backup_manager.list_backups(str(tmp_path / "b"))} == {"manual"}
