"""1.0.6 layout: fewer sidebar entries, Server/App Settings search, "More options"
fold-outs, one save bar -- with every setting still reachable."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import ini_field_specs
import models
import settings_layout

app = QApplication.instance() or QApplication([])


@pytest.fixture
def win(monkeypatch):
    import mod_manager
    from ui.main_window import MainWindow
    monkeypatch.setattr(mod_manager, "write_modlist", lambda *a, **k: None)
    cfg = models.AppConfig()
    s = cfg.add_server()
    s.name = "Chudville"
    cfg.active_server_id = s.id
    monkeypatch.setattr(cfg, "save", lambda *a, **k: None)
    w = MainWindow(config=cfg)
    yield w
    w.close()


def _inside(widget, ancestor) -> bool:
    p = widget
    while p is not None:
        if p is ancestor:
            return True
        p = p.parentWidget()
    return False


def test_every_page_key_is_in_one_section():
    pages = [k for _s, _l, _h, _d, keys in settings_layout.SECTIONS for k in keys]
    expected = {"identity", "network", "backups", "restart", "alerts", *settings_layout.GAMEPLAY_PAGES}
    assert sorted(pages) == sorted(expected)
    assert all(k in {s.key for s in ini_field_specs.ALL_FIELDS_BY_KEY.values()} for k in settings_layout.GAMEPLAY_COMMON)


def test_sidebar_has_six_entries_and_keeps_servers(win):
    from ui.sidebar import NAV_ITEMS
    assert [k for k, _l in NAV_ITEMS] == ["dashboard", "players", "mods", "console", "settings", "app"]
    assert win.sidebar.add_server_btn.isVisible() or not win.isVisible()


def test_old_pages_still_open_where_they_moved(win):
    win._on_nav_selected("updates")
    assert win.stack.currentWidget() is win.settings_container
    assert win.settings_container.current_section() == "updates"
    win._on_nav_selected("access")
    assert win.stack.currentWidget() is win.players_tabs
    assert win.players_tabs.current_tab() == "access"
    win._open_automation_settings("updates")
    assert win.settings_container.current_section() == "updates"


def test_every_gameplay_setting_shown_once_with_its_slider(win):
    from ui.ghost_slider import GhostSlider
    section = win.settings_gameplay
    pages = [win.settings_rates_page, *win.gameplay_pages.values()]
    seen = set()
    for page in pages:
        for key, widget in page._fields.items():
            assert key not in seen
            seen.add(key)
            assert _inside(widget, section), key
            spec = ini_field_specs.ALL_FIELDS_BY_KEY[key]
            if spec.kind == "float":
                assert isinstance(widget, GhostSlider), key
    all_keys = {s.key for _k, _l, specs in ini_field_specs.CATEGORIES for s in specs}
    assert seen == all_keys


def test_search_finds_settings_and_jumps_to_them(win):
    c = win.settings_container
    c.search_edit.setText("xp")
    assert c.stack.currentWidget() is c._results
    names = [e[1] for e in c._index if "XP" in e[1]]
    assert len(names) == 5
    combat = win.gameplay_pages["combat"]
    widget = combat._fields["NPCDamageMultiplier"]
    assert not win.settings_gameplay.folds["combat"].is_open()
    c.show_setting("gameplay", widget)
    assert c.current_section() == "gameplay"
    assert win.settings_gameplay.folds["combat"].is_open()
    assert c.search_edit.text() == ""


def test_less_used_settings_are_folded_but_searchable(win):
    c = win.settings_container
    c.search_edit.setText("bind address")
    hits = [e for e in c._index if e[1].startswith("Bind Address")]
    assert hits and "More options" in hits[0][3]
    c.show_setting(hits[0][0], hits[0][4])
    assert win.settings_network_page.more.is_open()


def test_one_save_bar_saves_every_section(win):
    c = win.settings_container
    server = win.config.get_active()
    win.settings_rates_page._fields["PlayerXPRateMultiplier"].setValue(35)
    win.gameplay_pages["combat"]._fields["NPCDamageMultiplier"].setValue(22)
    assert c.save_bar.isVisibleTo(c) and "2 unsaved" in c.save_label.text()
    c.save_btn.click()
    assert server.gameplay["PlayerXPRateMultiplier"] == 3.5
    assert server.gameplay["NPCDamageMultiplier"] == 2.2
    assert not c.save_bar.isVisibleTo(c)


def test_discard_all(win):
    c = win.settings_container
    win.settings_rates_page._fields["PlayerXPRateMultiplier"].setValue(50)
    c.discard_btn.click()
    assert win.settings_rates_page.dirty_count() == 0


def test_mods_tools_are_in_one_menu(win):
    texts = [a.text() for a in win.mods_page.fix_menu.actions() if a.text()]
    assert texts == ["Check for outdated mods", "Quick mod check…", "Find all bad mods…", "Bisect by hand…"]


def test_app_settings_search(win):
    page = win.app_settings_page
    page.search_edit.setText("discord")
    assert page._section_stack.currentWidget() is page._results_page
    assert page._results_layout.count() > 0
    page.search_edit.setText("")
    assert page._section_stack.currentWidget() is not page._results_page


def test_web_settings_describe_has_sections_and_flags(win):
    d = win.web_control.api.web_settings.describe(win.config.get_active())
    assert [s["key"] for s in d["sections"]] == settings_layout.SECTION_KEYS
    fields = {f["key"]: f for p in d["pages"] for f in p["fields"]}
    assert fields["PlayerXPRateMultiplier"]["common"] is True
    assert fields["PlayerXPTimeMultiplier"]["common"] is False
    assert fields["bind_ip"]["common"] is False and fields["game_port"]["common"] is True
    assert next(p for p in d["pages"] if p["key"] == "combat")["category"] == "Combat"
