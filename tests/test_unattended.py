"""Tests for unattended operation: background mode, keep-awake, Windows
Update restarts, the PowerShell runner, and uninstall cleanup."""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import datetime

import pytest
from PySide6.QtWidgets import QApplication

import background_mode
import models
import powershell
import windows_update


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


# ------------------------------------------------------------- powershell --

def test_script_file_hash_matches_what_the_elevated_check_computes(tmp_path):
    script = "Write-Output 'héllo ‘x’'\nexit 0\n"
    folder, path, h = powershell._write_script(script)
    try:
        with open(path, "rb") as f:
            raw = f.read()
        assert raw.startswith(b"\xef\xbb\xbf")
        assert hashlib.sha256(raw[3:]).hexdigest().upper() == h
        cmd = powershell._runner_command(path, h)
        assert h in cmd and "exit 97" in cmd and "Invoke-Expression" in cmd
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


def test_temp_script_folder_is_removed_after_run(monkeypatch):
    import subprocess
    seen = {}

    def fake_run(args, **k):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    powershell.run_readonly("exit 0")
    cmd = seen["args"][-1]
    path = cmd.split("ReadAllText('")[1].split("'")[0]
    assert not os.path.exists(path)
    assert "-EncodedCommand" not in seen["args"]


# --------------------------------------------------------- background mode --

def test_task_action_waits_only_on_conanops_not_its_children(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\ConanOps\ConanOps.exe")
    execute, args, workdir = background_mode.task_action()
    assert execute.lower().endswith("powershell.exe")
    assert "Get-Process -Name 'ConanOps'" in args
    assert "--background" in args
    assert "WaitForExit()" in args and " -Wait " not in args
    assert workdir == r"C:\ConanOps"


def test_handoff_and_owner_files(tmp_path, monkeypatch):
    import conanops_paths
    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: str(tmp_path))
    assert background_mode.read_owner() is None
    background_mode.write_owner("background")
    assert background_mode.read_owner() == {"mode": "background", "pid": os.getpid()}
    assert background_mode.background_instance_running() is True
    background_mode.write_owner("window")
    assert background_mode.background_instance_running() is False

    assert background_mode.handoff_requested() is False
    background_mode.request_handoff()
    assert background_mode.handoff_requested() is True
    background_mode.clear_handoff()
    assert background_mode.handoff_requested() is False

    background_mode.clear_owner()
    assert background_mode.read_owner() is None


def test_register_script_runs_unelevated_task_at_normal_priority(monkeypatch):
    monkeypatch.setattr(background_mode.sys, "platform", "win32")
    scripts = []
    monkeypatch.setattr(powershell, "run_privileged", lambda script, timeout=90.0: (scripts.append(script), "ok")[1])
    assert background_mode.register() == "ok"
    s = scripts[0]
    assert "-LogonType S4U" in s and "-RunLevel Limited" in s
    assert "$settings.Priority = 4" in s
    assert "-MultipleInstances IgnoreNew" in s
    assert "-AtStartup" in s


def test_unregister_never_stops_a_running_task(monkeypatch):
    monkeypatch.setattr(background_mode.sys, "platform", "win32")
    scripts = []
    monkeypatch.setattr(powershell, "run_privileged", lambda script, timeout=90.0: (scripts.append(script), "ok")[1])
    background_mode.unregister()
    assert "Unregister-ScheduledTask" in scripts[0]
    assert "Stop-ScheduledTask" not in scripts[0]


# --------------------------------------------------------- windows update --

@pytest.mark.parametrize("window,expected", [
    (("04:00", "06:00"), (6, 0)),        # 18 h cap: 06:00 -> 00:00
    (("03:30", "04:15"), (5, 23)),
    (("01:00", "09:00"), (9, 1)),        # 16 h, no cap needed
    (("00:00", "00:00"), None),
])
def test_compute_active_hours(window, expected):
    assert windows_update.compute_active_hours(*window) == expected


def _window_with(monkeypatch, tmp_path, **cfg_kwargs):
    from ui.main_window import MainWindow
    cfg = models.AppConfig(**cfg_kwargs)
    s = cfg.add_server("Chud")
    win = MainWindow(config=cfg)
    win.RUN_OPS_INLINE = True
    return win, s


def _patch_restart(monkeypatch, pending=True, uptime_hours=10, now=datetime(2026, 10, 5, 4, 30)):
    import psutil
    import time as time_mod
    import ui.main_window as mw
    import process_manager
    calls = {"restart": 0, "stopped": []}
    monkeypatch.setattr(windows_update, "reboot_pending", lambda: pending)
    monkeypatch.setattr(windows_update, "restart_pc", lambda delay_seconds=60: calls.__setitem__("restart", calls["restart"] + 1) or True)
    monkeypatch.setattr(process_manager, "graceful_stop", lambda srv, timeout=15.0: calls["stopped"].append(srv.id) or True)
    monkeypatch.setattr(psutil, "boot_time", lambda: time_mod.time() - uptime_hours * 3600)

    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    monkeypatch.setattr(mw, "datetime", FakeDT)
    return calls


def test_update_restart_happens_in_window_with_nobody_online(monkeypatch, tmp_path):
    win, s = _window_with(monkeypatch, tmp_path, handle_update_restarts=True)
    try:
        calls = _patch_restart(monkeypatch)
        win._known_running[s.id] = True
        win._online_by_server[s.id] = set()
        win._check_windows_update_restart()
        assert calls["restart"] == 1 and calls["stopped"] == [s.id]
        assert win.config.last_update_restart == "2026-10-05"
        win._update_restart_started = False
        win._check_windows_update_restart()
        assert calls["restart"] == 1  # at most once a day
    finally:
        win._really_quit = True
        win.close()


@pytest.mark.parametrize("scenario", ["players_online", "outside_window", "not_pending", "just_booted", "disabled"])
def test_update_restart_waits(monkeypatch, tmp_path, scenario):
    win, s = _window_with(monkeypatch, tmp_path, handle_update_restarts=(scenario != "disabled"))
    try:
        calls = _patch_restart(
            monkeypatch,
            pending=(scenario != "not_pending"),
            uptime_hours=(0.5 if scenario == "just_booted" else 10),
            now=datetime(2026, 10, 5, 12, 0) if scenario == "outside_window" else datetime(2026, 10, 5, 4, 30),
        )
        win._known_running[s.id] = True
        win._online_by_server[s.id] = {"SomePlayer"} if scenario == "players_online" else set()
        win._check_windows_update_restart()
        assert calls["restart"] == 0 and calls["stopped"] == []
    finally:
        win._really_quit = True
        win.close()


def test_keep_awake_follows_running_servers(monkeypatch, tmp_path):
    import power
    states = []
    monkeypatch.setattr(power, "set_keep_awake", lambda on: states.append(on) or True)
    win, s = _window_with(monkeypatch, tmp_path)
    try:
        monkeypatch.setattr(windows_update, "reboot_pending", lambda: False)
        win._known_running[s.id] = False
        win._unattended_tick()
        win._known_running[s.id] = True
        win._unattended_tick()
        win.config.keep_pc_awake = False
        win._unattended_tick()
        assert states[-3:] == [False, True, False]
    finally:
        win._really_quit = True
        win.close()


# ------------------------------------------------------------ disk space --

def test_backup_refuses_when_destination_is_too_full(tmp_path, monkeypatch):
    import shutil
    import backup_manager
    install = tmp_path / "srv"
    saved = install / "ConanSandbox" / "Saved"
    saved.mkdir(parents=True)
    (saved / "game.db").write_bytes(b"x" * 1024)
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage._replace(free=100 * 1024 ** 2))
    with pytest.raises(backup_manager.BackupSpaceError):
        backup_manager.create_backup(str(install), str(tmp_path / "backups"), "manual")


def test_preflight_refuses_to_launch_on_a_full_drive(tmp_path, monkeypatch):
    import shutil
    import preflight
    import steamcmd
    import process_manager
    monkeypatch.setattr(steamcmd, "is_steamcmd_installed", lambda d: True)
    monkeypatch.setattr(steamcmd, "get_install_state", lambda d: 4)
    monkeypatch.setattr(process_manager, "server_exe_path", lambda d: __file__)
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage._replace(free=200 * 1024 ** 2))
    server = models.ServerConfig(install_dir=str(tmp_path), steamcmd_dir=str(tmp_path), bind_ip="")
    result = preflight.run_preflight(server)
    assert not result.ok
    assert any("free on the server's drive" in p for p in result.problems)


# --------------------------------------------------------------- config --

def test_unattended_settings_round_trip(tmp_path):
    path = str(tmp_path / "config.json")
    cfg = models.AppConfig(background_mode_enabled=True, keep_pc_awake=False, handle_update_restarts=True,
                           update_restart_start="03:00", update_restart_end="05:30", last_update_restart="2026-10-01")
    cfg.save(path)
    loaded = models.AppConfig.load(path)
    assert (loaded.background_mode_enabled, loaded.keep_pc_awake, loaded.handle_update_restarts,
            loaded.update_restart_start, loaded.update_restart_end, loaded.last_update_restart) == \
        (True, False, True, "03:00", "05:30", "2026-10-01")


def test_migration_only_copies_conanops_own_files_never_server_installs(tmp_path, monkeypatch):
    import conanops_paths
    old = tmp_path / "old"
    new = tmp_path / "new"
    (old / "sessions").mkdir(parents=True)
    (old / "config.json").write_text("{}")
    (old / "theme.json").write_text("{}")
    (old / "sessions" / "s.json").write_text("{}")
    (old / "c4a83775" / "server").mkdir(parents=True)
    (old / "c4a83775" / "server" / "huge.pak").write_bytes(b"x" * 1024)
    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: str(new))
    monkeypatch.setattr(models.AppConfig, "_migration_candidates", staticmethod(lambda: [str(old)]))
    models.AppConfig._migrate_legacy_data_dir()
    assert (new / "config.json").exists() and (new / "theme.json").exists() and (new / "sessions" / "s.json").exists()
    assert not (new / "c4a83775").exists()


def test_update_restart_uses_restart_with_automatic_sign_on(monkeypatch):
    import subprocess
    seen = {}
    monkeypatch.setattr(windows_update.sys, "platform", "win32")
    monkeypatch.setattr(windows_update, "hidden_window_kwargs", lambda: {})
    monkeypatch.setattr(subprocess, "run", lambda args, **k: seen.setdefault("args", args) and subprocess.CompletedProcess(args, 0, "", ""))
    assert windows_update.restart_pc(60)
    assert seen["args"][:2] == ["shutdown", "/g"]


def test_background_option_hidden_unless_already_on(monkeypatch, tmp_path):
    from tests.test_app_settings_startup import _make_page
    monkeypatch.setattr(background_mode, "status", lambda: None)
    for enabled, visible in ((False, False), (True, True)):
        page = _make_page(models.AppConfig(background_mode_enabled=enabled))
        assert page.background_checkbox.isVisibleTo(page) is visible


@pytest.mark.parametrize("policy,opt_out,expected", [
    (1, None, "blocked"), (None, 1, "off"), (None, 0, "on"), (None, None, "unknown"),
])
def test_auto_sign_in_status(monkeypatch, policy, opt_out, expected):
    import types
    monkeypatch.setattr(windows_update.sys, "platform", "win32")
    monkeypatch.setattr(windows_update, "_current_user_sid", lambda: "S-1-5-21-1-2-3-1001")
    values = {("Policies\\System", "DisableAutomaticRestartSignOn"): policy,
              ("UserARSO\\S-1-5-21-1-2-3-1001", "OptOut"): opt_out}

    class Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def open_key(root, path, *a):
        return Key(path)

    def query(key, name):
        for (frag, n), v in values.items():
            if n == name and key.path.endswith(frag) and v is not None:
                return (v, 4)
        raise OSError("missing")

    fake = types.SimpleNamespace(HKEY_LOCAL_MACHINE=1, KEY_READ=1, KEY_WOW64_64KEY=0, OpenKey=open_key,
                                 QueryValueEx=query)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert windows_update.auto_sign_in_status() == expected
