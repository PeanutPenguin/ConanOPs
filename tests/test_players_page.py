from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox, QInputDialog

import models
from session_tracker import SessionTracker
from ui.players_page import PlayersPage


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _make_page(tmp_path, online=None):
    page = PlayersPage()
    tracker = SessionTracker(str(tmp_path / "sessions.json"))
    tracker.player_joined("Conan the Barbarian")
    tracker.player_left("Conan the Barbarian")
    page.set_tracker(tracker)
    server = models.ServerConfig(id="s1", name="Chudville", rcon_enabled=True)
    page.set_server(server)
    if online:
        page.set_online_players(set(online))
    return page, server


def test_kick_button_disabled_when_offline(tmp_path):
    from PySide6.QtWidgets import QPushButton
    page, server = _make_page(tmp_path, online=set())
    buttons = page.table.cellWidget(0, 4).findChildren(QPushButton)
    kick = next(b for b in buttons if b.text() == "Kick")
    assert kick.isEnabled() is False


def test_kick_button_enabled_when_online(tmp_path):
    from PySide6.QtWidgets import QPushButton
    page, server = _make_page(tmp_path, online={"Conan the Barbarian"})
    buttons = page.table.cellWidget(0, 4).findChildren(QPushButton)
    kick = next(b for b in buttons if b.text() == "Kick")
    assert kick.isEnabled() is True


def test_kick_calls_on_kick_after_confirmation(tmp_path, monkeypatch):
    page, server = _make_page(tmp_path, online={"Conan the Barbarian"})
    kicked = []
    page.on_kick = lambda srv, name: kicked.append((srv, name))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))

    page._handle_kick("Conan the Barbarian")

    assert kicked == [(server, "Conan the Barbarian")]


def test_kick_does_nothing_if_confirmation_declined(tmp_path, monkeypatch):
    page, server = _make_page(tmp_path, online={"Conan the Barbarian"})
    kicked = []
    page.on_kick = lambda srv, name: kicked.append((srv, name))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Cancel))

    page._handle_kick("Conan the Barbarian")

    assert kicked == []


def test_kick_warns_instead_of_calling_on_kick_when_rcon_disabled(tmp_path, monkeypatch):
    page, server = _make_page(tmp_path, online={"Conan the Barbarian"})
    server.rcon_enabled = False
    kicked = []
    page.on_kick = lambda srv, name: kicked.append((srv, name))
    info_shown = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: info_shown.append(a)))

    page._handle_kick("Conan the Barbarian")

    assert kicked == []
    assert len(info_shown) == 1


def test_ban_button_available_even_when_offline(tmp_path):
    from PySide6.QtWidgets import QPushButton
    page, server = _make_page(tmp_path, online=set())
    buttons = page.table.cellWidget(0, 4).findChildren(QPushButton)
    ban = next(b for b in buttons if b.text() == "Ban…")
    assert ban.isEnabled() is True


def test_ban_prompts_for_steam_id_and_calls_on_ban(tmp_path, monkeypatch):
    page, server = _make_page(tmp_path)
    banned = []
    page.on_ban = lambda srv, name, sid: banned.append((srv, name, sid))
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("76561198000000000", True)))

    page._handle_ban("Conan the Barbarian")

    assert banned == [(server, "Conan the Barbarian", "76561198000000000")]


def test_ban_does_nothing_if_dialog_cancelled(tmp_path, monkeypatch):
    page, server = _make_page(tmp_path)
    banned = []
    page.on_ban = lambda srv, name, sid: banned.append((srv, name, sid))
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))

    page._handle_ban("Conan the Barbarian")

    assert banned == []
