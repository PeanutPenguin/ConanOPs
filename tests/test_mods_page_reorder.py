from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QAbstractItemView

import models
from ui.mods_page import ModsPage


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _make_page_with_mods():
    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville")
    server.mods = [
        {"id": "1", "name": "First", "enabled": True},
        {"id": "2", "name": "Second", "enabled": True},
        {"id": "3", "name": "Third", "enabled": True},
    ]
    page.set_server(server)
    return page, server


def test_list_widget_uses_internal_move_only():
    """No dropping onto other widgets, no dragging items out -- purely
    reordering within this one list, which for a single-column vertical
    list is inherently a Y-axis-only operation."""
    page = ModsPage()
    assert page.list_widget.dragDropMode() == QAbstractItemView.InternalMove


def test_dragging_a_row_updates_server_mods_order(monkeypatch):
    page, server = _make_page_with_mods()
    changed = []
    page.on_changed = lambda srv: changed.append(list(srv.mods))

    # Simulate what Qt's InternalMove machinery does: it moves the
    # widget's own rows first, THEN emits rowsMoved -- so re-order the
    # widget items directly (same effect a real drag would have) and
    # fire the handler that responds to that signal.
    item = page.list_widget.takeItem(2)  # "Third"
    page.list_widget.insertItem(0, item)

    page._on_rows_moved()
    # Persisting is deferred out of the drop handler (see
    # _on_rows_moved) -- server.mods is updated immediately, the
    # save + list rebuild happens on the next event-loop pass.
    assert [m["id"] for m in server.mods] == ["3", "1", "2"]
    QApplication.processEvents()

    assert changed and [m["id"] for m in changed[-1]] == ["3", "1", "2"]


def test_rows_moved_with_no_actual_change_does_not_persist(monkeypatch):
    page, server = _make_page_with_mods()
    changed = []
    page.on_changed = lambda srv: changed.append(1)

    page._on_rows_moved()  # nothing was actually reordered

    assert changed == []


def test_rows_moved_does_nothing_with_no_server():
    page = ModsPage()
    page._on_rows_moved()  # must not raise
