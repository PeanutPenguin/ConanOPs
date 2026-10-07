from __future__ import annotations

import sys

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

import auto_bisect_runner
import models
import process_manager
from auto_bisect_runner import BisectOutcome


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _fake_bisect(monkeypatch, outcome_fn, seen):
    class Fake(QObject):
        finished_bisect = Signal(object)
        progress = Signal(object)
        finished = Signal()

        def __init__(self, server, mods, was_running, find_all=False, is_online=None, parent=None):
            super().__init__()
            seen.append({"was_running": was_running, "find_all": find_all, "mods": mods})
            self.mods = mods

        def run(self):
            self.finished_bisect.emit(outcome_fn(self.mods))

        def start(self):
            self.run()

        def isRunning(self):
            return False

        def wait(self, *a):
            return True

    monkeypatch.setattr(auto_bisect_runner, "AutoBisectWorker", Fake)


@pytest.fixture
def win(monkeypatch, tmp_path):
    from ui.main_window import MainWindow
    cfg = models.AppConfig()
    s = cfg.add_server("Modded")
    s.install_dir = str(tmp_path / "srv")
    s.steamcmd_dir = str(tmp_path / "sc")
    s.backup_destination = str(tmp_path / "bk")
    s.desired_running = True
    s.mods = [{"id": "111", "name": "Good Mod", "enabled": True}, {"id": "222", "name": "Broken Mod", "enabled": True}]
    w = MainWindow(config=cfg)
    w.RUN_OPS_INLINE = True
    notes = []
    monkeypatch.setattr(w, "_notify", lambda srv, msg, title="": notes.append((title, msg)))
    monkeypatch.setattr(w, "_handle_mods_changed", lambda srv: None)
    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda srv: launched.append(srv.id))
    monkeypatch.setattr(process_manager, "is_running", lambda d: False)
    w._test = {"server": s, "notes": notes, "launched": launched}
    yield w
    w._really_quit = True
    w.close()


def test_wait_mode_finds_culprit_keeps_world_and_holds(win, monkeypatch):
    seen = []
    _fake_bisect(monkeypatch, lambda mods: BisectOutcome(mods=mods, found_culprits=["222"]), seen)
    monkeypatch.setattr(win, "_query_culprits", lambda srv, baseline: None)
    s = win._test["server"]
    assert win._start_mod_recovery(s, "crashed")
    assert seen[0]["was_running"] is False and seen[0]["find_all"] is True  # never left running mid-test
    assert s.mod_recovery["culprits"] == ["222"]
    assert "Broken Mod" in s.update_hold
    assert all(m["enabled"] for m in s.mods)  # nothing switched off -- world keeps the mod's items
    assert win._test["launched"] == []
    assert s.id not in win._automation_locked


def test_start_without_mode_backs_up_disables_and_starts(win, monkeypatch):
    import backup_manager
    win.config.mod_recovery_mode = "start_without"
    backups = []
    monkeypatch.setattr(backup_manager, "create_backup",
                        lambda *a, **k: backups.append(a) or backup_manager.BackupEntry(path="x", when=None, trigger="manual", size_bytes=1))
    seen = []
    _fake_bisect(monkeypatch, lambda mods: BisectOutcome(mods=mods, found_culprits=["222"]), seen)
    s = win._test["server"]
    win._start_mod_recovery(s, "crashed")
    assert backups
    assert {m["id"]: m["enabled"] for m in s.mods} == {"111": True, "222": False}
    assert s.update_hold == "" and win._test["launched"] == [s.id]


def test_start_without_refuses_if_backup_fails(win, monkeypatch):
    import backup_manager
    win.config.mod_recovery_mode = "start_without"

    def fail(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(backup_manager, "create_backup", fail)
    _fake_bisect(monkeypatch, lambda mods: BisectOutcome(mods=mods, found_culprits=["222"]), [])
    s = win._test["server"]
    win._start_mod_recovery(s, "crashed")
    assert all(m["enabled"] for m in s.mods) and win._test["launched"] == []


def test_alert_mode_does_not_run_the_search(win, monkeypatch):
    win.config.mod_recovery_mode = "alert"
    seen = []
    _fake_bisect(monkeypatch, lambda mods: BisectOutcome(mods=mods), seen)
    assert win._start_mod_recovery(win._test["server"], "crashed") is False and seen == []


def test_not_mod_related_holds_with_explanation(win, monkeypatch):
    _fake_bisect(monkeypatch, lambda mods: BisectOutcome(mods=mods, not_mod_related=True), [])
    s = win._test["server"]
    win._start_mod_recovery(s, "crashed")
    assert "isn't a mod" in s.update_hold and win._test["launched"] == []


def test_could_not_reproduce_starts_it_again(win, monkeypatch):
    _fake_bisect(monkeypatch, lambda mods: BisectOutcome(mods=mods, could_not_reproduce=True), [])
    s = win._test["server"]
    win._start_mod_recovery(s, "crashed")
    assert s.update_hold == "" and win._test["launched"] == [s.id]


def test_waits_then_restarts_when_the_mod_is_updated(win, monkeypatch):
    import update_runner
    s = win._test["server"]
    s.mod_recovery = {"culprits": ["222"], "updated": {"222": 1000}}
    s.update_hold = "Waiting"

    class FakeDl(QObject):
        finished_download = Signal(object)
        finished = Signal()

        def __init__(self, *a, **k):
            super().__init__()

        def run(self):
            self.finished_download.emit(type("R", (), {"success": True, "output": ""})())

        def start(self):
            self.run()

    monkeypatch.setattr(update_runner, "ModDownloadWorker", FakeDl)
    win._on_culprit_times(s, {"222": 1000}, baseline=False)
    assert win._test["launched"] == []  # not updated yet
    win._on_culprit_times(s, {"222": 2000}, baseline=False)
    assert win._test["launched"] == [s.id] and s.update_hold == "" and s.mod_recovery == {}


def test_post_update_crash_loop_triggers_recovery(win, monkeypatch):
    s = win._test["server"]
    started = []
    monkeypatch.setattr(win, "_start_mod_recovery", lambda srv, what: started.append(what) or True)
    win._begin_post_update_watch(s)
    assert win._post_update_failure(s, "crashed") is False   # first failure: normal watchdog retry
    assert win._post_update_failure(s, "crashed") is True
    assert started == ["crashed"]


def test_watchdog_giving_up_triggers_recovery(win, monkeypatch):
    s = win._test["server"]
    started = []
    monkeypatch.setattr(win, "_start_mod_recovery", lambda srv, what: started.append(what) or True)
    win._watchdog_attempts[s.id] = win._WATCHDOG_MAX_ATTEMPTS
    win._attempt_watchdog_restart(s, reason="it crashed", need_stop_first=False)
    assert started == ["crashed"]


def test_manual_start_ends_the_wait(win, monkeypatch):
    import preflight
    s = win._test["server"]
    s.mod_recovery = {"culprits": ["222"]}
    s.update_hold = "Waiting"
    monkeypatch.setattr(preflight, "run_preflight", lambda srv: type("R", (), {"ok": True, "problems": [], "repairs": []})())
    win._handle_start(s)
    assert s.mod_recovery == {} and s.update_hold == ""


def test_wizard_has_keep_running_step_on_by_default():
    from ui.setup_wizard import SetupWizard, _WIZARD_STEPS
    w = SetupWizard(models.ServerConfig(id="s1", name="T"), get_reserved_ports=lambda: set(),
                    get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "")
    assert _WIZARD_STEPS[-1][1] == "keep_running_page"
    assert w.keep_running_page.keep_running is True


def test_apply_keep_running_turns_everything_on_with_one_prompt(win, monkeypatch):
    import keep_alive
    import powershell
    import startup_registration
    import windows_update
    prompts = []
    monkeypatch.setattr(startup_registration, "register", lambda: None)
    monkeypatch.setattr(keep_alive, "enable", lambda: True)
    monkeypatch.setattr(windows_update, "read_active_hours", lambda: {"ActiveHoursStart": 8})
    monkeypatch.setattr(windows_update, "set_active_hours", lambda a, b: prompts.append((a, b)) or powershell.RUN_OK)
    cfg = win.config
    cfg.keep_pc_awake = False
    win._apply_keep_running()
    assert cfg.start_with_windows and cfg.keep_alive_enabled and cfg.keep_pc_awake and cfg.handle_update_restarts
    assert prompts == [(6, 0)] and cfg.original_active_hours == {"ActiveHoursStart": 8}
    assert win.app_settings_page.keep_alive_checkbox.isChecked()
