"""2.0 backups: consistent copies of a live world, checked by SQLite,
retention by days and weeks, and a second copy in another folder."""
from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from datetime import datetime, timedelta

import pytest

import backup_manager as bm
import models
from tests.worlds import read_world, world_bytes, write_world


def _install(tmp_path, text="world data", wal=False):
    root = tmp_path / "server"
    saved = root / "ConanSandbox" / "Saved"
    (saved / "Config" / "WindowsServer").mkdir(parents=True)
    (saved / "Config" / "WindowsServer" / "ServerSettings.ini").write_text("[ServerSettings]\nMaxPlayers=10\n")
    write_world(saved / "game.db", text)
    if wal:
        con = sqlite3.connect(saved / "game.db")
        con.execute("PRAGMA journal_mode=WAL")
        con.close()
    return str(root), saved


def _zip_world(entry, tmp_path):
    out = tmp_path / "unzipped"
    with zipfile.ZipFile(entry.path) as zf:
        zf.extractall(out)
        names = zf.namelist()
    return out, names


def test_live_wal_world_is_copied_whole_including_unmerged_changes(tmp_path):
    """The server holds the database open with changes still in its -wal:
    the backup must contain them, as one consistent database."""
    root, saved = _install(tmp_path, wal=True)
    live = sqlite3.connect(saved / "game.db")
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("UPDATE marker SET v='newest'")
    live.commit()  # in the -wal only, not merged into game.db
    assert os.path.getsize(str(saved / "game.db") + "-wal") > 0
    entry = bm.create_backup(root, str(tmp_path / "bk"), "manual", live=True)
    live.close()
    out, names = _zip_world(entry, tmp_path)
    assert entry.verified
    assert not any(n.endswith("-wal") for n in names)  # one self-contained database
    assert read_world(out / "Saved" / "game.db") == "newest"


def test_backup_has_a_manifest_and_the_settings(tmp_path):
    root, _saved = _install(tmp_path)
    entry = bm.create_backup(root, str(tmp_path / "bk"), "manual", live=False, info={"server": "Chud"})
    with zipfile.ZipFile(entry.path) as zf:
        manifest = json.loads(zf.read(bm.MANIFEST_NAME))
        assert any(n.endswith("ServerSettings.ini") for n in zf.namelist())
    assert manifest["server"] == "Chud" and manifest["verified"] is True and manifest["worlds"] == ["game.db"]


def test_a_damaged_world_is_backed_up_but_flagged(tmp_path):
    root, saved = _install(tmp_path)
    (saved / "game.db").write_bytes(b"not a database at all" * 100)
    entry = bm.create_backup(root, str(tmp_path / "bk"), "manual", live=False)
    assert entry is not None and entry.verified is False


def test_live_old_journal_world_waits_for_the_save_then_copies_the_file(tmp_path, monkeypatch):
    root, saved = _install(tmp_path)
    used_sqlite, settled = [], []
    real = bm._sqlite_copy
    monkeypatch.setattr(bm, "_sqlite_copy", lambda s, d: used_sqlite.append(1) or real(s, d))
    entry = bm.create_backup(root, str(tmp_path / "bk"), "manual", live=True, settle=lambda: settled.append(1))
    assert entry.verified and not used_sqlite and settled


def test_restore_checks_the_backup_before_touching_the_world(tmp_path):
    root, saved = _install(tmp_path, "current")
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("Saved/game.db", b"garbage" * 50)
    entry = bm.BackupEntry(str(bad), datetime.now(), "imported", 0)
    with pytest.raises(OSError, match="damaged"):
        bm.restore_backup(root, entry)
    assert read_world(saved / "game.db") == "current"


def test_restore_puts_the_world_back_and_clears_stale_wal(tmp_path):
    root, saved = _install(tmp_path, "old")
    entry = bm.create_backup(root, str(tmp_path / "bk"), "manual", live=False)
    write_world(saved / "game.db", "new")
    (saved / "game.db-wal").write_bytes(b"stale")
    bm.restore_backup(root, entry)
    assert read_world(saved / "game.db") == "old" and not (saved / "game.db-wal").exists()
    assert any(e.trigger == bm.TRIGGER_SAFETY for e in bm.list_backups(str(tmp_path / "bk")))


def test_restore_stops_if_the_safety_copy_cant_be_made(tmp_path, monkeypatch):
    root, saved = _install(tmp_path, "current")
    entry = bm.create_backup(root, str(tmp_path / "bk"), "manual", live=False)
    monkeypatch.setattr(bm, "create_backup", lambda *a, **k: None)
    with pytest.raises(OSError, match="safety copy"):
        bm.restore_backup(root, entry)


# -------------------------------------------------------------- retention --

def _fake(dest, when, trigger="scheduled"):
    name = when.strftime("%Y%m%d-%H%M%S") + f"_{trigger}.zip"
    with zipfile.ZipFile(os.path.join(dest, name), "w") as zf:
        zf.writestr("Saved/x.txt", "x")
    return name


def test_daily_means_days_and_weekly_means_weeks(tmp_path):
    dest = str(tmp_path)
    now = datetime(2026, 11, 10, 12, 0)
    for h in range(0, 24 * 40, 6):  # every 6 hours for 40 days
        _fake(dest, now - timedelta(hours=h))
    bm.prune_backups(dest, daily_keep=7, weekly_keep=4, now=now)
    kept = sorted(bm.list_backups(dest), key=lambda e: e.when)
    days = {e.when.date() for e in kept}
    recent = [e for e in kept if now - e.when <= timedelta(hours=24)]
    assert len(recent) == 5  # everything from the last 24 hours
    assert all((now - timedelta(days=d)).date() in days for d in range(7))  # one a day for 7 days
    older = [e for e in kept if e.when.date() < (now - timedelta(days=6)).date()]
    assert len({e.when.isocalendar()[:2] for e in older}) == len(older) == 4  # then one a week, 4 weeks


def test_safety_copies_are_thinned_but_manual_ones_never(tmp_path):
    dest = str(tmp_path)
    now = datetime(2026, 11, 10, 12, 0)
    for d in range(60):
        _fake(dest, now - timedelta(days=d, hours=1), "pre-update")
        _fake(dest, now - timedelta(days=d, hours=2), "manual")
    bm.prune_backups(dest, 7, 4, now=now)
    kept = bm.list_backups(dest)
    pre = [e for e in kept if e.trigger == "pre-update"]
    assert len(pre) == bm.SAFETY_KEEP_DAYS and len([e for e in kept if e.trigger == "manual"]) == 60


def test_never_ends_with_no_backups(tmp_path):
    dest = str(tmp_path)
    _fake(dest, datetime(2020, 1, 1))
    bm.prune_backups(dest, 0, 0, now=datetime(2026, 1, 1))
    assert len(bm.list_backups(dest)) == 1


# ------------------------------------------------------------ second copy --

def test_second_copy_catches_up_and_keeps_its_own_retention(tmp_path):
    main, copy = tmp_path / "main", tmp_path / "copy"
    main.mkdir()
    now = datetime.now()
    names = [_fake(str(main), now - timedelta(hours=h)) for h in range(3)]
    copied, err = bm.sync_copies(str(main), str(copy), 7, 4)
    assert err == "" and sorted(os.listdir(copy)) == sorted(names)
    (main / names[0]).unlink()  # deleted from the main folder: the copy keeps it
    assert bm.sync_copies(str(main), str(copy), 7, 4) == ([], "")
    assert names[0] in os.listdir(copy)


def test_second_copy_same_folder_or_missing_drive(tmp_path):
    main = tmp_path / "main"
    main.mkdir()
    _fake(str(main), datetime.now())
    assert bm.sync_copies(str(main), str(main), 7, 4) == ([], "")
    blocker = tmp_path / "file"
    blocker.write_text("x")
    copied, err = bm.sync_copies(str(main), str(blocker / "sub"), 7, 4)
    assert copied == [] and "Couldn't open the second backup folder" in err


def test_backup_job_makes_copies_and_reports(tmp_path):
    root, _saved = _install(tmp_path)
    s = models.ServerConfig(name="x", install_dir=root, backup_destination=str(tmp_path / "bk"))
    s.backup_copy_destination = str(tmp_path / "copy")
    out = bm.run_backup_job(s, bm.TRIGGER_MANUAL)
    assert out.entry is not None and out.error == "" and out.copy_error == "" and out.copied == 1
    assert os.listdir(tmp_path / "copy") == [os.path.basename(out.entry.path)]


def test_backup_job_explains_failures(tmp_path, monkeypatch):
    s = models.ServerConfig(name="x", install_dir=str(tmp_path / "none"), backup_destination=str(tmp_path / "bk"))
    assert "no world save" in bm.run_backup_job(s, "manual").error
    root, _saved = _install(tmp_path)
    s.install_dir = root

    def disk_full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(bm, "create_backup", disk_full)
    assert "couldn't be written" in bm.run_backup_job(s, "manual").error


def test_space_check_counts_the_working_copy(tmp_path, monkeypatch):
    root, saved = _install(tmp_path)
    size = os.path.getsize(saved / "game.db")
    import shutil
    free = size + bm._BACKUP_HEADROOM_BYTES + 10  # enough for one copy, not two
    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"free": free})())
    with pytest.raises(bm.BackupSpaceError):
        bm.create_backup(root, str(tmp_path / "bk"), "manual", live=False)


# ---------------------------------------------------------- in the window --

def test_scheduled_backup_runs_in_the_background_and_retries_later(tmp_path, monkeypatch):
    from tests.test_server_finder import _window
    import ui.window.backups as wb
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    said = []
    monkeypatch.setattr(win, "_notify", lambda s, m, title="": said.append(title))
    try:
        s = cfg.add_server("x")
        s.install_dir, s.backup_destination = str(tmp_path / "none"), str(tmp_path / "bk")
        os.makedirs(os.path.join(s.install_dir, "ConanSandbox"))
        win._handle_scheduled_backup(s)  # nothing to back up: fails
        assert said == ["Backup Failed"] and s.last_backup_at == ""
        win._handle_scheduled_backup(s)  # not retried straight away
        assert said == ["Backup Failed"]
        monkeypatch.setattr(wb.time, "monotonic", lambda: 10 ** 9)
        root, _saved = _install(tmp_path)
        s.install_dir = root
        win._handle_scheduled_backup(s)
        assert s.last_backup_at and len(bm.list_backups(s.backup_destination)) == 1
    finally:
        win.close()


def test_restore_waits_for_a_running_backup(tmp_path, monkeypatch):
    from tests.test_server_finder import _window
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    said = []
    monkeypatch.setattr(win, "_notify", lambda s, m, title="": said.append(title))
    try:
        s = cfg.add_server("x")
        win._backup_workers[s.id] = object()
        win._handle_restore(s, None)
        assert said == ["Restore Not Started"] and s.id not in win._restore_workers
        assert win.server_busy(s) == "Backing up"
    finally:
        win._backup_workers.clear()
        win.close()
