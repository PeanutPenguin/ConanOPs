"""
Router port forwarding without UPnP: NAT-PMP (RFC 6886) and PCP (RFC 6887).

Many routers that have UPnP turned off, or that refuse UPnP changes, still
answer one of these (Apple, many ISP routers, OpenWrt/pfSense/OPNsense with
miniupnpd). network_setup uses them for any port UPnP couldn't forward.

Both are small UDP request/response protocols sent to the router on port
5351. Mappings have a lifetime, so ConanOps renews them while a server runs
(network_setup.refresh_upnp_async) and before every start (preflight).
Stdlib only; never raises.
"""
from __future__ import annotations

import hashlib
import ipaddress
import socket
import struct
from dataclasses import dataclass
from typing import Callable, Optional

import applog

_log = applog.get_logger(__name__)

PORT = 5351
LIFETIME = 4 * 3600  # seconds; renewed well before it runs out

NAT_PMP = "NAT-PMP"
PCP = "PCP"

_PMP_VERSION = 0
_PCP_VERSION = 2
_PCP_OPCODE_MAP = 1
_PCP_OPCODE_ANNOUNCE = 0
_UDP = 17
_PMP_UNSUPPORTED_VERSION = 1   # NAT-PMP result: "use another version" (a PCP-only router says this)
_PCP_UNSUPP_VERSION = 1
_TRIES = (0.25, 0.5, 1.0)      # RFC 6886 backoff, shortened: a LAN router answers in milliseconds


@dataclass
class Mapping:
    ok: bool
    method: str = ""            # NAT_PMP or PCP
    external_port: int = 0
    lifetime: int = 0
    error: str = ""


# ----------------------------------------------------------- transport --
def _exchange(gateway: str, local_ip: Optional[str], packet: bytes, tries=_TRIES) -> Optional[bytes]:
    """Sends `packet` to the router and returns its reply (from the router only)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            sock.bind((local_ip or "", 0))
        except OSError:
            sock.bind(("", 0))
        for wait in tries:
            try:
                sock.sendto(packet, (gateway, PORT))
                sock.settimeout(wait)
                while True:
                    data, addr = sock.recvfrom(1100)
                    if addr[0] == gateway:
                        return data
            except socket.timeout:
                continue
            except OSError as e:
                _log.info(f"NAT-PMP/PCP to {gateway}: {e}")
                return None
        return None
    finally:
        sock.close()


Exchange = Callable[[str, Optional[str], bytes], Optional[bytes]]


# ------------------------------------------------------------- NAT-PMP --
def pmp_request(port: int, lifetime: int) -> bytes:
    """Map UDP `port` (inside) to the same port outside."""
    return struct.pack("!BBHHHI", _PMP_VERSION, 1, 0, port, port, lifetime)


def pmp_parse(data: bytes) -> Optional[dict]:
    if len(data) >= 4 and data[0] != _PMP_VERSION:
        return {"result": _PMP_UNSUPPORTED_VERSION}
    if len(data) < 16 or data[1] != 129:
        return None
    _v, _op, result, _epoch, internal, external, lifetime = struct.unpack("!BBHIHHI", data[:16])
    return {"result": result, "internal": internal, "external": external, "lifetime": lifetime}


def pmp_external_ip(gateway: str, local_ip: Optional[str], exchange: Exchange = None) -> Optional[str]:
    data = (exchange or _exchange)(gateway, local_ip, struct.pack("!BB", _PMP_VERSION, 0))
    if not data or len(data) < 12 or data[0] != _PMP_VERSION or data[1] != 128:
        return None
    result = struct.unpack("!H", data[2:4])[0]
    return socket.inet_ntoa(data[8:12]) if result == 0 else None


# ----------------------------------------------------------------- PCP --
def _v4mapped(ip: str) -> bytes:
    return ipaddress.IPv6Address(f"::ffff:{ip}").packed


def nonce_for(key: str, port: int) -> bytes:
    """The same nonce every time for this server and port, as PCP requires
    for renewing or deleting a mapping (even after ConanOps restarts)."""
    return hashlib.sha256(f"ConanOps|{key}|{port}".encode("utf-8")).digest()[:12]


def pcp_request(local_ip: str, port: int, lifetime: int, nonce: bytes) -> bytes:
    header = struct.pack("!BBHI", _PCP_VERSION, _PCP_OPCODE_MAP, 0, lifetime) + _v4mapped(local_ip)
    body = nonce + struct.pack("!B3xHH", _UDP, port, port) + _v4mapped("0.0.0.0")
    return header + body


def pcp_parse(data: bytes, nonce: bytes) -> Optional[dict]:
    if len(data) < 24:
        return None
    if data[0] != _PCP_VERSION:
        return {"result": _PCP_UNSUPP_VERSION}
    if data[1] != (0x80 | _PCP_OPCODE_MAP):
        return None
    result = data[3]
    lifetime = struct.unpack("!I", data[4:8])[0]
    if result != 0:
        return {"result": result, "lifetime": lifetime}
    if len(data) < 60 or data[24:36] != nonce:
        return None
    internal, external = struct.unpack("!HH", data[40:44])
    ext_ip = ipaddress.IPv6Address(data[44:60])
    ext_ip = str(ext_ip.ipv4_mapped) if ext_ip.ipv4_mapped else str(ext_ip)
    return {"result": 0, "internal": internal, "external": external, "lifetime": lifetime, "external_ip": ext_ip}


_PCP_ERRORS = {
    2: "not authorized (the router doesn't allow this)", 3: "malformed request", 4: "unsupported request",
    7: "address mismatch (a second router or VPN in between)", 8: "the router is out of resources",
    11: "the router can't do this right now", 12: "the router is out of ports",
}


# ------------------------------------------------------------- public --
def map_udp(gateway: str, local_ip: str, port: int, key: str = "", lifetime: int = LIFETIME,
            exchange: Exchange = None) -> Mapping:
    """Forwards UDP `port` on the router to this PC, by NAT-PMP or else PCP.
    lifetime 0 removes it. Returns ok only if the router mapped the same port."""
    ex = exchange or _exchange
    if not gateway:
        return Mapping(False, error="router address unknown")
    want = int(port)
    reply = ex(gateway, local_ip, pmp_request(want, lifetime))
    parsed = pmp_parse(reply) if reply else None
    if parsed and parsed["result"] == 0:
        if lifetime and parsed["external"] != want:
            ex(gateway, local_ip, pmp_request(want, 0))  # don't leave a forward on the wrong port
            return Mapping(False, NAT_PMP, parsed["external"], error=f"port {want} is taken; the router offered {parsed['external']}")
        return Mapping(True, NAT_PMP, parsed["external"], parsed["lifetime"])
    pmp_error = (f"NAT-PMP result {parsed['result']}" if parsed else "no NAT-PMP answer")
    if parsed is not None and parsed["result"] not in (_PMP_UNSUPPORTED_VERSION,):
        # A NAT-PMP router that said no: PCP won't be there either.
        return Mapping(False, NAT_PMP, error=pmp_error)

    nonce = nonce_for(key, want)
    reply = ex(gateway, local_ip, pcp_request(local_ip, want, lifetime, nonce))
    parsed = pcp_parse(reply, nonce) if reply else None
    if parsed and parsed["result"] == 0:
        if lifetime and parsed["external"] != want:
            ex(gateway, local_ip, pcp_request(local_ip, want, 0, nonce))
            return Mapping(False, PCP, parsed["external"], error=f"port {want} is taken; the router offered {parsed['external']}")
        return Mapping(True, PCP, parsed["external"], parsed["lifetime"])
    if parsed:
        return Mapping(False, PCP, error=_PCP_ERRORS.get(parsed["result"], f"PCP result {parsed['result']}"))
    return Mapping(False, error=f"{pmp_error}; no PCP answer")


def unmap_udp(gateway: str, local_ip: str, port: int, key: str = "", exchange: Exchange = None) -> bool:
    return map_udp(gateway, local_ip, port, key=key, lifetime=0, exchange=exchange).ok


def available(gateway: str, local_ip: Optional[str], exchange: Exchange = None) -> str:
    """NAT_PMP or PCP if the router answers one of them (changes nothing), else ""."""
    ex = exchange or _exchange
    if not gateway:
        return ""
    if pmp_external_ip(gateway, local_ip, ex):
        return NAT_PMP
    # An ANNOUNCE request is PCP's harmless "are you there?".
    reply = ex(gateway, local_ip, struct.pack("!BBHI", _PCP_VERSION, _PCP_OPCODE_ANNOUNCE, 0, 0)
               + _v4mapped(local_ip or "0.0.0.0"))
    if reply and len(reply) >= 4 and reply[0] == _PCP_VERSION and reply[1] == 0x80 and reply[3] == 0:
        return PCP
    return ""

