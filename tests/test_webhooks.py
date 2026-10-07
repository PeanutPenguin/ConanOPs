from __future__ import annotations

import json
import urllib.error
import urllib.request

import webhooks


class _FakeResponse:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------- send_discord --

def test_send_discord_false_with_no_url():
    assert webhooks.send_discord("", "hello") is False


def test_send_discord_true_on_success(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=5.0: _FakeResponse(204))
    assert webhooks.send_discord("https://discord.com/api/webhooks/1/abc", "hello") is True


def test_send_discord_false_on_network_error(monkeypatch):
    def raise_oserror(req, timeout=5.0):
        raise OSError("no network")
    monkeypatch.setattr(urllib.request, "urlopen", raise_oserror)
    assert webhooks.send_discord("https://discord.com/api/webhooks/1/abc", "hello") is False


# --------------------------------------------------- update_discord_status --

def test_update_discord_status_none_with_no_url():
    assert webhooks.update_discord_status("", "", "status text") is None


def test_update_discord_status_posts_new_message_when_no_id(monkeypatch):
    """No message_id -- must POST with ?wait=true and return the new
    message's id from Discord's response."""
    captured = {}

    def fake_urlopen(req, timeout=5.0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResponse(200, json.dumps({"id": "999888777"}).encode("utf-8"))
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = webhooks.update_discord_status("https://discord.com/api/webhooks/1/abc", "", "Online: 3/40")

    assert result == "999888777"
    assert captured["method"] == "POST"
    assert "wait=true" in captured["url"]


def test_update_discord_status_edits_existing_message_when_id_given(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=5.0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        return _FakeResponse(200, b"")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = webhooks.update_discord_status("https://discord.com/api/webhooks/1/abc", "555444333", "Online: 5/40")

    assert result == "555444333"
    assert captured["method"] == "PATCH"
    assert captured["url"] == "https://discord.com/api/webhooks/1/abc/messages/555444333"


def test_update_discord_status_returns_none_on_post_failure(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=5.0: _FakeResponse(400, b"{}"))

    result = webhooks.update_discord_status("https://discord.com/api/webhooks/1/abc", "", "status")

    assert result is None


def test_update_discord_status_returns_none_when_edited_message_missing(monkeypatch):
    """The message_id no longer exists (deleted in Discord, e.g.) --
    the caller is responsible for retrying with an empty message_id to
    post a fresh one; this function itself just reports the failure."""
    def raise_404(req, timeout=5.0):
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", raise_404)

    result = webhooks.update_discord_status("https://discord.com/api/webhooks/1/abc", "555444333", "status")

    assert result is None


def test_update_discord_status_returns_none_on_network_error(monkeypatch):
    def raise_oserror(req, timeout=5.0):
        raise OSError("no network")
    monkeypatch.setattr(urllib.request, "urlopen", raise_oserror)

    result = webhooks.update_discord_status("https://discord.com/api/webhooks/1/abc", "", "status")

    assert result is None


def test_update_discord_status_returns_none_on_malformed_json_response(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=5.0: _FakeResponse(200, b"not json"))

    result = webhooks.update_discord_status("https://discord.com/api/webhooks/1/abc", "", "status")

    assert result is None
