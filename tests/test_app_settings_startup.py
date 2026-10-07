from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox

import models
import startup_registration
from ui.app_settings_page import AppSettingsPage


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


class _FakeWebControlServer:
    is_running = False


def _make_page(config=None):
    config = config or models.AppConfig()
    return AppSettingsPage(
        config, save_config=lambda: None,
        on_theme_changed=lambda p: None, on_lock_changed=lambda: None,
        install_dir="/tmp/fake-install", on_update_installed=lambda: None,
        web_control_server=_FakeWebControlServer(),
    )


def test_checkboxes_reflect_config_on_load():
    config = models.AppConfig(start_with_windows=True, start_minimized_to_tray=True)
    page = _make_page(config)
    assert page.start_with_windows_checkbox.isChecked() is True
    assert page.start_minimized_checkbox.isChecked() is True


def test_checking_start_with_windows_registers_and_saves(monkeypatch):
    config = models.AppConfig()
    saved = []
    page = AppSettingsPage(
        config, save_config=lambda: saved.append(True),
        on_theme_changed=lambda p: None, on_lock_changed=lambda: None,
        install_dir="/tmp/fake-install", on_update_installed=lambda: None,
        web_control_server=_FakeWebControlServer(),
    )
    registered = []
    monkeypatch.setattr(startup_registration, "register", lambda: registered.append(True))

    page.start_with_windows_checkbox.setChecked(True)

    assert registered == [True]
    assert config.start_with_windows is True
    assert saved == [True]


def test_unchecking_start_with_windows_unregisters_and_saves(monkeypatch):
    config = models.AppConfig(start_with_windows=True)
    saved = []
    page = AppSettingsPage(
        config, save_config=lambda: saved.append(True),
        on_theme_changed=lambda p: None, on_lock_changed=lambda: None,
        install_dir="/tmp/fake-install", on_update_installed=lambda: None,
        web_control_server=_FakeWebControlServer(),
    )
    unregistered = []
    monkeypatch.setattr(startup_registration, "unregister", lambda: unregistered.append(True))

    page.start_with_windows_checkbox.setChecked(False)

    assert unregistered == [True]
    assert config.start_with_windows is False
    assert saved == [True]


def test_registration_failure_reverts_checkbox_and_config(monkeypatch):
    config = models.AppConfig()
    page = _make_page(config)
    monkeypatch.setattr(startup_registration, "register", lambda: (_ for _ in ()).throw(OSError("access denied")))
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: None))

    page.start_with_windows_checkbox.setChecked(True)

    assert page.start_with_windows_checkbox.isChecked() is False
    assert config.start_with_windows is False


def test_start_minimized_toggle_saves_independently(monkeypatch):
    config = models.AppConfig()
    saved = []
    page = AppSettingsPage(
        config, save_config=lambda: saved.append(True),
        on_theme_changed=lambda p: None, on_lock_changed=lambda: None,
        install_dir="/tmp/fake-install", on_update_installed=lambda: None,
        web_control_server=_FakeWebControlServer(),
    )

    page.start_minimized_checkbox.setChecked(True)

    assert config.start_minimized_to_tray is True
    assert saved == [True]


def test_steam_api_key_loads_from_config():
    config = models.AppConfig(steam_api_key="ABCDEF123")
    page = _make_page(config)
    assert page.steam_api_key_edit.text() == "ABCDEF123"


def test_editing_steam_api_key_saves_it():
    config = models.AppConfig()
    saved = []
    page = AppSettingsPage(
        config, save_config=lambda: saved.append(True),
        on_theme_changed=lambda p: None, on_lock_changed=lambda: None,
        install_dir="/tmp/fake-install", on_update_installed=lambda: None,
        web_control_server=_FakeWebControlServer(),
    )

    page.steam_api_key_edit.setText("  NEWKEY123  ")
    page.steam_api_key_edit.editingFinished.emit()

    assert config.steam_api_key == "NEWKEY123"  # trimmed
    assert saved == [True]


def test_duckdns_fields_load_from_config():
    config = models.AppConfig(duckdns_domain="myserver", duckdns_token="TOKEN123")
    page = _make_page(config)
    assert page.duckdns_domain_edit.text() == "myserver"
    assert page.duckdns_token_edit.text() == "TOKEN123"


def test_editing_duckdns_fields_saves_them():
    config = models.AppConfig()
    saved = []
    page = AppSettingsPage(
        config, save_config=lambda: saved.append(True),
        on_theme_changed=lambda p: None, on_lock_changed=lambda: None,
        install_dir="/tmp/fake-install", on_update_installed=lambda: None,
        web_control_server=_FakeWebControlServer(),
    )

    page.duckdns_domain_edit.setText("  myserver  ")
    page.duckdns_domain_edit.editingFinished.emit()
    page.duckdns_token_edit.setText("  TOKEN123  ")
    page.duckdns_token_edit.editingFinished.emit()

    assert config.duckdns_domain == "myserver"
    assert config.duckdns_token == "TOKEN123"
    assert saved == [True, True]
