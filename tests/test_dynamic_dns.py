from __future__ import annotations

import urllib.error
import urllib.request

import dynamic_dns


class _FakeResponse:
    def __init__(self, text: str):
        self._text = text

    def read(self):
        return self._text.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_update_without_domain_or_token_fails_fast_with_no_network_call(monkeypatch):
    called = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: called.append(1))

    result = dynamic_dns.update(domain="", token="")

    assert result.ok is False
    assert "DuckDNS domain/token" in result.message
    assert called == []


def test_update_success_parses_verbose_response(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse("OK 203.0.113.9  UPDATED"))

    result = dynamic_dns.update(domain="myserver", token="sometoken")

    assert result.ok is True
    assert result.ip == "203.0.113.9"


def test_update_success_nochange_still_reports_ok(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse("OK 203.0.113.9  NOCHANGE"))

    result = dynamic_dns.update(domain="myserver", token="sometoken")

    assert result.ok is True


def test_update_rejects_ko_response(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse("KO"))

    result = dynamic_dns.update(domain="myserver", token="badtoken")

    assert result.ok is False
    assert "rejected" in result.message.lower()


def test_update_handles_http_error(monkeypatch):
    def raise_500(url, timeout=8.0):
        raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", raise_500)

    result = dynamic_dns.update(domain="myserver", token="sometoken")

    assert result.ok is False
    assert "HTTP 500" in result.message


def test_update_handles_network_failure(monkeypatch):
    def raise_oserror(url, timeout=8.0):
        raise OSError("no network")
    monkeypatch.setattr(urllib.request, "urlopen", raise_oserror)

    result = dynamic_dns.update(domain="myserver", token="sometoken")

    assert result.ok is False
    assert result.message


def test_update_handles_empty_response(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse(""))

    result = dynamic_dns.update(domain="myserver", token="sometoken")

    assert result.ok is False


def test_update_sends_domain_and_token_and_no_explicit_ip(monkeypatch):
    """Deliberately no ip= param -- DuckDNS auto-detects from the
    request itself, which is more reliable than trusting a separately
    fetched IP that could be stale."""
    captured = []

    def fake_urlopen(url, timeout=8.0):
        captured.append(url)
        return _FakeResponse("OK 203.0.113.9  UPDATED")
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    dynamic_dns.update(domain="myserver", token="sometoken")

    assert "domains=myserver" in captured[0]
    assert "token=sometoken" in captured[0]
    assert "ip=" not in captured[0]
