from __future__ import annotations

import os
import shutil
import sys

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

import conanops_paths
import mod_manager
import models
from ui.main_window import MainWindow


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def modlist_writes(monkeypatch):
    """Records mod_manager.write_modlist calls instead of writing.
    Several handlers here now rewrite modlist.txt (after a mod
    download finishes), and the servers in this file use made-up
    install paths like /tmp/fake-install -- without this, running the
    suite would leave real folders behind there."""
    calls = []
    monkeypatch.setattr(mod_manager, "write_modlist", lambda install_dir, steamcmd_dir, mods: calls.append((install_dir, [dict(m) for m in mods])))
    return calls


def test_fresh_config_has_no_auto_created_server():
    """A brand new install must start with zero servers -- no more
    auto-created 'My Server' placeholder."""
    cfg = models.AppConfig()
    assert cfg.servers == []


def test_main_window_starts_in_dashboard_empty_state_with_no_servers():
    cfg = models.AppConfig()
    win = MainWindow(config=cfg)
    try:
        assert win.dashboard_page.server is None
        assert win.dashboard_page.content.isHidden() is True
        assert win.dashboard_page.empty_frame.isHidden() is False
    finally:
        win.close()


def test_per_server_nav_disabled_with_no_servers_enabled_once_added(monkeypatch):
    cfg = models.AppConfig()
    win = MainWindow(config=cfg)
    monkeypatch.setattr(win, "_open_setup_wizard", lambda server: None)  # don't block on a real modal wizard
    try:
        nav_by_key = {btn.property("nav_key"): btn for btn in win.sidebar._nav_group.buttons()}
        # Dashboard and App Settings don't need an active server.
        assert nav_by_key["dashboard"].isEnabled() is True
        assert nav_by_key["app"].isEnabled() is True
        # Everything else does.
        for key in ("players", "updates", "mods", "access", "console", "settings"):
            assert nav_by_key[key].isEnabled() is False

        win._on_add_server()

        for key in ("players", "updates", "mods", "access", "console", "settings"):
            assert nav_by_key[key].isEnabled() is True
    finally:
        win.close()


def test_dashboard_empty_state_add_button_creates_a_server(monkeypatch):
    cfg = models.AppConfig()
    win = MainWindow(config=cfg)
    monkeypatch.setattr(win, "_open_setup_wizard", lambda server: None)  # don't block on a real modal wizard
    try:
        assert cfg.servers == []
        win.dashboard_page.empty_add_btn.click()
        assert len(cfg.servers) == 1
    finally:
        win.close()


def test_handle_start_launches_when_not_already_running(monkeypatch):
    import process_manager
    import preflight

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)
        monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
        launched = []
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))

        win._handle_start(server)

        assert launched == [server]
        assert win._known_running[server.id] is True
        assert server.desired_running is True  # so auto-resume brings it back after a restart
    finally:
        win.close()


def test_handle_start_does_nothing_if_already_running(monkeypatch):
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(process_manager, "is_running", lambda install_dir: True)
        launched = []
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))

        win._handle_start(server)

        assert launched == []
    finally:
        win.close()


def test_handle_stop_stops_when_running(monkeypatch):
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(process_manager, "is_running", lambda install_dir: True)
        stopped = []
        monkeypatch.setattr(process_manager, "graceful_stop", lambda s: stopped.append(s))

        win._handle_stop(server)

        assert stopped == [server]
        assert win._known_running[server.id] is False
        assert server.id not in win._expected_stop  # cleared again after the call
        assert server.desired_running is False  # so auto-resume leaves it down through a reboot
    finally:
        win.close()


def test_handle_stop_does_nothing_if_not_running(monkeypatch):
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)
        stopped = []
        monkeypatch.setattr(process_manager, "graceful_stop", lambda s: stopped.append(s))

        win._handle_stop(server)

        assert stopped == []
    finally:
        win.close()


def test_backup_sources_for_wizard_lists_other_servers_backups_newest_first(tmp_path):
    import backup_manager

    cfg = models.AppConfig()
    a = cfg.add_server("Chudville")
    a.backup_destination = str(tmp_path / "a-backups")
    b = cfg.add_server("Second")
    b.backup_destination = str(tmp_path / "b-backups")
    new_server = cfg.add_server("Brand New")  # the one we're excluding -- opening its own wizard

    os.makedirs(a.backup_destination)
    os.makedirs(b.backup_destination)
    with open(os.path.join(a.backup_destination, "20260101-100000_scheduled.zip"), "w") as f:
        f.write("x")
    with open(os.path.join(b.backup_destination, "20260301-100000_manual.zip"), "w") as f:
        f.write("x")  # newer than a's

    win = MainWindow(config=cfg)
    try:
        sources = win._backup_sources_for_wizard(exclude_id=new_server.id)
        assert len(sources) == 2
        # newest (b's March backup) first
        assert "Second" in sources[0][0]
        assert "Chudville" in sources[1][0]
    finally:
        win.close()


def test_backup_sources_for_wizard_excludes_the_target_server_and_ones_without_a_destination(tmp_path):
    cfg = models.AppConfig()
    a = cfg.add_server("Chudville")
    a.backup_destination = str(tmp_path / "a-backups")
    os.makedirs(a.backup_destination)
    with open(os.path.join(a.backup_destination, "20260101-100000_scheduled.zip"), "w") as f:
        f.write("x")
    b = cfg.add_server("No Backups Configured")  # backup_destination left blank

    win = MainWindow(config=cfg)
    try:
        # Excluding a itself -- only b remains, and b has no destination, so nothing.
        assert win._backup_sources_for_wizard(exclude_id=a.id) == []
        # Excluding b -- a's one backup should show up.
        sources = win._backup_sources_for_wizard(exclude_id=b.id)
        assert len(sources) == 1
        assert "Chudville" in sources[0][0]
    finally:
        win.close()


def _patch_cleanup_env(monkeypatch):
    """No real PowerShell, router, startup entry or self-removal in tests."""
    import network_setup
    import powershell
    import self_delete
    import startup_registration
    calls = {"fw": [], "ps": [], "spawn": [], "upnp": []}
    monkeypatch.setattr(network_setup, "remove_firewall_rules",
                        lambda ids, *a, legacy_names=(), program_dirs=(), **k: calls["fw"].append((list(ids), list(program_dirs))) or True)
    monkeypatch.setattr(network_setup, "remove_upnp_mappings", lambda *a, **k: calls["upnp"].append(a) or 0)
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: None)
    monkeypatch.setattr(powershell, "run_privileged", lambda script, timeout=90.0: calls["ps"].append(script) or powershell.RUN_OK)
    monkeypatch.setattr(self_delete, "spawn_self_delete_helper",
                        lambda install_dir, pid=None, preserve_name=None, extra_paths=(): calls["spawn"].append((install_dir, preserve_name, list(extra_paths))))
    monkeypatch.setattr(startup_registration, "unregister", lambda: None)
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    return calls


def _inline_window(cfg):
    win = MainWindow(config=cfg)
    win.RUN_OPS_INLINE = True
    return win


def test_handle_delete_app_only_spawns_helper_and_quits(monkeypatch):
    cfg = models.AppConfig()
    win = _inline_window(cfg)
    try:
        calls = _patch_cleanup_env(monkeypatch)
        win._handle_delete_app_only()
        assert len(calls["spawn"]) == 1
        assert calls["spawn"][0][1] == conanops_paths.APP_DATA_DIRNAME  # data kept
        assert calls["spawn"][0][2] == []
        assert win._really_quit is True
    finally:
        win.close()


def test_handle_delete_app_only_shows_error_if_spawn_fails(monkeypatch):
    import self_delete
    cfg = models.AppConfig()
    win = _inline_window(cfg)
    try:
        _patch_cleanup_env(monkeypatch)

        def _raise(*a, **k):
            raise OSError("no permission")
        monkeypatch.setattr(self_delete, "spawn_self_delete_helper", _raise)
        win._handle_delete_app_only()
        assert not getattr(win, "_really_quit", False)
    finally:
        win.close()


def _server_with_files(cfg, tmp_path, name, steamcmd=None):
    s = cfg.add_server(name)
    root = tmp_path / "data" / s.id
    s.install_dir = str(root / "server")
    s.steamcmd_dir = steamcmd or str(root / "steamcmd")
    s.backup_destination = str(root / "backups")
    for d in (s.install_dir, s.steamcmd_dir, s.backup_destination):
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "f.txt"), "w").close()
    return s


def test_delete_everything_removes_all_servers_and_windows_changes(monkeypatch, tmp_path):
    import cleanup
    monkeypatch.setattr(cleanup, "owned_roots", lambda: [str(tmp_path / "data")])
    cfg = models.AppConfig(original_active_hours={"ActiveHoursStart": 8, "ActiveHoursEnd": 17, "SmartActiveHoursState": 1})
    a = _server_with_files(cfg, tmp_path, "A")
    b = _server_with_files(cfg, tmp_path, "B", steamcmd=a.steamcmd_dir)  # shares A's SteamCMD
    win = _inline_window(cfg)
    try:
        calls = _patch_cleanup_env(monkeypatch)
        win._handle_delete_everything()
        for p in (a.install_dir, a.steamcmd_dir, a.backup_destination, b.install_dir, b.backup_destination):
            assert not os.path.exists(p), p
        script = calls["ps"][0]
        assert "Remove-NetFirewallRule" in script and "ActiveHoursStart -Value 8" in script
        assert "Unregister-ScheduledTask" in script
        assert len(calls["ps"]) == 1  # one permission prompt
        assert calls["spawn"] and calls["spawn"][0][1] is None  # app removed entirely
        assert str(tmp_path / "data") in calls["spawn"][0][2]
    finally:
        win.close()


def test_delete_everything_keeps_going_when_one_thing_fails(monkeypatch, tmp_path):
    import cleanup
    monkeypatch.setattr(cleanup, "owned_roots", lambda: [])
    cfg = models.AppConfig()
    a = _server_with_files(cfg, tmp_path, "A")
    b = _server_with_files(cfg, tmp_path, "B")
    real = cleanup.remove_path
    monkeypatch.setattr(cleanup, "remove_path",
                        lambda p, retries=3: "locked" if p == a.install_dir else real(p, retries=1))
    win = _inline_window(cfg)
    try:
        calls = _patch_cleanup_env(monkeypatch)
        warned = []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a_, **k: warned.append(a_[2])))
        win._handle_delete_everything()
        assert not os.path.exists(b.install_dir)
        assert warned and "locked" in warned[0]
        assert calls["spawn"]  # still removes itself
    finally:
        win.close()


class _FakeRemoveDialog:
    accept = True
    delete_files = True
    delete_backups = True

    def __init__(self, *a, **k):
        pass

    def exec(self):
        return QDialog.Accepted if self.accept else QDialog.Rejected


def _fake_remove_dialog(monkeypatch, *, accept=True, delete_files=True, delete_backups=True):
    import ui.main_window as main_window_mod
    fake = type("_Fake", (_FakeRemoveDialog,), {
        "accept": accept, "delete_files": delete_files, "delete_backups": delete_backups,
    })
    monkeypatch.setattr(main_window_mod, "RemoveServerDialog", fake)
    return fake


def test_removing_the_last_server_is_now_allowed(monkeypatch):
    cfg = models.AppConfig()
    server = cfg.add_server("Only Server")
    win = _inline_window(cfg)
    try:
        _patch_cleanup_env(monkeypatch)
        _fake_remove_dialog(monkeypatch, delete_files=False, delete_backups=False)
        win._on_remove_server_requested(server.id)
        assert cfg.servers == []
        assert win.dashboard_page.empty_frame.isHidden() is False
    finally:
        win.close()


def test_declining_the_remove_dialog_keeps_the_server(monkeypatch):
    cfg = models.AppConfig()
    server = cfg.add_server("Keep Me")
    win = _inline_window(cfg)
    try:
        _patch_cleanup_env(monkeypatch)
        _fake_remove_dialog(monkeypatch, accept=False)
        win._on_remove_server_requested(server.id)
        assert [s.id for s in cfg.servers] == [server.id]
    finally:
        win.close()


def test_remove_deletes_files_backups_folder_and_firewall_rules(monkeypatch, tmp_path):
    import cleanup
    monkeypatch.setattr(cleanup, "owned_roots", lambda: [str(tmp_path / "data")])
    cfg = models.AppConfig()
    s = _server_with_files(cfg, tmp_path, "Gone")
    win = _inline_window(cfg)
    try:
        calls = _patch_cleanup_env(monkeypatch)
        info = []
        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: info.append(a[2])))
        _fake_remove_dialog(monkeypatch)
        win._on_remove_server_requested(s.id)
        assert not os.path.exists(os.path.dirname(s.install_dir))  # the per-server folder too
        assert calls["fw"] == [([s.id], [s.install_dir])]
        assert calls["upnp"]
        assert info and "removed" in info[0]
    finally:
        win.close()


def test_remove_keeps_files_when_unticked(monkeypatch, tmp_path):
    cfg = models.AppConfig()
    s = _server_with_files(cfg, tmp_path, "Keep files")
    win = _inline_window(cfg)
    try:
        _patch_cleanup_env(monkeypatch)
        _fake_remove_dialog(monkeypatch, delete_files=False, delete_backups=False)
        win._on_remove_server_requested(s.id)
        assert os.path.exists(s.install_dir) and os.path.exists(s.backup_destination)
        assert cfg.servers == []
    finally:
        win.close()


def test_remove_keeps_a_steamcmd_folder_another_server_uses(monkeypatch, tmp_path):
    import cleanup
    monkeypatch.setattr(cleanup, "owned_roots", lambda: [str(tmp_path / "data")])
    cfg = models.AppConfig()
    a = _server_with_files(cfg, tmp_path, "A")
    b = _server_with_files(cfg, tmp_path, "B", steamcmd=a.steamcmd_dir)
    win = _inline_window(cfg)
    try:
        _patch_cleanup_env(monkeypatch)
        _fake_remove_dialog(monkeypatch)
        win._on_remove_server_requested(a.id)
        assert not os.path.exists(a.install_dir)
        assert os.path.exists(a.steamcmd_dir)  # B still needs it
        assert os.path.exists(b.install_dir)
    finally:
        win.close()


def test_remove_stops_a_running_server_before_deleting(monkeypatch, tmp_path):
    import process_manager
    cfg = models.AppConfig()
    s = _server_with_files(cfg, tmp_path, "Running")
    running = {"v": True}
    order = []
    monkeypatch.setattr(process_manager, "is_running", lambda d: running["v"])
    monkeypatch.setattr(process_manager, "graceful_stop",
                        lambda srv, timeout=15.0: (order.append("stop"), running.__setitem__("v", False)))
    import cleanup
    real = cleanup.remove_path
    monkeypatch.setattr(cleanup, "remove_path", lambda p, retries=3: (order.append("delete"), real(p, retries=1))[1])
    win = _inline_window(cfg)
    try:
        _patch_cleanup_env(monkeypatch)
        _fake_remove_dialog(monkeypatch)
        win._on_remove_server_requested(s.id)
        assert order and order[0] == "stop"
    finally:
        win.close()


def test_remove_failure_is_reported_and_server_still_removed(monkeypatch, tmp_path):
    import cleanup
    cfg = models.AppConfig()
    s = _server_with_files(cfg, tmp_path, "Stuck")
    monkeypatch.setattr(cleanup, "remove_path", lambda p, retries=3: f"Couldn't delete {p}: locked")
    win = _inline_window(cfg)
    try:
        _patch_cleanup_env(monkeypatch)
        warned = []
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a[2])))
        _fake_remove_dialog(monkeypatch)
        win._on_remove_server_requested(s.id)
        assert cfg.servers == [] and warned and "locked" in warned[0]
    finally:
        win.close()


def test_load_active_server_retroactively_defaults_backup_destination(tmp_path):
    """A server set up before this default existed had backup_destination
    stuck at "" forever, silently disabling its scheduled/pre-update
    backups -- viewing it must fix that going forward, not just newly
    created servers."""
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = str(tmp_path / "install")
    os.makedirs(server.install_dir)
    assert server.backup_destination == ""

    win = MainWindow(config=cfg)
    try:
        win._load_active_server()
        assert server.backup_destination == str(tmp_path / "backups")
    finally:
        win.close()


def test_load_active_server_does_not_touch_an_existing_backup_destination(tmp_path):
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = str(tmp_path / "install")
    os.makedirs(server.install_dir)
    server.backup_destination = "D:\\MyOwnBackups"

    win = MainWindow(config=cfg)
    try:
        win._load_active_server()
        assert server.backup_destination == "D:\\MyOwnBackups"
    finally:
        win.close()


# ------------------------------------------------------- setup wizard cancel --

class _FakeSetupWizard:
    """Stands in for ui.setup_wizard.SetupWizard so tests can drive
    _open_setup_wizard() without a real modal QWizard event loop."""
    accept: bool = True

    def __init__(self, server, **kwargs):
        self.server = server
        self.network_page = type("_FakeNetworkPage", (), {
            "_ran": False, "detected_game_port": 7777, "detected_query_port": 27015,
        })()

    def exec(self):
        return 1 if self.accept else 0

    def field(self, name):
        return self.server.name

    def apply_to_server(self):
        self.server.install_dir = "C:\\fake\\install"


def _fake_setup_wizard(monkeypatch, *, accept=True):
    import ui.main_window as main_window_mod
    fake = type("_Fake", (_FakeSetupWizard,), {"accept": accept})
    monkeypatch.setattr(main_window_mod, "SetupWizard", fake)
    return fake


def test_cancelling_setup_wizard_for_a_brand_new_server_removes_it(monkeypatch):
    """The bug this fixes: _on_add_server() saves a placeholder server
    to config BEFORE the wizard even opens (so the wizard has
    something to write into as the person goes). Cancelling used to
    leave that placeholder behind forever -- an empty, unconfigured
    server sitting in the sidebar with no install folder."""
    cfg = models.AppConfig()
    win = MainWindow(config=cfg)
    try:
        _fake_setup_wizard(monkeypatch, accept=False)

        win._on_add_server()

        assert cfg.servers == []
    finally:
        win.close()


def test_finishing_setup_wizard_keeps_the_server(monkeypatch):
    cfg = models.AppConfig()
    win = MainWindow(config=cfg)
    try:
        _fake_setup_wizard(monkeypatch, accept=True)

        win._on_add_server()

        assert len(cfg.servers) == 1
        assert cfg.servers[0].install_dir == "C:\\fake\\install"
    finally:
        win.close()


def test_cancelling_setup_wizard_for_an_already_configured_server_keeps_it(monkeypatch, tmp_path):
    """_open_setup_wizard() is never actually called this way today
    (dashboard_page.py's "Set Up Server" button only shows when
    install_dir is empty) -- this locks in the defensive guard that
    keeps it safe if that ever changes: an already-configured server
    must never be deleted just because someone reopened its setup and
    then backed out."""
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = str(tmp_path / "already-set-up")
    os.makedirs(server.install_dir)
    win = MainWindow(config=cfg)
    try:
        _fake_setup_wizard(monkeypatch, accept=False)

        win._open_setup_wizard(server)

        assert cfg.servers == [server]
        assert server.install_dir == str(tmp_path / "already-set-up")
    finally:
        win.close()


# ------------------------------------------------------------ auto-resume --

def test_auto_resume_starts_desired_but_not_running_servers(monkeypatch):
    import process_manager
    import preflight

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = True
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))

    win = MainWindow(config=cfg)  # __init__ calls _auto_resume_servers()
    try:
        assert launched == [server]
        assert win._known_running[server.id] is True
    finally:
        win.close()


def test_auto_resume_leaves_undesired_servers_alone(monkeypatch):
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = False
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)
    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))

    win = MainWindow(config=cfg)
    try:
        assert launched == []
    finally:
        win.close()


def test_auto_resume_skips_a_server_already_running(monkeypatch):
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = True
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: True)
    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))

    win = MainWindow(config=cfg)
    try:
        assert launched == []
    finally:
        win.close()


def test_auto_resume_notifies_instead_of_blocking_dialog_on_preflight_failure(monkeypatch):
    """Unlike a manual Start click, failures here must go through
    _notify() (tray/webhook), not a blocking QMessageBox -- nobody's
    necessarily there to dismiss one during an automatic startup."""
    import process_manager
    import preflight
    from PySide6.QtWidgets import QMessageBox

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = True
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=False, problems=["port in use"], repairs=[]))
    shown_dialogs = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: shown_dialogs.append(a)))
    notified = []

    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append((srv.id, title)))
        win._auto_resume_servers()

        assert shown_dialogs == []
        assert notified == [(server.id, "Auto-Resume Failed")]
    finally:
        win.close()


# --------------------------------------------------------------- watchdog --

def test_watchdog_restarts_a_crashed_desired_server(monkeypatch):
    import process_manager
    import preflight

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = True
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
        launched = []
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))
        win._known_running[server.id] = True  # was running before this health check

        win._on_health_check_finished([(server.id, False, None)])  # now it's not

        assert launched == [server]
        assert win._known_running[server.id] is True
        assert win._watchdog_attempts[server.id] == 1
    finally:
        win.close()


def test_watchdog_does_not_restart_a_crashed_undesired_server(monkeypatch):
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = False
    win = MainWindow(config=cfg)
    try:
        launched = []
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))
        win._known_running[server.id] = True

        win._on_health_check_finished([(server.id, False, None)])

        assert launched == []
    finally:
        win.close()


def test_watchdog_still_notifies_crash_even_when_not_desired(monkeypatch):
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = False
    win = MainWindow(config=cfg)
    try:
        notified = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append(title))
        win._known_running[server.id] = True

        win._on_health_check_finished([(server.id, False, None)])

        assert "Server Crashed" in notified
    finally:
        win.close()


def test_watchdog_respects_backoff_between_attempts(monkeypatch):
    import process_manager
    import preflight

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = True
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
        launched = []
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))

        win._attempt_watchdog_restart(server, reason="it crashed", need_stop_first=False)
        assert len(launched) == 1

        # Immediately try again -- still well within the first backoff
        # delay (30s), so this must NOT attempt another restart yet.
        win._attempt_watchdog_restart(server, reason="it crashed", need_stop_first=False)
        assert len(launched) == 1
    finally:
        win.close()


def test_watchdog_gives_up_after_max_attempts_and_alerts(monkeypatch):
    import process_manager
    import preflight

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = True
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
        monkeypatch.setattr(process_manager, "launch", lambda s: None)
        notified_titles = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified_titles.append(title))
        # Bypass the real backoff timing so MAX_ATTEMPTS attempts can
        # happen in this test without actually waiting: pretend each
        # one happened long enough ago to clear the delay.
        import time as time_mod

        t = [1000.0]
        monkeypatch.setattr(time_mod, "monotonic", lambda: t[0])

        for _ in range(win._WATCHDOG_MAX_ATTEMPTS):
            win._attempt_watchdog_restart(server, reason="it crashed", need_stop_first=False)
            t[0] += 301  # past even the longest backoff step (300s), well under the 1hr streak-reset

        assert win._watchdog_attempts[server.id] == win._WATCHDOG_MAX_ATTEMPTS
        assert "Watchdog Giving Up" in notified_titles

        # One more attempt after giving up must be a no-op.
        launched_count_before = win._watchdog_attempts[server.id]
        win._attempt_watchdog_restart(server, reason="it crashed", need_stop_first=False)
        assert win._watchdog_attempts[server.id] == launched_count_before
    finally:
        win.close()


def test_watchdog_streak_resets_after_sustained_uptime(monkeypatch):
    """A server that ran fine for over an hour since its last restart
    attempt shouldn't have that old crash count held against it --
    otherwise a server that crashes only rarely would eventually
    exhaust its retries forever."""
    import process_manager
    import preflight
    import time as time_mod

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = True
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
        monkeypatch.setattr(process_manager, "launch", lambda s: None)
        t = [1000.0]
        monkeypatch.setattr(time_mod, "monotonic", lambda: t[0])

        for _ in range(win._WATCHDOG_MAX_ATTEMPTS):
            win._attempt_watchdog_restart(server, reason="it crashed", need_stop_first=False)
            t[0] += 301
        assert win._watchdog_attempts[server.id] == win._WATCHDOG_MAX_ATTEMPTS

        t[0] += win._WATCHDOG_STREAK_RESET_SECONDS + 1  # a long healthy stretch passes

        win._attempt_watchdog_restart(server, reason="it crashed", need_stop_first=False)

        assert win._watchdog_attempts[server.id] == 1  # streak reset, this counts as attempt #1 again
    finally:
        win.close()


def test_watchdog_hang_restart_stops_before_relaunching(monkeypatch):
    import process_manager
    import preflight

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = True
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
        stopped = []
        launched = []
        monkeypatch.setattr(process_manager, "graceful_stop", lambda s: stopped.append(s))
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))

        win._attempt_watchdog_restart(server, reason="it stopped responding to queries", need_stop_first=True)

        assert stopped == [server]
        assert launched == [server]
        assert server.id not in win._expected_stop  # cleared again after
    finally:
        win.close()


def test_track_hung_state_marks_and_restarts_after_threshold(monkeypatch):
    import process_manager
    import preflight
    import time as time_mod

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = True
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
        monkeypatch.setattr(process_manager, "graceful_stop", lambda s: None)
        launched = []
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))
        t = [1000.0]
        monkeypatch.setattr(time_mod, "monotonic", lambda: t[0])

        win._track_hung_state(server, info=None)  # first sighting -- just records the time
        assert launched == []
        assert server.id in win._unresponsive_since

        t[0] += win._HUNG_THRESHOLD_SECONDS + 1
        win._track_hung_state(server, info=None)  # past the threshold now

        assert launched == [server]
        assert server.id not in win._unresponsive_since
    finally:
        win.close()


def test_track_hung_state_clears_once_it_responds_again(monkeypatch):
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        win._unresponsive_since[server.id] = 500.0

        win._track_hung_state(server, info={"name": "Chudville"})

        assert server.id not in win._unresponsive_since
    finally:
        win.close()


# -------------------------------------------------------- kick / ban --

def test_handle_kick_player_calls_banlist_manager_and_notifies(monkeypatch):
    import banlist_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.rcon_enabled = True
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(banlist_manager, "kick_player", lambda srv, name: f"Kicked {name}.")
        notified = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append((msg, title)))

        win._handle_kick_player(server, "Conan the Barbarian")

        assert notified == [("Kicked Conan the Barbarian.", "Player Kicked")]
    finally:
        win.close()


def test_handle_ban_player_calls_banlist_manager_and_refreshes_access_page(monkeypatch):
    import banlist_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    cfg.active_server_id = server.id
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(banlist_manager, "ban_player", lambda srv, sid: "Banned.")
        notified = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append((msg, title)))
        refreshed = []
        monkeypatch.setattr(win.access_page, "set_server", lambda srv: refreshed.append(srv))

        win._handle_ban_player(server, "Conan the Barbarian", "76561198000000000")

        assert notified == [("Conan the Barbarian (76561198000000000): Banned.", "Player Banned")]
        assert refreshed == [server]
    finally:
        win.close()


def test_players_page_online_state_updates_on_join_and_leave(monkeypatch):
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    cfg.active_server_id = server.id
    win = MainWindow(config=cfg)
    try:
        win._load_active_server()

        win._on_player_joined(server.id, "Conan the Barbarian")
        assert "Conan the Barbarian" in win.players_page._online_names

        win._on_player_left(server.id, "Conan the Barbarian")
        assert "Conan the Barbarian" not in win.players_page._online_names
    finally:
        win.close()


# -------------------------------------------------- mods update alongside --

def test_update_mods_then_relaunch_refreshes_every_mod(monkeypatch):
    import process_manager
    from update_runner import ModDownloadWorker

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.steamcmd_dir = "/tmp/fake-steamcmd"
    server.mods = [{"id": "111", "name": "Pippi", "enabled": True}, {"id": "222", "name": "Old", "enabled": False}]
    win = MainWindow(config=cfg)
    try:
        started = []
        monkeypatch.setattr(ModDownloadWorker, "start", lambda self: started.append((self.steamcmd_dir, self.workshop_ids)))

        win._update_mods_then_relaunch(server, was_running=True)

        # Disabled mods too -- otherwise they go stale and re-enabling
        # one later loads an outdated .pak (matches the Mods tab's own
        # Download button).
        assert started == [("/tmp/fake-steamcmd", ["111", "222"])]
        assert server.id in win._mod_refresh_workers
    finally:
        win.close()


def test_update_mods_then_relaunch_skips_mod_step_with_no_enabled_mods(monkeypatch):
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.steamcmd_dir = "/tmp/fake-steamcmd"
    server.mods = []
    win = MainWindow(config=cfg)
    try:
        launched = []
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))

        win._update_mods_then_relaunch(server, was_running=True)

        assert launched == [server]
        assert server.id not in win._mod_refresh_workers
    finally:
        win.close()


def test_update_mods_then_relaunch_skips_mod_step_with_no_steamcmd_dir(monkeypatch):
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.steamcmd_dir = ""
    server.mods = [{"id": "111", "name": "Pippi", "enabled": True}]
    win = MainWindow(config=cfg)
    try:
        launched = []
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))

        win._update_mods_then_relaunch(server, was_running=True)

        assert launched == [server]
    finally:
        win.close()


def test_mod_refresh_finished_relaunches_regardless_of_mod_result(monkeypatch):
    import process_manager
    import steamcmd

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        launched = []
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))
        notified = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append(title))
        win._mod_refresh_workers[server.id] = object()

        win._on_post_update_mod_refresh_finished(server, steamcmd.UpdateResult(False, "network error"), was_running=True)

        assert launched == [server]  # still relaunches even though the mod refresh failed
        assert "Mod Update Failed" in notified
        assert server.id not in win._mod_refresh_workers
    finally:
        win.close()


def test_mod_refresh_finished_does_not_relaunch_if_it_was_not_running(monkeypatch):
    import process_manager
    import steamcmd

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        launched = []
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))

        win._on_post_update_mod_refresh_finished(server, steamcmd.UpdateResult(True, "ok"), was_running=False)

        assert launched == []
    finally:
        win.close()


def test_manual_update_relaunch_also_goes_through_mod_refresh(monkeypatch):
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.steamcmd_dir = ""  # no mods to refresh -- exercises the pass-through path
    server.mods = []
    win = MainWindow(config=cfg)
    try:
        launched = []
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))

        win._relaunch_after_manual_update(server, was_running=True)

        assert launched == [server]
    finally:
        win.close()


# --------------------------------------------------- proactive disk space --

def test_low_disk_space_alerts_once_below_error_threshold(monkeypatch, tmp_path):
    import shutil as shutil_mod
    import diagnostics

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = str(tmp_path)
    win = MainWindow(config=cfg)
    try:
        notified = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append(title))
        low = shutil_mod._ntuple_diskusage(total=500 * 1024**3, used=499 * 1024**3, free=int(0.5 * 1024**3))
        monkeypatch.setattr(shutil_mod, "disk_usage", lambda path: low)

        win._check_disk_space_all()
        win._check_disk_space_all()  # a second tick while still low -- must NOT alert again

        assert notified == ["Low Disk Space"]
    finally:
        win.close()


def test_low_disk_space_alerts_again_after_recovering_and_dropping(monkeypatch, tmp_path):
    import shutil as shutil_mod

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = str(tmp_path)
    win = MainWindow(config=cfg)
    try:
        notified = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append(title))
        low = shutil_mod._ntuple_diskusage(total=500 * 1024**3, used=499 * 1024**3, free=int(0.5 * 1024**3))
        recovered = shutil_mod._ntuple_diskusage(total=500 * 1024**3, used=400 * 1024**3, free=100 * 1024**3)

        monkeypatch.setattr(shutil_mod, "disk_usage", lambda path: low)
        win._check_disk_space_all()
        monkeypatch.setattr(shutil_mod, "disk_usage", lambda path: recovered)
        win._check_disk_space_all()
        monkeypatch.setattr(shutil_mod, "disk_usage", lambda path: low)
        win._check_disk_space_all()

        assert notified == ["Low Disk Space", "Low Disk Space"]
    finally:
        win.close()


def test_disk_space_check_covers_install_and_backup_separately(monkeypatch, tmp_path):
    import shutil as shutil_mod

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    install_dir = tmp_path / "install"
    backup_dir = tmp_path / "backups"
    install_dir.mkdir()
    backup_dir.mkdir()
    server.install_dir = str(install_dir)
    server.backup_destination = str(backup_dir)
    win = MainWindow(config=cfg)
    try:
        notified = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append(msg))
        low = shutil_mod._ntuple_diskusage(total=500 * 1024**3, used=499 * 1024**3, free=int(0.5 * 1024**3))
        monkeypatch.setattr(shutil_mod, "disk_usage", lambda path: low)

        win._check_disk_space_all()

        assert len(notified) == 2  # one alert for install, one for backup
        assert any("install folder" in m for m in notified)
        assert any("backup folder" in m for m in notified)
    finally:
        win.close()


def test_disk_space_check_skips_missing_folders(monkeypatch, tmp_path):
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = str(tmp_path / "does-not-exist")
    win = MainWindow(config=cfg)
    try:
        notified = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append(title))

        win._check_disk_space_all()  # must not raise

        assert notified == []
    finally:
        win.close()


def test_disk_space_check_also_prunes_old_game_logs(monkeypatch, tmp_path):
    import game_log_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = str(tmp_path / "install")
    os.makedirs(server.install_dir)
    win = MainWindow(config=cfg)
    try:
        pruned_dirs = []
        monkeypatch.setattr(game_log_manager, "prune_old_logs", lambda install_dir: pruned_dirs.append(install_dir))

        win._check_disk_space_all()

        assert pruned_dirs == [server.install_dir]
    finally:
        win.close()


# ----------------------------------------------- repeated update failures --

def test_repeated_automatic_update_failures_trigger_distinct_alert(monkeypatch):
    import steamcmd

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        notified_titles = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified_titles.append(title))
        monkeypatch.setattr(win, "_update_mods_then_relaunch", lambda srv, wr, **kw: None)
        failure = steamcmd.UpdateResult(False, "network error")

        for _ in range(win._UPDATE_FAILURE_ALERT_THRESHOLD):
            win._on_update_apply_finished(server, failure, was_running=False)

        assert notified_titles.count("Update Failed") == win._UPDATE_FAILURE_ALERT_THRESHOLD
        assert notified_titles.count("Repeated Update Failures") == 1
        assert win._update_failure_streak[server.id] == win._UPDATE_FAILURE_ALERT_THRESHOLD
    finally:
        win.close()


def test_update_failure_streak_does_not_alert_before_threshold(monkeypatch):
    import steamcmd

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        notified_titles = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified_titles.append(title))
        monkeypatch.setattr(win, "_update_mods_then_relaunch", lambda srv, wr, **kw: None)
        failure = steamcmd.UpdateResult(False, "network error")

        win._on_update_apply_finished(server, failure, was_running=False)

        assert "Repeated Update Failures" not in notified_titles
    finally:
        win.close()


def test_update_success_resets_the_failure_streak(monkeypatch):
    import steamcmd

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: None)
        monkeypatch.setattr(win, "_update_mods_then_relaunch", lambda srv, wr, **kw: None)
        win._update_failure_streak[server.id] = 2

        win._on_update_apply_finished(server, steamcmd.UpdateResult(True, "ok", installed_buildid="123"), was_running=False)

        assert server.id not in win._update_failure_streak
    finally:
        win.close()


def test_manual_update_success_also_resets_the_failure_streak(monkeypatch):
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: None)
        win._update_failure_streak[server.id] = 2

        win._handle_update_applied(server, "123")

        assert server.id not in win._update_failure_streak
    finally:
        win.close()


# ---------------------------------------------------- periodic mod refresh --

def test_handle_mod_refresh_due_starts_download_worker(monkeypatch):
    from update_runner import ModDownloadWorker

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.steamcmd_dir = "/tmp/fake-steamcmd"
    server.mods = [{"id": "111", "name": "Pippi", "enabled": True}, {"id": "222", "name": "Old", "enabled": False}]
    win = MainWindow(config=cfg)
    try:
        started = []
        monkeypatch.setattr(ModDownloadWorker, "start", lambda self: started.append((self.steamcmd_dir, self.workshop_ids)))

        win._handle_mod_refresh_due(server)

        assert started == [("/tmp/fake-steamcmd", ["111", "222"])]
        assert server.id in win._mod_refresh_workers
    finally:
        win.close()


def test_handle_mod_refresh_due_skips_if_already_busy(monkeypatch):
    from update_runner import ModDownloadWorker

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.steamcmd_dir = "/tmp/fake-steamcmd"
    server.mods = [{"id": "111", "name": "Pippi", "enabled": True}]
    win = MainWindow(config=cfg)
    try:
        win._mod_refresh_workers[server.id] = object()  # pretend one's already running
        started = []
        monkeypatch.setattr(ModDownloadWorker, "start", lambda self: started.append(1))

        win._handle_mod_refresh_due(server)

        assert started == []
    finally:
        win.close()


def test_handle_mod_refresh_due_stamps_immediately_with_no_enabled_mods(monkeypatch):
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.steamcmd_dir = "/tmp/fake-steamcmd"
    server.mods = []
    win = MainWindow(config=cfg)
    try:
        win._handle_mod_refresh_due(server)

        assert server.last_mod_check_at != ""
        assert server.id not in win._mod_refresh_workers
    finally:
        win.close()


def test_periodic_mod_refresh_finished_stamps_on_success(monkeypatch):
    import steamcmd

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    win = MainWindow(config=cfg)
    try:
        win._mod_refresh_workers[server.id] = object()
        notified = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append(title))

        win._on_periodic_mod_refresh_finished(server, steamcmd.UpdateResult(True, "ok"))

        assert server.last_mod_check_at != ""
        assert server.id not in win._mod_refresh_workers
        assert notified == []  # silent on success -- see the handler's own comment
    finally:
        win.close()


def test_periodic_mod_refresh_finished_stamps_and_notifies_on_failure(monkeypatch):
    import steamcmd

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    win = MainWindow(config=cfg)
    try:
        notified = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append(title))

        win._on_periodic_mod_refresh_finished(server, steamcmd.UpdateResult(False, "network error"))

        assert server.last_mod_check_at != ""  # stamped even on failure -- gentle ~24h retry, not a hard backoff
        assert notified == ["Mod Check Failed"]
    finally:
        win.close()


def test_update_triggered_refresh_waits_for_an_in_flight_periodic_one(monkeypatch):
    """Prevents two ModDownloadWorkers racing the same SteamCMD
    Workshop folder: if a periodic check is already running when a
    server update needs its own mod refresh, the update path must
    reuse that worker's result instead of starting a second one."""
    from update_runner import ModDownloadWorker
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.steamcmd_dir = "/tmp/fake-steamcmd"
    server.mods = [{"id": "111", "name": "Pippi", "enabled": True}]
    win = MainWindow(config=cfg)
    try:
        started = []
        monkeypatch.setattr(ModDownloadWorker, "start", lambda self: started.append(1))
        launched = []
        monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s))

        # Simulate a periodic refresh already in flight.
        existing_worker = ModDownloadWorker("/tmp/fake-steamcmd", ["111"])
        win._mod_refresh_workers[server.id] = existing_worker

        win._update_mods_then_relaunch(server, was_running=True)

        assert started == []  # no second worker started
        assert win._mod_refresh_workers[server.id] is existing_worker  # still the original one

        # Once that existing worker finishes, the update path's own
        # relaunch logic should still run off its result.
        import steamcmd
        existing_worker.finished_download.emit(steamcmd.UpdateResult(True, "ok"))
        assert launched == [server]
    finally:
        win.close()


# ------------------------------------------------------- restart warning --

def test_manual_restart_broadcasts_warning_when_players_online(monkeypatch):
    import banlist_manager
    import preflight
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.rcon_enabled = True
    win = MainWindow(config=cfg)
    try:
        win._online_by_server[server.id] = {"Conan the Barbarian"}
        monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
        broadcasts = []
        monkeypatch.setattr(banlist_manager, "broadcast_message", lambda srv, msg: broadcasts.append(msg))
        scheduled = []
        monkeypatch.setattr("ui.main_window.QTimer.singleShot", staticmethod(lambda ms, cb: scheduled.append((ms, cb))))
        monkeypatch.setattr(process_manager, "restart", lambda s: None)

        win._handle_restart(server)

        assert len(broadcasts) == 1
        assert "restarting" in broadcasts[0].lower()
        assert scheduled and scheduled[0][0] == win._RESTART_WARNING_GRACE_SECONDS * 1000
    finally:
        win.close()


def test_manual_restart_skips_broadcast_when_nobody_online(monkeypatch):
    import banlist_manager
    import preflight
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.rcon_enabled = True
    win = MainWindow(config=cfg)
    try:
        broadcasts = []
        monkeypatch.setattr(banlist_manager, "broadcast_message", lambda srv, msg: broadcasts.append(msg))
        monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
        restarted = []
        monkeypatch.setattr(process_manager, "restart", lambda s: restarted.append(s))

        win._handle_restart(server)

        assert broadcasts == []
        assert restarted == [server]  # restarted immediately, no delay
    finally:
        win.close()


def test_manual_restart_skips_broadcast_when_rcon_disabled(monkeypatch):
    import banlist_manager
    import preflight
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.rcon_enabled = False
    win = MainWindow(config=cfg)
    try:
        win._online_by_server[server.id] = {"Conan the Barbarian"}
        monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
        broadcasts = []
        monkeypatch.setattr(banlist_manager, "broadcast_message", lambda srv, msg: broadcasts.append(msg))
        restarted = []
        monkeypatch.setattr(process_manager, "restart", lambda s: restarted.append(s))

        win._handle_restart(server)

        assert broadcasts == []
        assert restarted == [server]
    finally:
        win.close()


def test_finish_restart_shows_dialog_on_manual_failure(monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(process_manager, "restart", lambda s: (_ for _ in ()).throw(FileNotFoundError("no exe")))
        shown = []
        monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: shown.append(1)))

        win._finish_restart(server, manual=True)

        assert shown == [1]
    finally:
        win.close()


def test_finish_restart_notifies_on_unattended_failure(monkeypatch):
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(process_manager, "restart", lambda s: (_ for _ in ()).throw(FileNotFoundError("no exe")))
        notified = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append(title))

        win._finish_restart(server, manual=False)

        assert notified == ["Restart Failed"]
    finally:
        win.close()


def test_scheduled_restart_also_goes_through_warn_then_restart(monkeypatch):
    """Best-effort defense against the narrow race of someone joining
    between the scheduler's own online-check and the restart firing --
    the scheduled path uses the same warn-then-restart mechanism."""
    import preflight
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
        restarted = []
        monkeypatch.setattr(process_manager, "restart", lambda s: restarted.append(s))

        win._restart_unattended(server)

        assert restarted == [server]  # nobody online -- restarts immediately, no broadcast/delay needed
    finally:
        win.close()


# --------------------------------------------------------------- duckdns --

def test_check_duckdns_skips_without_domain_or_token(monkeypatch):
    from dynamic_dns_runner import DuckDnsWorker

    cfg = models.AppConfig()
    win = MainWindow(config=cfg)
    try:
        started = []
        monkeypatch.setattr(DuckDnsWorker, "start", lambda self: started.append(1))

        win._check_duckdns()

        assert started == []
    finally:
        win.close()


def test_check_duckdns_starts_worker_when_configured(monkeypatch):
    from dynamic_dns_runner import DuckDnsWorker

    cfg = models.AppConfig(duckdns_domain="myserver", duckdns_token="TOKEN123")
    win = MainWindow(config=cfg)
    try:
        started = []
        monkeypatch.setattr(DuckDnsWorker, "start", lambda self: started.append((self.domain, self.token)))

        win._check_duckdns()

        assert started == [("myserver", "TOKEN123")]
        assert win._duckdns_worker is not None
    finally:
        win.close()


def test_check_duckdns_skips_if_already_running(monkeypatch):
    from dynamic_dns_runner import DuckDnsWorker

    cfg = models.AppConfig(duckdns_domain="myserver", duckdns_token="TOKEN123")
    win = MainWindow(config=cfg)
    try:
        win._duckdns_worker = object()
        started = []
        monkeypatch.setattr(DuckDnsWorker, "start", lambda self: started.append(1))

        win._check_duckdns()

        assert started == []
    finally:
        win.close()


def test_duckdns_finished_silent_on_success(monkeypatch):
    import dynamic_dns

    cfg = models.AppConfig()
    win = MainWindow(config=cfg)
    try:
        win._duckdns_worker = object()
        messages = []
        if win.tray_icon:
            monkeypatch.setattr(win.tray_icon, "showMessage", lambda *a, **k: messages.append(a))
            monkeypatch.setattr(win.tray_icon, "isVisible", lambda: True)

        win._on_duckdns_finished(dynamic_dns.DuckDnsResult(ok=True, message="OK 1.2.3.4  UPDATED", ip="1.2.3.4"))

        assert win._duckdns_worker is None
        assert messages == []
    finally:
        win.close()


def test_duckdns_finished_shows_tray_message_on_failure(monkeypatch):
    import dynamic_dns

    cfg = models.AppConfig()
    win = MainWindow(config=cfg)
    try:
        win._duckdns_worker = object()
        messages = []
        if win.tray_icon:
            monkeypatch.setattr(win.tray_icon, "showMessage", lambda *a, **k: messages.append(a))
            monkeypatch.setattr(win.tray_icon, "isVisible", lambda: True)

        win._on_duckdns_finished(dynamic_dns.DuckDnsResult(ok=False, message="DuckDNS rejected this domain/token: KO"))

        assert win._duckdns_worker is None
        if win.tray_icon:
            assert len(messages) == 1
    finally:
        win.close()


# ---------------------------------------------------- discord live status --

def test_check_discord_status_skips_servers_without_it_enabled(monkeypatch):
    from discord_status_runner import DiscordStatusWorker

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    cfg.discord_status_enabled = False
    cfg.alert_discord_url = "https://discord.com/api/webhooks/1/abc"
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        started = []
        monkeypatch.setattr(DiscordStatusWorker, "start", lambda self: started.append(1))

        win._check_discord_status_all()

        assert started == []
    finally:
        win.close()


def test_check_discord_status_skips_without_webhook_url(monkeypatch):
    from discord_status_runner import DiscordStatusWorker

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    cfg.discord_status_enabled = True
    cfg.alert_discord_url = ""
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        started = []
        monkeypatch.setattr(DiscordStatusWorker, "start", lambda self: started.append(1))

        win._check_discord_status_all()

        assert started == []
    finally:
        win.close()


def test_check_discord_status_starts_worker_when_enabled(monkeypatch):
    from discord_status_runner import DiscordStatusWorker

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    cfg.discord_status_enabled = True
    cfg.alert_discord_url = "https://discord.com/api/webhooks/1/abc"
    server.install_dir = "/tmp/fake-install"
    server.discord_status_message_id = "12345"
    win = MainWindow(config=cfg)
    try:
        started = []
        monkeypatch.setattr(DiscordStatusWorker, "start", lambda self: started.append((self.webhook_url, self.message_id)))

        win._check_discord_status_all()

        assert started == [("https://discord.com/api/webhooks/1/abc", "12345")]
        assert server.id in win._discord_status_workers
    finally:
        win.close()


def test_check_discord_status_skips_if_already_in_flight(monkeypatch):
    from discord_status_runner import DiscordStatusWorker

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    cfg.discord_status_enabled = True
    cfg.alert_discord_url = "https://discord.com/api/webhooks/1/abc"
    server.install_dir = "/tmp/fake-install"
    win = MainWindow(config=cfg)
    try:
        win._discord_status_workers[server.id] = object()
        started = []
        monkeypatch.setattr(DiscordStatusWorker, "start", lambda self: started.append(1))

        win._check_discord_status_all()

        assert started == []
    finally:
        win.close()


def test_build_discord_status_message_online(monkeypatch):
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    win = MainWindow(config=cfg)
    try:
        win._known_running[server.id] = True
        win._online_by_server[server.id] = {"Conan the Barbarian", "Zim-Zam"}

        msg = win._build_discord_status_message(server)

        assert "Chudville" in msg
        assert "Online" in msg
        assert "2 player" in msg
    finally:
        win.close()


def test_build_discord_status_message_offline(monkeypatch):
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    win = MainWindow(config=cfg)
    try:
        win._known_running[server.id] = False

        msg = win._build_discord_status_message(server)

        assert "Offline" in msg
    finally:
        win.close()


def test_discord_status_finished_stores_new_message_id(monkeypatch):
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.discord_status_message_id = ""
    win = MainWindow(config=cfg)
    try:
        win._discord_status_workers[server.id] = object()

        win._on_discord_status_finished(server, "999888777")

        assert server.discord_status_message_id == "999888777"
        assert server.id not in win._discord_status_workers
    finally:
        win.close()


def test_discord_status_finished_clears_failure_streak_on_success(monkeypatch):
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    win = MainWindow(config=cfg)
    try:
        win._discord_status_failures[server.id] = 3

        win._on_discord_status_finished(server, "12345")

        assert server.id not in win._discord_status_failures
    finally:
        win.close()


def test_discord_status_finished_keeps_message_id_on_a_single_failure(monkeypatch):
    """A transient network blip shouldn't throw away a perfectly good
    message id and start posting a fresh message next time."""
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.discord_status_message_id = "12345"
    win = MainWindow(config=cfg)
    try:
        win._on_discord_status_finished(server, None)

        assert server.discord_status_message_id == "12345"
        assert win._discord_status_failures[server.id] == 1
    finally:
        win.close()


def test_discord_status_finished_clears_message_id_after_repeated_failures(monkeypatch):
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.discord_status_message_id = "12345"
    win = MainWindow(config=cfg)
    try:
        for _ in range(win._DISCORD_STATUS_FAILURE_RESET_THRESHOLD):
            win._on_discord_status_finished(server, None)

        assert server.discord_status_message_id == ""
        assert win._discord_status_failures[server.id] == 0
    finally:
        win.close()


# --------------------------------------------- auto-bisect coordination --

def test_mods_page_lock_and_unlock_wired_to_automation_locked():
    cfg = models.AppConfig()
    win = MainWindow(config=cfg)
    try:
        win.mods_page.lock_server_for_automation("abc123")
        assert "abc123" in win._automation_locked

        win.mods_page.unlock_server_for_automation("abc123")
        assert "abc123" not in win._automation_locked
    finally:
        win.close()


def test_automation_lock_is_not_cleared_by_unrelated_expected_stop_activity():
    """The actual bug this decoupling fixes: an unrelated flow (an
    update, a restore, a manual restart) finishing and discarding the
    server's id from _expected_stop must NOT also release an active
    bisect's lock."""
    cfg = models.AppConfig()
    win = MainWindow(config=cfg)
    try:
        win.mods_page.lock_server_for_automation("abc123")
        win._expected_stop.add("abc123")   # some unrelated flow also marks it
        win._expected_stop.discard("abc123")  # ...and later clears ITS OWN marker

        assert "abc123" in win._automation_locked  # the bisect's lock is untouched
    finally:
        win.close()


def test_open_auto_bisect_locks_and_unlocks_around_the_dialog(monkeypatch):
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.mods = [{"id": "1", "name": "A", "enabled": True}]
    win = MainWindow(config=cfg)
    try:
        win._load_active_server()
        monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)
        lock_calls = []
        during_dialog = {}

        class _FakeDialog:
            def __init__(self, server, mods, was_running, find_all=False, on_changed=None, is_online=None, restart_server=None, parent=None):
                during_dialog["locked"] = server.id in win._automation_locked

            def exec(self):
                return 1

        monkeypatch.setattr("ui.mods_page.AutoBisectDialog", _FakeDialog)

        win.mods_page._open_auto_bisect()

        assert during_dialog["locked"] is True  # locked WHILE the dialog was up
        assert server.id not in win._automation_locked  # released again once it closed
    finally:
        win.close()


def test_open_auto_bisect_unlocks_even_if_dialog_raises(monkeypatch):
    import process_manager

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.mods = [{"id": "1", "name": "A", "enabled": True}]
    win = MainWindow(config=cfg)
    try:
        win._load_active_server()
        monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)

        class _RaisingDialog:
            def __init__(self, server, mods, was_running, find_all=False, on_changed=None, is_online=None, restart_server=None, parent=None):
                pass

            def exec(self):
                raise RuntimeError("something broke")

        monkeypatch.setattr("ui.mods_page.AutoBisectDialog", _RaisingDialog)

        with pytest.raises(RuntimeError):
            win.mods_page._open_auto_bisect()

        assert server.id not in win._automation_locked  # still released despite the exception
    finally:
        win.close()


def test_health_check_ignores_a_server_locked_for_bisect(monkeypatch):
    """The actual bug this whole fix is for: without the lock, a
    server stopped mid-bisect would look exactly like an unexpected
    crash to the health check."""
    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.desired_running = True
    win = MainWindow(config=cfg)
    try:
        notified = []
        monkeypatch.setattr(win, "_notify", lambda srv, msg, title=None: notified.append(title))
        restart_attempts = []
        monkeypatch.setattr(win, "_attempt_watchdog_restart", lambda srv, reason, need_stop_first: restart_attempts.append(reason))

        win._known_running[server.id] = True
        win._automation_locked.add(server.id)  # simulates ModsPage having locked it for a bisect

        win._on_health_check_finished([(server.id, False, None)])  # bisect just stopped it for a round

        assert notified == []  # no false "Server Crashed" alert
        assert restart_attempts == []  # and no watchdog restart racing the bisect's own relaunch
    finally:
        win.close()


def test_close_event_cancels_an_active_bisect_dialog(monkeypatch):
    """The app-quit backstop: closing MainWindow while a Quick Mod
    Check/Find All dialog happens to be open (an unusual path, since
    the dialog is normally modal, but reachable via e.g. the tray
    icon's Exit action) must not abandon the worker mid-run."""
    cfg = models.AppConfig()
    win = MainWindow(config=cfg)
    try:
        win._really_quit = True
        if win.tray_icon:
            monkeypatch.setattr(win.tray_icon, "isVisible", lambda: False)

        class _FakeActiveDialog:
            def __init__(self):
                self.rejected = False

            def reject(self):
                self.rejected = True

        fake_dialog = _FakeActiveDialog()
        win.mods_page._active_bisect_dialog = fake_dialog

        win.close()

        assert fake_dialog.rejected is True
    finally:
        pass


def test_close_event_is_a_noop_with_no_active_bisect_dialog(monkeypatch):
    cfg = models.AppConfig()
    win = MainWindow(config=cfg)
    try:
        win._really_quit = True
        if win.tray_icon:
            monkeypatch.setattr(win.tray_icon, "isVisible", lambda: False)
        assert win.mods_page._active_bisect_dialog is None

        win.close()  # must not raise
    finally:
        pass


# ------------------------------------------- modlist.txt after downloads --

def test_post_update_mod_refresh_rewrites_modlist_before_relaunching(monkeypatch, modlist_writes):
    """A freshly downloaded mod's real .pak path only exists AFTER the
    download -- relaunching without rewriting modlist.txt first starts
    the server pointed at a guessed path that doesn't exist."""
    import process_manager
    import steamcmd

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.steamcmd_dir = "/tmp/fake-steamcmd"
    server.mods = [{"id": "111", "name": "Pippi", "enabled": True}]
    win = MainWindow(config=cfg)
    try:
        order = []
        modlist_writes.clear()
        monkeypatch.setattr(mod_manager, "write_modlist", lambda i, s, m: order.append("modlist"))
        monkeypatch.setattr(process_manager, "launch", lambda srv: order.append("launch"))

        win._on_post_update_mod_refresh_finished(server, steamcmd.UpdateResult(True, "ok"), was_running=True)

        assert order == ["modlist", "launch"]
    finally:
        win.close()


def test_periodic_mod_refresh_rewrites_modlist(monkeypatch, modlist_writes):
    import steamcmd

    cfg = models.AppConfig()
    server = cfg.add_server("Chudville")
    server.install_dir = "/tmp/fake-install"
    server.steamcmd_dir = "/tmp/fake-steamcmd"
    server.mods = [{"id": "111", "name": "Pippi", "enabled": True}]
    win = MainWindow(config=cfg)
    try:
        modlist_writes.clear()
        win._on_periodic_mod_refresh_finished(server, steamcmd.UpdateResult(True, "ok"))
        assert [c[0] for c in modlist_writes] == ["/tmp/fake-install"]
    finally:
        win.close()


def test_missing_server_files_offer_reinstall_instead_of_error(monkeypatch, tmp_path):
    cfg = models.AppConfig()
    s = cfg.add_server("Lost")
    s.install_dir = str(tmp_path / "gone" / "server")
    s.steamcmd_dir = str(tmp_path / "gone" / "steamcmd")
    win = MainWindow(config=cfg)
    try:
        opened, warned = [], []
        monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(a)))
        monkeypatch.setattr(win, "_open_setup_wizard", lambda server: opened.append(server.id))
        win._handle_start(s)
        assert opened == [s.id] and warned == []
    finally:
        win.close()
