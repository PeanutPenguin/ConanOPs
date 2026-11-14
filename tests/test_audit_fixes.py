"""Fixes from the November 2026 code audit."""
from __future__ import annotations

import json
import os

import backup_manager
import models
import process_manager
from tests.test_server_finder import _make_install, _window


def _cfg(tmp_path, names=("A",)):
    cfg = models.AppConfig()
    for name in names:
        root = tmp_path / name
        _make_install(root)
        s = models.ServerConfig(name=name, install_dir=str(root), steamcmd_dir=str(tmp_path / "steamcmd"))
        s.installed_buildid = "1"
        cfg.servers.append(s)
    cfg.active_server_id = cfg.servers[0].id
    return cfg


def _quiet(win, monkeypatch):
    sent = []
    monkeypatch.setattr(win, "_notify", lambda server, message, title="": sent.append((title, message)))
    return sent


# ------------------------------------------- 1. auto-update off means off --

def test_scheduled_restart_doesnt_update_when_automatic_updates_are_off(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    win = _window(monkeypatch, cfg)
    _quiet(win, monkeypatch)
    s = cfg.servers[0]
    calls = []
    monkeypatch.setattr(win, "_start_update_apply", lambda srv, **k: calls.append("update"))
    monkeypatch.setattr(win, "_restart_unattended", lambda srv: calls.append("restart"))
    s.auto_update = False
    win._on_restart_update_check(s, "2")
    assert calls == ["restart"]
    s.auto_update = True
    win._on_restart_update_check(s, "2")
    assert calls == ["restart", "update"]


# ------------------------------ 2. a held server starts after its update --

def test_update_found_for_a_held_server_starts_it_afterwards(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    win = _window(monkeypatch, cfg)
    _quiet(win, monkeypatch)
    s = cfg.servers[0]
    seen = []
    monkeypatch.setattr(win, "_start_update_apply", lambda srv, relaunch_after=False: seen.append(relaunch_after))
    win._known_running[s.id] = False
    s.desired_running, s.update_hold = True, "The last update failed."
    win._on_periodic_update_check(s, "2")
    s.update_hold = ""
    win._on_periodic_update_check(s, "3")
    assert seen == [True, False]


# ------------------------------------ 3. disk space checked before the stop --

def test_full_drive_stops_the_update_before_the_server_is_stopped(monkeypatch):
    import steamcmd
    from update_runner import UpdateWorker
    stopped = []
    monkeypatch.setattr(process_manager, "is_running", lambda d: True)
    monkeypatch.setattr(process_manager, "graceful_stop", lambda s: stopped.append(s) or True)
    monkeypatch.setattr(steamcmd, "check_update_disk_space", lambda d: (False, 1024 ** 3, 15 * 1024 ** 3))
    monkeypatch.setattr(steamcmd, "update_server", lambda *a, **k: stopped.append("ran"))
    s = models.ServerConfig(name="x", install_dir="/tmp/x")
    res = UpdateWorker("/tmp/steamcmd", "/tmp/x", stop_server=s)._run_steps()
    assert not res.success and res.reason == "disk" and stopped == []


# ------------------------------------------ 4. one backup folder per server --

def test_default_backup_folder_avoids_one_another_server_uses(tmp_path):
    a, b = str(tmp_path / "A"), str(tmp_path / "B")
    first = backup_manager.default_backup_destination(a)
    second = backup_manager.default_backup_destination(b, taken=[first])
    third = backup_manager.default_backup_destination(b, taken=[first, second])
    assert first == str(tmp_path / "backups")
    assert second == str(tmp_path / "backups-B")
    assert len({first, second, third}) == 3


def test_side_by_side_servers_get_separate_backup_folders(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, names=("A", "B"))
    a, b = cfg.servers
    a.backup_destination = backup_manager.default_backup_destination(a.install_dir)
    win = _window(monkeypatch, cfg)
    _quiet(win, monkeypatch)
    cfg.active_server_id = b.id
    win._load_active_server()
    assert b.backup_destination and b.backup_destination != a.backup_destination


def test_choosing_another_servers_backup_folder_is_refused(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    cfg = _cfg(tmp_path, names=("A", "B"))
    a, b = cfg.servers
    a.backup_destination = str(tmp_path / "a-backups")
    b.backup_destination = str(tmp_path / "b-backups")
    win = _window(monkeypatch, cfg)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *args, **k: warned.append(args[1])))
    win._apply_backups_settings({"backup_destination": a.backup_destination, "backup_daily_keep": 9}, b)
    assert b.backup_destination == str(tmp_path / "b-backups") and b.backup_daily_keep == 9
    assert warned == ["Folder Already Used"]


# ------------------- 5. the daily refresh leaves running servers to the watch --

def test_daily_mod_refresh_waits_while_a_watched_server_is_running(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    win = _window(monkeypatch, cfg)
    s = cfg.servers[0]
    s.mods = [{"id": "111", "enabled": True, "name": "Mod"}]
    s.mod_auto_update = True
    monkeypatch.setattr(process_manager, "is_running", lambda d: True)
    win._handle_mod_refresh_due(s)
    assert s.id not in win._mod_refresh_workers and not s.last_mod_check_at


# --------------------------------------------- 6. the save command's reply --

def test_world_save_request_reads_the_servers_reply(monkeypatch):
    import rcon
    s = models.ServerConfig(name="x", rcon_enabled=True, rcon_password="p")
    replies = iter(["World saved.", "Unknown command: saveworld", ""])
    monkeypatch.setattr(rcon, "send_command", lambda *a, **k: next(replies))
    assert process_manager.request_world_save(s) is True
    assert process_manager.request_world_save(s) is False
    assert process_manager.request_world_save(s) is True

    def down(*a, **k):
        raise rcon.RconError("no")
    monkeypatch.setattr(rcon, "send_command", down)
    assert process_manager.request_world_save(s) is False
    s.rcon_enabled = False
    assert process_manager.request_world_save(s) is False


# ------------------------------------------------ 7. rollback asks to restart --

def test_rollback_marks_that_this_run_must_restart(tmp_path, monkeypatch):
    import self_update
    marker = tmp_path / "pending.json"
    install, backup = tmp_path / "app", tmp_path / "backup"
    install.mkdir()
    backup.mkdir()
    (backup / "main.py").write_text("old")
    (install / "main.py").write_text("new")
    marker.write_text(json.dumps({"backup_dir": str(backup), "install_dir": str(install), "attempted": True}))
    monkeypatch.setattr(self_update, "_pending_marker_path", lambda: str(marker))
    monkeypatch.setattr(self_update, "rolled_back_this_run", False)
    assert self_update.check_and_recover_pending_update()
    assert self_update.rolled_back_this_run is True
    assert (install / "main.py").read_text() == "old"


def test_first_run_after_an_update_doesnt_ask_to_restart(tmp_path, monkeypatch):
    import self_update
    marker = tmp_path / "pending.json"
    marker.write_text(json.dumps({"backup_dir": "b", "install_dir": "i", "attempted": False}))
    monkeypatch.setattr(self_update, "_pending_marker_path", lambda: str(marker))
    monkeypatch.setattr(self_update, "rolled_back_this_run", False)
    assert self_update.check_and_recover_pending_update() is None
    assert self_update.rolled_back_this_run is False


# ------------------- 8. a refused Windows Update restart starts servers again --

def test_servers_start_again_when_the_update_restart_is_refused(tmp_path, monkeypatch):
    import psutil
    import windows_update
    cfg = _cfg(tmp_path)
    win = _window(monkeypatch, cfg)
    sent = _quiet(win, monkeypatch)
    s = cfg.servers[0]
    s.desired_running = True
    win._known_running[s.id] = True
    cfg.handle_update_restarts = True
    cfg.update_restart_start, cfg.update_restart_end, cfg.last_update_restart = "00:00", "23:59", ""
    monkeypatch.setattr(psutil, "boot_time", lambda: 0.0)
    monkeypatch.setattr(windows_update, "reboot_pending", lambda: True)
    monkeypatch.setattr(windows_update, "restart_pc", lambda **k: False)
    monkeypatch.setattr(process_manager, "graceful_stop", lambda srv: True)
    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda srv: launched.append(srv.id))
    win._check_windows_update_restart()
    assert launched == [s.id]
    assert win._known_running[s.id] is True and s.id not in win._expected_stop
    assert any(title == "Windows Update Restart Failed" for title, _m in sent)
    assert os.path.isdir(s.install_dir)
