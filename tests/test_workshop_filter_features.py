"""The Workshop filter improvements: cursor paging, server-side
filters, status labels, sort options, dependencies, keyless lookups,
caching, the configurable cutoff, and the UI around them."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

import pytest

import models
import steam_workshop_api as swa

AFTER = swa.IRIS_CUTOFF_TIMESTAMP + 10
BEFORE = swa.IRIS_CUTOFF_TIMESTAMP - 10


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _d(pfid, tags=("Enhanced",), ts=AFTER, children=(), title=None):
    d = {"result": 1, "publishedfileid": pfid, "title": title or f"Mod {pfid}", "time_updated": ts,
         "tags": [{"tag": t} for t in tags]}
    if children:
        d["children"] = [{"publishedfileid": c} for c in children]
    return d


def _params(url):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))


@pytest.fixture(autouse=True)
def _fresh_cache():
    swa.clear_cache()
    yield
    swa.clear_cache()


# ------------------------------------------------------------- paging --

def test_walks_cursor_pages_until_enough_results_pass_the_filter(monkeypatch):
    pages = {
        "*": {"response": {"total": 9, "next_cursor": "c2", "publishedfiledetails": [_d("1", ts=BEFORE), _d("2", ts=BEFORE)]}},
        "c2": {"response": {"total": 9, "next_cursor": "c3", "publishedfiledetails": [_d("3"), _d("4", ts=BEFORE)]}},
        "c3": {"response": {"total": 9, "next_cursor": "c4", "publishedfiledetails": [_d("5"), _d("6")]}},
    }
    seen = []

    def fake(url, timeout=8.0):
        cur = _params(url)["cursor"]
        seen.append(cur)
        return _Resp(pages[cur])
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    result = swa.search("KEY", "x", count=3)

    assert [i.id for i in result.items] == ["3", "5", "6"]
    assert seen == ["*", "c2", "c3"]
    assert result.next_cursor == "c4"


def test_stops_at_max_pages(monkeypatch):
    calls = []

    def fake(url, timeout=8.0):
        calls.append(1)
        return _Resp({"response": {"total": 99, "next_cursor": f"c{len(calls)}", "publishedfiledetails": [_d(str(len(calls)), ts=BEFORE)]}})
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    result = swa.search("KEY", "x", count=5, max_pages=3)

    assert len(calls) == 3
    assert result.ok and result.items == []


def test_filling_up_mid_page_resumes_from_that_same_page(monkeypatch):
    page = {"response": {"total": 4, "next_cursor": "c2", "publishedfiledetails": [_d("1"), _d("2"), _d("3"), _d("4")]}}
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: seen.append(_params(url)["cursor"]) or _Resp(page))

    first = swa.search("KEY", "x", count=2)
    assert [i.id for i in first.items] == ["1", "2"]
    assert first.next_cursor == "*"  # items 3 and 4 are still on this page

    second = swa.search("KEY", "x", count=2, cursor=first.next_cursor, skip_ids={"1", "2"})
    assert [i.id for i in second.items] == ["3", "4"]


def test_an_error_on_a_later_page_keeps_earlier_results(monkeypatch):
    def fake(url, timeout=8.0):
        if _params(url)["cursor"] == "*":
            return _Resp({"response": {"total": 5, "next_cursor": "c2", "publishedfiledetails": [_d("1")]}})
        raise OSError("dropped")
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    result = swa.search("KEY", "x", count=5)

    assert result.ok and [i.id for i in result.items] == ["1"]


# ----------------------------------------------------- server-side filters --

def _capture(monkeypatch):
    urls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: urls.append(url) or _Resp({"response": {"total": 0, "publishedfiledetails": []}}))
    return urls


def test_only_regular_items_and_children_are_requested(monkeypatch):
    urls = _capture(monkeypatch)
    swa.search("KEY", "x")
    p = _params(urls[0])
    assert p["filetype"] == str(swa._FILETYPE_ITEMS)
    assert p["return_children"] == "1"


def test_default_requires_enhanced_tag(monkeypatch):
    urls = _capture(monkeypatch)
    swa.search("KEY", "x")
    p = _params(urls[0])
    assert p["requiredtags[0]"] == "Enhanced"
    assert "excludedtags[0]" not in p


def test_showing_untagged_but_not_legacy_excludes_legacy_server_side(monkeypatch):
    urls = _capture(monkeypatch)
    swa.search("KEY", "x", show={swa.STATUS_UPDATED, swa.STATUS_UNKNOWN})
    p = _params(urls[0])
    assert "requiredtags[0]" not in p
    assert p["excludedtags[0]"] == "Legacy"


def test_showing_legacy_sends_no_tag_filters(monkeypatch):
    urls = _capture(monkeypatch)
    swa.search("KEY", "x", show=set(swa.ALL_STATUSES))
    p = _params(urls[0])
    assert "requiredtags[0]" not in p and "excludedtags[0]" not in p


# ------------------------------------------------------------- statuses --

def test_classify():
    assert swa.classify(["Enhanced"], AFTER) == swa.STATUS_UPDATED
    assert swa.classify(["Enhanced"], BEFORE) == swa.STATUS_STALE
    assert swa.classify(["Legacy"], AFTER) == swa.STATUS_LEGACY
    assert swa.classify([], AFTER) == swa.STATUS_UNKNOWN


def test_show_returns_labelled_stale_mods_when_asked(monkeypatch):
    payload = {"response": {"total": 2, "publishedfiledetails": [_d("1"), _d("2", ts=BEFORE)]}}
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _Resp(payload))

    result = swa.search("KEY", "x", show={swa.STATUS_UPDATED, swa.STATUS_STALE})

    assert {i.id: i.status for i in result.items} == {"1": swa.STATUS_UPDATED, "2": swa.STATUS_STALE}


def test_custom_cutoff_changes_the_verdict(monkeypatch):
    payload = {"response": {"total": 1, "publishedfiledetails": [_d("1", ts=AFTER)]}}
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: _Resp(payload))

    later = swa.cutoff_timestamp("2027-01-01")
    result = swa.search("KEY", "x", cutoff_ts=later, show=set(swa.ALL_STATUSES))

    assert result.items[0].status == swa.STATUS_STALE


def test_cutoff_timestamp_parses_and_falls_back():
    assert swa.cutoff_timestamp("2026-09-01") == swa.IRIS_CUTOFF_TIMESTAMP
    assert swa.cutoff_timestamp("not a date") == swa.IRIS_CUTOFF_TIMESTAMP


# --------------------------------------------------------------- sorting --

@pytest.mark.parametrize("query,sort,expected", [
    ("pippi", "", swa._QUERY_TYPE_TEXT_SEARCH),
    ("", "", swa._QUERY_TYPE_MOST_SUBSCRIBED),
    ("pippi", swa.SORT_POPULAR, swa._QUERY_TYPE_MOST_SUBSCRIBED),
    ("", swa.SORT_RECENTLY_UPDATED, swa._QUERY_TYPE_LAST_UPDATED),
    ("pippi", swa.SORT_RECENTLY_UPDATED, swa._QUERY_TYPE_LAST_UPDATED),
    ("", swa.SORT_BEST_MATCH, swa._QUERY_TYPE_MOST_SUBSCRIBED),  # nothing to match against
])
def test_sort_maps_to_query_type(monkeypatch, query, sort, expected):
    urls = _capture(monkeypatch)
    swa.search("KEY", query, sort=sort)
    assert _params(urls[0])["query_type"] == str(expected)


# ---------------------------------------------------------- dependencies --

def test_children_are_parsed_and_titles_resolved(monkeypatch):
    def fake(url, timeout=8.0):
        if url.startswith(swa.DETAILS_URL):
            return _Resp({"response": {"publishedfiledetails": [_d("9", title="Shared Library")]}})
        return _Resp({"response": {"total": 2, "publishedfiledetails": [_d("1", children=["9", "2"]), _d("2", title="Other")]}})
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    result = swa.search("KEY", "x", resolve_children=True)

    item = result.items[0]
    assert item.children == ["9", "2"]
    assert item.child_titles == {"9": "Shared Library", "2": "Other"}


# ------------------------------------------------------------ get_details --

def test_get_details_uses_keyed_endpoint_with_children(monkeypatch):
    urls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: urls.append(url) or _Resp(
        {"response": {"publishedfiledetails": [_d("1", children=["2"])]}}))

    result = swa.get_details(["1"], api_key="KEY")

    assert urls[0].startswith(swa.DETAILS_URL)
    assert _params(urls[0])["includechildren"] == "1"
    assert result.items["1"].children == ["2"]


def test_get_details_works_without_a_key(monkeypatch):
    calls = []

    def fake(url, data=None, timeout=8.0):
        calls.append((url, data))
        return _Resp({"response": {"result": 1, "resultcount": 1, "publishedfiledetails": [_d("1", ts=BEFORE)]}})
    monkeypatch.setattr(urllib.request, "urlopen", fake)

    result = swa.get_details(["1"])

    assert calls[0][0] == swa.KEYLESS_DETAILS_URL
    assert b"itemcount=1" in calls[0][1]
    assert result.ok and result.items["1"].status == swa.STATUS_STALE


def test_get_details_batches_by_100(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: calls.append(url) or _Resp({"response": {"publishedfiledetails": []}}))
    swa.get_details([str(1000 + i) for i in range(250)], api_key="KEY")
    assert len(calls) == 3


def test_get_details_ignores_non_numeric_ids(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: pytest.fail("no request expected"))
    assert swa.get_details(["abc"]).ok


# ---------------------------------------------------------- rate limits --

def test_429_gets_a_rate_limit_message(monkeypatch):
    def raise_429(url, timeout=8.0):
        raise urllib.error.HTTPError(url, 429, "Too Many", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", raise_429)
    assert "rate-limiting" in swa.search("KEY", "x").error
    assert "rate-limiting" in swa.get_details(["1"], api_key="KEY").error


def test_cache_avoids_a_second_request_only_when_asked(monkeypatch):
    calls = []
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0: calls.append(1) or _Resp({"response": {"total": 0, "publishedfiledetails": []}}))
    swa.search("KEY", "x", use_cache=True)
    swa.search("KEY", "x", use_cache=True)
    assert len(calls) == 1
    swa.search("KEY", "x")
    assert len(calls) == 2


# ---------------------------------------------------------------- config --

def test_cutoff_setting_round_trips(tmp_path):
    cfg = models.AppConfig()
    cfg.workshop_update_cutoff = "2027-02-03"
    path = str(tmp_path / "config.json")
    cfg.save(path)
    assert models.AppConfig.load(path).workshop_update_cutoff == "2027-02-03"


# -------------------------------------------------------------------- UI --

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _item(pfid, status=swa.STATUS_UPDATED, children=(), titles=None):
    return swa.WorkshopItem(id=pfid, title=f"Mod {pfid}", description="", author_steam_id="", subscriptions=1,
                            time_updated=AFTER, status=status, children=list(children), child_titles=titles or {})


def _dialog(monkeypatch, server=None):
    from ui.workshop_browser_dialog import WorkshopBrowserDialog
    from workshop_search_runner import WorkshopSearchWorker
    started = []
    monkeypatch.setattr(WorkshopSearchWorker, "start", lambda self: started.append(self))
    dlg = WorkshopBrowserDialog(server or models.ServerConfig(id="s1", name="x"), api_key="KEY")
    return dlg, started


def test_browser_passes_filters_sort_and_cutoff_to_the_worker(qapp, monkeypatch):
    dlg, started = _dialog(monkeypatch)
    dlg._on_search_finished(swa.SearchResult(ok=True))
    dlg.status_checks[swa.STATUS_STALE].setChecked(True)  # triggers a new search
    w = started[-1]
    assert w.show == {swa.STATUS_UPDATED, swa.STATUS_STALE}
    assert w.sort == swa.SORT_POPULAR
    assert w.cutoff_ts == swa.IRIS_CUTOFF_TIMESTAMP


def test_browser_reruns_with_latest_settings_if_changed_mid_search(qapp, monkeypatch):
    dlg, started = _dialog(monkeypatch)
    dlg.status_checks[swa.STATUS_LEGACY].setChecked(True)  # while the first is still running
    assert len(started) == 1
    dlg._on_search_finished(swa.SearchResult(ok=True, items=[_item("1")]))
    assert len(started) == 2 and swa.STATUS_LEGACY in started[-1].show
    assert dlg.results_layout.count() == 1  # stale result discarded


def test_load_more_appends_and_skips_shown(qapp, monkeypatch):
    dlg, started = _dialog(monkeypatch)
    dlg._on_search_finished(swa.SearchResult(ok=True, items=[_item("1"), _item("2")], next_cursor="c2"))
    assert dlg.load_more_btn.isVisibleTo(dlg)
    dlg._load_more()
    assert started[-1].cursor == "c2" and started[-1].skip_ids == {"1", "2"}
    dlg._on_search_finished(swa.SearchResult(ok=True, items=[_item("2"), _item("3")]))
    assert dlg.results_layout.count() == 4  # 1, 2, 3 + stretch
    assert not dlg.load_more_btn.isVisibleTo(dlg)


def test_typing_debounces_and_switches_to_best_match(qapp, monkeypatch):
    dlg, started = _dialog(monkeypatch)
    dlg._on_search_finished(swa.SearchResult(ok=True))
    dlg.search_edit.setText("pippi")
    dlg._on_text_edited("pippi")
    assert len(started) == 1 and dlg._debounce.isActive()
    assert dlg.sort_combo.currentData() == swa.SORT_BEST_MATCH
    dlg._debounce.timeout.emit()
    assert len(started) == 2 and started[-1].query == "pippi"


def test_row_shows_status_and_requirements(qapp, monkeypatch):
    from PySide6.QtWidgets import QLabel
    dlg, _ = _dialog(monkeypatch)
    row = dlg._make_result_row(_item("1", status=swa.STATUS_STALE, children=["9"], titles={"9": "Lib"}), set())
    texts = " | ".join(l.text() for l in row.findChildren(QLabel))
    assert swa.STATUS_LABELS[swa.STATUS_STALE] in texts
    assert "Requires: Lib" in texts


def test_adding_a_mod_offers_its_missing_requirements_first(qapp, monkeypatch):
    server = models.ServerConfig(id="s1", name="x")
    server.mods = [{"id": "8", "name": "Have", "enabled": True}]
    dlg, _ = _dialog(monkeypatch, server)
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)

    dlg._add_item(_item("1", children=["8", "9"], titles={"9": "Lib"}), QPushButton())

    assert len(asked) == 1 and "Lib" in asked[0] and "Have" not in asked[0]
    assert [m["id"] for m in server.mods] == ["8", "9", "1"]


def test_declining_requirements_adds_only_the_mod(qapp, monkeypatch):
    server = models.ServerConfig(id="s1", name="x")
    dlg, _ = _dialog(monkeypatch, server)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    dlg._add_item(_item("1", children=["9"]), QPushButton())
    assert [m["id"] for m in server.mods] == ["1"]


def test_adding_a_legacy_mod_needs_confirmation(qapp, monkeypatch):
    server = models.ServerConfig(id="s1", name="x")
    dlg, _ = _dialog(monkeypatch, server)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Cancel)
    btn = QPushButton("Add")
    dlg._add_item(_item("1", status=swa.STATUS_LEGACY), btn)
    assert server.mods == [] and btn.text() == "Add"


# -------------------------------------------------- Mods tab status check --

def _page_with(mods):
    from ui.mods_page import ModsPage
    page = ModsPage()
    server = models.ServerConfig(id="s1", name="x")
    server.mods = mods
    page.set_server(server)
    return page, server


def _row_texts(page):
    return [page.list_widget.item(i).text() for i in range(page.list_widget.count())]


def test_mods_tab_flags_outdated_legacy_missing_and_needs(qapp):
    page, _ = _page_with([
        {"id": "1", "name": "Fresh", "enabled": True},
        {"id": "2", "name": "Stale", "enabled": True},
        {"id": "3", "name": "Old", "enabled": True},
        {"id": "4", "name": "Gone", "enabled": True},
        {"id": "5", "name": "Needy", "enabled": True},
    ])
    info = {
        "1": _item("1"), "2": _item("2", status=swa.STATUS_STALE), "3": _item("3", status=swa.STATUS_LEGACY),
        "5": _item("5", children=["9"]), "9": _item("9"),
    }
    page._on_mod_status("s1", swa.DetailsResult(ok=True, items=info))

    rows = _row_texts(page)
    assert "⚠" not in rows[0]
    assert "not updated for current patch" in rows[1]
    assert "Legacy" in rows[2]
    assert "not found on the Workshop" in rows[3]
    assert "needs: Mod 9" in rows[4]
    summary = page.mod_status_label.text()
    for bit in ("1 not updated", "1 Legacy", "1 not found", "1 missing a required mod"):
        assert bit in summary


def test_mods_tab_all_clear_message(qapp):
    page, _ = _page_with([{"id": "1", "name": "Fresh", "enabled": True}])
    page._on_mod_status("s1", swa.DetailsResult(ok=True, items={"1": _item("1")}))
    assert page.mod_status_label.text().startswith("✓")


def test_mods_tab_shows_lookup_errors(qapp):
    page, _ = _page_with([{"id": "1", "name": "Fresh", "enabled": True}])
    page._on_mod_status("s1", swa.DetailsResult(ok=False, error="Steam is down"))
    assert "Steam is down" in page.mod_status_label.text()


def test_mods_tab_auto_checks_only_when_enabled_and_once_per_server(qapp, monkeypatch):
    from ui import mods_page as mp
    started = []
    monkeypatch.setattr(mp.ModStatusWorker, "start", lambda self: started.append(self))
    page = mp.ModsPage()
    server = models.ServerConfig(id="s1", name="x")
    server.mods = [{"id": "1", "name": "A", "enabled": True}]
    page.set_server(server)
    assert started == []  # off by default
    page.auto_check_mod_status = True
    page.get_update_cutoff = lambda: "2027-01-01"
    page.set_server(server)
    assert len(started) == 1 and started[0].cutoff_ts == swa.cutoff_timestamp("2027-01-01")
    page._on_mod_status("s1", swa.DetailsResult(ok=True, items={}))
    page.set_server(server)
    assert len(started) == 1  # already checked this session


def test_settings_page_rejects_a_bad_cutoff(qapp, monkeypatch):
    from ui.app_settings_page import AppSettingsPage
    cfg = models.AppConfig()
    saved = []

    class _Web:
        is_running = False
    page = AppSettingsPage(
        cfg, save_config=lambda: saved.append(1), on_theme_changed=lambda p: None, on_lock_changed=lambda: None,
        install_dir="/tmp/fake-install", on_update_installed=lambda: None, web_control_server=_Web(),
    )
    page.workshop_cutoff_edit.setText("tomorrow")
    page._on_workshop_cutoff_changed()
    assert cfg.workshop_update_cutoff == "2026-09-01" and page.workshop_cutoff_edit.text() == "2026-09-01"
    page.workshop_cutoff_edit.setText("2027-03-01")
    page._on_workshop_cutoff_changed()
    assert cfg.workshop_update_cutoff == "2027-03-01" and saved
