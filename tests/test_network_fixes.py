"""Regression tests for the networking/setup-wizard review fixes."""
from __future__ import annotations

import sys

import pytest
from PySide6.QtWidgets import QApplication

import dynamic_dns
import network_utils


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _page(monkeypatch, reserved=frozenset(), local_ips=frozenset({"192.168.1.50"})):
    from ui.settings_network_page import SettingsNetworkPage
    monkeypatch.setattr(network_utils, "is_udp_port_free", lambda port, host="0.0.0.0": True)
    monkeypatch.setattr(network_utils, "list_local_ipv4s", lambda: set(local_ips))
    page = SettingsNetworkPage(get_reserved_ports=lambda: set(reserved))
    page.load_committed({"name": "Chud", "password": "", "game_port": 7777, "query_port": 27015,
                         "bind_ip": "192.168.1.50", "max_players": 40})
    return page


def test_query_port_equal_to_game_plus_one_is_a_conflict(monkeypatch):
    page = _page(monkeypatch)
    page.query_port_spin.setValue(7778)
    assert page._has_conflict and not page.can_apply()


def test_game_plus_one_reserved_by_another_server_is_a_conflict(monkeypatch):
    page = _page(monkeypatch, reserved={7777, 7778, 27016})
    page.game_port_spin.setValue(7776)  # 7777 belongs to another server
    assert page._has_conflict


def test_game_port_max_leaves_room_for_plus_one(monkeypatch):
    page = _page(monkeypatch)
    assert page.game_port_spin.maximum() == 65534


def test_bind_ip_must_be_a_local_address(monkeypatch):
    page = _page(monkeypatch)
    page.ip_edit.setText("203.0.113.5")
    assert not page.can_apply()
    page.ip_edit.setText("not an ip")
    assert not page.can_apply()
    page.ip_edit.setText("192.168.1.50")
    assert page.can_apply()
    page.ip_edit.setText("")  # empty = auto-detect
    assert page.can_apply()


def test_fix_port_conflict_does_not_crash_when_search_fails(monkeypatch):
    page = _page(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("none free")
    monkeypatch.setattr(network_utils, "find_free_port_pair", boom)
    page._fix_port_conflict()  # must not raise
    assert "Couldn't find free ports" in page.conflict_label.text()


def test_repair_button_calls_hook(monkeypatch):
    page = _page(monkeypatch)
    calls = []
    page.on_repair_network = lambda: calls.append(1)
    page._on_repair_clicked()
    assert calls == [1]
    assert not page.repair_btn.isEnabled()
    page.repair_finished()
    assert page.repair_btn.isEnabled()


@pytest.mark.parametrize("body", ["OK\n203.0.113.4\n\nUPDATED", "OK 203.0.113.4  UPDATED"])
def test_duckdns_parses_both_response_layouts(monkeypatch, body):
    import urllib.request

    class R:
        def read(self):
            return body.encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: R())
    result = dynamic_dns.update("mydomain", "token")
    assert result.ok and result.ip == "203.0.113.4"


def test_preflight_does_not_repair_to_an_apipa_address(monkeypatch):
    import models
    import preflight
    import process_manager
    import steamcmd
    monkeypatch.setattr(network_utils, "list_local_ipv4s", lambda: {"169.254.2.2"})
    monkeypatch.setattr(network_utils, "get_local_ip", lambda: "169.254.2.2")
    monkeypatch.setattr(steamcmd, "is_steamcmd_installed", lambda d: True)
    monkeypatch.setattr(steamcmd, "get_install_state", lambda d: 4)
    monkeypatch.setattr(process_manager, "server_exe_path", lambda d: __file__)
    server = models.ServerConfig(install_dir=".", steamcmd_dir=".", bind_ip="192.168.1.50")
    result = preflight.run_preflight(server)
    assert not result.ok
    assert server.bind_ip == "192.168.1.50"
