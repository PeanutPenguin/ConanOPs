"""Opens the ports a Conan server needs: Windows Firewall rules and best-effort
UPnP router forwarding (a UPnP failure means "show manual steps", not an error).

Firewall rules use PowerShell NetSecurity cmdlets, never cmd.exe/.bat, which
can't safely quote user text (and an elevated injection is a security hole).
Rule Names use only the server id, label and port; the typed server name goes
only into DisplayName. All rules sit in the "ConanOps" group, results are read
from cmdlet objects (language-independent), and each operation is one script,
so at most one UAC prompt.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple, Union
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

import applog
import network_utils
import powershell
import proc_utils  # noqa: F401 - re-exported for callers/tests that patch it here

_log = applog.get_logger(__name__)

RULE_GROUP = "ConanOps"
RULE_PREFIX = "ConanOps-"


@dataclass
class FirewallResult:
    success: bool
    message: str
    checked: bool = True  # False when the status couldn't be determined at all


_LABEL_SLUGS = {"Game": "Game", "Game+1": "GamePlus1", "Query": "Query"}


def _safe_id(server_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(server_id or ""))[:32] or "server"


def _port_labels(game_port: int, query_port: int) -> List[tuple]:
    # The server also binds game_port+1 itself; it needs a rule too or connections fail.
    return [("Game", game_port), ("Game+1", game_port + 1), ("Query", query_port)]


def rule_name(server_id: str, label: str, port: int) -> str:
    """Unique rule Name; needs no escaping and survives server renames."""
    return f"{RULE_PREFIX}{_safe_id(server_id)}-{_LABEL_SLUGS.get(label, 'Port')}-{int(port)}"


_rule_name = rule_name


def _clean_display(text: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(text or "")).strip()
    return text[:80] or "Server"


def display_name(server_name: str, label: str, port: int) -> str:
    return f"ConanOps - {_clean_display(server_name)} - {label} ({int(port)}/UDP)"


def legacy_rule_prefix(server_name: str) -> str:
    """DisplayName prefix of legacy netsh-created rules."""
    return f"ConanOps - {server_name} - "


# Aliases so tests and callers can patch these names on this module.
_ps_str = powershell.ps_str
_ps_array = powershell.ps_array
RUN_OK = powershell.RUN_OK
RUN_DECLINED = powershell.RUN_DECLINED
RUN_FAILED = powershell.RUN_FAILED


def _run_ps_readonly(script: str, timeout: float = 30.0) -> Optional[subprocess.CompletedProcess]:
    return powershell.run_readonly(script, timeout=timeout)


def _run_ps_privileged(script: str, timeout: float = 90.0) -> str:
    return powershell.run_privileged(script, timeout=timeout)


def _remove_script(server_ids: Iterable[str], legacy_names: Iterable[str]) -> str:
    ids = [_safe_id(s) for s in server_ids if s]
    names = [n for n in legacy_names if n]
    lines = []
    if ids:
        lines.append(
            f"$ids = {_ps_array(ids)}\n"
            f"Get-NetFirewallRule -Group {_ps_str(RULE_GROUP)} -ErrorAction SilentlyContinue | "
            "Where-Object { $n = $_.Name; $ids | Where-Object { $n -like ('" + RULE_PREFIX + "' + $_ + '-*') } } | "
            "Remove-NetFirewallRule -ErrorAction SilentlyContinue"
        )
    if names:
        # Legacy rules have GUID Names, so skipping "ConanOps-" Names spares
        # current rules; the exact DisplayName match keeps "Chud" from
        # matching "Chud - PvP".
        lines.append(
            f"$legacy = {_ps_array(names)}\n"
            "$res = $legacy | ForEach-Object { '^ConanOps - ' + [regex]::Escape($_) + ' - (Game|Game\\+1|Query) \\(\\d+/UDP\\)$' }\n"
            "Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { "
            "$dn = [string]$_.DisplayName; "
            f"(-not ([string]$_.Name).StartsWith({_ps_str(RULE_PREFIX)}, [StringComparison]::OrdinalIgnoreCase)) -and "
            "($res | Where-Object { $dn -match $_ }) "
            "} | Remove-NetFirewallRule -ErrorAction SilentlyContinue"
        )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------- #
# Firewall: read-only status (no admin needed)
# --------------------------------------------------------------------- #

def present_rule_names(server_id: str) -> Optional[set]:
    """Names of this server's enabled inbound allow rules, or None if the check couldn't run."""
    sid = _safe_id(server_id)
    script = (
        f"Get-NetFirewallRule -Group {_ps_str(RULE_GROUP)} -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Name -like {_ps_str(RULE_PREFIX + sid + '-*')} -and "
        "[string]$_.Enabled -eq 'True' -and [string]$_.Action -eq 'Allow' -and "
        "[string]$_.Direction -eq 'Inbound' } | ForEach-Object { $_.Name }\n"
        "exit 0\n"
    )
    proc = _run_ps_readonly(script)
    if proc is None or proc.returncode != 0:
        return None
    return {line.strip() for line in (proc.stdout or "").splitlines() if line.strip()}


def rule_exists(name: str) -> bool:
    script = (
        f"$r = Get-NetFirewallRule -Name {_ps_str(name)} -ErrorAction SilentlyContinue\n"
        "if ($r) { exit 0 } else { exit 1 }\n"
    )
    proc = _run_ps_readonly(script)
    return bool(proc is not None and proc.returncode == 0)


def firewall_status(server_id: str, game_port: int, query_port: int) -> List[FirewallResult]:
    """Whether each of this server's three rules is present and active."""
    present = present_rule_names(server_id)
    results = []
    for label, port in _port_labels(game_port, query_port):
        if present is None:
            results.append(FirewallResult(False, f"{label} port {port}", checked=False))
        else:
            results.append(FirewallResult(rule_name(server_id, label, port) in present, f"{label} port {port}"))
    return results


def _exe_block_rules_ps(exe_path: str) -> str:
    """PowerShell setting $blockRules to enabled inbound block rules for this program.
    Windows makes these when its "allow this app?" popup is dismissed; block beats allow."""
    return (
        f"$exe = {_ps_str(exe_path)}\n"
        "$blockRules = @(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Program -and ([Environment]::ExpandEnvironmentVariables([string]$_.Program) -ieq $exe) } | "
        "Get-NetFirewallRule -ErrorAction SilentlyContinue | "
        "Where-Object { [string]$_.Enabled -eq 'True' -and [string]$_.Action -eq 'Block' -and "
        "[string]$_.Direction -eq 'Inbound' })\n"
    )


def firewall_environment(exe_path: str = "") -> dict:
    """Other things that affect reachability: {"block_rules": block rules for the
    server exe, "third_party": firewall products in Security Center (they may
    ignore Windows Firewall), "checked": False if the check couldn't run}."""
    import json
    script = "$out = @{ block = @(); third = @() }\n"
    if exe_path:
        script += "try {\n" + _exe_block_rules_ps(exe_path) + \
            "$out.block = @($blockRules | ForEach-Object { [string]$_.DisplayName })\n} catch {}\n"
    script += (
        "try { $out.third = @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName FirewallProduct "
        "-ErrorAction Stop | ForEach-Object { [string]$_.displayName }) } catch {}\n"
        "$out | ConvertTo-Json -Compress\n"
        "exit 0\n"
    )
    proc = _run_ps_readonly(script, timeout=45.0)
    empty = {"checked": False, "block_rules": [], "third_party": []}
    if proc is None or proc.returncode != 0:
        return empty
    try:
        data = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return empty

    def as_list(v):
        if v is None:
            return []
        return [str(x) for x in (v if isinstance(v, list) else [v]) if str(x).strip()]

    return {"checked": True, "block_rules": as_list(data.get("block")), "third_party": as_list(data.get("third"))}


# --------------------------------------------------------------------- #
# Firewall: changes (one script, one UAC prompt)
# --------------------------------------------------------------------- #

def add_firewall_rules(
    server_id: str, game_port: int, query_port: int,
    display: str = "", legacy_names: Iterable[str] = (), exe_path: str = "",
) -> List[FirewallResult]:
    """Replaces all of this server's rules (and legacy ones) with the three
    current ports, and with `exe_path` disables block rules for the server exe.
    One UAC prompt. Returns each rule's status as read back afterward."""
    ports = _port_labels(game_port, query_port)
    script = _remove_script([server_id], legacy_names)
    if exe_path:
        script += "try {\n" + _exe_block_rules_ps(exe_path) + \
            "$blockRules | Disable-NetFirewallRule -ErrorAction SilentlyContinue\n} catch {}\n"
    script += "$fail = 0\n"
    for label, port in ports:
        script += (
            "try { New-NetFirewallRule "
            f"-Name {_ps_str(rule_name(server_id, label, port))} "
            f"-DisplayName {_ps_str(display_name(display or server_id, label, port))} "
            f"-Group {_ps_str(RULE_GROUP)} "
            "-Description 'Created by ConanOps for a Conan Exiles dedicated server.' "
            f"-Direction Inbound -Action Allow -Protocol UDP -LocalPort {int(port)} -Profile Any | Out-Null }} "
            "catch { $fail++ }\n"
        )
    script += "exit $fail\n"
    outcome = _run_ps_privileged(script)

    present = present_rule_names(server_id)
    results = []
    for label, port in ports:
        name = rule_name(server_id, label, port)
        if present is not None and name in present:
            results.append(FirewallResult(True, f"{label} port {port}: rule added."))
        elif outcome == RUN_DECLINED:
            results.append(FirewallResult(False, f"{label} port {port}: not added -- the Windows permission prompt was declined."))
        elif present is None:
            results.append(FirewallResult(False, f"{label} port {port}: couldn't confirm the rule (Windows Firewall couldn't be read).", checked=False))
        else:
            results.append(FirewallResult(False, f"{label} port {port}: rule wasn't found after adding it -- see conanops.log."))
    if exe_path and outcome == RUN_OK:
        env = firewall_environment(exe_path)
        if env["block_rules"]:
            results.append(FirewallResult(
                False, "Windows still has a rule blocking the server program itself "
                       f"({', '.join(env['block_rules'][:3])}) -- see Diagnostics.",
            ))
    return results


def remove_firewall_rules(
    server_ids: Union[str, Iterable[str]], game_port: Optional[int] = None,
    query_port: Optional[int] = None, legacy_names: Iterable[str] = (),
    program_dirs: Iterable[str] = (),
) -> bool:
    """Removes all rules for the server id(s) plus legacy ones, in one prompt.
    game_port/query_port are ignored (rules are found by id)."""
    ids = [server_ids] if isinstance(server_ids, str) else list(server_ids)
    legacy = [n for n in legacy_names if n]
    dirs = [d for d in program_dirs if d]
    if not ids and not legacy and not dirs:
        return True
    if dirs:
        # Also removes rules Windows created for programs in these folders.
        import cleanup
        return _run_ps_privileged(cleanup.cleanup_script(ids, legacy, dirs)) == RUN_OK
    return _run_ps_privileged(_remove_script(ids, legacy) + "exit 0\n") == RUN_OK


# --------------------------------------------------------------------- #
# Minimal stdlib-only UPnP IGD client: SSDP discovery + SOAP.
# --------------------------------------------------------------------- #

_SSDP_ADDR = "239.255.255.250"
_SSDP_PORT = 1900
_SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
)


def _msearch(st: str) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {_SSDP_ADDR}:{_SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {st}\r\n"
        "\r\n"
    ).encode("ascii")


_SSDP_MSEARCH = _msearch(_SEARCH_TARGETS[0])


@dataclass
class UpnpDevice:
    control_url: str
    service_type: str
    location: str = ""


UPNP_CONFLICT = 718            # ConflictInMappingEntry
UPNP_NO_SUCH_ENTRY = 714       # NoSuchEntryInArray
UPNP_ARRAY_INDEX_INVALID = 713  # SpecifiedArrayIndexInvalid


def _ssdp_locations(timeout: float, local_ip: Optional[str]) -> List[str]:
    """Every LOCATION answering an IGD search (a TV or NAS may answer first).
    Sent from the LAN adapter so a VPN default route doesn't swallow it."""
    locations: List[str] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        if local_ip and network_utils.is_usable_lan_ipv4(local_ip):
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
                sock.bind((local_ip, 0))
            except OSError as e:
                _log.info(f"Couldn't pin UPnP discovery to {local_ip}: {e}")
        for st in _SEARCH_TARGETS:
            try:
                sock.sendto(_msearch(st), (_SSDP_ADDR, _SSDP_PORT))
            except OSError as e:
                _log.info(f"SSDP send failed: {e}")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, _ = sock.recvfrom(4096)
            except (socket.timeout, OSError):
                break
            text = data.decode("utf-8", errors="replace")
            m = re.search(r"^LOCATION:\s*(\S+)", text, re.IGNORECASE | re.MULTILINE)
            if m:
                loc = m.group(1).strip()
                if loc not in locations:
                    locations.append(loc)
    finally:
        sock.close()
    return locations


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _services_at(location: str, timeout: float) -> List[UpnpDevice]:
    try:
        with urllib.request.urlopen(location, timeout=timeout) as resp:
            desc = resp.read()
        root = ET.fromstring(desc)
    except Exception as e:  # noqa: BLE001 - OSError, http.client.HTTPException, ValueError, ParseError...
        _log.info(f"UPnP device description at {location} unusable: {e}")
        return []
    base = location
    for el in root.iter():
        if _local_tag(el.tag) == "URLBase" and (el.text or "").strip():
            base = el.text.strip()
            break
    found = []
    for svc in root.iter():
        if _local_tag(svc.tag) != "service":
            continue
        fields = {_local_tag(c.tag): (c.text or "").strip() for c in svc}
        stype = fields.get("serviceType", "")
        if "WANIPConnection" in stype or "WANPPPConnection" in stype:
            ctrl = fields.get("controlURL", "")
            if ctrl:
                found.append(UpnpDevice(urllib.parse.urljoin(base, ctrl), stype, location))
    # Prefer IP over PPP: combo modems often list an unused PPP service.
    found.sort(key=lambda d: 0 if "WANIPConnection" in d.service_type else 1)
    return found


def _soap(device: UpnpDevice, action: str, args: List[Tuple[str, object]],
          timeout: float = 5.0) -> Tuple[Optional[Dict[str, str]], Optional[int]]:
    """Returns (fields, None) on success or (None, upnp_error_code or None). Never raises."""
    arg_xml = "".join(f"<{k}>{xml_escape(str(v))}</{k}>" for k, v in args)
    body = (
        '<?xml version="1.0"?>\n'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
        f'<u:{action} xmlns:u="{xml_escape(device.service_type)}">{arg_xml}</u:{action}>'
        "</s:Body></s:Envelope>"
    )
    req = urllib.request.Request(
        device.control_url, data=body.encode("utf-8"), method="POST",
        headers={"Content-Type": 'text/xml; charset="utf-8"',
                 "SOAPAction": f'"{device.service_type}#{action}"'},
    )

    def parse(raw: bytes) -> Dict[str, str]:
        out = {}
        for el in ET.fromstring(raw).iter():
            if len(el) == 0:
                out[_local_tag(el.tag)] = (el.text or "").strip()
        return out

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return parse(resp.read()), None
    except urllib.error.HTTPError as e:
        code = None
        try:
            code_text = parse(e.read()).get("errorCode")
            code = int(code_text) if code_text else None
        except Exception:  # noqa: BLE001
            pass
        _log.info(f"UPnP {action} failed: HTTP {e.code}, UPnP error {code}")
        return None, code
    except Exception as e:  # noqa: BLE001 - OSError, HTTPException, ValueError, ParseError
        _log.info(f"UPnP {action} failed: {e}")
        return None, None


def discover_igd(timeout: float = 3.0, local_ip: Optional[str] = None) -> Optional[UpnpDevice]:
    """The router's WAN connection service, preferring one reporting Connected.
    None if nothing usable responds (normal when UPnP is off)."""
    candidates: List[UpnpDevice] = []
    for loc in _ssdp_locations(timeout, local_ip):
        candidates.extend(_services_at(loc, timeout))
    if not candidates:
        return None
    for dev in candidates:
        fields, _ = _soap(dev, "GetStatusInfo", [], timeout=timeout)
        if fields and fields.get("NewConnectionStatus", "").lower() == "connected":
            return dev
    return candidates[0]


def get_external_ip(device: UpnpDevice, timeout: float = 5.0) -> Optional[str]:
    fields, _ = _soap(device, "GetExternalIPAddress", [], timeout=timeout)
    ip = (fields or {}).get("NewExternalIPAddress", "")
    return ip if network_utils.is_valid_ipv4(ip) else None


def get_port_mapping(device: UpnpDevice, external_port: int, protocol: str = "UDP",
                     timeout: float = 5.0) -> Optional[Dict[str, str]]:
    fields, _ = _soap(device, "GetSpecificPortMappingEntry", [
        ("NewRemoteHost", ""), ("NewExternalPort", int(external_port)), ("NewProtocol", protocol),
    ], timeout=timeout)
    return fields


def delete_port_mapping(device: UpnpDevice, external_port: int, protocol: str = "UDP",
                        timeout: float = 5.0) -> bool:
    fields, code = _soap(device, "DeletePortMapping", [
        ("NewRemoteHost", ""), ("NewExternalPort", int(external_port)), ("NewProtocol", protocol),
    ], timeout=timeout)
    return fields is not None or code == UPNP_NO_SUCH_ENTRY


def list_port_mappings(device: UpnpDevice, max_entries: int = 256, timeout: float = 5.0) -> List[Dict[str, str]]:
    out = []
    for i in range(max_entries):
        fields, _code = _soap(device, "GetGenericPortMappingEntry", [("NewPortMappingIndex", i)], timeout=timeout)
        if fields is None:
            break
        out.append(fields)
    return out


def upnp_tag(server_id: str) -> str:
    return f"ConanOps {_safe_id(server_id)} "


def _legacy_upnp_description(desc: str) -> bool:
    return desc.startswith("ConanOps ") and desc.split(" ")[1:2] in (["Game"], ["Query"])


def _is_ours(entry: Dict[str, str], server_id: str, internal_ip: Optional[str]) -> bool:
    desc = entry.get("NewPortMappingDescription", "")
    if server_id and desc.startswith(upnp_tag(server_id)):
        return True
    # Legacy untagged mappings count only if they point here; another PC may run ConanOps.
    return _legacy_upnp_description(desc) and bool(internal_ip) and entry.get("NewInternalClient") == internal_ip


MAP_OK = "ok"
MAP_CONFLICT = "conflict"
MAP_FAILED = "failed"


def map_port(device: UpnpDevice, port: int, internal_ip: str, description: str,
             server_id: str = "", protocol: str = "UDP", timeout: float = 5.0) -> str:
    """Adds (or takes over our own stale) mapping and verifies it."""

    def add(lease: int):
        return _soap(device, "AddPortMapping", [
            ("NewRemoteHost", ""), ("NewExternalPort", int(port)), ("NewProtocol", protocol),
            ("NewInternalPort", int(port)), ("NewInternalClient", internal_ip), ("NewEnabled", 1),
            ("NewPortMappingDescription", description[:60]), ("NewLeaseDuration", lease),
        ], timeout=timeout)

    fields, code = add(0)
    if fields is None and code == UPNP_CONFLICT:
        existing = get_port_mapping(device, port, protocol, timeout) or {}
        if existing.get("NewInternalClient") == internal_ip or _is_ours(existing, server_id, internal_ip) \
                or existing.get("NewPortMappingDescription", "").startswith(upnp_tag(server_id)):
            delete_port_mapping(device, port, protocol, timeout)
            fields, code = add(0)
        else:
            _log.info(f"UPnP: port {port}/{protocol} is already forwarded to {existing.get('NewInternalClient', 'another device')}.")
            return MAP_CONFLICT
    if fields is None and code != UPNP_CONFLICT:
        # Some IGDv2 routers refuse permanent leases; refresh_upnp_async renews this one.
        fields, code = add(7 * 24 * 3600)
    if fields is None:
        return MAP_CONFLICT if code == UPNP_CONFLICT else MAP_FAILED

    check = get_port_mapping(device, port, protocol, timeout)
    if check is not None and check.get("NewInternalClient") not in (None, "", internal_ip):
        _log.info(f"UPnP: router accepted {port}/{protocol} but reports it pointing at {check.get('NewInternalClient')}.")
        return MAP_FAILED
    return MAP_OK


def add_port_mapping(device: UpnpDevice, external_port: int, internal_port: int, internal_ip: str,
                     protocol: str = "UDP", description: str = "ConanOps", timeout: float = 5.0) -> bool:
    return map_port(device, external_port, internal_ip, description, protocol=protocol, timeout=timeout) == MAP_OK


def remove_upnp_mappings(server_id: str, ports: Iterable[int] = (), local_ip: Optional[str] = None,
                         device: Optional[UpnpDevice] = None, keep: Iterable[int] = ()) -> int:
    """Deletes this server's router mappings (found by tag in the table, or by
    checking `ports` if listing isn't supported), except `keep`. Returns the count."""
    local_ip = local_ip or network_utils.get_local_ip()
    device = device or discover_igd(local_ip=local_ip)
    if device is None:
        return 0
    keep = {int(p) for p in keep}
    removed = 0
    entries = list_port_mappings(device)
    seen = set()
    for e in entries:
        try:
            port = int(e.get("NewExternalPort", "0"))
        except ValueError:
            continue
        proto = e.get("NewProtocol", "UDP") or "UDP"
        seen.add(port)
        if port in keep or not _is_ours(e, server_id, local_ip):
            continue
        if delete_port_mapping(device, port, proto):
            removed += 1
    for port in ports:
        port = int(port)
        if port in keep or port in seen:
            continue
        e = get_port_mapping(device, port, "UDP")
        if e and _is_ours(e, server_id, local_ip) and delete_port_mapping(device, port, "UDP"):
            removed += 1
    return removed


def reconcile_upnp(server_id: str, internal_ip: str, game_port: int, query_port: int,
                   old_ports: Iterable[int] = ()) -> dict:
    """Makes this server's forwards exactly game/game+1/query -> internal_ip,
    removing stale ones, and checks the router's WAN IP for double NAT."""
    result = {
        "upnp_available": False, "game_port_forwarded": False,
        "game_port_plus_one_forwarded": False, "query_port_forwarded": False,
        "conflicts": [], "external_ip": None, "double_nat": False,
    }
    device = discover_igd(local_ip=internal_ip)
    if device is None:
        return result
    result["upnp_available"] = True
    wanted = [("Game", game_port, "game_port_forwarded"),
              ("Game+1", game_port + 1, "game_port_plus_one_forwarded"),
              ("Query", query_port, "query_port_forwarded")]
    if server_id:
        remove_upnp_mappings(server_id, ports=old_ports, local_ip=internal_ip, device=device,
                             keep=[p for _l, p, _k in wanted])
    for label, port, key in wanted:
        status = map_port(device, port, internal_ip, f"{upnp_tag(server_id)}{label}".strip(), server_id=server_id)
        result[key] = status == MAP_OK
        if status == MAP_CONFLICT:
            result["conflicts"].append(port)
    ext = get_external_ip(device)
    result["external_ip"] = ext
    result["double_nat"] = bool(ext and network_utils.is_non_public_ipv4(ext))
    return result


def try_upnp_forward(internal_ip: str, game_port: int, query_port: int, server_id: str = "") -> dict:
    return reconcile_upnp(server_id, internal_ip, game_port, query_port)


# Refresh before each launch re-points forwards after a DHCP change and renews
# leases. Throttled per configuration.
_refresh_lock = threading.Lock()
_had_router: Dict[str, bool] = {}
_last_refresh: Dict[str, Tuple[tuple, float]] = {}
_REFRESH_MIN_INTERVAL = 30 * 60  # below the hourly check in MainWindow, so that one always runs
UPNP_REFRESH_ENABLED = sys.platform == "win32"


def refresh_upnp_async(server) -> None:
    ip = server.bind_ip
    if not ip or not network_utils.is_usable_lan_ipv4(ip):
        return
    key = (ip, int(server.game_port), int(server.query_port))
    now = time.monotonic()
    with _refresh_lock:
        prev = _last_refresh.get(server.id)
        if prev and prev[0] == key and now - prev[1] < _REFRESH_MIN_INTERVAL:
            return
        _last_refresh[server.id] = (key, now)

    def work():
        try:
            r = reconcile_upnp(server.id, ip, key[1], key[2])
            if r["upnp_available"]:
                _log.info(f"UPnP refresh for {server.id}: {r}")
            elif _had_router.get(server.id):
                _log.warning(f"UPnP refresh for {server.id}: the router stopped answering (restarting, or UPnP turned off).")
            _had_router[server.id] = r["upnp_available"]
        except Exception as e:  # noqa: BLE001 - background best-effort
            _log.warning(f"UPnP refresh failed: {e}")

    threading.Thread(target=work, name=f"upnp-refresh-{server.id}", daemon=True).start()


def get_public_ip(timeout: float = 4.0) -> Optional[str]:
    """For forwarding instructions and the double-NAT check only, never -MULTIHOME."""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=timeout) as resp:
            ip = resp.read().decode("utf-8", errors="replace").strip()
    except Exception as e:  # noqa: BLE001
        _log.info(f"Couldn't detect public IP: {e}")
        return None
    return ip if network_utils.is_valid_ipv4(ip) else None


# ------------------------------- Web UI port (TCP) for home-network devices --

WEB_RULE_PREFIX = "ConanOps-web-"


def web_rule_remove_script() -> str:
    return (
        "Get-NetFirewallRule -Group 'ConanOps' -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Name -like {_ps_str(WEB_RULE_PREFIX + '*')} }} | "
        "Remove-NetFirewallRule -ErrorAction SilentlyContinue\n"
    )


def allow_web_port(port: int) -> str:
    """Allows the web UI on `port` from Private/Domain networks only (public stays
    blocked). One UAC prompt. Returns powershell.RUN_OK / RUN_DECLINED / RUN_FAILED."""
    port = int(port)
    script = web_rule_remove_script() + (
        f"New-NetFirewallRule -Name {_ps_str(WEB_RULE_PREFIX + str(port))} "
        f"-DisplayName {_ps_str(f'ConanOps web version ({port}/TCP)')} -Group 'ConanOps' "
        "-Description 'Lets phones and PCs on your home network open the ConanOps web version.' "
        f"-Direction Inbound -Action Allow -Protocol TCP -LocalPort {port} -Profile Private,Domain | Out-Null\n"
        "exit 0\n"
    )
    return _run_ps_privileged(script)


def web_port_allowed(port: int) -> Optional[bool]:
    proc = _run_ps_readonly(
        f"$r = Get-NetFirewallRule -Name {_ps_str(WEB_RULE_PREFIX + str(int(port)))} -ErrorAction SilentlyContinue\n"
        "if ($r -and [string]$r.Enabled -eq 'True') { 'YES' } else { 'NO' }\nexit 0\n")
    if proc is None or proc.returncode != 0:
        return None
    return (proc.stdout or "").strip().endswith("YES")
