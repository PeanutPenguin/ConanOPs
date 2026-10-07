from __future__ import annotations

import sys

import pytest

import keep_alive
import powershell


@pytest.fixture
def data(tmp_path, monkeypatch):
    import conanops_paths
    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\ConanOps\ConanOps(1).exe")


def test_watcher_relaunches_only_after_two_misses_and_respects_quit(data, frozen):
    s = keep_alive.watcher_script()
    assert "$gone -ge 2" in s
    assert "Test-Path -LiteralPath $quit" in s and "while (Test-Path -LiteralPath $on)" in s
    assert "'--keep-alive'" in s
    assert "$_.Path -ieq $exe" in s  # matches by full path, so a renamed exe still works
    assert "'C:\\ConanOps\\ConanOps(1).exe'" in s


def test_register_is_interactive_unelevated_and_never_stores_a_password(data, frozen, monkeypatch):
    monkeypatch.setattr(keep_alive.sys, "platform", "win32")
    scripts = []
    monkeypatch.setattr(powershell, "run_readonly",
                        lambda script, timeout=30.0: scripts.append(script) or type("P", (), {"returncode": 0, "stderr": ""})())
    monkeypatch.setattr(powershell, "run_privileged", lambda *a, **k: pytest.fail("must not ask for admin"))
    assert keep_alive.enable()
    s = scripts[0]
    assert "-LogonType Interactive" in s and "S4U" not in s and "-RunLevel Limited" in s
    assert "-AtLogOn" in s and "IgnoreNew" in s and "$settings.Priority = 4" in s
    assert (data / "keep-alive.on").exists()


def test_disable_removes_switch_and_never_stops_the_task(data, monkeypatch):
    monkeypatch.setattr(keep_alive.sys, "platform", "win32")
    (data / "keep-alive.on").write_text("x")
    scripts = []
    monkeypatch.setattr(powershell, "run_readonly",
                        lambda script, timeout=30.0: scripts.append(script) or type("P", (), {"returncode": 0})())
    assert keep_alive.disable()
    assert not (data / "keep-alive.on").exists()
    assert "Unregister-ScheduledTask" in scripts[0] and "Stop-ScheduledTask" not in scripts[0]


def test_quit_marker_round_trip(data):
    keep_alive.mark_user_quit()
    assert (data / "user-quit").exists()
    keep_alive.clear_user_quit()
    assert not (data / "user-quit").exists()


@pytest.mark.parametrize("enabled,st,expect", [
    (True, {"exists": True, "matches": True}, None),
    (True, {"exists": True, "matches": False}, "enable"),
    (True, {"exists": False, "matches": False}, "enable"),
    (False, {"exists": True, "matches": True}, "disable"),
    (False, {"exists": False, "matches": False}, None),
])
def test_ensure(data, monkeypatch, enabled, st, expect):
    monkeypatch.setattr(keep_alive.sys, "platform", "win32")
    (data / "keep-alive.on").write_text("x")
    calls = []
    monkeypatch.setattr(keep_alive, "status", lambda: st)
    monkeypatch.setattr(keep_alive, "enable", lambda: calls.append("enable"))
    monkeypatch.setattr(keep_alive, "disable", lambda: calls.append("disable"))
    keep_alive.ensure(enabled)
    assert calls == ([expect] if expect else [])


def test_cleanup_script_removes_keep_alive_task():
    import cleanup
    assert keep_alive.TASK_NAME in cleanup.cleanup_script(remove_task=True)


def test_quit_from_tray_marks_deliberate_quit(data, monkeypatch):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import models
    from ui.main_window import MainWindow
    win = MainWindow(config=models.AppConfig())
    try:
        win._quit_from_tray()
        assert (data / "user-quit").exists()
    finally:
        win._really_quit = True
        win.close()


def test_hourly_router_refresh_only_for_running_servers(monkeypatch):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import models
    import network_setup
    from ui.main_window import MainWindow
    cfg = models.AppConfig()
    a = cfg.add_server("A")
    b = cfg.add_server("B")
    a.bind_ip = b.bind_ip = "192.168.1.50"
    win = MainWindow(config=cfg)
    try:
        refreshed = []
        monkeypatch.setattr(network_setup, "UPNP_REFRESH_ENABLED", True)
        monkeypatch.setattr(network_setup, "refresh_upnp_async", lambda s: refreshed.append(s.id))
        win._known_running[a.id] = True
        win._known_running[b.id] = False
        win._refresh_router_forwards()
        assert refreshed == [a.id]
        assert win._upnp_refresh_timer.interval() == 60 * 60 * 1000
    finally:
        win._really_quit = True
        win.close()
