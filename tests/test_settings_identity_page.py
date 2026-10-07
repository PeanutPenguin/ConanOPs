from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox

import proc_utils
from ui.settings_identity_page import SettingsIdentityPage


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def test_open_server_folder_calls_open_in_explorer_with_install_dir(monkeypatch, tmp_path):
    page = SettingsIdentityPage()
    page.set_install_dir(str(tmp_path))
    calls = []
    monkeypatch.setattr(proc_utils, "open_in_explorer", lambda p: calls.append(p))

    page._open_server_folder()

    assert calls == [str(tmp_path)]


def test_open_server_folder_with_no_install_dir_shows_info_instead_of_calling(monkeypatch, tmp_path):
    page = SettingsIdentityPage()
    # set_install_dir() never called -- same state as a server with no
    # install_dir configured yet.
    calls = []
    monkeypatch.setattr(proc_utils, "open_in_explorer", lambda p: calls.append(p))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

    page._open_server_folder()

    assert calls == []


def test_open_server_folder_missing_dir_shows_warning_not_an_unhandled_exception(monkeypatch, tmp_path):
    page = SettingsIdentityPage()
    page.set_install_dir(str(tmp_path / "does-not-exist"))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))

    page._open_server_folder()  # should not raise

    assert len(warnings) == 1


def test_switching_active_server_updates_install_dir(tmp_path):
    """MainWindow._load_active_server() calls set_install_dir() every
    time the active server changes -- confirms the page actually picks
    up the new value rather than keeping the first server's forever."""
    page = SettingsIdentityPage()
    page.set_install_dir(str(tmp_path / "server-a"))
    assert page._install_dir == str(tmp_path / "server-a")

    page.set_install_dir(str(tmp_path / "server-b"))
    assert page._install_dir == str(tmp_path / "server-b")
