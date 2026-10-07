"""
Networking helpers used across ConanOps.

- get_local_ip(): the machine's real LAN-facing IP (for -MULTIHOME). This is
  deliberately NOT the public/WAN IP -- MULTIHOME needs a local interface
  address to bind to.
- is_udp_port_free / is_tcp_port_free: true bind-tests, not just "ask the OS
  for a process list" -- this catches anything holding the port regardless
  of what owns it.
- find_free_port_pair(): given a starting game port, finds a free
  (game_port, query_port) pair, skipping any also used by other ConanOps
  servers (passed in as `reserved`).
- query_a2s_info(): sends a real Source-engine A2S_INFO query over UDP so
  the app can confirm a server is actually bound and answering, not just
  that its process exists.
- get_default_gateway(): best-effort router IP (from the routing table), for the manual
  port-forwarding guide (ui/port_forwarding_guide_dialog.py) -- so its
  first step can say "try this address" instead of nothing at all.
"""
from __future__ import annotations

import re
import socket
import subprocess
from typing import List, Optional, Tuple

import psutil

import proc_utils


# Interface names that are almost never the LAN adapter a home server
# should bind to / have ports forwarded to: VPN clients, virtual
# switches, VM host adapters. Matched case-insensitively against the
# adapter's friendly name (psutil.net_if_addrs() keys).
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
    """RFC 6598 shared address space (100.64.0.0/10) -- what carrier-
    grade NAT hands a home router as its 'WAN' address."""
    o = _octets(ip)
    return bool(o) and o[0] == 100 and 64 <= o[1] <= 127


def is_non_public_ipv4(ip: str) -> bool:
    """True for any address that can't be a real public internet
    address: private, CGNAT, loopback, link-local, or unspecified. Used
    to spot double NAT / CGNAT when the router reports its own WAN IP."""
    o = _octets(ip)
    if not o:
        return False
    return (
        is_private_ipv4(ip) or is_cgnat_ipv4(ip)
        or o[0] in (0, 127) or (o[0] == 169 and o[1] == 254)
    )


def is_usable_lan_ipv4(ip: str) -> bool:
    """A real, assigned interface address: not loopback, not
    unspecified, and not a 169.254.x.x APIPA address (which is what
    Windows self-assigns when DHCP hasn't answered yet -- e.g. early
    during boot -- and is never what a server should bind to)."""
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


# `route print -4` data rows are plain numbers in every Windows display
# language (only the headers are translated), unlike `ipconfig`, whose
# "Default Gateway" label is localized and whose IPv4 gateway sits on an
# unlabeled continuation line on any dual-stack adapter.
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
    """Best-effort LAN IP of the PHYSICAL network adapter -- the one a
    home router forwards ports to.

    The old approach (open a UDP 'connection' to 8.8.8.8 and read the
    local address back) returns whatever adapter owns the default route,
    which with a VPN connected is the VPN tunnel -- so the server got
    bound to, and port-forwarded to, the VPN's address. This prefers,
    in order: the lowest-metric default route on a non-VPN/non-virtual
    adapter; the routing-table guess if it isn't a virtual adapter; any
    private address on a non-virtual adapter; then anything usable at
    all. Never returns a 169.254.x.x APIPA address (DHCP not ready yet);
    falls back to 127.0.0.1 only if nothing real exists."""
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
    """Every non-loopback IPv4 address currently bound to a local
    network interface, via psutil (already a dependency). Used to
    tell a genuinely stale bind_ip (the interface it named no longer
    exists -- NIC change, DHCP renewal, a USB adapter unplugged) apart
    from one that's simply not whichever interface get_local_ip()'s
    single default-route guess would currently pick -- e.g. a
    deliberately chosen secondary NIC on a multi-homed machine.
    Returns an empty set if enumeration itself fails, so callers can
    tell "couldn't check" apart from "checked and found nothing"."""
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
    """On Windows, a plain bind to 0.0.0.0 can SUCCEED while another
    process holds the same port on a specific address (exactly how the
    Conan server binds with -MULTIHOME), so the test would report a taken
    port as free. SO_EXCLUSIVEADDRUSE makes the test bind fail if any
    socket holds the port on any local address. (SO_REUSEADDR does the
    opposite on Windows -- it lets a bind succeed on a port that's
    already in use -- which is why the old TCP check used it wrongly.)"""
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
    """Returns (game_port, query_port), each independently the first free
    UDP port at/after its own starting point that isn't in `reserved`
    (e.g. ports already claimed by this app's other configured servers).
    Game port and query port have no fixed numeric relationship in Conan
    -- they're just two independent ports -- so each is searched on its
    own rather than derived from the other.

    The game port's search also requires game_port + 1 to be free and
    unreserved: Conan's dedicated server binds a second UDP port right
    above the game port for its own networking, so a game_port whose
    neighbor is taken would still collide even though this function
    only hands back game_port itself."""
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
    # Guard against the query search landing on the game port or its
    # +1 neighbor.
    while query in (game, game + 1):
        query = find_one(query + 1)
    return game, query


def query_a2s_info(ip: str, port: int, timeout: float = 1.5) -> Optional[dict]:
    """Sends a Source-engine A2S_INFO query. Returns a dict with at least
    {'name', 'map', 'players', 'max_players'} on success, or None if the
    server didn't answer in time. This is the same query Steam's server
    browser uses, so a successful reply means the server is genuinely
    bound and reachable -- not just that the process exists."""
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
    """Best-effort router (default gateway) IP, for the manual
    port-forwarding guide's first step.

    Reads the IPv4 routing table (`route print -4`, whose data rows are
    language-independent) and returns the gateway of the default route
    that goes out through `local_ip`'s adapter -- not just the first
    gateway listed, which with a VPN or Hyper-V adapter present can be
    the wrong network entirely. Falls back to the lowest-metric
    non-virtual default route, then to the ".1 on your own subnet"
    convention (a guess, which is why the guide phrases it as "try this
    first"). Returns None if not even a guess is possible.

    Runs a subprocess -- call it off the GUI thread."""
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
