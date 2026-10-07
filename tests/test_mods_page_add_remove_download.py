from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox

import models
from ui.mods_page import ModsPage
from update_runner import ModDownloadWorker


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _page_with_server(**server_kwargs):
    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville", **server_kwargs)
    page.set_server(server)
    return page, server


# ------------------------------------------------------------ _extract_workshop_id --

def test_extract_workshop_id_accepts_a_bare_number():
    assert ModsPage._extract_workshop_id("1234567890") == "1234567890"


def test_extract_workshop_id_accepts_a_workshop_url():
    url = "https://steamcommunity.com/sharedfiles/filedetails/?id=1234567890"
    assert ModsPage._extract_workshop_id(url) == "1234567890"


def test_extract_workshop_id_accepts_a_url_with_extra_query_params():
    url = "https://steamcommunity.com/sharedfiles/filedetails/?id=42&searchtext=pippi"
    assert ModsPage._extract_workshop_id(url) == "42"


def test_extract_workshop_id_rejects_a_mod_name():
    assert ModsPage._extract_workshop_id("Pippi") is None


def test_extract_workshop_id_rejects_garbage():
    assert ModsPage._extract_workshop_id("not-a-real-id-at-all") is None


def test_extract_workshop_id_rejects_empty():
    assert ModsPage._extract_workshop_id("") is None


# --------------------------------------------------------------------- _add_mod --

def test_add_mod_with_valid_id_adds_it():
    page, server = _page_with_server()
    page.id_edit.setText("1234567890")
    page.name_edit.setText("Pippi")

    page._add_mod()

    assert any(m["id"] == "1234567890" for m in server.mods)
    assert page.id_edit.text() == ""  # cleared after a successful add


def test_add_mod_extracts_id_from_a_pasted_url():
    page, server = _page_with_server()
    page.id_edit.setText("https://steamcommunity.com/sharedfiles/filedetails/?id=999")

    page._add_mod()

    assert any(m["id"] == "999" for m in server.mods)


def test_add_mod_rejects_invalid_input_and_warns(monkeypatch):
    page, server = _page_with_server()
    page.id_edit.setText("Pippi")
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warned.append(1)))

    page._add_mod()

    assert server.mods == []
    assert warned == [1]
    assert page.id_edit.text() == "Pippi"  # left alone so they can fix it


# --------------------------------------------------------------- _remove_selected --

def test_remove_selected_asks_for_confirmation(monkeypatch):
    page, server = _page_with_server()
    server.mods = [{"id": "1", "name": "Pippi", "enabled": True}]
    page._refresh()
    page.list_widget.setCurrentRow(0)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: asked.append(a) or QMessageBox.Cancel))

    page._remove_selected()

    assert len(asked) == 1
    assert server.mods == [{"id": "1", "name": "Pippi", "enabled": True}]  # declined -- nothing removed


def test_remove_selected_removes_when_confirmed(monkeypatch):
    page, server = _page_with_server()
    server.mods = [{"id": "1", "name": "Pippi", "enabled": True}]
    page._refresh()
    page.list_widget.setCurrentRow(0)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))

    page._remove_selected()

    assert server.mods == []


def test_remove_selected_confirmation_mentions_the_mod_name(monkeypatch):
    page, server = _page_with_server()
    server.mods = [{"id": "1", "name": "Pippi", "enabled": True}]
    page._refresh()
    page.list_widget.setCurrentRow(0)
    captured = {}

    def fake_question(self_or_parent, title, text, *a, **k):
        captured["text"] = text
        return QMessageBox.Cancel
    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))

    page._remove_selected()

    assert "Pippi" in captured["text"]


# ---------------------------------------------------------------- _download_mods --

def test_download_mods_downloads_disabled_mods_too(monkeypatch):
    page, server = _page_with_server(steamcmd_dir="/tmp/fake-steamcmd")
    server.mods = [
        {"id": "1", "name": "On", "enabled": True},
        {"id": "2", "name": "Off", "enabled": False},
    ]
    captured = {}
    real_init = ModDownloadWorker.__init__

    def capturing_init(self, steamcmd_dir, workshop_ids):
        captured["ids"] = workshop_ids
        real_init(self, steamcmd_dir, workshop_ids)
    monkeypatch.setattr(ModDownloadWorker, "__init__", capturing_init)
    monkeypatch.setattr(ModDownloadWorker, "start", lambda self: None)

    page._download_mods()

    assert set(captured["ids"]) == {"1", "2"}


def test_download_mods_refreshes_the_list_when_finished(monkeypatch):
    page, server = _page_with_server(steamcmd_dir="/tmp/fake-steamcmd")
    server.mods = [{"id": "1", "name": "A", "enabled": True}]
    monkeypatch.setattr(ModDownloadWorker, "start", lambda self: None)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    page._download_mods()
    refreshed = []
    real_refresh = page._refresh
    monkeypatch.setattr(page, "_refresh", lambda: (refreshed.append(1), real_refresh()))

    import steamcmd
    page._on_download_finished(server, steamcmd.UpdateResult(True, "ok"))

    assert refreshed == [1]


def test_download_button_resyncs_after_switching_servers_and_back(monkeypatch):
    """The actual stuck-button bug: finishing while a DIFFERENT server
    is showing used to leave the button stuck on "Downloading…"
    forever once you switched back."""
    page, server = _page_with_server(steamcmd_dir="/tmp/fake-steamcmd")
    server.mods = [{"id": "1", "name": "A", "enabled": True}]
    other_server = models.ServerConfig(id="s2", name="Other")
    monkeypatch.setattr(ModDownloadWorker, "start", lambda self: None)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))

    page._download_mods()
    assert page.download_btn.isEnabled() is False

    page.set_server(other_server)  # switch away mid-download
    # Only one manual download runs at a time -- the button says so
    # instead of staying clickable and silently doing nothing.
    assert page.download_btn.isEnabled() is False
    assert "another server" in page.download_btn.text()

    import steamcmd
    page._on_download_finished(server, steamcmd.UpdateResult(True, "ok"))  # finishes while looking at the OTHER server

    page.set_server(server)  # switch back
    assert page.download_btn.isEnabled() is True
    assert page.download_btn.text() == "Download Mods"


def test_download_mods_respects_a_claimed_slot(monkeypatch):
    page, server = _page_with_server(steamcmd_dir="/tmp/fake-steamcmd")
    server.mods = [{"id": "1", "name": "A", "enabled": True}]
    page.claim_mod_refresh_slot = lambda sid, worker: False  # already busy elsewhere
    started = []
    monkeypatch.setattr(ModDownloadWorker, "start", lambda self: started.append(1))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))

    page._download_mods()

    assert started == []
    assert page._download_worker is None


def test_download_mods_releases_slot_when_finished(monkeypatch):
    page, server = _page_with_server(steamcmd_dir="/tmp/fake-steamcmd")
    server.mods = [{"id": "1", "name": "A", "enabled": True}]
    released = []
    page.claim_mod_refresh_slot = lambda sid, worker: True
    page.release_mod_refresh_slot = lambda sid: released.append(sid)
    monkeypatch.setattr(ModDownloadWorker, "start", lambda self: None)
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    page._download_mods()

    import steamcmd
    page._on_download_finished(server, steamcmd.UpdateResult(True, "ok"))

    assert released == ["s1"]
