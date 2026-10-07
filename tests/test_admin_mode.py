"""'Run with admin rights' (admin_mode.py) and the web version never
leaving a Windows permission prompt on an empty PC."""
from __future__ import annotations

import sys
import threading
import time

import pytest
from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

import admin_mode
import cleanup
import models
import powershell
import proc_utils


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def data(tmp_path, monkeypatch):
    import conanops_paths
    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\ConanOps\ConanOps.exe")


# ------------------------------------------------------------ the gate --

def test_no_prompt_during_web_actions(monkeypatch):
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    monkeypatch.setattr(proc_utils, "run_elevated_and_wait", lambda *a, **k: pytest.fail("prompted"))
    with powershell.no_prompts() as hits:
        assert powershell.prompts_blocked()
        assert powershell.run_privileged("# Add firewall rules\nexit 0\n") == powershell.RUN_NEEDS_PC
    assert hits == ["Add firewall rules"]
    assert not powershell.prompts_blocked()


def test_gate_does_nothing_when_already_admin(monkeypatch):
    monkeypatch.setattr(proc_utils, "is_admin", lambda: True)
    with powershell.no_prompts():
        assert not powershell.prompts_blocked()


def test_bridge_reports_a_change_that_needed_the_pc(monkeypatch):
    from webui.bridge import GuiBridge
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    result, msgs = GuiBridge().call(lambda: powershell.run_privileged("# Change Windows' update hours\n"))
    assert result == powershell.RUN_NEEDS_PC
    assert len(msgs) == 1 and "Change Windows' update hours" in msgs[0] and "at the PC" in msgs[0]


def test_bridge_never_shows_a_dialog():
    from webui.bridge import GuiBridge
    _r, msgs = GuiBridge().call(lambda: QMessageBox.question(None, "Sure?", "Really?"))
    assert _r == QMessageBox.No and msgs == ["Sure?: Really?"]


# ------------------------------------------- firewall waits for the PC --

class _FakeWorker:
    made = []

    def __init__(self, server_id, **kw):
        self.kw = kw
        _FakeWorker.made.append(self)
        from PySide6.QtCore import QObject, Signal

        class S(QObject):
            sig = Signal(object)
            fin = Signal()
        self._s = S()
        self.finished_reconcile = self._s.sig
        self.finished = self._s.fin

    def start(self):
        self.finished_reconcile.emit({})


@pytest.fixture
def window(monkeypatch):
    import network_setup_runner
    from ui.main_window import MainWindow
    monkeypatch.setattr(models.AppConfig, "save", lambda self, *a, **k: None)
    monkeypatch.setattr(network_setup_runner, "FirewallReconcileWorker", _FakeWorker)
    _FakeWorker.made = []
    server = models.ServerConfig(id="s1", name="Exiled", install_dir="")
    win = MainWindow(config=models.AppConfig(servers=[server], active_server_id="s1"))
    yield win
    win.close()


def test_web_firewall_change_waits_for_the_pc_and_router_part_runs_now(window, monkeypatch):
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    s = window.config.servers[0]
    with powershell.no_prompts():
        window._start_network_reconcile(s, legacy_names=[s.name])
    assert [w.kw["do_firewall"] for w in _FakeWorker.made] == [False]
    assert _FakeWorker.made[0].kw["do_upnp"] is True
    assert window.needs_pc_labels() == ["Windows Firewall rules for Exiled"]

    # Someone uses the PC: asked once, then the firewall part runs.
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    window._run_needs_pc()
    assert [(w.kw["do_firewall"], w.kw["do_upnp"]) for w in _FakeWorker.made] == [(False, True), (True, False)]
    assert window.needs_pc_labels() == []


def test_declining_at_the_pc_drops_the_waiting_change(window, monkeypatch):
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    s = window.config.servers[0]
    with powershell.no_prompts():
        window._start_network_reconcile(s)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.No))
    window._run_needs_pc()
    assert window.needs_pc_labels() == [] and len(_FakeWorker.made) == 1


def test_not_asked_while_another_web_action_runs(window, monkeypatch):
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    window.queue_for_pc("x", lambda: pytest.fail("ran during a web action"))
    with powershell.no_prompts():
        window._run_needs_pc()
    assert window.needs_pc_labels() == ["x"]


def test_admin_rights_mean_nothing_waits(window, monkeypatch):
    monkeypatch.setattr(proc_utils, "is_admin", lambda: True)
    s = window.config.servers[0]
    with powershell.no_prompts():
        window._start_network_reconcile(s)
    assert _FakeWorker.made[0].kw["do_firewall"] is True and window.needs_pc_labels() == []


def test_web_update_hours_change_waits_for_the_pc(window, monkeypatch):
    from webui.api import Request, WebApi
    from webui.bridge import GuiBridge
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    api = WebApi(window, GuiBridge())
    res = api.app_set(Request("POST", "", {"option": "update_restarts"}, {}, {"value": True}, "1.2.3.4"))
    assert res["ok"] and res.get("pending") and "Waiting for the PC" in res["message"]
    assert window.config.handle_update_restarts is False
    assert window.needs_pc_labels() == ["Changing Windows' update restart hours"]
    data = api.app_options(Request("GET", "", {}, {}, {}, ""))
    assert data["needs_pc"] == ["Changing Windows' update restart hours"] and data["admin"] is False


# ---------------------------------------------------------- admin mode --

def test_task_runs_elevated_on_demand_only_with_normal_priority(data, frozen):
    s = admin_mode.register_script()
    assert "-RunLevel Highest" in s and "-LogonType Interactive" in s and "S4U" not in s
    assert "New-ScheduledTaskTrigger" not in s and "-Trigger" not in s  # only runs when ConanOps asks
    assert "ExecutionTimeLimit ([TimeSpan]::Zero)" in s and "$settings.Priority = 4" in s
    assert "'C:\\ConanOps\\ConanOps.exe'" in s and "'--elevated'" in s


def test_enable_and_disable(data, frozen, monkeypatch):
    monkeypatch.setattr(admin_mode.sys, "platform", "win32")
    ran = []
    monkeypatch.setattr(powershell, "run_privileged", lambda script, timeout=0: ran.append(script) or powershell.RUN_OK)
    assert admin_mode.enable() == powershell.RUN_OK and admin_mode.is_on()
    monkeypatch.setattr(admin_mode, "status", lambda: {"exists": True, "matches": True})
    assert admin_mode.disable() == powershell.RUN_OK and not admin_mode.is_on()
    assert "Unregister-ScheduledTask" in ran[1] and "Stop-ScheduledTask" not in ran[1]


def test_declined_prompt_leaves_it_off(data, frozen, monkeypatch):
    monkeypatch.setattr(admin_mode.sys, "platform", "win32")
    monkeypatch.setattr(powershell, "run_privileged", lambda *a, **k: powershell.RUN_DECLINED)
    assert admin_mode.enable() == powershell.RUN_DECLINED and not admin_mode.is_on()


def test_switch_goes_even_if_the_task_cant_be_removed(data, monkeypatch):
    monkeypatch.setattr(admin_mode.sys, "platform", "win32")
    (data / "admin-mode.on").write_text("x")
    monkeypatch.setattr(admin_mode, "status", lambda: None)
    monkeypatch.setattr(powershell, "run_privileged", lambda *a, **k: powershell.RUN_DECLINED)
    admin_mode.disable()
    assert not admin_mode.is_on()


def test_should_relaunch(data, monkeypatch):
    monkeypatch.setattr(admin_mode.sys, "platform", "win32")
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    monkeypatch.setattr(admin_mode, "task_points_here", lambda: True)
    assert not admin_mode.should_relaunch(["x"])  # off
    (data / "admin-mode.on").write_text("x")
    assert admin_mode.should_relaunch(["x"])
    assert not admin_mode.should_relaunch(["x", "--elevated"])  # never loops
    monkeypatch.setattr(proc_utils, "is_admin", lambda: True)
    assert not admin_mode.should_relaunch(["x"])


def test_launch_args_are_filtered_and_expire(data, monkeypatch):
    import json
    (data / "admin-launch-args.json").write_text(json.dumps({"time": time.time(), "args": ["--keep-alive", "--evil"]}))
    assert admin_mode.take_launch_args() == ["--keep-alive"]
    assert admin_mode.take_launch_args() == []  # used once
    (data / "admin-launch-args.json").write_text(json.dumps({"time": time.time() - 999, "args": ["--keep-alive"]}))
    assert admin_mode.take_launch_args() == []


def test_hands_over_once_the_elevated_copy_has_the_lock(data, monkeypatch):
    path = str(data / "conanops.lock")
    lock = QLockFile(path)
    assert lock.tryLock(0)
    other = {}

    def elevated_copy_starts():
        def run():
            time.sleep(0.5)
            other["lock"] = QLockFile(path)
            other["got"] = other["lock"].tryLock(5000)
        threading.Thread(target=run, daemon=True).start()
        return True
    monkeypatch.setattr(admin_mode, "_run_task", elevated_copy_starts)
    assert admin_mode.relaunch_elevated(["ConanOps.exe", "--keep-alive"], lock, path, wait_seconds=5) is True
    deadline = time.monotonic() + 5
    while "got" not in other and time.monotonic() < deadline:
        time.sleep(0.05)
    assert other.get("got") is True
    assert admin_mode.take_launch_args() == ["--keep-alive"]
    other["lock"].unlock()


def test_carries_on_unelevated_if_the_task_doesnt_start(data, monkeypatch):
    path = str(data / "conanops.lock")
    lock = QLockFile(path)
    assert lock.tryLock(0)
    monkeypatch.setattr(admin_mode, "_run_task", lambda: False)
    assert admin_mode.relaunch_elevated(["ConanOps.exe"], lock, path, wait_seconds=1) is False
    assert lock.isLocked()
    lock.unlock()


def test_quit_request_round_trip(data):
    assert not admin_mode.quit_requested()
    admin_mode.request_quit()
    assert admin_mode.quit_requested()
    admin_mode.clear_quit_request()
    assert not admin_mode.quit_requested()


def test_delete_everything_removes_the_admin_task():
    assert admin_mode.TASK_NAME in cleanup.cleanup_script(remove_task=True)
    # Must not end the larger cleanup script early.
    assert "exit" not in admin_mode.unregister_script()


def test_config_round_trip(tmp_path):
    cfg = models.AppConfig(admin_mode_enabled=True)
    path = tmp_path / "c.json"
    cfg.save(str(path))
    assert models.AppConfig.load(str(path)).admin_mode_enabled is True


# ------------------------------------------------ real Windows (CI) --

@pytest.mark.skipif(sys.platform != "win32" or not proc_utils.is_admin(), reason="needs Windows with admin rights")
def test_real_task_registers_and_unregisters_on_windows(data):
    try:
        assert admin_mode.enable() == powershell.RUN_OK
        st = admin_mode.status()
        assert st == {"exists": True, "matches": True}, st
        assert admin_mode.task_points_here()
    finally:
        assert admin_mode.disable() == powershell.RUN_OK
    assert admin_mode.status() == {"exists": False, "matches": False}


def test_a_task_for_another_copy_is_not_used(data, frozen, monkeypatch):
    monkeypatch.setattr(admin_mode.sys, "platform", "win32")
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    monkeypatch.setattr(proc_utils, "hidden_window_kwargs", lambda: {})
    (data / "admin-mode.on").write_text("x")

    def xml(command):
        body = (f'<?xml version="1.0" encoding="UTF-16"?><Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
                f'<Actions><Exec><Command>{command}</Command><Arguments>--elevated</Arguments></Exec></Actions></Task>')
        return type("P", (), {"returncode": 0, "stdout": body.encode("utf-16")})()
    monkeypatch.setattr(admin_mode.subprocess, "run", lambda *a, **k: xml(r"C:\ConanOps\ConanOps.exe"))
    assert admin_mode.should_relaunch(["x"])
    monkeypatch.setattr(admin_mode.subprocess, "run", lambda *a, **k: xml(r"D:\Old\ConanOps.exe"))
    assert not admin_mode.should_relaunch(["x"])
    monkeypatch.setattr(admin_mode.subprocess, "run", lambda *a, **k: type("P", (), {"returncode": 1, "stdout": b""})())
    assert not admin_mode.should_relaunch(["x"])


def test_prompt_block_is_per_thread_and_follows_handed_on_work(monkeypatch):
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    other = {}
    with powershell.no_prompts():
        t = threading.Thread(target=lambda: other.setdefault("pc", powershell.prompts_blocked()))
        t.start()
        t.join()
        carried = powershell.carry_gate(powershell.prompts_blocked)
        t2 = threading.Thread(target=lambda: other.setdefault("web", carried()))
        t2.start()
        t2.join()
    assert other == {"pc": False, "web": True}
    assert not powershell.prompts_blocked()


def test_a_repeated_change_still_says_it_waits_for_the_pc(window, monkeypatch):
    from webui.api import WebApi
    from webui.bridge import GuiBridge
    monkeypatch.setattr(proc_utils, "is_admin", lambda: False)
    api = WebApi(window, GuiBridge())
    s = window.config.servers[0]
    for _ in range(2):
        _r, msgs = api.gui(lambda: window._start_network_reconcile(s) if powershell.prompts_blocked() else None)
        with powershell.no_prompts():
            pass
    window._needs_pc = []
    with powershell.no_prompts():
        pass
    _r, msgs = api.gui(lambda: window.queue_for_pc("X", lambda: None))
    _r, msgs2 = api.gui(lambda: window.queue_for_pc("X", lambda: None))
    assert msgs and msgs2 and "Waiting for the PC" in msgs2[0]
