from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from ui.remove_server_dialog import RemoveServerDialog


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def test_no_install_dir_means_no_files_checkbox_at_all():
    dialog = RemoveServerDialog("Chudville", "", "", is_running=False)
    assert dialog.delete_files_cb is None
    assert dialog.delete_files is False


def test_no_backup_destination_means_no_backups_checkbox_at_all():
    dialog = RemoveServerDialog("Chudville", "/some/install", "", is_running=False)
    assert dialog.delete_backups_cb is None
    assert dialog.delete_backups is False


def test_both_checkboxes_default_checked():
    """Removing a server leaves nothing behind unless someone deliberately
    unticks a box to keep its files or backups."""
    dialog = RemoveServerDialog("Chudville", "/some/install", "/some/backups", is_running=False)
    assert dialog.delete_files_cb is not None
    assert dialog.delete_backups_cb is not None
    assert dialog.delete_files is True
    assert dialog.delete_backups is True


def test_checking_boxes_is_reflected_in_properties():
    dialog = RemoveServerDialog("Chudville", "/some/install", "/some/backups", is_running=False)
    dialog.delete_files_cb.setChecked(True)
    dialog.delete_backups_cb.setChecked(True)
    assert dialog.delete_files is True
    assert dialog.delete_backups is True


def test_unticking_backups_keeps_them():
    dialog = RemoveServerDialog("Chudville", "/some/install", "/some/backups", is_running=False)
    dialog.delete_backups_cb.setChecked(False)
    assert dialog.delete_files is True
    assert dialog.delete_backups is False
