from __future__ import annotations

import os
import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import mod_manager
import models
from ui.mods_page import ModsPage


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _make_content_dir(steamcmd_dir, workshop_id):
    content_dir = os.path.join(steamcmd_dir, "steamapps", "workshop", "content", str(mod_manager.WORKSHOP_APP_ID), workshop_id)
    os.makedirs(content_dir, exist_ok=True)
    return content_dir


def test_downloaded_mod_shows_no_extra_note(tmp_path):
    steamcmd_dir = str(tmp_path)
    content_dir = _make_content_dir(steamcmd_dir, "1")
    open(os.path.join(content_dir, "RealMod.pak"), "w").close()

    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville", steamcmd_dir=steamcmd_dir)
    server.mods = [{"id": "1", "name": "RealMod", "enabled": True}]
    page.set_server(server)

    label = page.list_widget.item(0).text()
    assert "not downloaded" not in label
    assert "RealMod" in label


def test_undownloaded_mod_shows_not_downloaded_note(tmp_path):
    steamcmd_dir = str(tmp_path)  # nothing downloaded at all

    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville", steamcmd_dir=steamcmd_dir)
    server.mods = [{"id": "2", "name": "MissingMod", "enabled": True}]
    page.set_server(server)

    label = page.list_widget.item(0).text()
    assert "not downloaded" in label


def test_no_steamcmd_dir_shows_no_download_status_at_all(tmp_path):
    """Can't check download status without knowing where SteamCMD's
    workshop content even lives -- rather than guess wrong, this
    shows nothing extra, distinct from a confirmed "not downloaded.\""""
    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville", steamcmd_dir="")
    server.mods = [{"id": "3", "name": "SomeMod", "enabled": True}]
    page.set_server(server)

    label = page.list_widget.item(0).text()
    assert "not downloaded" not in label


def test_mixed_downloaded_and_not(tmp_path):
    steamcmd_dir = str(tmp_path)
    content_dir = _make_content_dir(steamcmd_dir, "1")
    open(os.path.join(content_dir, "Present.pak"), "w").close()

    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville", steamcmd_dir=steamcmd_dir)
    server.mods = [
        {"id": "1", "name": "Present", "enabled": True},
        {"id": "2", "name": "Absent", "enabled": True},
    ]
    page.set_server(server)

    labels = [page.list_widget.item(i).text() for i in range(page.list_widget.count())]
    present_label = next(l for l in labels if "Present" in l)
    absent_label = next(l for l in labels if "Absent" in l)
    assert "not downloaded" not in present_label
    assert "not downloaded" in absent_label
