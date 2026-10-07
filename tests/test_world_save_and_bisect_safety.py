"""
Regression tests for the world-save / bisect safety review round:

1. A FAILED world-save snapshot is never mistaken for "nothing to protect".
2. A failed restore never deletes the only good copy, and never relaunches.
3. Restoring stages + atomically replaces -- the live .db is never missing,
   and the current -wal is never left next to the restored .db.
4. graceful_stop() waits for the `saveworld` it asked for to finish writing.
5. Auto-bisect refuses to start while a mod download/refresh is in flight.
6. Cancel / player-joined still report culprits already confirmed.
7. ddmin's stop check can't disagree with itself (no TypeError race).
8. The manual download worker is kept alive until its QThread really exits.
Plus: timeout retry, manual-bisect lock + restart button, deferred unlock.
"""
from __future__ import annotations

import os
import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication

import auto_bisect_runner
import mod_manager
import models
import network_utils
import preflight
import process_manager
from auto_bisect_runner import AutoBisectWorker


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


# ------------------------------------------------------------- helpers --

class _Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _fake_clock(monkeypatch, module):
    clock = _Clock()
    monkeypatch.setattr(module.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(module.time, "sleep", clock.sleep)
    return clock


class _Proc:
    def __init__(self, running=False):
        self.running = running
        self.events = []

    def is_running(self, install_dir):
        return self.running

    def launch(self, srv):
        self.events.append("launch")
        self.running = True

    def graceful_stop(self, srv, timeout=15.0):
        self.events.append("stop")
        self.running = False
        return True


def _mods(ids):
    return [{"id": i, "name": i, "enabled": True} for i in ids]


def _server(install_dir="/tmp/fake"):
    return models.ServerConfig(
        id="s1", name="Chudville", install_dir=install_dir, steamcmd_dir="/tmp/fake-steamcmd",
        bind_ip="127.0.0.1", query_port=27015,
    )


def _worker_with_mocks(monkeypatch, ids, was_running=False, **kwargs):
    _fake_clock(monkeypatch, auto_bisect_runner)
    worker = AutoBisectWorker(_server(), _mods(ids), was_running=was_running, **kwargs)
    proc = _Proc(running=was_running)
    monkeypatch.setattr(process_manager, "is_running", proc.is_running)
    monkeypatch.setattr(process_manager, "launch", proc.launch)
    monkeypatch.setattr(process_manager, "graceful_stop", proc.graceful_stop)
    monkeypatch.setattr(mod_manager, "write_modlist", lambda *a: None)
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
    monkeypatch.setattr(AutoBisectWorker, "_mod_file_exists", lambda self, mod_id: True)
    monkeypatch.setattr(AutoBisectWorker, "_snapshot_saved_dir", lambda self: None)
    monkeypatch.setattr(AutoBisectWorker, "_restore_saved_dir", lambda self: True)
    return worker, proc


def _broken_if_any_enabled(bad, worker):
    def query(ip, port, timeout=1.5):
        enabled = {m["id"] for m in worker.mods if m.get("enabled", True)}
        return None if bad & enabled else {"name": "ok"}
    return query


def _run(worker):
    captured = {}
    worker.finished_bisect.connect(lambda o: captured.setdefault("outcome", o))
    worker.run()
    return captured["outcome"]


def _saved(tmp_path):
    saved = tmp_path / "ConanSandbox" / "Saved"
    saved.mkdir(parents=True, exist_ok=True)
    return saved


# ---------------------------------------------------- 1. failed snapshot --

def test_snapshot_copy_failure_raises_and_cleans_up_temp_dir(tmp_path, monkeypatch):
    saved = _saved(tmp_path)
    (saved / "game.db").write_text("world")
    made = []
    real_mkdtemp = mod_manager.tempfile.mkdtemp
    monkeypatch.setattr(mod_manager.tempfile, "mkdtemp", lambda **kw: made.append(real_mkdtemp(**kw)) or made[-1])

    def broken_copy(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(mod_manager.shutil, "copy2", broken_copy)

    with pytest.raises(mod_manager.WorldSaveError):
        mod_manager.snapshot_world_save(str(tmp_path))
    assert made and not os.path.exists(made[0])


def test_worker_refuses_to_test_when_snapshot_fails(monkeypatch):
    worker, proc = _worker_with_mocks(monkeypatch, ["a", "b"])

    def failing_snapshot(self):
        raise mod_manager.WorldSaveError("Couldn't make a safety copy of the world save: disk full")
    monkeypatch.setattr(AutoBisectWorker, "_snapshot_saved_dir", failing_snapshot)

    outcome = _run(worker)

    assert "launch" not in proc.events  # nothing was ever tested
    assert "safety copy" in outcome.error
    assert all(m["enabled"] for m in outcome.mods)


# ---------------------------------------------------- 2. failed restore --

def test_settle_keeps_snapshot_and_skips_relaunch_when_restore_fails(monkeypatch, tmp_path):
    worker, proc = _worker_with_mocks(monkeypatch, ["a", "b"], was_running=True)
    snap = tmp_path / "snap"
    snap.mkdir()
    worker._saved_snapshot_dir = str(snap)
    monkeypatch.setattr(AutoBisectWorker, "_restore_saved_dir", lambda self: False)
    proc.events.clear()
    captured = []
    worker.finished_bisect.connect(captured.append)

    worker._settle(auto_bisect_runner.BisectOutcome(mods=worker._final_mods(["a"]), found_culprits=["a"]))

    outcome = captured[0]
    assert snap.exists()                    # the only good copy survives
    assert worker._saved_snapshot_dir == str(snap)
    assert "launch" not in proc.events      # not relaunched onto an unrestored world
    assert str(snap) in outcome.error
    assert all(m["enabled"] for m in outcome.mods)  # nothing unconfirmed applied


def test_round_aborts_instead_of_testing_on_an_unreset_world(monkeypatch):
    worker, proc = _worker_with_mocks(monkeypatch, ["a", "b"])
    monkeypatch.setattr(network_utils, "query_a2s_info", _broken_if_any_enabled({"a"}, worker))
    calls = []

    def restore_fails_after_first(self):
        calls.append(1)
        return len(calls) == 1
    monkeypatch.setattr(AutoBisectWorker, "_restore_saved_dir", restore_fails_after_first)

    outcome = _run(worker)

    assert proc.events.count("launch") == 1  # only the round whose reset succeeded ran
    assert "world save" in outcome.error.lower()
    assert outcome.found_culprits == []


# ------------------------------------------------------ 3. atomic restore --

def test_restore_never_leaves_current_wal_next_to_restored_db(tmp_path):
    saved = _saved(tmp_path)
    (saved / "game.db").write_text("original")
    snap = mod_manager.snapshot_world_save(str(tmp_path))
    (saved / "game.db").write_text("mutated")
    (saved / "game.db-wal").write_text("mutation log")
    (saved / "game.db-shm").write_text("shm")

    assert mod_manager.restore_world_save(str(tmp_path), snap) is True

    assert (saved / "game.db").read_text() == "original"
    assert sorted(p.name for p in saved.iterdir()) == ["game.db"]
    mod_manager.cleanup_world_save_snapshot(snap)


def test_restore_leaves_live_db_intact_when_staging_fails(tmp_path, monkeypatch):
    saved = _saved(tmp_path)
    (saved / "game.db").write_text("original")
    snap = mod_manager.snapshot_world_save(str(tmp_path))
    (saved / "game.db").write_text("current")

    def broken_copy(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(mod_manager.shutil, "copy2", broken_copy)

    assert mod_manager.restore_world_save(str(tmp_path), snap) is False
    assert (saved / "game.db").read_text() == "current"  # never deleted first
    assert sorted(p.name for p in saved.iterdir()) == ["game.db"]
    monkeypatch.undo()
    mod_manager.cleanup_world_save_snapshot(snap)


# ------------------------------------------------------ 4. graceful_stop --

def test_graceful_stop_waits_for_the_save_to_finish_before_killing(monkeypatch):
    import rcon
    clock = _fake_clock(monkeypatch, process_manager)
    server = models.ServerConfig(id="s1", name="x", install_dir="/tmp/fake", rcon_enabled=True)
    monkeypatch.setattr(process_manager, "find_running_pid", lambda d: 4242)
    monkeypatch.setattr(process_manager.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(rcon, "send_command", lambda *a: "ok")
    # The database keeps changing for the first 8 seconds of the save.
    monkeypatch.setattr(process_manager, "_latest_world_save_mtime", lambda d: min(clock.t, 8.0))
    stopped_at = []
    monkeypatch.setattr(process_manager, "stop", lambda d, timeout=10.0: stopped_at.append(clock.t) or True)

    process_manager.graceful_stop(server)

    assert stopped_at and stopped_at[0] >= 8.0 + process_manager.SAVE_QUIET_SECONDS
    assert stopped_at[0] < process_manager.SAVE_MAX_WAIT_SECONDS


def test_graceful_stop_gives_up_waiting_at_the_cap(monkeypatch):
    import rcon
    clock = _fake_clock(monkeypatch, process_manager)
    server = models.ServerConfig(id="s1", name="x", install_dir="/tmp/fake", rcon_enabled=True)
    monkeypatch.setattr(process_manager, "find_running_pid", lambda d: 4242)
    monkeypatch.setattr(process_manager.psutil, "pid_exists", lambda pid: True)
    monkeypatch.setattr(rcon, "send_command", lambda *a: "ok")
    monkeypatch.setattr(process_manager, "_latest_world_save_mtime", lambda d: clock.t)  # never settles
    stopped_at = []
    monkeypatch.setattr(process_manager, "stop", lambda d, timeout=10.0: stopped_at.append(clock.t) or True)

    process_manager.graceful_stop(server)

    assert stopped_at[0] == pytest.approx(process_manager.SAVE_MAX_WAIT_SECONDS, abs=1.0)


# -------------------------------------------- 5. refresh in flight blocks --

def test_auto_bisect_refuses_while_a_mod_refresh_is_running(monkeypatch):
    from ui import mods_page as mp
    page = mp.ModsPage()
    server = _server()
    server.mods = _mods(["a"])
    page.set_server(server)
    page.is_mod_refresh_busy = lambda sid: True
    page.is_server_online = lambda: False
    monkeypatch.setattr(mp.process_manager, "is_running", lambda d: False)
    shown = []
    monkeypatch.setattr(mp.QMessageBox, "information", lambda *a, **k: shown.append(a[1]))
    opened = []
    monkeypatch.setattr(mp, "AutoBisectDialog", lambda *a, **k: opened.append(1))

    page._open_auto_bisect()

    assert opened == []
    assert shown == ["Mods are downloading"]


# --------------------------------------- 6. culprits kept on early stop --

def test_cancel_still_reports_already_confirmed_culprits(monkeypatch):
    worker, proc = _worker_with_mocks(monkeypatch, ["a", "b", "c"])
    query = _broken_if_any_enabled({"a"}, worker)
    def query_then_cancel(ip, port, timeout=1.5):
        # "a" was already tested alone (and failed) -- cancel during "b"'s round.
        enabled = {m["id"] for m in worker.mods if m.get("enabled", True)}
        if enabled == {"b"}:
            worker.cancel()
        return query(ip, port, timeout)
    monkeypatch.setattr(network_utils, "query_a2s_info", query_then_cancel)

    outcome = _run(worker)

    assert outcome.cancelled
    assert outcome.found_culprits == ["a"]
    assert all(m["enabled"] for m in outcome.mods)  # list itself still restored


def test_player_joined_still_reports_already_confirmed_culprits(monkeypatch):
    online = {"v": False}
    worker, proc = _worker_with_mocks(monkeypatch, ["a", "b", "c"], is_online=lambda: online["v"])
    query = _broken_if_any_enabled({"a"}, worker)

    def query_then_join(ip, port, timeout=1.5):
        enabled = {m["id"] for m in worker.mods if m.get("enabled", True)}
        if enabled == {"b"}:  # someone joins during the round after "a" was confirmed
            online["v"] = True
        return query(ip, port, timeout)
    monkeypatch.setattr(network_utils, "query_a2s_info", query_then_join)

    outcome = _run(worker)

    assert outcome.player_joined
    assert outcome.found_culprits == ["a"]


# ------------------------------------------------------ 7. ddmin stop race --

def test_find_all_handles_a_player_who_leaves_right_after_joining(monkeypatch):
    """is_online() True for exactly one call during a ddmin pass. Before,
    _drive_ddmin saw True and returned (None, ...), then _blocked() saw
    False and carried on into found_culprits.extend(None) -> TypeError."""
    calls = {"n": 0}

    def flicker():
        calls["n"] += 1
        return calls["n"] == 5
    worker, proc = _worker_with_mocks(monkeypatch, ["a", "b", "c", "d"], find_all=True, is_online=flicker)
    monkeypatch.setattr(network_utils, "query_a2s_info", _broken_if_any_enabled({"c"}, worker))

    outcome = _run(worker)

    assert outcome.player_joined
    assert not outcome.error


# ------------------------------------------------------- timeout retry --

def test_a_slow_start_gets_one_longer_retry_instead_of_a_conviction(monkeypatch):
    worker, proc = _worker_with_mocks(monkeypatch, ["a"])
    worker.mods = worker._apply_baseline(["a"], [])
    clock = _fake_clock(monkeypatch, auto_bisect_runner)
    first_launch = {}

    def slow_first_start(ip, port, timeout=1.5):
        first_launch.setdefault("t", clock.t)
        # Only answers once it's been loading longer than the base timeout.
        return {"name": "ok"} if clock.t - first_launch["t"] > auto_bisect_runner.STARTUP_TIMEOUT_SECONDS + 30 else None
    monkeypatch.setattr(network_utils, "query_a2s_info", slow_first_start)

    assert worker._restart_and_wait() is True
    assert proc.events.count("launch") == 2


def test_a_crash_is_not_retried(monkeypatch):
    worker, proc = _worker_with_mocks(monkeypatch, ["a"])
    real_launch = proc.launch

    def launch_then_crash(srv):
        real_launch(srv)
        proc.running = False
    monkeypatch.setattr(process_manager, "launch", launch_then_crash)
    monkeypatch.setattr(network_utils, "query_a2s_info", lambda *a, **k: None)

    assert worker._restart_and_wait() is False
    assert proc.events.count("launch") == 1


# ------------------------------------------------ 8. download worker kept --

def test_download_worker_is_kept_alive_until_its_thread_exits(monkeypatch):
    from ui import mods_page as mp

    class _FakeWorker(QThread):
        finished_download = Signal(object)

        def __init__(self, steamcmd_dir, ids, parent=None):
            super().__init__(parent)

        def start(self):
            pass

    monkeypatch.setattr(mp, "ModDownloadWorker", _FakeWorker)
    monkeypatch.setattr(mp.QMessageBox, "information", lambda *a, **k: None)
    page = mp.ModsPage()
    server = _server()
    server.mods = _mods(["a"])
    page.set_server(server)

    page._download_mods()
    worker = page._download_worker
    worker.finished_download.emit(type("R", (), {"success": True, "output": ""})())

    assert page._download_worker is None
    assert worker in page._retiring_download_workers  # still referenced -- run() hasn't returned yet
    worker.finished.emit()
    assert worker not in page._retiring_download_workers


# ------------------------------------------ manual bisect: lock + restart --

def test_manual_bisect_locks_server_and_offers_a_restart_button(monkeypatch, tmp_path):
    from ui import mods_page as mp
    monkeypatch.setattr(mp.process_manager, "is_running", lambda d: False)
    page = mp.ModsPage()
    server = _server(install_dir=str(tmp_path))
    server.mods = _mods(["a", "b"])
    page.set_server(server)
    locked = set()
    page.lock_server_for_automation = locked.add
    page.unlock_server_for_automation = locked.discard
    restarted = []
    page.restart_server = restarted.append
    seen = {}

    def fake_exec(dlg):
        seen["locked"] = server.id in locked
        seen["button_enabled"] = dlg.restart_btn.isEnabled()
        dlg.restart_btn.click()
        dlg._resolved = True  # skip the restore prompt on close
        return 0
    monkeypatch.setattr(mp.BisectDialog, "exec", fake_exec)

    page._open_bisect()

    assert seen == {"locked": True, "button_enabled": True}
    assert restarted == [server]
    assert server.id not in locked


def test_manual_bisect_note_says_no_world_save_rather_than_server_running(monkeypatch, tmp_path):
    from ui import mods_page as mp
    monkeypatch.setattr(mp.process_manager, "is_running", lambda d: False)
    server = _server(install_dir=str(tmp_path))
    server.mods = _mods(["a", "b"])
    dlg = mp.BisectDialog(server)
    labels = [w.text() for w in dlg.findChildren(mp.QLabel)]
    assert any("no world save yet" in t for t in labels)
    assert not any("was running" in t for t in labels)
    dlg._resolved = True


# --------------------------------- auto bisect: unlock only once finished --

def test_lock_is_held_until_a_still_running_worker_finishes(monkeypatch):
    from ui import mods_page as mp
    page = mp.ModsPage()
    locked = {"s1"}
    page.unlock_server_for_automation = locked.discard

    class _Worker(QThread):
        def isRunning(self):  # noqa: N802
            return True

    class _Dlg:
        def __init__(self):
            self._worker = _Worker()

    dlg = _Dlg()
    page._release_after_bisect(dlg, "s1")
    assert "s1" in locked          # worker still restoring -- stay locked
    dlg._worker.finished.emit()
    assert "s1" not in locked
