from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import webhooks
from discord_status_runner import DiscordStatusWorker


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _run_synchronously(worker: DiscordStatusWorker):
    captured = []
    worker.finished_update.connect(lambda result: captured.append(result))
    worker.run()
    return captured[0]


def test_worker_returns_new_message_id(monkeypatch):
    monkeypatch.setattr(webhooks, "update_discord_status", lambda url, mid, content: "999888777")
    worker = DiscordStatusWorker("https://discord.com/api/webhooks/1/abc", "", "Online: 3/40")

    result = _run_synchronously(worker)

    assert result == "999888777"


def test_worker_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(webhooks, "update_discord_status", lambda url, mid, content: None)
    worker = DiscordStatusWorker("https://discord.com/api/webhooks/1/abc", "555", "Online: 3/40")

    result = _run_synchronously(worker)

    assert result is None


def test_worker_catches_unexpected_exceptions(monkeypatch):
    def raise_it(url, mid, content):
        raise RuntimeError("something broke")
    monkeypatch.setattr(webhooks, "update_discord_status", raise_it)
    worker = DiscordStatusWorker("https://discord.com/api/webhooks/1/abc", "", "status")

    result = _run_synchronously(worker)  # must not raise

    assert result is None
