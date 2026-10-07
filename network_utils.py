"""
Networking helpers: LAN IP detection (for -MULTIHOME, never the public IP),
real bind-tests for free ports, free port-pair search, A2S_INFO queries to
confirm a server is answering, and best-effort default gateway lookup.
"""
from __future__ import annotations

import re
import socket
import subprocess
from typing import List, Optional, Tuple

import psutil

import proc_utils


# Adapter names (VPNs, virtual switches, VMs) that are never the LAN adapter
# a home server should bind to. Matched against psutil's friendly names.
_VIRTUAL_IFACE_RE = re.compile(
    r"vpn|\btap\b|tap-|\btun\b|wireguard|\bwg\d|nordlynx|openvpn|tailscale|zerotier|hamachi|"
    r"radmin|proton|mullvad|expressvpn|surfshark|windscribe|cisco|anyconnect|fortinet|forticlient|"
    r"globalprotect|pangp|vethernet|hyper-v|virtualbox|vmware|vbox|wsl|docker|loopback|npcap|bluetooth",
    re.IGNORECASE,
)

_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def is_valid_ipv4(ip: str) -> bool:
    if not ip or not _IPV4_RE.match(ip):
        return False
    return all(0 <= int(p) <= 255 for p in ip.split("."))


def _octets(ip: str) -> Optional[Tuple[int, int, int, int]]:
    if not is_valid_ipv4(ip):
        return None
    a, b, c, d = (int(p) for p in ip.split("."))
    return a, b, c, d


def is_private_ipv4(ip: str) -> bool:
    """RFC 1918 private ranges (10/8, 172.16/12, 192.168/16)."""
    o = _octets(ip)
    if not o:
        return False
    a, b = o[0], o[1]
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)


def is_cgnat_ipv4(ip: str) -> bool:
    """RFC 6598 CGNAT range (100.64.0.0/10)."""
    o = _octets(ip)
    return bool(o) and o[0] == 100 and 64 <= o[1] <= 127


def is_non_public_ipv4(ip: str) -> bool:
    """Private, CGNAT, loopback, link-local or unspecified. Used to spot
    double NAT / CGNAT from the router's WAN IP."""
    o = _octets(ip)
    if not o:
        return False
    return (
        is_private_ipv4(ip) or is_cgnat_ipv4(ip)
        or o[0] in (0, 127) or (o[0] == 169 and o[1] == 254)
    )


def is_usable_lan_ipv4(ip: str) -> bool:
    """Not loopback, unspecified, or 169.254.x.x (APIPA, DHCP not ready)."""
    o = _octets(ip)
    if not o:
        return False
    return not (o[0] in (0, 127) or (o[0] == 169 and o[1] == 254))


def _iface_name_by_ip() -> dict:
    names = {}
    try:
        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            st = stats.get(name)
            if st is not None and not st.isup:
                continue
            for addr in addrs:
                if addr.family == socket.AF_INET:
                    names[addr.address] = name
    except (OSError, AttributeError):
        pass
    return names


def is_virtual_interface_ip(ip: str) -> bool:
    name = _iface_name_by_ip().get(ip, "")
    return bool(name and _VIRTUAL_IFACE_RE.search(name))


# `route print -4` data rows are language-independent, unlike `ipconfig`'s
# localized labels and split gateway lines on dual-stack adapters.
_DEFAULT_ROUTE_RE = re.compile(
    r"^\s*0\.0\.0\.0\s+0\.0\.0\.0\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+)\s*$"
)


def _default_routes() -> List[Tuple[str, str, int]]:
    """Active IPv4 default routes as (gateway, interface_ip, metric),
    lowest metric first. Empty if `route` isn't available."""
    try:
        proc = subprocess.run(
            ["route", "print", "-4"], capture_output=True, text=True, timeout=10,
            **proc_utils.hidden_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    routes = []
    for line in (proc.stdout or "").splitlines():
        m = _DEFAULT_ROUTE_RE.match(line)
        if m and is_valid_ipv4(m.group(1)) and m.group(1) != "0.0.0.0":
            routes.append((m.group(1), m.group(2), int(m.group(3))))
    routes.sort(key=lambda r: r[2])
    return routes


def _udp_route_guess() -> Optional[str]:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def get_local_ip() -> str:
    """Best-effort LAN IP of the physical adapter (not a VPN tunnel that
    owns the default route). Prefers the lowest-metric non-virtual default
    route, then other non-virtual guesses. Never returns APIPA; falls back
    to 127.0.0.1."""
    names = _iface_name_by_ip()

    def virtual(ip: str) -> bool:
        return bool(_VIRTUAL_IFACE_RE.search(names.get(ip, "")))

    for _gw, iface_ip, _metric in _default_routes():
        if is_usable_lan_ipv4(iface_ip) and not virtual(iface_ip) and iface_ip in names:
            return iface_ip

    guess = _udp_route_guess()
    if guess and is_usable_lan_ipv4(guess) and not virtual(guess):
        return guess

    for ip in sorted(names):
        if is_usable_lan_ipv4(ip) and is_private_ipv4(ip) and not virtual(ip):
            return ip

    if guess and is_usable_lan_ipv4(guess):
        return guess
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if is_usable_lan_ipv4(ip):
            return ip
    except OSError:
        pass
    return "127.0.0.1"


def list_local_ipv4s() -> set:
    """Non-loopback IPv4 addresses on local interfaces, to tell a stale
    bind_ip from a deliberately chosen secondary NIC. Empty set if
    enumeration fails."""
    addrs = set()
    try:
        for family_addrs in psutil.net_if_addrs().values():
            for addr in family_addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    addrs.add(addr.address)
    except (OSError, AttributeError):
        pass
    return addrs


def _exclusive(sock: socket.socket) -> None:
    """Windows: a bind to 0.0.0.0 can succeed while another process holds
    the port on a specific address (as -MULTIHOME does). SO_EXCLUSIVEADDRUSE
    makes the test fail in that case; SO_REUSEADDR would do the opposite."""
    opt = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if opt is not None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, opt, 1)
        except OSError:
            pass


def _port_free(kind: int, port: int, host: str) -> bool:
    if not (1 <= int(port) <= 65535):
        return False
    s = socket.socket(socket.AF_INET, kind)
    try:
        _exclusive(s)
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def is_udp_port_free(port: int, host: str = "0.0.0.0") -> bool:
    return _port_free(socket.SOCK_DGRAM, port, host)


def is_tcp_port_free(port: int, host: str = "0.0.0.0") -> bool:
    return _port_free(socket.SOCK_STREAM, port, host)


def find_free_port_pair(
    start_game_port: int = 7777,
    reserved: Optional[set] = None,
    start_query_port: int = 27015,
    max_attempts: int = 50,
) -> Tuple[int, int]:
    """(game_port, query_port), each the first free UDP port at/after its
    start and not in `reserved`. The two are independent in Conan, but
    game_port + 1 must also be free because Conan binds it too."""
    reserved = reserved or set()

    def find_one(start: int) -> int:
        port = start
        for _ in range(max_attempts):
            if port > 65535:
                break
            if port not in reserved and is_udp_port_free(port):
                return port
            port += 1
        raise RuntimeError(f"Could not find a free port at/after {start} after {max_attempts} attempts")

    def find_game(start: int) -> int:
        port = start
        for _ in range(max_attempts):
            if port + 1 > 65535:
                break
            if (port not in reserved and port + 1 not in reserved
                    and is_udp_port_free(port) and is_udp_port_free(port + 1)):
                return port
            port += 1
        raise RuntimeError(f"Could not find a free game port pair at/after {start} after {max_attempts} attempts")

    game = find_game(start_game_port)
    query = find_one(start_query_port if start_query_port not in (start_game_port, game, game + 1)
                      else game + 2)
    while query in (game, game + 1):
        query = find_one(query + 1)
    return game, query


def query_a2s_info(ip: str, port: int, timeout: float = 1.5) -> Optional[dict]:
    """Source-engine A2S_INFO query (what Steam's browser uses). Returns
    {'name', 'map', 'players', 'max_players'}, or None if no answer."""
    req = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(req, (ip, port))
        data, _ = sock.recvfrom(4096)
    except (socket.timeout, OSError):
        return None
    finally:
        sock.close()

    try:
        # Some servers reply with a challenge (0x41) first; retry once with it.
        header = data[4:5]
        if header == b"\x41":
            challenge = data[5:9]
            sock2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock2.settimeout(timeout)
            try:
                sock2.sendto(req + challenge, (ip, port))
                data, _ = sock2.recvfrom(4096)
            finally:
                sock2.close()
            header = data[4:5]

        if header != b"\x49":  # 'I' = A2S_INFO response
            return None

        buf = data[5:]

        def read_cstr(b: bytes, offset: int) -> Tuple[str, int]:
            end = b.index(b"\x00", offset)
            return b[offset:end].decode("utf-8", errors="replace"), end + 1

        offset = 1  # protocol version byte
        name, offset = read_cstr(buf, offset)
        map_name, offset = read_cstr(buf, offset)
        _folder, offset = read_cstr(buf, offset)
        _game, offset = read_cstr(buf, offset)
        offset += 2  # app id (short)
        players = buf[offset]
        offset += 1
        max_players = buf[offset]
        offset += 1

        return {
            "name": name,
            "map": map_name,
            "players": players,
            "max_players": max_players,
        }
    except (IndexError, ValueError, socket.timeout, OSError):
        return None


def get_default_gateway(local_ip: Optional[str] = None) -> Optional[str]:
    """Best-effort router IP for the port-forwarding guide: the default
    route through `local_ip`'s adapter, then any non-virtual default route,
    then a ".1" guess. Runs a subprocess -- call off the GUI thread."""
    ip = local_ip or get_local_ip()
    routes = _default_routes()
    for gw, iface_ip, _metric in routes:
        if iface_ip == ip:
            return gw
    names = _iface_name_by_ip()
    for gw, iface_ip, _metric in routes:
        if not _VIRTUAL_IFACE_RE.search(names.get(iface_ip, "")):
            return gw

    o = _octets(ip)
    if o and is_usable_lan_ipv4(ip):
        return f"{o[0]}.{o[1]}.{o[2]}.1"
    return None
