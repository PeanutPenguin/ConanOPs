from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import models
import steam_workshop_api as swa
from ui.workshop_browser_dialog import WorkshopBrowserDialog
from workshop_search_runner import WorkshopSearchWorker


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _server():
    return models.ServerConfig(id="s1", name="Chudville")


def test_no_api_key_disables_search_and_shows_message(monkeypatch):
    monkeypatch.setattr(WorkshopSearchWorker, "start", lambda self: pytest.fail("must not search with no key"))
    dlg = WorkshopBrowserDialog(_server(), api_key="", on_changed=None)
    assert dlg.search_edit.isEnabled() is False
    assert dlg.search_btn.isEnabled() is False
    assert "API key" in dlg.status_label.text()


def test_with_api_key_searches_immediately_on_open(monkeypatch):
    started = []
    monkeypatch.setattr(WorkshopSearchWorker, "start", lambda self: started.append(self.query))
    dlg = WorkshopBrowserDialog(_server(), api_key="FAKEKEY", on_changed=None)
    assert started == [""]  # empty query -- browse mode


def test_search_error_shown_in_status_label(monkeypatch):
    monkeypatch.setattr(WorkshopSearchWorker, "start", lambda self: None)
    dlg = WorkshopBrowserDialog(_server(), api_key="FAKEKEY", on_changed=None)

    dlg._on_search_finished(swa.SearchResult(ok=False, error="Steam rejected this API key."))

    assert dlg.status_label.text() == "Steam rejected this API key."


def test_no_results_message(monkeypatch):
    monkeypatch.setattr(WorkshopSearchWorker, "start", lambda self: None)
    dlg = WorkshopBrowserDialog(_server(), api_key="FAKEKEY", on_changed=None)

    dlg._on_search_finished(swa.SearchResult(ok=True, items=[], total=0))

    assert "no results" in dlg.status_label.text().lower()


def test_successful_results_render_a_row_per_item(monkeypatch):
    monkeypatch.setattr(WorkshopSearchWorker, "start", lambda self: None)
    dlg = WorkshopBrowserDialog(_server(), api_key="FAKEKEY", on_changed=None)

    items = [
        swa.WorkshopItem(id="1", title="Pippi", description="Admin tools", author_steam_id="x", subscriptions=1000),
        swa.WorkshopItem(id="2", title="Emberlight", description="", author_steam_id="y", subscriptions=500),
    ]
    dlg._on_search_finished(swa.SearchResult(ok=True, items=items, total=2))

    # results_layout has N result rows + 1 trailing stretch
    assert dlg.results_layout.count() == 3


def test_add_button_calls_on_changed_and_updates_server_mods(monkeypatch):
    monkeypatch.setattr(WorkshopSearchWorker, "start", lambda self: None)
    server = _server()
    changed = []
    dlg = WorkshopBrowserDialog(server, api_key="FAKEKEY", on_changed=lambda s: changed.append(s))

    item = swa.WorkshopItem(id="123", title="Pippi", description="", author_steam_id="x", subscriptions=1000)
    from PySide6.QtWidgets import QPushButton
    btn = QPushButton("Add")

    dlg._add_item(item, btn)

    assert any(m["id"] == "123" for m in server.mods)
    assert changed == [server]
    assert btn.text() == "Added"
    assert btn.isEnabled() is False


def test_already_added_item_shows_added_and_disabled(monkeypatch):
    monkeypatch.setattr(WorkshopSearchWorker, "start", lambda self: None)
    server = _server()
    server.mods = [{"id": "123", "name": "Pippi", "enabled": True}]
    dlg = WorkshopBrowserDialog(server, api_key="FAKEKEY", on_changed=None)

    item = swa.WorkshopItem(id="123", title="Pippi", description="", author_steam_id="x", subscriptions=1000)
    row = dlg._make_result_row(item, existing_ids={"123"})

    from PySide6.QtWidgets import QPushButton
    btn = row.findChildren(QPushButton)[0]
    assert btn.text() == "Added"
    assert btn.isEnabled() is False


def test_run_search_is_a_noop_while_already_searching(monkeypatch):
    started = []
    monkeypatch.setattr(WorkshopSearchWorker, "start", lambda self: started.append(1))
    dlg = WorkshopBrowserDialog(_server(), api_key="FAKEKEY", on_changed=None)
    assert started == [1]  # from the initial browse-on-open

    dlg._run_search()  # worker never "finished" -- should be a no-op

    assert started == [1]


def test_accept_waits_for_an_in_flight_search(monkeypatch):
    """The actual bug: clicking Close (the most common way this
    dialog gets closed) used to call QDialog's default accept(),
    which never waited on an in-flight search at all."""
    monkeypatch.setattr(WorkshopSearchWorker, "start", lambda self: None)
    dlg = WorkshopBrowserDialog(_server(), api_key="FAKEKEY", on_changed=None)
    waited = []
    monkeypatch.setattr(dlg._worker, "wait", lambda ms: waited.append(ms))

    dlg.accept()

    assert waited == [2000]


def test_reject_waits_for_an_in_flight_search(monkeypatch):
    """Same bug via Escape (or any other dismissal path)."""
    monkeypatch.setattr(WorkshopSearchWorker, "start", lambda self: None)
    dlg = WorkshopBrowserDialog(_server(), api_key="FAKEKEY", on_changed=None)
    waited = []
    monkeypatch.setattr(dlg._worker, "wait", lambda ms: waited.append(ms))

    dlg.reject()

    assert waited == [2000]


def test_accept_is_a_noop_wait_with_no_worker_running(monkeypatch):
    monkeypatch.setattr(WorkshopSearchWorker, "start", lambda self: None)
    dlg = WorkshopBrowserDialog(_server(), api_key="", on_changed=None)  # no key -- never searches
    assert dlg._worker is None

    dlg.accept()  # must not raise
