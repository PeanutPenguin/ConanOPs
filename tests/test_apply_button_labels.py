from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from ini_field_specs import FieldSpec
from ui.base_settings_page import SettingsPageBase
from ui.generic_settings_page import GenericSettingsPage
from ui.settings_backups_page import SettingsBackupsPage
from ui.settings_restart_page import SettingsRestartPage
from ui.settings_alerts_page import SettingsAlertsPage
from ui.settings_network_page import SettingsNetworkPage


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def test_default_requires_restart_label():
    page = SettingsPageBase("Some Gameplay Page")
    assert page.apply_btn.text() == "Apply on Next Restart"


def test_requires_restart_false_label():
    page = SettingsPageBase("Some Live Page", requires_restart=False)
    assert page.apply_btn.text() == "Apply"


def test_generic_settings_page_defaults_to_restart_required():
    """Every gameplay/identity category writes into the server's own
    .ini files -- these must keep the restart-required label."""
    specs = [FieldSpec("SomeKey", "Some Key", "float", 1.0, "", min=0.0, max=10.0, step=0.1, decimals=1)]
    page = GenericSettingsPage("Combat", specs)
    assert page.apply_btn.text() == "Apply on Next Restart"


def test_network_page_requires_restart():
    page = SettingsNetworkPage(get_reserved_ports=lambda: set())
    assert page.apply_btn.text() == "Apply on Next Restart"


def test_backups_page_does_not_require_restart():
    """Pure ConanOps scheduler settings -- never written to any Conan
    Exiles .ini file, so labeling this as restart-gated would be
    actively wrong, not just imprecise."""
    page = SettingsBackupsPage()
    assert page.apply_btn.text() == "Apply"


def test_restart_schedule_page_does_not_require_restart():
    """Ironic name, but the SCHEDULE WINDOW itself is a ConanOps-side
    setting read live -- only the eventual restart it triggers is a
    server restart, not applying this setting."""
    page = SettingsRestartPage(get_hourly_activity=lambda: [0] * 24)
    assert page.apply_btn.text() == "Apply"


def test_alerts_page_does_not_overclaim_restart_for_webhooks():
    """Mixed page: RCON needs a restart, webhooks don't. The page-level
    button must not claim the whole page needs one -- that's scoped to
    an inline note next to the RCON fields specifically instead."""
    page = SettingsAlertsPage()
    assert page.apply_btn.text() == "Apply"


def test_alerts_page_has_a_restart_note_scoped_to_rcon():
    from PySide6.QtWidgets import QLabel
    page = SettingsAlertsPage()
    all_text = " ".join(label.text() for label in page.findChildren(QLabel))
    assert "next time the server restarts" in all_text
    assert "immediately" in all_text
