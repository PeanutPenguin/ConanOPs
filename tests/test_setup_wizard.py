from __future__ import annotations

import os
import sys

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QWizard

from models import ServerConfig
from ui.setup_wizard import SetupWizard


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def no_blocking_dialogs(monkeypatch):
    # A validation warning would otherwise pop a real modal QMessageBox
    # and hang the test suite waiting for a click that never comes.
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))
    # Plenty of disk space unless a test says otherwise (the CI box may not have 40 GB free).
    import ui.setup_wizard as sw
    monkeypatch.setattr(sw, "_free_bytes", lambda path: 500 * 1024 ** 3)


def test_paths_page_is_complete_with_prefilled_defaults():
    """Both folder fields are pre-filled programmatically (not typed
    by the user) when the wizard opens. PySide6's automatic mandatory-
    field wiring (registerField("name*", widget)) doesn't reliably
    reflect that -- isComplete() could come back False despite valid
    text in both boxes, leaving Next silently disabled with no
    explanation. This must be True as soon as the page is built,
    before any user interaction."""
    server = ServerConfig(id="s1", name="Test")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    wizard.show()
    page = wizard.paths_page

    assert page.steamcmd_edit.text()  # sanity: really is pre-filled
    assert page.install_edit.text()
    assert page.isComplete() is True


def test_next_button_is_enabled_and_advances_without_touching_the_fields(monkeypatch):
    """End-to-end: with no user interaction beyond opening the wizard,
    the Next button must be enabled and clicking it must actually move
    to the next page -- this is the exact bug report (Next silently
    doing nothing)."""
    import steamcmd as steamcmd_mod
    # Clicking Next lands on InstallPage, whose initializePage() starts
    # a real background install immediately -- stub it out so this test
    # doesn't hit the network or spin up a real long-running download.
    monkeypatch.setattr(steamcmd_mod, "install_steamcmd", lambda *a, **k: False)

    server = ServerConfig(id="s1", name="Test")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    wizard.show()
    paths_page_id = wizard.pageIds()[0]
    next_btn = wizard.button(QWizard.NextButton)

    try:
        assert next_btn.isEnabled() is True

        QTest.mouseClick(next_btn, Qt.LeftButton)

        assert wizard.currentId() != paths_page_id
    finally:
        wizard.reject()  # stops InstallPage's worker thread before teardown


def test_paths_page_becomes_incomplete_when_a_field_is_cleared():
    server = ServerConfig(id="s1", name="Test")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    wizard.show()
    page = wizard.paths_page

    page.steamcmd_edit.clear()
    assert page.isComplete() is False
    assert wizard.button(QWizard.NextButton).isEnabled() is False

    page.steamcmd_edit.setText("/some/path")
    assert page.isComplete() is True
    assert wizard.button(QWizard.NextButton).isEnabled() is True


def test_paths_page_prefills_shared_steamcmd_dir_and_stays_complete():
    """Covers the same bug in the other common path: a second server
    where the SteamCMD field is pre-filled with an EXISTING server's
    folder (see AppConfig.default_steamcmd_dir())."""
    server = ServerConfig(id="s2", name="Second")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: {"/srv/conan1"},
        get_default_steamcmd_dir=lambda: "/srv/shared-steamcmd",
    )
    wizard.show()
    page = wizard.paths_page

    assert page.steamcmd_edit.text() == "/srv/shared-steamcmd"
    assert page.isComplete() is True
    assert wizard.button(QWizard.NextButton).isEnabled() is True


def test_paths_page_prefills_name_and_it_is_mandatory():
    server = ServerConfig(id="s1", name="Chudville")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    wizard.show()
    page = wizard.paths_page

    assert page.name_edit.text() == "Chudville"
    assert page.isComplete() is True

    page.name_edit.clear()
    assert page.isComplete() is False
    assert wizard.button(QWizard.NextButton).isEnabled() is False


def test_paths_page_blocks_a_name_already_used_by_another_server():
    """A server's name doubles as its Windows Firewall rule name, so
    two servers sharing one would make those rules collide."""
    server = ServerConfig(id="s2", name="Second")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
        get_reserved_names=lambda: {"Chudville"},
    )
    wizard.show()
    page = wizard.paths_page

    page.name_edit.setText("Chudville")
    assert page.validatePage() is False

    page.name_edit.setText("Chudville 2")
    assert page.validatePage() is True


def test_no_space_default_base_never_contains_a_space():
    from ui.setup_wizard import _no_space_default_base
    assert " " not in _no_space_default_base()


def test_path_has_spaces_detects_spaces():
    from ui.setup_wizard import _path_has_spaces
    assert _path_has_spaces("C:\\Users\\Oran McGee\\ConanOps") is True
    assert _path_has_spaces("C:\\ConanOps\\server1") is False


def test_paths_page_default_paths_have_no_spaces():
    """The pre-filled defaults must never land in a space-containing
    location like the Windows user profile folder (%USERPROFILE%,
    which is ~ here) whenever the account's display name has a space
    in it -- confirmed against a real install where exactly this
    caused SteamCMD's app_update to fail with "Missing configuration"."""
    server = ServerConfig(id="s1", name="Test")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    page = wizard.paths_page

    assert " " not in page.steamcmd_edit.text()
    assert " " not in page.install_edit.text()


def test_paths_page_blocks_a_folder_path_containing_a_space():
    server = ServerConfig(id="s1", name="Test")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    page = wizard.paths_page
    page.install_edit.setText("C:\\Users\\Oran McGee\\ConanOps\\server")

    assert page.validatePage() is False


def test_paths_page_allows_folder_paths_with_no_spaces():
    server = ServerConfig(id="s1", name="Test")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    page = wizard.paths_page
    page.steamcmd_edit.setText("C:\\ConanOps\\shared-steamcmd")
    page.install_edit.setText("C:\\ConanOps\\server1")

    assert page.validatePage() is True


def test_apply_to_server_writes_the_chosen_name():
    server = ServerConfig(id="s1", name="New Server")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    wizard.paths_page.name_edit.setText("Chudville")
    wizard.paths_page.steamcmd_edit.setText("/srv/steamcmd")
    wizard.paths_page.install_edit.setText("/srv/server")

    wizard.apply_to_server()

    assert server.name == "Chudville"


def test_network_page_uses_the_in_progress_name_not_the_stale_one(monkeypatch):
    """The firewall rule created during setup must be named after
    whatever the user just typed on the first page, not the server's
    old/default name -- apply_to_server() doesn't write server.name
    until the whole wizard finishes, so NetworkPage reading
    self.server.name directly would still see the stale value while
    the wizard is still in progress."""
    import network_setup_runner

    captured = {}

    class _FakeWorker:
        finished_setup = type("Sig", (), {"connect": lambda self, fn: None})()

        def __init__(self, server_id, name, reserved_ports, parent=None, **kwargs):
            captured["name"] = name
            captured["server_id"] = server_id

        def start(self):
            pass

    monkeypatch.setattr(network_setup_runner, "WizardNetworkSetupWorker", _FakeWorker)

    server = ServerConfig(id="s1", name="Old Name")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    wizard.paths_page.name_edit.setText("Brand New Name")
    # Manually set the wizard field the way QWizard would once the user
    # actually left PathsPage (registerField ties this to the widget,
    # but pushing text alone is enough to update the field's value here).
    wizard.network_page._run()

    assert captured["name"] == "Brand New Name"
    assert captured["server_id"] == "s1"  # firewall rules/forwards are keyed by id


def test_install_page_progress_bar_starts_indeterminate():
    from ui.setup_wizard import InstallPage

    page = InstallPage()
    assert page.progress_bar.maximum() == 0  # indeterminate ("spinner") until a real percentage arrives


def test_install_page_progress_bar_becomes_determinate_on_first_percent():
    """The core UX fix: a multi-hour download used to show only an
    indeterminate spinner the whole time (indistinguishable from a
    hang); it should switch to a real 0-100 bar the first time
    SteamCMD reports an actual percentage."""
    from ui.setup_wizard import InstallPage

    page = InstallPage()
    page._on_progress_percent(37.5)

    assert page.progress_bar.maximum() == 100
    assert page.progress_bar.value() == 37


def test_install_page_progress_bar_updates_on_subsequent_percents():
    from ui.setup_wizard import InstallPage

    page = InstallPage()
    page._on_progress_percent(10.0)
    page._on_progress_percent(55.9)

    assert page.progress_bar.value() == 55


def test_install_page_progress_bar_clamps_to_valid_range():
    from ui.setup_wizard import InstallPage

    page = InstallPage()
    page._on_progress_percent(150.0)
    assert page.progress_bar.value() == 100

    page._on_progress_percent(-5.0)
    assert page.progress_bar.value() == 0


def _make_backup_zip(path: str) -> None:
    import zipfile
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Saved/game.db", b"fake db content")
        zf.writestr("Saved/Config/ServerSettings.ini", "[Foo]\nBar=1\n")


def test_restore_page_defaults_to_fresh_with_no_backup_selected(tmp_path):
    from ui.setup_wizard import RestorePage

    zpath = str(tmp_path / "backup.zip")
    _make_backup_zip(zpath)
    page = RestorePage(get_backup_sources=lambda: [("Chudville — backup", zpath)])

    assert page.fresh_radio.isChecked() is True
    assert page.validatePage() is True
    assert page.selected_backup_path is None


def test_restore_page_picking_from_combo_sets_selected_path(tmp_path):
    from ui.setup_wizard import RestorePage

    zpath = str(tmp_path / "backup.zip")
    _make_backup_zip(zpath)
    page = RestorePage(get_backup_sources=lambda: [("Chudville — backup", zpath)])

    page.restore_radio.setChecked(True)
    page.source_combo.setCurrentIndex(0)

    assert page.validatePage() is True
    assert page.selected_backup_path == zpath


def test_restore_page_rejects_an_invalid_backup_file(tmp_path):
    from ui.setup_wizard import RestorePage

    bad_path = str(tmp_path / "not_a_backup.txt")
    with open(bad_path, "w") as f:
        f.write("nope")

    page = RestorePage(get_backup_sources=lambda: [])
    page.restore_radio.setChecked(True)
    page._external_path = bad_path

    assert page.validatePage() is False
    assert page.selected_backup_path is None


def test_restore_page_requires_a_choice_when_restoring(tmp_path):
    """Restore mode with nothing picked (no combo entries, no browsed
    file) must not silently proceed as if starting fresh."""
    from ui.setup_wizard import RestorePage

    page = RestorePage(get_backup_sources=lambda: [])
    page.restore_radio.setChecked(True)

    assert page.validatePage() is False


def test_restore_page_with_no_other_servers_backups_disables_combo():
    from ui.setup_wizard import RestorePage

    page = RestorePage(get_backup_sources=lambda: [])
    page.restore_radio.setChecked(True)

    assert page.source_combo.isEnabled() is False


def test_install_worker_restores_the_chosen_backup_after_a_successful_download(tmp_path, monkeypatch):
    import steamcmd as steamcmd_mod
    from ui.setup_wizard import _InstallWorker

    zpath = str(tmp_path / "backup.zip")
    _make_backup_zip(zpath)
    install_dir = str(tmp_path / "server")

    monkeypatch.setattr(steamcmd_mod, "install_steamcmd", lambda *a, **k: True)
    monkeypatch.setattr(
        steamcmd_mod, "update_server",
        lambda *a, **k: steamcmd_mod.UpdateResult(True, "ok"),
    )

    worker = _InstallWorker("C:\\fake\\steamcmd", install_dir, restore_backup_path=zpath)
    results = []
    worker.finished_ok.connect(results.append)

    worker.run()

    assert results == [True]
    assert os.path.exists(os.path.join(install_dir, "ConanSandbox", "Saved", "game.db"))


def test_install_worker_does_not_restore_if_download_failed(tmp_path, monkeypatch):
    """A failed download must not proceed to overwrite anything with
    the chosen backup -- there's nothing to restore ONTO yet."""
    import steamcmd as steamcmd_mod
    from ui.setup_wizard import _InstallWorker

    zpath = str(tmp_path / "backup.zip")
    _make_backup_zip(zpath)
    install_dir = str(tmp_path / "server")

    monkeypatch.setattr(steamcmd_mod, "install_steamcmd", lambda *a, **k: True)
    monkeypatch.setattr(
        steamcmd_mod, "update_server",
        lambda *a, **k: steamcmd_mod.UpdateResult(False, "failed"),
    )

    worker = _InstallWorker("C:\\fake\\steamcmd", install_dir, restore_backup_path=zpath)
    results = []
    worker.finished_ok.connect(results.append)

    worker.run()

    assert results == [False]
    assert not os.path.exists(os.path.join(install_dir, "ConanSandbox", "Saved", "game.db"))


def test_install_worker_reports_failure_if_restore_itself_fails(tmp_path, monkeypatch):
    import steamcmd as steamcmd_mod
    from ui.setup_wizard import _InstallWorker

    install_dir = str(tmp_path / "server")
    missing_backup_path = str(tmp_path / "does_not_exist.zip")

    monkeypatch.setattr(steamcmd_mod, "install_steamcmd", lambda *a, **k: True)
    monkeypatch.setattr(
        steamcmd_mod, "update_server",
        lambda *a, **k: steamcmd_mod.UpdateResult(True, "ok"),
    )

    worker = _InstallWorker("C:\\fake\\steamcmd", install_dir, restore_backup_path=missing_backup_path)
    results = []
    worker.finished_ok.connect(results.append)

    worker.run()

    assert results == [False]


def test_apply_to_server_defaults_backup_destination_when_unset(tmp_path):
    server = ServerConfig(id="s1", name="New Server")  # backup_destination left blank
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    install_dir = str(tmp_path / "s1" / "server")
    wizard.paths_page.install_edit.setText(install_dir)
    wizard.paths_page.steamcmd_edit.setText(str(tmp_path / "shared-steamcmd"))

    wizard.apply_to_server()

    assert server.backup_destination == str(tmp_path / "s1" / "backups")


def test_apply_to_server_does_not_override_an_existing_backup_destination(tmp_path):
    server = ServerConfig(id="s1", name="Existing", backup_destination="D:\\MyOwnBackups")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    wizard.paths_page.install_edit.setText(str(tmp_path / "s1" / "server"))
    wizard.paths_page.steamcmd_edit.setText(str(tmp_path / "shared-steamcmd"))

    wizard.apply_to_server()

    assert server.backup_destination == "D:\\MyOwnBackups"


# ------------------------------------------------------------------ reject --

def _wizard_with_cleanup_spy():
    calls = []
    server = ServerConfig(id="s1", name="Chudville")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
        start_network_cleanup=lambda sid, ports, ip: calls.append((sid, list(ports), ip)),
    )
    return wizard, calls


def test_reject_cleans_up_networking_if_networking_step_ran():
    """The Networking step adds real firewall rules and router forwards
    the moment it runs -- cancelling must not leave those behind."""
    wizard, calls = _wizard_with_cleanup_spy()
    page = wizard.network_page
    page.touched = True
    page.detected_game_port = 7777
    page.detected_query_port = 27015
    page.detected_ip = "192.168.1.50"

    wizard.reject()

    assert calls == [("s1", [7777, 7778, 27015], "192.168.1.50")]


def test_reject_skips_cleanup_if_networking_step_never_ran():
    wizard, calls = _wizard_with_cleanup_spy()
    assert wizard.network_page.touched is False
    wizard.reject()
    assert calls == []


def test_reject_while_networking_still_running_cleans_up_once_it_finishes():
    """Cancelling mid-run used to skip cleanup entirely (the step hadn't
    'finished' yet), leaking whatever it went on to add."""
    wizard, calls = _wizard_with_cleanup_spy()
    page = wizard.network_page
    page.touched = True
    page._worker = object()  # pretend a run is in flight

    wizard.reject()
    assert calls == []  # not yet -- it would race the in-flight run

    page._on_setup_finished({"error": None, "detected_ip": "192.168.1.50", "game_port": 7779,
                             "query_port": 27016, "fw_results": [], "upnp": {}, "public_ip": None})
    assert calls == [("s1", [7779, 7780, 27016], "192.168.1.50")]


def test_network_page_error_is_shown_and_blocks_finish():
    page = _make_network_page()
    page._on_setup_finished({"error": "No network adapter found"})
    assert "No network adapter found" in page.status_label.text()
    assert page.isComplete() is False


def test_network_page_reports_double_nat():
    page = _make_network_page()
    page._on_setup_finished(_base_result(double_nat=True))
    assert "double NAT" in page.status_label.text()


def test_network_page_shows_plain_words_not_python_booleans():
    page = _make_network_page()
    page._on_setup_finished(_base_result())
    assert "True" not in page.status_label.text()


# ------------------------------------------------------------------ install --

def test_failed_install_does_not_unlock_continue():
    from ui.setup_wizard import InstallPage
    page = InstallPage()
    page._on_finished(False)
    assert page.isComplete() is False
    assert page.retry_btn.isHidden() is False
    page._on_finished(True)
    assert page.isComplete() is True


# -------------------------------------------------------------------- paths --

def test_paths_page_blocks_names_differing_only_by_case_or_spacing():
    server = ServerConfig(id="s2", name="Second")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
        get_reserved_names=lambda: {"Chudville"},
    )
    page = wizard.paths_page
    page.name_edit.setText("  chudville ")
    assert page.validatePage() is False


def test_paths_page_writes_back_cleaned_values(tmp_path):
    server = ServerConfig(id="s1", name="Test")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    page = wizard.paths_page
    page.name_edit.setText("  My   Server ")
    page.steamcmd_edit.setText(str(tmp_path / "steamcmd") + "  ")
    page.install_edit.setText(str(tmp_path / "server") + " ")
    assert page.validatePage() is True
    assert page.name_edit.text() == "My Server"
    assert page.install_edit.text() == str(tmp_path / "server")
    assert page.steamcmd_edit.text() == str(tmp_path / "steamcmd")


def test_paths_page_blocks_install_folder_inside_steamcmd_folder(tmp_path):
    server = ServerConfig(id="s1", name="Test")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    page = wizard.paths_page
    page.steamcmd_edit.setText(str(tmp_path / "steamcmd"))
    page.install_edit.setText(str(tmp_path / "steamcmd" / "server"))
    assert page.validatePage() is False


def test_paths_page_blocks_install_folder_nested_in_another_servers(tmp_path):
    server = ServerConfig(id="s1", name="Test")
    other = str(tmp_path / "other-server")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: {os.path.normcase(os.path.abspath(other))}, get_default_steamcmd_dir=lambda: "",
    )
    page = wizard.paths_page
    page.steamcmd_edit.setText(str(tmp_path / "steamcmd"))
    page.install_edit.setText(os.path.join(other, "nested"))
    assert page.validatePage() is False


# ------------------------------------------------------ port forwarding guide --

def _make_network_page():
    server = ServerConfig(id="s1", name="Chudville")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    return wizard.network_page


def _base_result(**overrides):
    result = {
        "detected_ip": "192.168.1.50",
        "game_port": 7777,
        "query_port": 27015,
        "fw_results": [],
        "upnp": {
            "upnp_available": True,
            "game_port_forwarded": True,
            "game_port_plus_one_forwarded": True,
            "query_port_forwarded": True,
        },
        "public_ip": None,
        "router_ip": "192.168.1.1",
        "error": None,
    }
    result.update(overrides)
    return result


def test_guide_button_hidden_when_upnp_fully_succeeds():
    page = _make_network_page()
    page._on_setup_finished(_base_result())
    assert page.guide_btn.isHidden() is True


def test_guide_button_shown_when_upnp_unavailable():
    page = _make_network_page()
    page._on_setup_finished(_base_result(
        upnp={"upnp_available": False, "game_port_forwarded": False,
              "game_port_plus_one_forwarded": False, "query_port_forwarded": False},
        public_ip="203.0.113.9",
    ))
    assert page.guide_btn.isHidden() is False
    assert page._last_public_ip == "203.0.113.9"


def test_guide_button_shown_when_upnp_partially_fails():
    """Even with UPnP available, if not every port got forwarded, a
    manual step is still likely needed for whichever one(s) didn't."""
    page = _make_network_page()
    page._on_setup_finished(_base_result(
        upnp={"upnp_available": True, "game_port_forwarded": True,
              "game_port_plus_one_forwarded": False, "query_port_forwarded": True},
    ))
    assert page.guide_btn.isHidden() is False


def test_open_guide_uses_detected_values(monkeypatch):
    page = _make_network_page()
    page._on_setup_finished(_base_result(
        upnp={"upnp_available": False, "game_port_forwarded": False,
              "game_port_plus_one_forwarded": False, "query_port_forwarded": False},
        public_ip="203.0.113.9",
    ))
    import network_utils
    monkeypatch.setattr(network_utils, "get_default_gateway", lambda ip: "192.168.1.1")

    captured = {}

    class _FakeDialog:
        def __init__(self, game_port, query_port, local_ip, router_ip, public_ip, parent=None):
            captured.update(game_port=game_port, query_port=query_port, local_ip=local_ip,
                             router_ip=router_ip, public_ip=public_ip)

        def exec(self):
            return 1

    import ui.port_forwarding_guide_dialog as guide_mod
    monkeypatch.setattr(guide_mod, "PortForwardingGuideDialog", _FakeDialog)

    page._open_guide()

    assert captured == {
        "game_port": 7777, "query_port": 27015, "local_ip": "192.168.1.50",
        "router_ip": "192.168.1.1", "public_ip": "203.0.113.9",
    }


def test_paths_page_blocks_install_on_a_nearly_full_drive(tmp_path, monkeypatch):
    import ui.setup_wizard as sw
    monkeypatch.setattr(sw, "_free_bytes", lambda path: 2 * 1024 ** 3)
    server = ServerConfig(id="s1", name="Test")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    page = wizard.paths_page
    page.steamcmd_edit.setText(str(tmp_path / "steamcmd"))
    page.install_edit.setText(str(tmp_path / "server"))
    assert page.validatePage() is False


def test_paths_page_asks_when_disk_space_is_merely_low(tmp_path, monkeypatch):
    import ui.setup_wizard as sw
    monkeypatch.setattr(sw, "_free_bytes", lambda path: 25 * 1024 ** 3)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: (asked.append(1), QMessageBox.No)[1]))
    server = ServerConfig(id="s1", name="Test")
    wizard = SetupWizard(
        server, get_reserved_ports=lambda: set(),
        get_reserved_dirs=lambda: set(), get_default_steamcmd_dir=lambda: "",
    )
    page = wizard.paths_page
    page.steamcmd_edit.setText(str(tmp_path / "steamcmd"))
    page.install_edit.setText(str(tmp_path / "server"))
    assert page.validatePage() is False
    assert asked == [1]
