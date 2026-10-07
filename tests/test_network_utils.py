from __future__ import annotations

import socket

import network_utils


def test_is_udp_port_free_true_when_actually_free():
    # Bind to port 0 to get a genuinely free ephemeral port from the OS,
    # then release it and confirm the function agrees it's free.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    assert network_utils.is_udp_port_free(port) is True


def test_is_udp_port_free_false_while_held():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    try:
        assert network_utils.is_udp_port_free(port) is False
    finally:
        s.close()


def test_find_free_port_pair_avoids_reserved():
    game, query = network_utils.find_free_port_pair(20000, reserved={20000, 20001, 27015})
    assert game not in {20000, 20001, 27015}
    assert query not in {20000, 20001, 27015}
    assert game != query


def test_find_free_port_pair_returns_distinct_ports():
    game, query = network_utils.find_free_port_pair(21000, reserved=set(), start_query_port=21000)
    assert game != query


def test_list_local_ipv4s_returns_a_set_with_no_loopback():
    ips = network_utils.list_local_ipv4s()
    assert isinstance(ips, set)
    assert all(not ip.startswith("127.") for ip in ips)


# ---------------------------------------------------------- default gateway --

_ROUTE_PRINT = """===========================================================================
Liste des interfaces
===========================================================================
IPv4 Table de routage
===========================================================================
Itin\u00e9raires actifs :
Destination r\u00e9seau    Masque r\u00e9seau  Adr. passerelle   Adr. interface M\u00e9trique
          0.0.0.0          0.0.0.0         10.8.0.1        10.8.0.6      5
          0.0.0.0          0.0.0.0      192.168.1.1    192.168.1.50     25
        127.0.0.0        255.0.0.0         On-link         127.0.0.1    331
===========================================================================
Itin\u00e9raires persistants :
  Adresse r\u00e9seau    Masque r\u00e9seau  Adresse passerelle M\u00e9trique
          0.0.0.0          0.0.0.0      192.168.1.1  Par d\u00e9faut
"""


def _fake_route(monkeypatch, output=_ROUTE_PRINT):
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, output, ""))


def test_default_routes_parse_regardless_of_display_language(monkeypatch):
    _fake_route(monkeypatch)
    assert network_utils._default_routes() == [("10.8.0.1", "10.8.0.6", 5), ("192.168.1.1", "192.168.1.50", 25)]


def test_get_default_gateway_matches_the_adapter_of_local_ip(monkeypatch):
    """Not just the first gateway listed -- that's the VPN here."""
    _fake_route(monkeypatch)
    assert network_utils.get_default_gateway("192.168.1.50") == "192.168.1.1"


def test_get_default_gateway_skips_virtual_adapters_when_local_ip_unknown(monkeypatch):
    _fake_route(monkeypatch)
    monkeypatch.setattr(network_utils, "_iface_name_by_ip",
                        lambda: {"10.8.0.6": "OpenVPN TAP-Windows6", "192.168.1.50": "Ethernet"})
    assert network_utils.get_default_gateway("172.20.0.9") == "192.168.1.1"


def test_get_default_gateway_falls_back_to_dot_one_guess_when_route_unavailable(monkeypatch):
    import subprocess
    def _raise(*a, **k):
        raise OSError("route not found")
    monkeypatch.setattr(subprocess, "run", _raise)

    assert network_utils.get_default_gateway(local_ip="192.168.50.77") == "192.168.50.1"


def test_get_default_gateway_falls_back_when_no_default_route(monkeypatch):
    _fake_route(monkeypatch, "no useful output here\n")
    assert network_utils.get_default_gateway(local_ip="10.20.30.40") == "10.20.30.1"


def test_get_default_gateway_uses_get_local_ip_when_no_local_ip_given(monkeypatch):
    import subprocess
    def _raise(*a, **k):
        raise OSError("route not found")
    monkeypatch.setattr(subprocess, "run", _raise)
    monkeypatch.setattr(network_utils, "get_local_ip", lambda: "172.16.5.9")

    assert network_utils.get_default_gateway() == "172.16.5.1"


def test_get_default_gateway_none_when_ip_shape_is_unexpected(monkeypatch):
    import subprocess
    def _raise(*a, **k):
        raise OSError("route not found")
    monkeypatch.setattr(subprocess, "run", _raise)

    assert network_utils.get_default_gateway(local_ip="not-an-ip") is None


# ------------------------------------------------------------- local ip --

def test_get_local_ip_prefers_the_physical_adapter_over_a_vpn(monkeypatch):
    _fake_route(monkeypatch)
    monkeypatch.setattr(network_utils, "_iface_name_by_ip",
                        lambda: {"10.8.0.6": "NordLynx", "192.168.1.50": "Ethernet"})
    assert network_utils.get_local_ip() == "192.168.1.50"


def test_get_local_ip_never_returns_apipa(monkeypatch):
    _fake_route(monkeypatch, "")
    monkeypatch.setattr(network_utils, "_iface_name_by_ip", lambda: {"169.254.10.2": "Ethernet"})
    monkeypatch.setattr(network_utils, "_udp_route_guess", lambda: "169.254.10.2")
    import socket
    monkeypatch.setattr(socket, "gethostbyname", lambda h: "169.254.10.2")
    assert network_utils.get_local_ip() == "127.0.0.1"


# ----------------------------------------------------------- ip helpers --

def test_ip_classification():
    assert network_utils.is_private_ipv4("192.168.0.4")
    assert network_utils.is_private_ipv4("172.31.1.1") and not network_utils.is_private_ipv4("172.32.1.1")
    assert network_utils.is_cgnat_ipv4("100.64.0.1") and not network_utils.is_cgnat_ipv4("100.128.0.1")
    assert network_utils.is_non_public_ipv4("10.1.2.3") and not network_utils.is_non_public_ipv4("203.0.113.9")
    assert not network_utils.is_usable_lan_ipv4("169.254.3.3")
    assert not network_utils.is_valid_ipv4("300.1.1.1")


# ---------------------------------------------------------------- ports --

def test_port_free_rejects_out_of_range():
    assert network_utils.is_udp_port_free(65536) is False
    assert network_utils.is_tcp_port_free(0) is False


def test_tcp_port_free_false_while_held():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    s.listen(1)
    try:
        assert network_utils.is_tcp_port_free(s.getsockname()[1]) is False
    finally:
        s.close()


def test_find_free_port_pair_never_returns_game_port_65535():
    import pytest
    with pytest.raises(RuntimeError):
        network_utils.find_free_port_pair(65535, set(), max_attempts=1)
