from __future__ import annotations

import json
import urllib.error
import urllib.request

import steam_workshop_api as swa

# One second after Iris's release -- a mod updated at this instant
# genuinely could have been rebuilt for it.
_AFTER_IRIS = swa.IRIS_CUTOFF_TIMESTAMP + 1
# One second before -- couldn't possibly have been, whatever its tag says.
_BEFORE_IRIS = swa.IRIS_CUTOFF_TIMESTAMP - 1


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _detail(pfid, title="Mod", tags=None, time_updated=_AFTER_IRIS, **extra):
    d = {
        "result": 1,
        "publishedfileid": pfid,
        "title": title,
        "time_updated": time_updated,
        "tags": [{"tag": t, "adminonly": False} for t in (tags if tags is not None else ["Enhanced"])],
    }
    d.update(extra)
    return d


def test_search_without_api_key_fails_fast_with_no_network_call(monkeypatch):
    called = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: called.append(1))

    result = swa.search(api_key="", query="pippi")

    assert result.ok is False
    assert "API key" in result.error
    assert called == []


def test_search_parses_a_successful_iris_ready_response(monkeypatch):
    payload = {
        "response": {
            "total": 2,
            "publishedfiledetails": [
                _detail("123", "Pippi", subscriptions=50000, file_description="Admin tools", creator="76561198000000000", preview_url="https://example.com/pippi.jpg"),
                _detail("456", "Emberlight", subscriptions=20000, file_description=""),
            ],
        }
    }
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse(payload))

    result = swa.search(api_key="FAKEKEY", query="admin")

    assert result.ok is True
    assert result.total == 2
    assert len(result.items) == 2
    assert result.items[0].id == "123"
    assert result.items[0].title == "Pippi"
    assert result.items[0].subscriptions == 50000
    assert result.items[1].description == ""
    assert result.items[0].tags == ["Enhanced"]
    assert result.items[0].time_updated == _AFTER_IRIS


def test_search_skips_items_steam_could_not_fully_return(monkeypatch):
    """result != 1 means Steam couldn't actually return full details for
    that item (removed/banned/etc) -- shown as absent, not a broken row."""
    payload = {
        "response": {
            "total": 1,
            "publishedfiledetails": [
                _detail("123", "Good One"),
                {"result": 9, "publishedfileid": "999"},
            ],
        }
    }
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse(payload))

    result = swa.search(api_key="FAKEKEY", query="x")

    assert result.ok is True
    assert len(result.items) == 1
    assert result.items[0].id == "123"


# --------------------------------------------------------- Iris filter --

def test_excludes_a_mod_not_tagged_enhanced(monkeypatch):
    payload = {"response": {"total": 1, "publishedfiledetails": [
        _detail("1", "Old One", tags=["Legacy"]),
    ]}}
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse(payload))

    result = swa.search(api_key="FAKEKEY", query="x")

    assert result.items == []


def test_excludes_an_enhanced_mod_not_yet_updated_for_iris(monkeypatch):
    """The real gap this whole filter exists for: a mod can be tagged
    Enhanced (rebuilt for the original May 2026 UE5 release) without
    having been updated again for the LATER Iris-specific patch --
    confirmed against real Workshop mods reported "outdated"
    immediately after 2.2.0 shipped despite already carrying the
    Enhanced tag from months earlier."""
    payload = {"response": {"total": 1, "publishedfiledetails": [
        _detail("1", "Stale Enhanced Mod", tags=["Enhanced"], time_updated=_BEFORE_IRIS),
    ]}}
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse(payload))

    result = swa.search(api_key="FAKEKEY", query="x")

    assert result.items == []


def test_includes_a_mod_tagged_enhanced_and_updated_after_iris(monkeypatch):
    payload = {"response": {"total": 1, "publishedfiledetails": [
        _detail("1", "Fresh Mod", tags=["Enhanced"], time_updated=_AFTER_IRIS),
    ]}}
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse(payload))

    result = swa.search(api_key="FAKEKEY", query="x")

    assert len(result.items) == 1
    assert result.items[0].id == "1"


def test_includes_a_mod_updated_exactly_at_the_iris_release_moment(monkeypatch):
    payload = {"response": {"total": 1, "publishedfiledetails": [
        _detail("1", "Exact", tags=["Enhanced"], time_updated=swa.IRIS_CUTOFF_TIMESTAMP),
    ]}}
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse(payload))

    result = swa.search(api_key="FAKEKEY", query="x")

    assert len(result.items) == 1


def test_request_asks_steam_to_filter_by_the_enhanced_tag_server_side(monkeypatch):
    captured_urls = []

    def fake_urlopen(url, timeout=8.0):
        captured_urls.append(url)
        return _FakeResponse({"response": {"total": 0, "publishedfiledetails": []}})
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    swa.search(api_key="FAKEKEY", query="pippi")

    assert "requiredtags" in captured_urls[0]
    assert "Enhanced" in captured_urls[0]
    assert "return_tags=1" in captured_urls[0]


def test_a_mod_with_no_tags_at_all_is_excluded(monkeypatch):
    payload = {"response": {"total": 1, "publishedfiledetails": [
        {"result": 1, "publishedfileid": "1", "title": "No Tags", "time_updated": _AFTER_IRIS},
    ]}}
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse(payload))

    result = swa.search(api_key="FAKEKEY", query="x")

    assert result.items == []


# ------------------------------------------------------------ everything else --

def test_search_empty_query_uses_browse_mode_not_text_search(monkeypatch):
    captured_urls = []

    def fake_urlopen(url, timeout=8.0):
        captured_urls.append(url)
        return _FakeResponse({"response": {"total": 0, "publishedfiledetails": []}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    swa.search(api_key="FAKEKEY", query="")

    assert f"query_type={swa._QUERY_TYPE_MOST_SUBSCRIBED}" in captured_urls[0]


def test_search_nonempty_query_uses_text_search_mode(monkeypatch):
    captured_urls = []

    def fake_urlopen(url, timeout=8.0):
        captured_urls.append(url)
        return _FakeResponse({"response": {"total": 0, "publishedfiledetails": []}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    swa.search(api_key="FAKEKEY", query="pippi")

    assert f"query_type={swa._QUERY_TYPE_TEXT_SEARCH}" in captured_urls[0]


def test_search_handles_auth_error(monkeypatch):
    def raise_401(url, timeout=8.0):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", raise_401)

    result = swa.search(api_key="BADKEY", query="pippi")

    assert result.ok is False
    assert "rejected this API key" in result.error


def test_search_handles_other_http_errors(monkeypatch):
    def raise_500(url, timeout=8.0):
        raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", raise_500)

    result = swa.search(api_key="FAKEKEY", query="pippi")

    assert result.ok is False
    assert "HTTP 500" in result.error


def test_search_handles_network_failure(monkeypatch):
    def raise_oserror(url, timeout=8.0):
        raise OSError("no network")
    monkeypatch.setattr(urllib.request, "urlopen", raise_oserror)

    result = swa.search(api_key="FAKEKEY", query="pippi")

    assert result.ok is False
    assert result.error  # some message, don't care about exact wording


def test_search_handles_malformed_response(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse({"unexpected": "shape"}))

    result = swa.search(api_key="FAKEKEY", query="pippi")

    assert result.ok is False


def test_search_appid_scoped_to_conan_exiles_workshop(monkeypatch):
    import mod_manager
    captured_urls = []

    def fake_urlopen(url, timeout=8.0):
        captured_urls.append(url)
        return _FakeResponse({"response": {"total": 0, "publishedfiledetails": []}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    swa.search(api_key="FAKEKEY", query="pippi")

    assert f"appid={mod_manager.WORKSHOP_APP_ID}" in captured_urls[0]


def test_search_oversamples_to_compensate_for_client_side_date_filtering(monkeypatch):
    captured_urls = []

    def fake_urlopen(url, timeout=8.0):
        captured_urls.append(url)
        return _FakeResponse({"response": {"total": 0, "publishedfiledetails": []}})
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    swa.search(api_key="FAKEKEY", query="pippi", count=20)

    assert f"numperpage={20 * swa._OVERSAMPLE_FACTOR}" in captured_urls[0]


def test_search_truncates_to_the_requested_count_after_filtering(monkeypatch):
    payload = {"response": {"total": 5, "publishedfiledetails": [
        _detail(str(i), f"Mod{i}") for i in range(5)
    ]}}
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _FakeResponse(payload))

    result = swa.search(api_key="FAKEKEY", query="x", count=3)

    assert len(result.items) == 3
