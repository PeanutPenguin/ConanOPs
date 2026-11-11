"""2.0: fixes from the whole-app review, and the countdown before restarts."""
from __future__ import annotations

import os
import time

import pytest

import backup_manager
import cleanup
import history
import log_monitor
import models
import process_manager
from tests.test_server_finder import _make_install, _window
from tests.worlds import read_world, write_world


def _server_with_world(tmp_path, name="S"):
    root = tmp_path / name
    _make_install(root)
    write_world(root / "ConanSandbox" / "Saved" / "game.db", "live world")
    return str(root)


# ------------------------------------------------- removing keeps backups --

def test_deleting_server_files_keeps_the_backups_kept_by_choice(tmp_path, monkeypatch):
    import conanops_paths
    from removal_runner import ServerRemovalWorker
    import network_setup
    root = str(tmp_path / "ConanOps")
    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: root)
    monkeypatch.setattr(network_setup, "remove_firewall_rules", lambda *a, **k: True)
    monkeypatch.setattr(network_setup, "remove_upnp_mappings", lambda *a, **k: None)
    s = models.ServerConfig(id="abc12345", name="x")
    s.adopted = False
    s.install_dir = os.path.join(root, s.id, "server")
    s.steamcmd_dir = os.path.join(root, s.id, "steamcmd")
    s.backup_destination = backup_manager.default_backup_destination(s.install_dir)
    for p in (s.install_dir, s.steamcmd_dir, s.backup_destination):
        os.makedirs(p)
    kept = os.path.join(s.backup_destination, "20260101-000000_manual.zip")
    open(kept, "w").write("x")
    w = ServerRemovalWorker(s, [], delete_files=True, delete_backups=False)
    problems = []
    w.finished_removal.connect(problems.append)
    w.run()
    assert os.path.exists(kept)
    assert not os.path.exists(s.install_dir) and not os.path.exists(s.steamcmd_dir)


def test_running_server_files_are_left_when_it_wont_stop(tmp_path, monkeypatch):
    from removal_runner import ServerRemovalWorker
    import network_setup
    import removal_runner
    monkeypatch.setattr(network_setup, "remove_firewall_rules", lambda *a, **k: True)
    monkeypatch.setattr(network_setup, "remove_upnp_mappings", lambda *a, **k: None)
    monkeypatch.setattr(removal_runner, "_stop_and_wait", lambda s: False)
    s = models.ServerConfig(name="x", install_dir=_server_with_world(tmp_path))
    out = []
    w = ServerRemovalWorker(s, [], delete_files=True, delete_backups=False)
    w.finished_removal.connect(out.append)
    w.run()
    assert os.path.exists(s.install_dir) and "didn't stop" in out[0][0]


def test_remove_path_except_keeps_a_nested_folder(tmp_path):
    (tmp_path / "a" / "b" / "keep").mkdir(parents=True)
    (tmp_path / "a" / "b" / "keep" / "x.zip").write_text("1")
    (tmp_path / "a" / "b" / "gone.txt").write_text("1")
    (tmp_path / "a" / "other").mkdir()
    assert cleanup.remove_path_except(str(tmp_path / "a"), str(tmp_path / "a" / "b" / "keep")) == []
    assert (tmp_path / "a" / "b" / "keep" / "x.zip").exists()
    assert not (tmp_path / "a" / "b" / "gone.txt").exists() and not (tmp_path / "a" / "other").exists()


# ---------------------------------------------------- stop must be honest --

def test_restore_refuses_while_the_server_still_runs(tmp_path, monkeypatch):
    root = _server_with_world(tmp_path)
    entry = backup_manager.create_backup(root, str(tmp_path / "bk"), "manual", live=False)
    monkeypatch.setattr(process_manager, "find_running_pid", lambda d: 4242)
    with pytest.raises(backup_manager.ServerRunningError):
        backup_manager.restore_backup(root, entry)


def test_restore_worker_reports_a_server_that_wont_stop(tmp_path, monkeypatch):
    import backup_runner
    root = _server_with_world(tmp_path)
    entry = backup_manager.create_backup(root, str(tmp_path / "bk"), "manual", live=False)
    monkeypatch.setattr(process_manager, "graceful_stop", lambda s: False)
    restored = []
    monkeypatch.setattr(backup_manager, "restore_backup", lambda *a: restored.append(1))
    out = []
    w = backup_runner.RestoreWorker(models.ServerConfig(name="x", install_dir=root), entry, True)
    w.finished_restore.connect(lambda ok, err: out.append((ok, err)))
    w.run()
    assert out[0][0] is False and "still running" in out[0][1] and restored == []


def test_update_is_not_run_on_a_server_that_wont_stop(monkeypatch):
    import steamcmd
    from update_runner import UpdateWorker
    monkeypatch.setattr(process_manager, "is_running", lambda d: True)
    monkeypatch.setattr(process_manager, "graceful_stop", lambda s: False)
    ran = []
    monkeypatch.setattr(steamcmd, "update_server", lambda *a, **k: ran.append(1))
    s = models.ServerConfig(name="x", install_dir="/tmp/x")
    res = UpdateWorker("/tmp/steamcmd", "/tmp/x", stop_server=s)._run_steps()
    assert not res.success and res.reason == "still_running" and ran == []


# --------------------------------------------- desktop buttons while busy --

def test_start_during_an_update_is_refused_and_never_doubles(tmp_path, monkeypatch):
    import preflight
    from PySide6.QtWidgets import QMessageBox
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    launches, told = [], []
    monkeypatch.setattr(process_manager, "launch", lambda s: launches.append(s.id))
    monkeypatch.setattr(process_manager, "is_running", lambda d: False)
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: told.append(a[2])))
    try:
        s = cfg.add_server("x")
        s.install_dir = _server_with_world(tmp_path)
        win._update_apply_workers[s.id] = object()
        win._handle_start(s)
        win._handle_restart(s)
        assert launches == [] and len(told) == 2 and "Updating" in told[0]
    finally:
        win._update_apply_workers.clear()
        win.close()


# ------------------------------------------------------- startup grace etc --

def test_log_monitor_survives_a_log_renamed_mid_scan(tmp_path, monkeypatch):
    logs = tmp_path / "ConanSandbox" / "Saved" / "Logs"
    logs.mkdir(parents=True)
    (logs / "ConanSandbox.log").write_text("x")
    (logs / "ConanSandbox-backup.log").write_text("x")
    real = os.path.getmtime

    def renamed(path):
        if path.endswith("ConanSandbox.log"):
            raise FileNotFoundError(path)
        return real(path)
    monkeypatch.setattr(log_monitor.os.path, "getmtime", renamed)
    assert log_monitor.find_latest_log(str(tmp_path)).endswith("ConanSandbox-backup.log")


def test_log_monitor_keeps_going_after_a_failed_read(tmp_path, monkeypatch):
    mon = log_monitor.LogMonitor(str(tmp_path), poll_interval=0.01)
    calls = []

    def boom():
        calls.append(1)
        if len(calls) >= 3:
            mon.stop()
        raise OSError("disk hiccup")
    monkeypatch.setattr(mon, "_poll_once", boom)
    mon.run()
    assert len(calls) == 3


def test_secrets_windows_cant_decrypt_survive_a_save(tmp_path, monkeypatch):
    import json
    import secrets_store
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"servers": [], "alert_discord_url": "ENC:locked", "discord_bot_token": "ENC:tok",
                                "steam_api_key": "ENC:key"}))

    def unprotect(v):
        if str(v).startswith("ENC:"):
            raise secrets_store.DecryptionError("other account")
        return v
    monkeypatch.setattr(secrets_store, "unprotect", unprotect)
    monkeypatch.setattr(secrets_store, "protect", lambda v: v)
    cfg = models.AppConfig.load(str(path))
    assert cfg.alert_discord_url == "" and cfg.discord_bot_token == ""
    cfg.save(str(path))
    raw = json.loads(path.read_text())
    assert raw["alert_discord_url"] == "ENC:locked" and raw["discord_bot_token"] == "ENC:tok"
    cfg.steam_api_key = "new-key"
    cfg.save(str(path))
    assert json.loads(path.read_text())["steam_api_key"] == "new-key"


def test_steamcmd_runs_one_at_a_time_per_folder(monkeypatch):
    import threading
    import steamcmd
    active, peak = [0], [0]

    @steamcmd.one_at_a_time
    def job(folder):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        active[0] -= 1

    threads = [threading.Thread(target=job, args=("C:/steamcmd",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1


# --------------------------------------------------------------- countdown --

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _countdown_window(tmp_path, monkeypatch):
    import banlist_manager
    import ui.window.countdown as cd
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    s = cfg.add_server("x")
    s.install_dir = _server_with_world(tmp_path)
    win._known_running[s.id] = True
    win._online_by_server[s.id] = {"Conan"}
    said = []
    monkeypatch.setattr(banlist_manager, "broadcast_message", lambda srv, m: said.append(m) or True)
    monkeypatch.setattr(win, "_notify", lambda *a, **k: None)
    clock = _Clock()
    monkeypatch.setattr(cd.time, "monotonic", clock)
    return win, s, said, clock


def test_countdown_warns_at_each_mark_then_goes_ahead(tmp_path, monkeypatch):
    win, s, said, clock = _countdown_window(tmp_path, monkeypatch)
    done = []
    try:
        assert win._with_warning(s, "restart", lambda: done.append(1)) == "countdown"
        assert "15 minutes" in said[-1]
        clock.t += 10 * 60
        win._tick_countdowns()
        assert "5 minutes" in said[-1] and not done
        clock.t += 4 * 60
        win._tick_countdowns()
        assert "1 minute." in said[-1]
        clock.t += 60
        win._tick_countdowns()
        assert done == [1] and said[-1] == "Server restart now."
        assert win._with_warning(s, "restart", lambda: done.append(2)) == "countdown"  # a fresh one
    finally:
        win.close()


def test_countdown_goes_ahead_as_soon_as_everyone_leaves(tmp_path, monkeypatch):
    win, s, said, clock = _countdown_window(tmp_path, monkeypatch)
    done = []
    try:
        win._with_warning(s, "update", lambda: done.append(1))
        win._online_by_server[s.id] = set()
        win._tick_countdowns()
        assert done == [1] and s.id not in win._countdowns
    finally:
        win.close()


def test_set_to_wait_or_without_rcon_nothing_happens(tmp_path, monkeypatch):
    win, s, said, clock = _countdown_window(tmp_path, monkeypatch)
    done = []
    try:
        s.when_players_online = "wait"
        assert win._with_warning(s, "restart", lambda: done.append(1)) == "wait"
        s.when_players_online, s.rcon_enabled = "warn", False
        assert win._with_warning(s, "restart", lambda: done.append(1)) == "wait"
        assert not done and not said
        win._online_by_server[s.id] = set()
        assert win._with_warning(s, "restart", lambda: done.append(1)) == "now" and done == [1]
    finally:
        win.close()


def test_stopping_cancels_the_countdown(tmp_path, monkeypatch):
    win, s, said, clock = _countdown_window(tmp_path, monkeypatch)
    done = []
    try:
        win._with_warning(s, "restart", lambda: done.append(1))
        win._known_running[s.id] = False
        win._tick_countdowns()
        clock.t += 3600
        win._tick_countdowns()
        assert not done and s.id not in win._countdowns
    finally:
        win.close()


def test_scheduler_restarts_with_players_on_when_set_to_warn():
    from datetime import datetime
    import scheduler as sched
    s = models.ServerConfig(name="x", install_dir="/x", restart_enabled=True, restart_start="04:00",
                            restart_end="06:00")
    fired, skipped = [], []
    sc = sched.Scheduler(get_servers=lambda: [s], is_online=lambda srv: True, on_state_changed=lambda: None,
                         is_running=lambda srv: True)
    sc.restart_due.connect(fired.append)
    sc.restart_skipped_online.connect(skipped.append)
    s.rcon_enabled, s.when_players_online = True, "warn"
    sc._check_restart(s, datetime(2026, 11, 10, 4, 30))
    assert fired == [s]
    s.last_restart_date, s.when_players_online = "", "wait"
    sc._check_restart(s, datetime(2026, 11, 11, 4, 30))
    assert skipped == [s]


# ------------------------------------------------------------------ history --

def test_history_records_samples_events_and_sums_them_up():
    sid = "hist1"
    t0 = 1_700_000_000
    for i in range(12):  # an hour, every 5 minutes; down for the middle 20
        up = not (4 <= i < 8)
        assert history.record_sample(sid, up, players=i if up else None, cpu=10.0, mem_mb=2048,
                                     now=t0 + i * 300, force=True)
    history.record_event(sid, "crash", "boom", now=t0 + 1200)
    history.record_event(sid, "restart", now=t0 + 2400)
    s = history.summary(sid, t0, t0 + 3600)
    assert round(s.uptime_percent) == 67
    assert s.peak_players == 11 and s.count("crash") == 1 and s.count("restart") == 1
    ev = history.events(sid, t0)
    assert [e.kind for e in ev] == ["restart", "crash"] and ev[1].problem
    thin = history.samples(sid, t0, t0 + 3600, max_points=4)
    assert len(thin) == 4 and max(p.players or 0 for p in thin) == 11


def test_history_sample_rate_is_limited():
    assert history.record_sample("hist2", True, now=5000.0)
    assert not history.record_sample("hist2", True, now=5100.0)
    assert history.record_sample("hist2", True, now=5000.0 + history.SAMPLE_EVERY_SECONDS)


def test_cpu_percent_from_two_readings(monkeypatch):
    monkeypatch.setattr(history.os, "cpu_count", lambda: 4)
    assert history.cpu_percent("c", 7, 100.0, now=0.0) is None
    assert history.cpu_percent("c", 7, 102.0, now=10.0) == pytest.approx(5.0)
    assert history.cpu_percent("c", 8, 1.0, now=20.0) is None  # restarted: a new process
