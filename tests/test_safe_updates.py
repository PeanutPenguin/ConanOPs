"""Safer server updates: stop-then-backup order, disk check, no relaunch
of the old build after a failed update (held + retried), post-update
crash watch, RCON on by default, and stops off the UI thread."""
from __future__ import annotations

import sys
import time

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402

import models  # noqa: E402
import process_manager  # noqa: E402
import steamcmd  # noqa: E402
import update_runner  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


# ---------------------------------------------------------- UpdateWorker --

def _record_steps(monkeypatch, *, disk_ok=True, backup_raises=None):
    import backup_manager
    calls = []
    monkeypatch.setattr(process_manager, "is_running", lambda d: True)
    monkeypatch.setattr(process_manager, "graceful_stop", lambda s, **k: calls.append("stop"))

    def backup(server, dest, trigger, label=""):
        calls.append("backup")
        if backup_raises:
            raise backup_raises
    monkeypatch.setattr(backup_manager, "create_backup_for_server", backup)
    monkeypatch.setattr(steamcmd, "check_update_disk_space",
                        lambda d: (calls.append("disk") or (disk_ok, 1, 2)))
    monkeypatch.setattr(steamcmd, "update_server",
                        lambda *a, **k: calls.append("steamcmd") or steamcmd.UpdateResult(True, "ok", "9"))
    return calls


def _run_worker(server, **kw):
    w = update_runner.UpdateWorker("/steamcmd", "/install", stop_server=server, backup_server=server,
                                   backup_destination="/backups", **kw)
    out = []
    w.finished_update.connect(out.append)
    w.run()
    return out[0]


def test_update_stops_first_then_backs_up_then_checks_disk_then_updates(monkeypatch):
    calls = _record_steps(monkeypatch)
    result = _run_worker(models.ServerConfig(name="x", install_dir="/install"))
    assert calls == ["stop", "backup", "disk", "steamcmd"]
    assert result.success


def test_failed_backup_stops_before_steamcmd(monkeypatch):
    calls = _record_steps(monkeypatch, backup_raises=OSError("folder missing"))
    result = _run_worker(models.ServerConfig(name="x", install_dir="/install"))
    assert "steamcmd" not in calls and result.reason == "backup"
    assert "nothing was changed" in result.output


def test_low_disk_stops_before_steamcmd(monkeypatch):
    calls = _record_steps(monkeypatch, disk_ok=False)
    result = _run_worker(models.ServerConfig(name="x", install_dir="/install"))
    assert "steamcmd" not in calls and result.reason == "disk"


def test_disk_check_needs_the_larger_of_floor_and_install_size(monkeypatch, tmp_path):
    import shutil
    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"free": 20 * 1024 ** 3})())
    monkeypatch.setattr(steamcmd, "folder_size", lambda p: 30 * 1024 ** 3)
    ok, free, needed = steamcmd.check_update_disk_space(str(tmp_path))
    assert not ok and needed == 30 * 1024 ** 3
    monkeypatch.setattr(steamcmd, "folder_size", lambda p: 1)
    assert steamcmd.check_update_disk_space(str(tmp_path))[0] is True  # 20 GB free >= 15 GB floor


# ------------------------------------------------------ hold after failure --

def _window(monkeypatch):
    import mod_manager
    from ui import main_window as mw
    monkeypatch.setattr(mod_manager, "write_modlist", lambda *a, **k: None)
    cfg = models.AppConfig()
    s = cfg.add_server("Chudville")
    s.install_dir = "/tmp/fake-install"
    cfg.active_server_id = s.id
    monkeypatch.setattr(cfg, "save", lambda *a, **k: None)
    win = mw.MainWindow(config=cfg)
    notes = []
    monkeypatch.setattr(win, "_notify", lambda srv, msg, title="": notes.append((title, msg)))
    return win, s, notes, mw


def test_failed_update_leaves_server_stopped_and_schedules_a_retry(monkeypatch):
    win, s, notes, mw = _window(monkeypatch)
    timers = []
    monkeypatch.setattr(mw.QTimer, "singleShot", lambda ms, fn: timers.append((ms, fn)))
    relaunched = []
    monkeypatch.setattr(win, "_update_mods_then_relaunch", lambda *a, **k: relaunched.append(1))
    try:
        win._on_update_apply_finished(s, steamcmd.UpdateResult(False, "log", reason="steamcmd"), was_running=True)
        assert relaunched == []
        assert s.update_hold and any("Held" in t for t, _ in notes)
        assert timers and timers[0][0] == win._UPDATE_RETRY_MINUTES * 60 * 1000
        started = []
        monkeypatch.setattr(win, "_start_update_apply", lambda srv, relaunch_after=False: started.append(relaunch_after))
        timers[0][1]()
        assert started == [True]  # retry, and start it afterwards
    finally:
        win.close()


def test_failure_when_server_was_already_stopped_doesnt_hold(monkeypatch):
    win, s, notes, mw = _window(monkeypatch)
    try:
        win._on_update_apply_finished(s, steamcmd.UpdateResult(False, "log", reason="steamcmd"), was_running=False)
        assert s.update_hold == "" and notes[0][0] == "Update Failed"
    finally:
        win.close()


def test_held_server_is_not_watchdog_restarted_or_auto_resumed(monkeypatch):
    win, s, notes, mw = _window(monkeypatch)
    try:
        s.desired_running = True
        s.update_hold = "SteamCMD couldn't finish."
        attempts = []
        monkeypatch.setattr(win, "_attempt_watchdog_restart", lambda *a, **k: attempts.append(1))
        win._known_running[s.id] = True
        win._on_health_check_finished([(s.id, False, None)])
        assert attempts == []
        launched = []
        monkeypatch.setattr(process_manager, "is_running", lambda d: False)
        monkeypatch.setattr(process_manager, "launch", lambda srv: launched.append(srv))
        win._auto_resume_servers()
        assert launched == [] and any(t == "Server Held" for t, _ in notes)
    finally:
        win.close()


def test_success_clears_the_hold(monkeypatch):
    win, s, notes, mw = _window(monkeypatch)
    monkeypatch.setattr(win, "_update_mods_then_relaunch", lambda *a, **k: None)
    try:
        s.update_hold = "x"
        win._on_update_apply_finished(s, steamcmd.UpdateResult(True, "ok", "10"), was_running=True)
        assert s.update_hold == ""
    finally:
        win.close()


def test_manual_update_failure_also_holds(monkeypatch):
    win, s, notes, mw = _window(monkeypatch)
    monkeypatch.setattr(mw.QTimer, "singleShot", lambda ms, fn: None)
    relaunched = []
    monkeypatch.setattr(win, "_update_mods_then_relaunch", lambda *a, **k: relaunched.append(1))
    try:
        win._relaunch_after_manual_update(s, True, steamcmd.UpdateResult(False, "No space", reason="disk"))
        assert relaunched == [] and "No space" in s.update_hold
    finally:
        win.close()


# --------------------------------------------------------- post-update watch --

def test_repeat_crash_after_update_holds_and_names_stale_mods(monkeypatch):
    win, s, notes, mw = _window(monkeypatch)
    import steam_workshop_api as swa
    import workshop_search_runner as wsr
    s.desired_running = True
    s.mods = [{"id": "1", "name": "Fresh", "enabled": True}, {"id": "2", "name": "Emberlight", "enabled": True}]

    def fake_start(self):
        self.finished_status.emit(swa.DetailsResult(ok=True, items={
            "1": swa.WorkshopItem("1", "Fresh", "", "", 1, status=swa.STATUS_UPDATED),
            "2": swa.WorkshopItem("2", "Emberlight", "", "", 1, status=swa.STATUS_STALE),
        }))
    monkeypatch.setattr(wsr.ModStatusWorker, "start", fake_start)
    attempts = []
    monkeypatch.setattr(win, "_attempt_watchdog_restart", lambda *a, **k: attempts.append(1))
    monkeypatch.setattr(process_manager, "is_running", lambda d: False)
    try:
        win._begin_post_update_watch(s)
        win._known_running[s.id] = True
        win._on_health_check_finished([(s.id, False, None)])
        assert attempts == [1]  # first crash: one normal watchdog try
        win._known_running[s.id] = True
        win._on_health_check_finished([(s.id, False, None)])
        assert attempts == [1]  # second: held instead
        assert s.update_hold
        report = [m for t, m in notes if t == "Update Broke the Server"]
        assert report and "Emberlight" in report[0] and "Fresh" not in report[0]
    finally:
        win.close()


def test_watch_expires(monkeypatch):
    win, s, notes, mw = _window(monkeypatch)
    try:
        win._begin_post_update_watch(s)
        win._post_update_watch[s.id]["until"] = time.monotonic() - 1
        assert win._post_update_failure(s, "crashed") is False
        assert s.id not in win._post_update_watch
    finally:
        win.close()


def test_start_overrides_the_hold(monkeypatch):
    import preflight
    win, s, notes, mw = _window(monkeypatch)
    monkeypatch.setattr(process_manager, "is_running", lambda d: False)
    monkeypatch.setattr(process_manager, "launch", lambda srv: None)
    monkeypatch.setattr(preflight, "run_preflight", lambda srv: preflight.CheckResult(ok=True, problems=[], repairs=[]))
    try:
        s.update_hold = "x"
        win._handle_start(s)
        assert s.update_hold == ""
    finally:
        win.close()


# --------------------------------------------------------------------- RCON --

def test_new_servers_get_rcon_with_password_and_unique_ports():
    cfg = models.AppConfig()
    a, b = cfg.add_server("A"), cfg.add_server("B")
    assert a.rcon_enabled and b.rcon_enabled
    assert len(a.rcon_password) >= 12 and a.rcon_password != b.rcon_password
    assert a.rcon_port != b.rcon_port


def test_rcon_ini_synced_once_and_only_when_different(tmp_path):
    s = models.ServerConfig(name="x", install_dir=str(tmp_path), rcon_enabled=True, rcon_password="pw", rcon_port=25580)
    assert process_manager.sync_rcon_ini(s) is True
    text = open(process_manager.game_ini_path(str(tmp_path))).read()
    assert "RconEnabled=1" in text and "RconPassword=pw" in text and "RconPort=25580" in text
    assert process_manager.sync_rcon_ini(s) is False  # already matches: no rewrite, no extra ini backup


def test_launch_syncs_rcon_first(monkeypatch, tmp_path):
    synced = []
    monkeypatch.setattr(process_manager, "sync_rcon_ini", lambda srv: synced.append(srv))
    monkeypatch.setattr(process_manager.os.path, "exists", lambda p: True)
    monkeypatch.setattr(process_manager.subprocess, "Popen", lambda *a, **k: None)
    process_manager.launch(models.ServerConfig(name="x", install_dir=str(tmp_path)))
    assert synced


def test_dashboard_offers_to_turn_rcon_on(monkeypatch):
    win, s, notes, mw = _window(monkeypatch)
    try:
        s.rcon_enabled = False
        win._refresh_chrome()
        assert not win.dashboard_page.rcon_notice.isHidden()
        win._enable_rcon_for_active()
        assert s.rcon_enabled and s.rcon_password
        assert win.dashboard_page.rcon_notice.isHidden()
    finally:
        win.close()


def test_save_wait_gives_big_worlds_time():
    assert process_manager.SAVE_QUIET_SECONDS >= 5 and process_manager.SAVE_MAX_WAIT_SECONDS >= 120


# ------------------------------------------------------------ off-thread --

def test_stop_runs_off_the_ui_thread(monkeypatch):
    win, s, notes, mw = _window(monkeypatch)
    monkeypatch.setattr(mw.MainWindow, "RUN_OPS_INLINE", False)
    monkeypatch.setattr(process_manager, "is_running", lambda d: True)
    import threading
    threads = []

    def slow_stop(srv, **k):
        threads.append(threading.current_thread() is threading.main_thread())
        time.sleep(0.3)
    monkeypatch.setattr(process_manager, "graceful_stop", slow_stop)
    try:
        t0 = time.monotonic()
        win._handle_stop(s)
        assert time.monotonic() - t0 < 0.2  # returned straight away
        deadline = time.monotonic() + 3
        while s.id in win._expected_stop and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        assert threads == [False] and s.id not in win._expected_stop
        assert win._known_running.get(s.id) is False
    finally:
        win.close()
