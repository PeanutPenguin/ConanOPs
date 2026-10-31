"""Router forwarding beyond plain UPnP (1.0.17): finding the router more
reliably, NAT-PMP/PCP fallback, refused changes, quick re-checks, and NAT
loopback in Diagnostics."""
import struct
import time

import network_setup
import port_mapping

IP_SVC = "urn:schemas-upnp-org:service:WANIPConnection:1"


# --------------------------------------------------------- NAT-PMP / PCP --
class FakeRouter:
    """Answers NAT-PMP, PCP, or neither, the way a real router would."""

    def __init__(self, speaks=port_mapping.NAT_PMP, taken=(), ext_ip="203.0.113.7"):
        self.speaks, self.taken, self.ext_ip = speaks, set(taken), ext_ip
        self.sent = []

    def __call__(self, gateway, local_ip, packet, tries=None):
        self.sent.append(packet)
        version = packet[0]
        if version == 0:  # NAT-PMP
            if self.speaks == port_mapping.PCP:
                return struct.pack("!BBH", 2, 0x80, 0) + b"\x01" + b"\x00" * 20  # "use PCP"
            if self.speaks != port_mapping.NAT_PMP:
                return None
            if packet[1] == 0:
                return struct.pack("!BBHI", 0, 128, 0, 1) + bytes(map(int, self.ext_ip.split(".")))
            _v, _op, _r, internal, ext, lifetime = struct.unpack("!BBHHHI", packet)
            given = ext + 1 if ext in self.taken and lifetime else ext
            return struct.pack("!BBHIHHI", 0, 129, 0, 1, internal, given if lifetime else 0, lifetime)
        if version == 2 and self.speaks == port_mapping.PCP:
            if packet[1] == 0:  # ANNOUNCE
                return struct.pack("!BBBBII", 2, 0x80, 0, 0, 0, 1) + b"\x00" * 12
            lifetime = struct.unpack("!I", packet[4:8])[0]
            nonce = packet[24:36]
            internal, ext = struct.unpack("!HH", packet[40:44])
            given = ext + 1 if ext in self.taken and lifetime else ext
            head = struct.pack("!BBBBII", 2, 0x81, 0, 0, lifetime, 1) + b"\x00" * 12
            return head + nonce + struct.pack("!B3xHH", 17, internal, given) + port_mapping._v4mapped(self.ext_ip)
        return None


def test_nat_pmp_maps_the_same_port():
    router = FakeRouter()
    m = port_mapping.map_udp("192.168.1.1", "192.168.1.50", 7779, key="s2", exchange=router)
    assert m.ok and m.method == port_mapping.NAT_PMP and m.external_port == 7779
    assert port_mapping.pmp_external_ip("192.168.1.1", "192.168.1.50", router) == "203.0.113.7"


def test_pcp_is_used_when_the_router_says_so_and_nonce_is_stable():
    router = FakeRouter(speaks=port_mapping.PCP)
    m = port_mapping.map_udp("192.168.1.1", "192.168.1.50", 27016, key="s2", exchange=router)
    assert m.ok and m.method == port_mapping.PCP and m.external_port == 27016
    assert port_mapping.nonce_for("s2", 27016) == port_mapping.nonce_for("s2", 27016)
    assert port_mapping.nonce_for("s2", 27016) != port_mapping.nonce_for("s1", 27016)
    assert port_mapping.available("192.168.1.1", "192.168.1.50", router) == port_mapping.PCP


def test_a_taken_port_is_not_left_forwarded_elsewhere():
    router = FakeRouter(taken={7779})
    m = port_mapping.map_udp("192.168.1.1", "192.168.1.50", 7779, key="s2", exchange=router)
    assert not m.ok and "taken" in m.error
    # It asked again with lifetime 0 to remove the forward on the wrong port.
    assert struct.unpack("!I", router.sent[-1][8:12])[0] == 0


def test_silent_router_means_no_method():
    m = port_mapping.map_udp("192.168.1.1", "192.168.1.50", 7779, exchange=FakeRouter(speaks=""))
    assert not m.ok and m.method == ""
    assert port_mapping.available("192.168.1.1", "192.168.1.50", FakeRouter(speaks="")) == ""


# ---------------------------------------------------------- reconcile --
def _no_upnp(monkeypatch):
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: None)
    monkeypatch.setattr(network_setup, "_gateway_for", lambda local_ip=None: "192.168.1.1")


def test_reconcile_falls_back_to_nat_pmp(monkeypatch):
    _no_upnp(monkeypatch)
    router = FakeRouter()
    monkeypatch.setattr(port_mapping, "_exchange", router)
    r = network_setup.reconcile_upnp("s2", "192.168.1.50", 7779, 27016)
    assert r["upnp_available"] and r["method"] == "NAT-PMP"
    assert r["game_port_forwarded"] and r["game_port_plus_one_forwarded"] and r["query_port_forwarded"]
    assert network_setup._pmp_mapped["s2"] == {7779, 7780, 27016}
    # Removing the server removes them too.
    assert network_setup.remove_upnp_mappings("s2", local_ip="192.168.1.50") == 3
    assert not network_setup._pmp_mapped["s2"]


def test_reconcile_gives_up_quickly_on_a_silent_router(monkeypatch):
    _no_upnp(monkeypatch)
    router = FakeRouter(speaks="")
    monkeypatch.setattr(port_mapping, "_exchange", router)
    r = network_setup.reconcile_upnp("s2", "192.168.1.50", 7779, 27016)
    assert not r["upnp_available"] and r["method"] == ""
    assert len(router.sent) == 2  # one NAT-PMP and one PCP try, not one per port


def test_upnp_gaps_are_filled_by_nat_pmp(monkeypatch):
    dev = network_setup.UpnpDevice("http://r/ctl", IP_SVC, name="AmpliFi HD")
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: dev)
    monkeypatch.setattr(network_setup, "_gateway_for", lambda local_ip=None: "192.168.1.1")
    monkeypatch.setattr(network_setup, "remove_upnp_mappings", lambda *a, **k: 0)
    monkeypatch.setattr(network_setup, "get_external_ip", lambda device, timeout=5.0: "203.0.113.7")
    monkeypatch.setattr(network_setup, "map_port",
                        lambda d, port, *a, **k: network_setup.MAP_OK if port == 7779 else network_setup.MAP_FAILED)
    monkeypatch.setattr(port_mapping, "_exchange", FakeRouter())
    r = network_setup.reconcile_upnp("s2", "192.168.1.50", 7779, 27016)
    assert r["router"] == "AmpliFi HD" and r["method"] == "UPnP + NAT-PMP"
    assert r["game_port_forwarded"] and r["game_port_plus_one_forwarded"] and r["query_port_forwarded"]


def test_refused_changes_are_reported(monkeypatch):
    dev = network_setup.UpnpDevice("http://r/ctl", IP_SVC)
    monkeypatch.setattr(network_setup, "_soap", lambda d, action, args, timeout=5.0:
                        (None, 606) if action == "AddPortMapping" else (None, None))
    assert network_setup.map_port(dev, 7779, "192.168.1.50", "ConanOps s2 Game", server_id="s2") \
        == network_setup.MAP_DENIED
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: dev)
    monkeypatch.setattr(network_setup, "remove_upnp_mappings", lambda *a, **k: 0)
    r = network_setup.reconcile_upnp("s2", "192.168.1.50", 7779, 27016)
    assert r["denied"] and not r["game_port_forwarded"]


def test_a_router_that_dislikes_symbols_gets_a_plain_description(monkeypatch):
    dev = network_setup.UpnpDevice("http://r/ctl", IP_SVC)
    descs = []

    def soap(d, action, args, timeout=5.0):
        if action == "AddPortMapping":
            desc = dict(args)["NewPortMappingDescription"]
            descs.append(desc)
            return ({}, None) if "+" not in desc else (None, 501)
        return None, None
    monkeypatch.setattr(network_setup, "_soap", soap)
    assert network_setup.map_port(dev, 7780, "192.168.1.50", "ConanOps s2 Game+1", server_id="s2") \
        == network_setup.MAP_OK
    assert descs[-1] == "ConanOps s2 GamePlus1"


def test_other_wan_services_are_tried(monkeypatch):
    first = network_setup.UpnpDevice("http://r/ppp", "urn:schemas-upnp-org:service:WANPPPConnection:1")
    second = network_setup.UpnpDevice("http://r/ip", IP_SVC)
    network_setup._last_candidates["192.168.1.50"] = [first, second]
    monkeypatch.setattr(network_setup, "discover_igd", lambda timeout=3.0, local_ip=None: first)
    monkeypatch.setattr(network_setup, "remove_upnp_mappings", lambda *a, **k: 0)
    monkeypatch.setattr(network_setup, "get_external_ip", lambda device, timeout=5.0: None)
    monkeypatch.setattr(network_setup, "map_port", lambda d, *a, **k:
                        network_setup.MAP_OK if d is second else network_setup.MAP_FAILED)
    r = network_setup.reconcile_upnp("s2", "192.168.1.50", 7779, 27016)
    assert r["game_port_forwarded"] and r["query_port_forwarded"] and r["method"] == "UPnP"


# ---------------------------------------------------------- discovery --
def test_discovery_remembers_the_router_and_falls_back_to_asking_it_directly(monkeypatch):
    dev = network_setup.UpnpDevice("http://192.168.1.1:5000/ctl", IP_SVC, "http://192.168.1.1:5000/rootDesc.xml")
    asked = []

    def ssdp(timeout, local_ip, unicast_to=None):
        asked.append(unicast_to)
        return []  # multicast blocked, and the router ignores searches
    monkeypatch.setattr(network_setup, "_ssdp_locations", ssdp)
    monkeypatch.setattr(network_setup, "_gateway_for", lambda local_ip=None: "192.168.1.1")
    monkeypatch.setattr(network_setup, "_probe_common_locations", lambda gw, timeout=1.0: [dev.location])
    monkeypatch.setattr(network_setup, "_services_at", lambda loc, timeout: [dev] if loc == dev.location else [])
    monkeypatch.setattr(network_setup, "_soap", lambda *a, **k: ({"NewConnectionStatus": "Connected"}, None))
    assert network_setup.discover_igd(local_ip="192.168.1.50") == dev
    assert asked == [None, "192.168.1.1"]
    # Next time the remembered router answers without any search.
    asked.clear()
    assert network_setup.discover_igd(local_ip="192.168.1.50") == dev
    assert asked == []


def test_router_name_is_read_from_its_description(monkeypatch):
    import urllib.request

    class Resp:
        def __init__(self, body):
            self.body = body

        def read(self, *a):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    desc = (b'<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
            b'<friendlyName>AmpliFi HD</friendlyName><serviceList><service>'
            b'<serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>'
            b'<controlURL>/ctl</controlURL></service></serviceList></device></root>')
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=0: Resp(desc))
    assert network_setup._services_at("http://192.168.1.1:5000/rootDesc.xml", 1.0)[0].name == "AmpliFi HD"


# ------------------------------------------------------- staying alive --
def test_quick_check_spots_a_lost_forward(monkeypatch):
    dev = network_setup.UpnpDevice("http://r/ctl", IP_SVC)
    network_setup._last_device["192.168.1.50"] = dev
    table = {7779: "192.168.1.50", 7780: "192.168.1.50", 27016: "192.168.1.50"}
    monkeypatch.setattr(network_setup, "get_port_mapping", lambda d, port, proto="UDP", timeout=5.0:
                        {"NewInternalClient": table[port]} if port in table else None)
    assert network_setup.forwards_intact("s2", "192.168.1.50", 7779, 27016)
    del table[27016]  # the router rebooted and forgot one
    assert not network_setup.forwards_intact("s2", "192.168.1.50", 7779, 27016)


def test_nat_pmp_forwards_are_renewed_in_time(monkeypatch):
    network_setup._pmp_mapped["s2"] = {7779, 7780, 27016}
    network_setup._pmp_renewed["s2"] = time.monotonic()
    assert network_setup.forwards_intact("s2", "192.168.1.50", 7779, 27016)
    network_setup._pmp_renewed["s2"] = time.monotonic() - network_setup._PMP_RENEW_SECONDS - 1
    assert not network_setup.forwards_intact("s2", "192.168.1.50", 7779, 27016)
    assert network_setup._PMP_RENEW_SECONDS * 2 < port_mapping.LIFETIME
