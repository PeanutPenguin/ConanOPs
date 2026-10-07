from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import models


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _app_page(cfg):
    from tests.test_app_settings_startup import _FakeWebControlServer
    from ui.app_settings_page import AppSettingsPage
    return AppSettingsPage(cfg, lambda: None, lambda p: None, lambda: None, install_dir="/tmp/fake-install",
                           on_update_installed=lambda: None, web_control_server=_FakeWebControlServer())


def test_alerts_live_in_app_settings_and_load_saved_values():
    cfg = models.AppConfig(alert_discord_url="https://discord.com/api/webhooks/1/abc", discord_status_enabled=True)
    page = _app_page(cfg)
    assert page.alert_discord_edit.text() == "https://discord.com/api/webhooks/1/abc"
    assert page.alert_discord_status_check.isChecked() is True
    assert any(k == "alerts" for k, _l, _g in page.SECTIONS)


def test_saving_alerts_validates_and_tells_the_window():
    cfg = models.AppConfig()
    page = _app_page(cfg)
    seen = []
    page.on_alerts_changed = seen.append
    page.alert_discord_edit.setText("https://example.com/not-discord")
    page._on_alert_links_changed()
    assert cfg.alert_discord_url == "" and not page.alert_error_label.isHidden()
    page.alert_discord_edit.setText("https://discord.com/api/webhooks/1/abc")
    page.alert_ntfy_edit.setText("https://ntfy.sh/private-topic")
    page._on_alert_links_changed()
    assert cfg.alert_discord_url == "https://discord.com/api/webhooks/1/abc"
    assert cfg.alert_ntfy_url == "https://ntfy.sh/private-topic"
    assert seen == [True] and page.alert_error_label.isHidden()


def test_old_per_server_links_carry_over(tmp_path):
    import json
    s = models.ServerConfig(id="a", name="A", webhook_discord_url="https://discord.com/api/webhooks/1/x",
                            discord_status_enabled=True, webhook_ntfy_url="https://ntfy.sh/t")
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"servers": [models.ServerConfig(id="b", name="B").to_dict(), s.to_dict()]}))
    cfg = models.AppConfig.load(str(path))
    assert (cfg.alert_discord_url, cfg.alert_ntfy_url, cfg.discord_status_enabled) == (
        "https://discord.com/api/webhooks/1/x", "https://ntfy.sh/t", True)
    cfg.alert_ntfy_url = ""
    cfg.save(str(path))
    assert models.AppConfig.load(str(path)).alert_ntfy_url == ""  # not re-copied from the old field
