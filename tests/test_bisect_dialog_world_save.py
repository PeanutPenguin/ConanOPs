from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox

import mod_manager
import models
import process_manager
from ui.mods_page import BisectDialog


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def answer_prompts(monkeypatch):
    """Restoring now ASKS before resetting the world (it also undoes
    real play since the bisect started) -- answer Yes by default so
    these tests exercise the reset, and never block on a real dialog.
    Returns a dict the test can change to answer No."""
    answers = {"question": QMessageBox.Yes, "asked": []}

    def fake_question(*a, **k):
        answers["asked"].append(a[1] if len(a) > 1 else "")
        return answers["question"]
    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: answers.setdefault("warned", True)))
    return answers


def _server_with_mods(install_dir):
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(install_dir))
    server.mods = [
        {"id": "a", "name": "a", "enabled": True},
        {"id": "b", "name": "b", "enabled": True},
        {"id": "c", "name": "c", "enabled": True},
    ]
    return server


def _make_saved_dir(install_dir, content="original"):
    saved_dir = install_dir / "ConanSandbox" / "Saved"
    saved_dir.mkdir(parents=True)
    (saved_dir / "game.db").write_text(content)
    return saved_dir


def test_takes_a_snapshot_when_server_is_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)
    _make_saved_dir(tmp_path)
    server = _server_with_mods(tmp_path)

    dlg = BisectDialog(server, on_changed=None)

    assert dlg._saved_snapshot_dir is not None
    mod_manager.cleanup_world_save_snapshot(dlg._saved_snapshot_dir)


def test_skips_snapshot_when_server_is_running(monkeypatch, tmp_path):
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: True)
    _make_saved_dir(tmp_path)
    server = _server_with_mods(tmp_path)

    dlg = BisectDialog(server, on_changed=None)

    assert dlg._saved_snapshot_dir is None


def test_restore_button_resets_the_world_save(monkeypatch, tmp_path):
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)
    saved_dir = _make_saved_dir(tmp_path, "original world")
    server = _server_with_mods(tmp_path)
    dlg = BisectDialog(server, on_changed=None)

    (saved_dir / "game.db").write_text("mutated by a manual test round")
    dlg._restore_and_close(already_closing=True)

    assert (saved_dir / "game.db").read_text() == "original world"
    assert dlg._saved_snapshot_dir is None  # cleaned up


def test_close_before_conclusion_restores_the_world_save(monkeypatch, tmp_path):
    """Closing via the X button while unresolved must behave exactly
    like clicking Restore -- including putting the world back."""
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)
    saved_dir = _make_saved_dir(tmp_path, "original world")
    server = _server_with_mods(tmp_path)
    dlg = BisectDialog(server, on_changed=None)
    (saved_dir / "game.db").write_text("mutated")

    from PySide6.QtGui import QCloseEvent
    dlg.closeEvent(QCloseEvent())

    assert (saved_dir / "game.db").read_text() == "original world"


def test_a_concluded_bisect_does_not_reset_the_world_save(monkeypatch, tmp_path):
    """A real result (culprit found, or ruled out) is left as-is --
    the world-save reset is only for an ABANDONED run, never a
    completed one."""
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)
    saved_dir = _make_saved_dir(tmp_path, "original world")
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(tmp_path))
    server.mods = [{"id": "only", "name": "only", "enabled": True}]
    dlg = BisectDialog(server, on_changed=None)

    (saved_dir / "game.db").write_text("state after the person's own manual restart")
    dlg._report(still_broken=False)  # single-candidate bisect resolves immediately -- "fixed"

    assert dlg.state.done is True
    assert (saved_dir / "game.db").read_text() == "state after the person's own manual restart"
    assert dlg._saved_snapshot_dir is None  # cleaned up, not held onto forever


def test_close_after_conclusion_does_not_reset_the_world_save(monkeypatch, tmp_path):
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)
    saved_dir = _make_saved_dir(tmp_path, "original world")
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(tmp_path))
    server.mods = [{"id": "only", "name": "only", "enabled": True}]
    dlg = BisectDialog(server, on_changed=None)
    dlg._report(still_broken=False)
    (saved_dir / "game.db").write_text("state after the conclusion")

    from PySide6.QtGui import QCloseEvent
    dlg.closeEvent(QCloseEvent())

    assert (saved_dir / "game.db").read_text() == "state after the conclusion"
