from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import models
from ui.mods_page import ModsPage
from ui.workshop_browser_dialog import WorkshopBrowserDialog


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def test_open_workshop_browser_passes_the_api_key_through(monkeypatch):
    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville")
    page.server = server
    page.get_api_key = lambda: "MYKEY123"

    captured = {}

    class _FakeDialog:
        def __init__(self, server, api_key, on_changed=None, parent=None, **kwargs):
            captured["server"] = server
            captured["api_key"] = api_key

        def exec(self):
            return 1

    monkeypatch.setattr("ui.mods_page.WorkshopBrowserDialog", _FakeDialog)

    page._open_workshop_browser()

    assert captured["server"] is server
    assert captured["api_key"] == "MYKEY123"


def test_open_workshop_browser_does_nothing_with_no_server(monkeypatch):
    page = ModsPage()
    page.get_api_key = lambda: "MYKEY123"
    constructed = []
    monkeypatch.setattr("ui.mods_page.WorkshopBrowserDialog", lambda *a, **k: constructed.append(1))

    page._open_workshop_browser()

    assert constructed == []


def test_open_workshop_browser_passes_empty_key_when_unset(monkeypatch):
    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville")
    page.server = server
    page.get_api_key = None

    captured = {}

    class _FakeDialog:
        def __init__(self, server, api_key, on_changed=None, parent=None, **kwargs):
            captured["api_key"] = api_key

        def exec(self):
            return 1

    monkeypatch.setattr("ui.mods_page.WorkshopBrowserDialog", _FakeDialog)

    page._open_workshop_browser()

    assert captured["api_key"] == ""


# ------------------------------------------------------------- auto bisect --

def test_open_auto_bisect_refuses_when_players_online(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville", install_dir="/tmp/fake")
    server.mods = [{"id": "1", "name": "A", "enabled": True}]
    page.server = server
    page.is_server_online = lambda: True
    constructed = []
    monkeypatch.setattr("ui.mods_page.AutoBisectDialog", lambda *a, **k: constructed.append(1))
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(1)))

    page._open_auto_bisect()

    assert constructed == []
    assert warned == [1]


def test_open_auto_bisect_proceeds_when_nobody_online(monkeypatch):
    import process_manager

    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville", install_dir="/tmp/fake")
    server.mods = [{"id": "1", "name": "A", "enabled": True}]
    page.server = server
    page.is_server_online = lambda: False
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)

    captured = {}

    class _FakeDialog:
        def __init__(self, server, mods, was_running, find_all=False, on_changed=None, is_online=None, restart_server=None, parent=None):
            captured["was_running"] = was_running

        def exec(self):
            return 1

    monkeypatch.setattr("ui.mods_page.AutoBisectDialog", _FakeDialog)

    page._open_auto_bisect()

    assert captured["was_running"] is False


def test_open_auto_bisect_requires_mods(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville", install_dir="/tmp/fake")
    server.mods = []
    page.server = server
    constructed = []
    monkeypatch.setattr("ui.mods_page.AutoBisectDialog", lambda *a, **k: constructed.append(1))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))

    page._open_auto_bisect()

    assert constructed == []


def test_open_auto_bisect_requires_install_dir(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville", install_dir="")
    server.mods = [{"id": "1", "name": "A", "enabled": True}]
    page.server = server
    constructed = []
    monkeypatch.setattr("ui.mods_page.AutoBisectDialog", lambda *a, **k: constructed.append(1))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    page._open_auto_bisect()

    assert constructed == []
