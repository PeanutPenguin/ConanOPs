from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from ui.settings_alerts_page import SettingsAlertsPage


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def test_discord_status_checkbox_loads_committed_value():
    page = SettingsAlertsPage()
    page.load_committed({
        "rcon_enabled": False, "rcon_port": 25575, "rcon_password": "",
        "webhook_discord_url": "https://discord.com/api/webhooks/1/abc",
        "discord_status_enabled": True,
        "webhook_ntfy_url": "",
    })
    assert page.discord_status_check.isChecked() is True
