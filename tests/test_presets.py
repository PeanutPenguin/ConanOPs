"""Presets and Simple/Advanced mode (1.0.14)."""
import sys

import pytest

import models
import presets

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def win(monkeypatch, tmp_path):
    import mod_manager
    import network_utils
    monkeypatch.setattr(mod_manager, "write_modlist", lambda *a, **k: None)
    monkeypatch.setattr(network_utils, "is_udp_port_free", lambda p, host="0.0.0.0": True)
    from ui.main_window import MainWindow
    cfg = models.AppConfig()
    monkeypatch.setattr(cfg, "save", lambda *a, **k: None)
    s = cfg.add_server("Chud")
    s.install_dir = str(tmp_path / "srv")
    w = MainWindow(config=cfg)
    w._load_active_server()
    yield w
    w._really_quit = True
    w.close()


# ------------------------------------------------------------- matching --

def test_every_preset_matches_itself_and_nothing_else_does():
    for group in presets.ALL.values():
        ids = [o.id for o in group.options]
        assert len(ids) == len(set(ids))
        for o in group.options:
            got = presets.match(group, presets.values_for(group, o.id))
            # Two presets may share values (Quietest hours without any history); the first wins.
            assert got == o.id or presets.values_for(group, got) == presets.values_for(group, o.id)
        # One recommended choice, except where it's purely a preference (server type).
        assert sum(o.recommended for o in group.options) == (0 if group.key == "server_type" else 1), group.key


def test_custom_when_nothing_matches_and_floats_compare_loosely():
    g = presets.PLAY_STYLE
    vals = presets.values_for(g, "relaxed")
    vals["PlayerXPRateMultiplier"] = 2.0000001
    assert presets.match(g, vals) == "relaxed"
    vals["PlayerXPRateMultiplier"] = 3.0
    assert presets.match(g, vals) == presets.CUSTOM
    assert presets.match(g, {}) == presets.CUSTOM


def test_gameplay_preset_values_sit_on_the_sliders_steps():
    import ini_field_specs
    for o in presets.PLAY_STYLE.options:
        for key, v in o.values.items():
            spec = ini_field_specs.ALL_FIELDS_BY_KEY[key]
            if spec.kind == "float":
                assert spec.min <= v <= spec.max and abs(round(v * 10) - v * 10) < 1e-9, (o.id, key)


def test_choice_groups_and_resolve():
    g = presets.MOD_RECOVERY
    assert presets.values_for(g, "alert") == {"mod_recovery_mode": "alert"}
    assert presets.match(g, {"mod_recovery_mode": "start_without"}) == "start_without"
    r = presets.RESTART

    def resolve(oid, values):
        return dict(values, restart_start="01:00", restart_end="03:00") if oid == "quiet" else values
    assert presets.match(r, {"restart_enabled": True, "restart_start": "01:00", "restart_end": "03:00"},
                         resolve) == "quiet"
    assert presets.match(r, {"restart_enabled": True, "restart_start": "04:00", "restart_end": "06:00"},
                         resolve) == "early"


# --------------------------------------------------------------- widget --

def test_preset_group_widget_and_binding():
    from PySide6.QtWidgets import QCheckBox, QWidget
    from ui.preset_group import PresetBinding, PresetGroup
    details = QWidget()
    g = PresetGroup(presets.APP_UPDATES, details=details)
    a, b = QCheckBox(), QCheckBox()
    boxes = {"auto_check": a, "auto_install": b}
    PresetBinding(g, lambda: {k: x.isChecked() for k, x in boxes.items()},
                  lambda v: [boxes[k].setChecked(bool(x)) for k, x in v.items()],
                  [a.toggled, b.toggled])
    assert g.current() == "manual" and not g.custom_badge.isVisibleTo(g)
    g.buttons()["auto"].click()
    assert a.isChecked() and b.isChecked() and g.current() == "auto"
    a.setChecked(False)  # auto-install without checking matches nothing
    assert g.current() == presets.CUSTOM and g.custom_badge.isVisibleTo(g)
    assert not details.isVisibleTo(g) and g.advanced_link.isVisibleTo(g)
    g.set_advanced(True)
    assert details.isVisibleTo(g) and not g.advanced_link.isVisibleTo(g)
    asked = []
    g.advanced_requested.connect(lambda: asked.append(1))
    g.advanced_link.click()
    assert asked == [1]


# ------------------------------------------------------- the app itself --

def test_simple_mode_hides_advanced_things_and_is_remembered(win):
    assert win.config.advanced_mode is False
    console = win.sidebar._nav_buttons_by_key["console"]
    assert not console.isVisibleTo(win.sidebar)
    page = win.app_settings_page
    assert not page._section_buttons["lock"].isVisibleTo(page)
    assert not win.settings_alerts_page.form_container.isVisibleTo(win.settings_container)
    win.advanced_btn.click()
    assert win.config.advanced_mode is True
    assert console.isVisibleTo(win.sidebar) and page._section_buttons["lock"].isVisibleTo(page)
    win._on_nav_selected("console")
    win.simple_btn.click()
    assert win._current_nav == "dashboard"  # Console isn't available in Simple mode
    assert win.config.advanced_mode is False


def test_server_presets_stage_changes_for_the_save_bar(win):
    sections = win.server_sections
    assert sections.backups.current() == "standard"   # a new server's defaults
    sections.backups.buttons()["frequent"].click()
    assert win.settings_backups_page.interval_spin.value() == 2
    assert win.settings_container.save_bar.isVisibleTo(win.settings_container)  # staged, not yet saved
    assert win.config.get_active().backup_interval_hours == 6
    win.settings_container.apply_all()
    assert win.config.get_active().backup_interval_hours == 2
    win.settings_backups_page.daily_spin.setValue(9)
    assert sections.backups.current() == presets.CUSTOM
    sections.play_style.buttons()["boosted"].click()
    assert win.settings_rates_page._current_values()["PlayerXPRateMultiplier"] == pytest.approx(5.0)
    assert sections.play_style.current() == "boosted"
    sections.server_type.buttons()["pve"].click()
    assert win.settings_identity_page._current_values()["PVPEnabled"] is False


def test_mod_recovery_preset_is_the_app_setting(win):
    win.server_sections.mod_recovery.buttons()["alert"].click()
    assert win.config.mod_recovery_mode == "alert"
    combo = win.app_settings_page.mod_recovery_combo
    combo.setCurrentIndex(combo.findData("start_without"))
    assert win.server_sections.mod_recovery.current() == "start_without"


def test_app_presets_drive_the_same_switches(win, monkeypatch):
    page = win.app_settings_page
    calls = []
    monkeypatch.setattr(type(page), "_on_start_with_windows_toggled", lambda self, on: calls.append(("start", on)))
    # Rebind the toggled signal to the patched handler for this test.
    page.start_with_windows_checkbox.toggled.disconnect()
    page.start_with_windows_checkbox.toggled.connect(page._on_start_with_windows_toggled)
    page.start_with_windows_checkbox.toggled.connect(lambda *_: page._preset_bindings[0].refresh())
    page.keep_awake_checkbox.setChecked(True)
    assert page.run_preset.current() == "manual"
    page.start_with_windows_checkbox.setChecked(True)
    assert page.run_preset.current() == "signed_in" and calls == [("start", True)]


def test_search_opens_advanced_when_the_setting_lives_there(win):
    c = win.settings_container
    target = win.settings_alerts_page._fields["rcon_port"]
    c.show_setting("server", target)
    assert win.config.advanced_mode is True
    win.set_advanced_mode(False)
    win.app_settings_page.show_section("ddns")  # old key: now Remote Access
    assert win.app_settings_page.current_section() == "remote"
    win.app_settings_page.show_section("lock")
    assert win.config.advanced_mode is True


def test_network_ports_show_in_simple_mode_only_when_they_clash(win, monkeypatch):
    net = win.settings_network_page
    net.set_simple(True)
    net._check_port_conflict()
    assert not net.ports_box.isVisibleTo(net.form_container) or net._has_conflict
    import network_utils
    monkeypatch.setattr(network_utils, "is_udp_port_free", lambda p, host="0.0.0.0": False)
    net.game_port_spin.setValue(net.game_port_spin.value() + 10)
    assert net._has_conflict and net.ports_box.isVisibleTo(net.form_container)


def test_dashboard_shows_one_status_and_a_friendly_address(win, monkeypatch):
    from ui import dashboard_page as dp
    dash = win.dashboard_page
    monkeypatch.setitem(dp._PUBLIC_IP, "ip", "203.0.113.9")
    monkeypatch.setitem(dp._PUBLIC_IP, "at", 10 ** 12)
    dash._show_address()
    assert dash.connect_label.text() == "Friends connect to 203.0.113.9:7777"
    win.config.duckdns_domain, win.config.duckdns_token = "chud", "t"
    dash._show_address()
    assert "chud.duckdns.org:7777" in dash.connect_label.text()
    dash._copy_address()
    assert QApplication.clipboard().text() == "chud.duckdns.org:7777"
    dash._set_status("● Online", "on")
    assert not dash.status_label.isVisibleTo(dash)  # the window header shows it
    dash._set_status("● Not set up", "warn")
    assert dash.status_label.isVisibleTo(dash)
    assert not dash.automation_tiles["ddns"].isVisibleTo(dash)  # Advanced only
    win.config.get_active().bind_ip = "192.168.117.219"
    dash._show_address()
    assert dash.connect_label.text().endswith("On this network, use 192.168.117.219:7777")
    dash._copy_address()
    assert QApplication.clipboard().text() == "chud.duckdns.org:7777"  # Copy stays the friends' address
