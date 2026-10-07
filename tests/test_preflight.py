from __future__ import annotations

import models
import network_utils
import preflight


def test_valid_non_default_bind_ip_is_preserved(monkeypatch):
    """A deliberately chosen secondary NIC shouldn't get silently
    overwritten just because it's not the OS's default-route pick."""
    monkeypatch.setattr(network_utils, "list_local_ipv4s", lambda: {"10.0.0.5", "192.168.1.50"})
    monkeypatch.setattr(network_utils, "get_local_ip", lambda: "10.0.0.5")
    s = models.ServerConfig(name="t", install_dir="", steamcmd_dir="", bind_ip="192.168.1.50")

    result = preflight.run_preflight(s)

    assert s.bind_ip == "192.168.1.50"
    assert not any("Bind IP" in r for r in result.repairs)


def test_genuinely_stale_bind_ip_is_repaired(monkeypatch):
    monkeypatch.setattr(network_utils, "list_local_ipv4s", lambda: {"10.0.0.5"})
    monkeypatch.setattr(network_utils, "get_local_ip", lambda: "10.0.0.5")
    s = models.ServerConfig(name="t", install_dir="", steamcmd_dir="", bind_ip="10.0.0.99")

    result = preflight.run_preflight(s)

    assert s.bind_ip == "10.0.0.5"
    assert any("stale" in r for r in result.repairs)


def test_empty_bind_ip_is_auto_detected(monkeypatch):
    monkeypatch.setattr(network_utils, "list_local_ipv4s", lambda: {"10.0.0.5"})
    monkeypatch.setattr(network_utils, "get_local_ip", lambda: "10.0.0.5")
    s = models.ServerConfig(name="t", install_dir="", steamcmd_dir="", bind_ip="")

    preflight.run_preflight(s)

    assert s.bind_ip == "10.0.0.5"


def test_enumeration_failure_does_not_cause_a_false_repair(monkeypatch):
    """If interface enumeration itself fails (empty set), we can't tell
    stale from valid -- so leave bind_ip alone rather than guess."""
    monkeypatch.setattr(network_utils, "list_local_ipv4s", lambda: set())
    s = models.ServerConfig(name="t", install_dir="", steamcmd_dir="", bind_ip="192.168.9.9")

    result = preflight.run_preflight(s)

    assert s.bind_ip == "192.168.9.9"
    assert not any("Bind IP" in r for r in result.repairs)


def test_loopback_only_detection_is_flagged_not_silently_accepted(monkeypatch):
    """get_local_ip() falling all the way through to 127.0.0.1 means
    detection genuinely failed -- that must surface as a real problem,
    not get saved as though it were a successful auto-repair (which
    would silently leave the server bound only to itself)."""
    monkeypatch.setattr(network_utils, "list_local_ipv4s", lambda: {"10.0.0.5"})
    monkeypatch.setattr(network_utils, "get_local_ip", lambda: "127.0.0.1")
    s = models.ServerConfig(name="t", install_dir="", steamcmd_dir="", bind_ip="")

    result = preflight.run_preflight(s)

    assert s.bind_ip == ""  # not overwritten with loopback
    assert result.ok is False
    assert any("loopback" in p.lower() or "127.0.0.1" in p for p in result.problems)
    assert not any("Bind IP" in r for r in result.repairs)


def test_stale_bind_ip_with_only_loopback_replacement_is_flagged(monkeypatch):
    monkeypatch.setattr(network_utils, "list_local_ipv4s", lambda: {"10.0.0.5"})
    monkeypatch.setattr(network_utils, "get_local_ip", lambda: "127.0.0.1")
    s = models.ServerConfig(name="t", install_dir="", steamcmd_dir="", bind_ip="10.0.0.99")

    result = preflight.run_preflight(s)

    assert s.bind_ip == "10.0.0.99"  # left alone, not overwritten with loopback
    assert result.ok is False
    assert not any("stale" in r for r in result.repairs)
