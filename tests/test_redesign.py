"""The redesigned interface: theme, assets, sidebar, header, dashboard
automation tiles, the Restart Schedule activity chart, splash screen."""
from __future__ import annotations

import sys
import urllib.error

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

import models  # noqa: E402
import steam_workshop_api as swa  # noqa: E402
import theme_config  # noqa: E402
from ui import assets  # noqa: E402
from ui.activity_chart import ActivityChart, hour_name, window_hours  # noqa: E402
from ui.theme import _luminance, build_stylesheet, primary_fill  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


# --------------------------------------------------------------- theme --

@pytest.mark.parametrize("accent", ["#c9752f", "#f0b54a", "#5fd38a", "#ffffff", "#3f7fd6"])
def test_primary_fill_keeps_white_text_readable(accent):
    fill = primary_fill(accent)
    assert 1.05 / (_luminance(fill) + 0.05) >= 4.5


def test_stylesheet_uses_the_palette_and_switch_images():
    pal = theme_config.ThemePalette(accent="#3f7fd6", bg="#101010")
    qss = build_stylesheet(pal)
    assert "#101010" in qss
    assert "switch-on-" in qss and "switch-off-" in qss
    for name in ("PillOn", "PillWarn", "WarnNote", "ContentHeader", "NavButton"):
        assert name in qss


def test_default_palette_is_the_charcoal_theme():
    assert theme_config.DEFAULT_PALETTE.bg == "#1d1d1d"
    assert "Geist" in theme_config.DEFAULT_PALETTE.font_ui


# -------------------------------------------------------------- assets --

def test_bundled_assets_load():
    assert not assets.app_icon().isNull()
    assert not assets.app_icon_pixmap(38).isNull()
    assert assets.frame_count() > 10
    families = assets.load_fonts()
    assert any("Geist" in f for f in families) or assets._fonts_loaded


def test_spinner_animates_only_while_visible():
    spinner = assets.LoadingSpinner(24)
    assert not spinner.is_animating()
    spinner.show()
    assert spinner.is_animating()
    spinner.hide()
    assert not spinner.is_animating()


def test_line_icons_render():
    assert not assets.line_icon("dashboard", "#a3a3a3", "#e2914f").isNull()


# ------------------------------------------------------- activity chart --

def test_window_hours_wraps_midnight():
    assert window_hours(4, 5) == [4]
    assert window_hours(22, 2) == [22, 23, 0, 1]
    assert window_hours(None, 5) == []


def test_chart_summary_and_axis():
    chart = ActivityChart()
    assert not chart.has_data()
    vals = [9, 6, 3, 1, 0, 1, 1, 2, 3, 4, 5, 6, 6, 7, 8, 9, 12, 15, 19, 23, 25, 22, 18, 14]
    chart.set_values(vals)
    assert chart.quietest_and_busiest() == (4, 20)
    assert "Quietest 4 AM" in chart.summary() and "Busiest 8 PM" in chart.summary()
    assert chart._top() == 25
    chart.resize(600, 200)
    chart.grab()  # paints without error


def test_hour_names():
    assert [hour_name(h) for h in (0, 11, 12, 23)] == ["12 AM", "11 AM", "12 PM", "11 PM"]


def test_restart_page_overlays_the_window_on_the_chart():
    from ui.settings_restart_page import SettingsRestartPage
    page = SettingsRestartPage(get_hourly_activity=lambda: [1] * 24)
    page.load_committed({"restart_enabled": True, "restart_start": "22:00", "restart_end": "01:00"})
    assert page.activity_chart._window == [22, 23, 0]
    page.enabled_check.setChecked(False)
    assert page.activity_chart._window == []


def test_restart_page_refreshes_history_per_server():
    from ui.settings_restart_page import SettingsRestartPage
    data = {"v": [0] * 24}
    page = SettingsRestartPage(get_hourly_activity=lambda: data["v"])
    assert not page.activity_chart.has_data()
    data["v"] = [3] * 24
    page.load_committed({"restart_enabled": True, "restart_start": "04:00", "restart_end": "05:00"})
    assert page.activity_chart.has_data()


# ------------------------------------------------------------- sidebar --

def test_sidebar_status_dots_counts_and_power_button():
    from ui.sidebar import Sidebar, NAV_ITEMS
    bar = Sidebar()
    s = models.ServerConfig(id="a", name="Chudville")
    bar.set_servers([s], "a")
    bar.set_server_status("a", True, 3)
    assert bar._server_players["a"].text() == "3"
    bar.set_server_status("a", False, 3)
    assert bar._server_players["a"].text() == ""
    bar.set_power_state(True)
    assert bar.power_btn.text() == "Stop Server" and bar.power_btn.isEnabled()
    bar.set_power_state(False)
    assert bar.power_btn.text() == "Start Server"
    bar.set_servers([], "")
    assert not bar.power_btn.isEnabled()
    assert ("app", "App Settings") in NAV_ITEMS and ("settings", "Server Settings") in NAV_ITEMS


def test_settings_subnav_escapes_ampersands_and_has_group_headings():
    from PySide6.QtWidgets import QLabel, QWidget
    from ui.settings_container import SettingsContainer
    c = SettingsContainer([("diagnostics", "Diagnostics", QWidget()), ("network", "Network & Ports", QWidget()),
                           ("progression", "Progression", QWidget())])
    texts = [b.text() for b in c.findChildren(QPushButton)]
    assert "Network && Ports" in texts
    headings = [l.text() for l in c.findChildren(QLabel) if l.objectName() == "SectionLabel"]
    assert headings == ["Server", "Gameplay"]


# ----------------------------------------------------------- dashboard --

def test_dashboard_automation_tiles():
    from ui.dashboard_page import DashboardPage
    page = DashboardPage()
    opened = []
    page.on_open_automation = opened.append
    page.set_automation({"restart": (True, "Daily 04:00–05:00"), "ddns": (False, "Not set up")})
    tile = page.automation_tiles["restart"]
    assert tile.state_label.objectName() == "PillOn" and "04:00" in tile.state_label.text()
    assert page.automation_tiles["ddns"].state_label.objectName() == "PillOff"
    tile.open_btn.click()
    assert opened == ["restart"]


def test_main_window_header_and_automation_states(monkeypatch):
    import mod_manager
    from ui.main_window import MainWindow
    monkeypatch.setattr(mod_manager, "write_modlist", lambda *a, **k: None)
    cfg = models.AppConfig()
    s = cfg.add_server()
    s.name = "Chudville"
    s.webhook_discord_url = "https://discord.example/hook"
    cfg.active_server_id = s.id
    monkeypatch.setattr(cfg, "save", lambda *a, **k: None)
    win = MainWindow(config=cfg)
    try:
        win._on_nav_selected("mods")
        assert win.header_title.text() == "Mods"
        assert win.mods_page.title_label.isHidden()
        win._known_running[s.id] = True
        win._online_by_server[s.id] = {"Mira"}
        win._refresh_chrome()
        assert "Online" in win.header_status.text() and win.header_status.objectName() == "PillOn"
        assert win.sidebar.power_btn.text() == "Stop Server"
        states = win._automation_states(s)
        assert states["alerts"] == (True, "Discord")
        assert states["ddns"][0] is False
        win._open_automation_settings("backups")
        assert win.stack.currentWidget() is win.settings_container
    finally:
        win.close()


# --------------------------------------------------------- small fixes --

def test_keyless_403_doesnt_blame_an_api_key(monkeypatch):
    import urllib.request

    def refuse(*a, **k):
        raise urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    msg = swa.get_details(["1"]).error
    assert "API key" not in msg.split("--")[0] and "refused" in msg


def test_splash_builds():
    from ui.splash import SplashScreen
    splash = SplashScreen()
    splash.apply_stylesheet(build_stylesheet())
    splash.set_status("Loading your servers…")
    assert splash.status_label.text() == "Loading your servers…"
    splash.close()


def test_web_app_uses_the_app_palette():
    import web_control
    css = open(web_control._asset("web", "app.css"), encoding="utf-8").read()
    assert "#1d1d1d" in css


# ------------------------------------------------- mockup page layouts --

def test_mod_rows_have_switch_pills_and_row_buttons(monkeypatch):
    from PySide6.QtWidgets import QCheckBox, QLabel
    from ui.mods_page import ModsPage
    page = ModsPage()
    server = models.ServerConfig(id="s1", name="x")
    server.mods = [{"id": "1", "name": "Fresh", "enabled": True}, {"id": "2", "name": "Old", "enabled": True}]
    page.set_server(server)
    item = swa.WorkshopItem(id="2", title="Old", description="", author_steam_id="", subscriptions=1,
                            status=swa.STATUS_STALE, time_updated=1)
    page._on_mod_status("s1", swa.DetailsResult(ok=True, items={"1": swa.WorkshopItem(
        id="1", title="Fresh", description="", author_steam_id="", subscriptions=1, status=swa.STATUS_UPDATED), "2": item}))
    row = page.list_widget.itemWidget(page.list_widget.item(1))
    assert row.findChildren(QCheckBox)[0].isChecked()
    assert any(l.objectName() == "PillWarn" for l in row.findChildren(QLabel))
    assert len(row.findChildren(QPushButton)) == 3
    assert "not updated for current patch" in page.list_widget.item(1).text()  # text kept for search/a11y


def test_mod_row_switch_toggles_through_the_existing_action(monkeypatch):
    from PySide6.QtWidgets import QCheckBox
    from ui.mods_page import ModsPage
    page = ModsPage()
    server = models.ServerConfig(id="s1", name="x")
    server.mods = [{"id": "1", "name": "A", "enabled": True}]
    page.set_server(server)
    page.list_widget.itemWidget(page.list_widget.item(0)).findChildren(QCheckBox)[0].setChecked(False)
    QApplication.processEvents()
    assert server.mods[0]["enabled"] is False


def test_generic_settings_rows_put_controls_on_the_right():
    from PySide6.QtWidgets import QFrame
    from ui.generic_settings_page import GenericSettingsPage
    import ini_field_specs
    key, label, specs = ini_field_specs.CATEGORIES[1]
    page = GenericSettingsPage(label, specs)
    rows = [f for f in page.card.findChildren(QFrame) if f.objectName().startswith("SettingRow")]
    assert len(rows) == len([s for s in specs if not s.key.startswith("__")]) or len(rows) == len(specs)


def test_card_form_pages_wrap_their_fields():
    from ui.settings_alerts_page import SettingsAlertsPage
    page = SettingsAlertsPage()
    assert page.form_card.objectName() == "Card"
    assert page.form_layout.parentWidget() is page.form_card


def test_default_theme_buttons_use_the_mockups_exact_colors():
    from ui.theme import button_colors
    c = button_colors("#c9752f", "#f28b80", "#1d1d1d")
    assert (c["fill"], c["danger_border"], c["danger_text"]) == ("#a9581c", "#6b2c27", "#f19a91")
    qss = build_stylesheet()
    assert "min-height: 38px" in qss and "#a9581c" in qss


def test_custom_accent_buttons_stay_readable():
    from ui.theme import button_colors
    fill = button_colors("#f0e040", "#ff0000", "#101010")["fill"]
    assert 1.05 / (_luminance(fill) + 0.05) >= 4.5


def test_dashboard_restart_is_the_primary_button():
    from ui.dashboard_page import DashboardPage
    page = DashboardPage()
    assert page.restart_btn.objectName() == "PrimaryButton"
    assert page.start_btn.objectName() != "PrimaryButton"


def test_mod_switch_updates_in_place_without_rebuilding_or_selecting():
    from PySide6.QtWidgets import QCheckBox
    from ui.mods_page import ModsPage
    page = ModsPage()
    saved = []
    page.on_changed = lambda srv: saved.append([m["enabled"] for m in srv.mods])
    server = models.ServerConfig(id="s1", name="x")
    server.mods = [{"id": "1", "name": "A", "enabled": True}, {"id": "2", "name": "B", "enabled": True}]
    page.set_server(server)
    row = page.list_widget.itemWidget(page.list_widget.item(1))
    row.findChildren(QCheckBox)[0].setChecked(False)
    assert page.list_widget.itemWidget(page.list_widget.item(1)) is row   # same widget: no rebuild
    assert page.list_widget.currentRow() == -1                            # nothing got selected
    assert page.list_widget.item(1).text().startswith("✗")
    assert row.name_label.property("tone") == "dim"
    assert page.list_widget.item(1).toolTip() == ""                       # no raw-text popup
    QApplication.processEvents()
    assert saved == [[True, False]]                                       # saved once, after repaint


# ---------------------------------------------------------- smoothness --

def test_switch_images_are_pngs_with_hidpi_versions():
    import os
    import re
    urls = re.findall(r'url\("([^"]+)"\)', build_stylesheet())
    assert urls and all(u.endswith(".png") and os.path.exists(u) for u in urls)
    assert all(os.path.exists(u[:-4] + "@2x.png") for u in urls)


def test_timer_refreshes_skip_unchanged_widgets(monkeypatch):
    from ui.sidebar import Sidebar
    from ui.dashboard_page import AutomationTile
    bar = Sidebar()
    bar.set_servers([models.ServerConfig(id="a", name="A")], "a")
    polished = []
    dot = bar._server_dots["a"]
    monkeypatch.setattr(dot.style(), "polish", lambda w: polished.append(w), raising=False)
    bar.set_server_status("a", True, 2)
    count = len(polished)
    bar.set_server_status("a", True, 2)
    bar.set_server_status("a", True, 2)
    assert len(polished) == count          # unchanged: no restyle
    assert dot.property("running") is True

    tile = AutomationTile("restart", "Scheduled restarts", "RS", "#fff")
    tile.set_state(True, "Daily")
    label_before = tile.state_label.text()
    tile.state_label.setText("sentinel")
    tile.set_state(True, "Daily")          # same state: left alone
    assert tile.state_label.text() == "sentinel" and label_before == "● Daily"


def test_no_per_widget_stylesheets_on_timer_driven_widgets():
    from ui.sidebar import Sidebar
    bar = Sidebar()
    bar.set_servers([models.ServerConfig(id="a", name="A")], "a")
    bar.set_server_status("a", True, 1)
    assert bar._server_dots["a"].styleSheet() == ""
    assert bar._server_buttons["a"].styleSheet() == ""


# --------------------------------------------------------- setup wizard --

def _wizard():
    from ui.setup_wizard import SetupWizard
    return SetupWizard(models.ServerConfig(name="x"), get_reserved_ports=lambda: set())


def test_wizard_uses_the_themed_style_and_step_list():
    from PySide6.QtWidgets import QWizard
    wiz = _wizard()
    assert wiz.wizardStyle() == QWizard.ClassicStyle      # no native black header / white footer
    assert wiz.sideWidget() is wiz.steps_panel
    assert wiz.button(QWizard.NextButton).property("primary") is True
    badge, label = wiz.steps_panel._rows[0]
    assert badge.property("state") == "current"
    wiz.steps_panel.set_current(2)
    assert wiz.steps_panel._rows[0][0].text() == "✓" and wiz.steps_panel._rows[2][1].property("state") == "current"


def test_install_page_shows_spinner_and_percent():
    wiz = _wizard()
    page = wiz.install_page
    page._on_progress_percent(42.7)
    assert "42%" in page.status_label.text()
    page._on_finished(True)
    assert page.spinner.isHidden() and page.status_label.text() == "Installed."


def test_radio_buttons_get_visible_indicators():
    import os
    import re
    qss = build_stylesheet()
    urls = re.findall(r'QRadioButton::indicator:checked \{ image: url\("([^"]+)"\)', qss)
    assert urls and os.path.exists(urls[0])


def test_backup_restore_buttons_are_never_clipped_and_line_up(monkeypatch):
    import datetime
    import types
    import backup_manager
    from ui.backups_page import BackupsPage as BackupsWidget
    entries = [types.SimpleNamespace(when=datetime.datetime(2026, 10, 3, 4, 0), trigger=t, size_label="1 MB", path=f"/tmp/{i}")
               for i, t in enumerate(["scheduled", "before update", "manual"])]
    monkeypatch.setattr(backup_manager, "list_backups", lambda d: entries)
    w = BackupsWidget()
    w.setStyleSheet(build_stylesheet())
    w.set_server(models.ServerConfig(name="x", backup_destination="/tmp/b"))
    buttons = [w.table.cellWidget(r, 3).findChildren(QPushButton)[0] for r in range(3)]
    for b in buttons:
        assert b.minimumWidth() >= b.fontMetrics().horizontalAdvance("Restore") + 28
    assert w.table.columnWidth(3) >= buttons[0].minimumWidth() + 18
    assert len({b.minimumWidth() for b in buttons}) == 1


def test_main_window_styles_every_table():
    import mod_manager
    from PySide6.QtWidgets import QTableWidget
    from ui.main_window import MainWindow
    win = MainWindow(config=models.AppConfig())
    try:
        tables = win.stack.findChildren(QTableWidget)
        assert tables and all(not t.showGrid() for t in tables)
    finally:
        win.close()
