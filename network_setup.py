"""
Networking setup: opening the ports a Conan server needs so it's actually
reachable from the internet, not just bound locally.

Two independent pieces:
  - Windows Firewall rules -- reliable, always attempted.
  - UPnP port forwarding on the router -- best-effort. Not every router
    has UPnP enabled, so this can legitimately fail; callers should treat
    a UPnP failure as "show the manual forwarding instructions", not as
    an error to alarm the person with.

Firewall rules
--------------
Rules are created through PowerShell's NetSecurity cmdlets (see
powershell.py for how scripts are passed), never through a .bat file or
cmd.exe:

  * The old path wrote `netsh` lines into a batch file. subprocess's
    list2cmdline() escapes for C programs, not cmd.exe, so a server
    named  Bob"s & Co  closed the quote early and ran the rest as its
    own command -- elevated. A `%` in the name was eaten by batch
    variable expansion, and non-ASCII names were mangled because cmd
    reads .bat files in the OEM codepage, not UTF-8.
  * Each rule's unique Name is built ONLY from the server's id (hex),
    the port label and the port number -- never from anything the
    person typed. The friendly name only goes into DisplayName (what the
    Windows Firewall UI shows), as an escaped PowerShell literal.
  * Every rule is in the "ConanOps" group, so "remove everything for
    this server" is a group query, not a parse of localized `netsh`
    output (which only worked on English Windows).
  * Results are read back from cmdlet objects / exit codes, so they
    don't depend on the Windows display language.

All rule changes for one operation go into ONE script -> at most ONE
UAC prompt, and the elevated process is waited on via its real process
handle (proc_utils.run_elevated_and_wait), not a marker file.

Nothing here can log into someone's router for them if UPnP isn't
available -- that's a hard limit.
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


# --------------------------------------------------------------------- #
# Rule naming
# --------------------------------------------------------------------- #

_LABEL_SLUGS = {"Game": "Game", "Game+1": "GamePlus1", "Query": "Query"}


def _safe_id(server_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(server_id or ""))[:32] or "server"


def _port_labels(game_port: int, query_port: int) -> List[tuple]:
    # "Game+1" isn't a port ConanOps' own UI shows or lets you edit --
    # Conan's dedicated server binds it itself, right above the game
    # port, for its own networking. It needs the same inbound allow
    # rule as the game port itself or connections can silently fail.
    return [("Game", game_port), ("Game+1", game_port + 1), ("Query", query_port)]


def rule_name(server_id: str, label: str, port: int) -> str:
    """The rule's unique Name (not its DisplayName). Built only from the
    server id, label and port, so it can never contain anything that
    needs escaping and never changes when the server is renamed."""
    return f"{RULE_PREFIX}{_safe_id(server_id)}-{_LABEL_SLUGS.get(label, 'Port')}-{int(port)}"


_rule_name = rule_name


def _clean_display(text: str) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(text or "")).strip()
    return text[:80] or "Server"


def display_name(server_name: str, label: str, port: int) -> str:
    return f"ConanOps - {_clean_display(server_name)} - {label} ({int(port)}/UDP)"


def legacy_rule_prefix(server_name: str) -> str:
    """DisplayName prefix of rules made by older versions (via netsh,
    named after the server's display name)."""
    return f"ConanOps - {server_name} - "


# --------------------------------------------------------------------- #
# PowerShell plumbing (shared module; aliases kept so tests and callers
# can keep patching these names on this module)
# --------------------------------------------------------------------- #

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
        # Legacy rules (created by netsh in older versions) have GUID
        # Names, never our "ConanOps-" scheme -- that check keeps this
        # from touching a CURRENT rule of another server that has since
        # taken this display name. The display-name match is exact
        # (name + one of the three labels + port), so "Chud" never
        # sweeps up "Chud - PvP"'s rules.
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
# Firewall: read-only status
# --------------------------------------------------------------------- #

def present_rule_names(server_id: str) -> Optional[set]:
    """Names of this server's ConanOps rules that currently exist AND are
    enabled, inbound, allow. None if the check itself couldn't run
    (as opposed to "ran and found nothing"). Reading rules doesn't need
    Administrator rights."""
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
    """Whether a rule with this exact Name exists (any state)."""
    script = (
        f"$r = Get-NetFirewallRule -Name {_ps_str(name)} -ErrorAction SilentlyContinue\n"
        "if ($r) { exit 0 } else { exit 1 }\n"
    )
    proc = _run_ps_readonly(script)
    return bool(proc is not None and proc.returncode == 0)


def firewall_status(server_id: str, game_port: int, query_port: int) -> List[FirewallResult]:
    """Read-only: whether each of the 3 rules add_firewall_rules()
    creates for this server is currently present and active. Used by
    diagnostics.py. One PowerShell call for all three."""
    present = present_rule_names(server_id)
    results = []
    for label, port in _port_labels(game_port, query_port):
        if present is None:
            results.append(FirewallResult(False, f"{label} port {port}", checked=False))
        else:
            results.append(FirewallResult(rule_name(server_id, label, port) in present, f"{label} port {port}"))
    return results


def _exe_block_rules_ps(exe_path: str) -> str:
    """PowerShell pipeline yielding the ENABLED inbound BLOCK rules tied
    to this program. Windows creates these when someone clicks Cancel
    (or unticks every box) on the "allow this app?" popup the first time
    the server starts -- and a block rule beats any allow rule, so the
    port rules alone can't fix it."""
    return (
        f"$exe = {_ps_str(exe_path)}\n"
        "$blockRules = @(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Program -and ([Environment]::ExpandEnvironmentVariables([string]$_.Program) -ieq $exe) } | "
        "Get-NetFirewallRule -ErrorAction SilentlyContinue | "
        "Where-Object { [string]$_.Enabled -eq 'True' -and [string]$_.Action -eq 'Block' -and "
        "[string]$_.Direction -eq 'Inbound' })\n"
    )


def firewall_environment(exe_path: str = "") -> dict:
    """Read-only look at things outside ConanOps' own rules that still
    decide whether the server is reachable:

      block_rules  -- display names of enabled inbound block rules for
                      the server program (see _exe_block_rules_ps)
      third_party  -- names of third-party firewall products registered
                      with Windows Security Center (Norton, Bitdefender,
                      ...). When one is in charge, Windows Firewall rules
                      may simply not apply.
      checked      -- False if the check itself couldn't run.
    """
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
    """Makes this server's inbound UDP allow rules exactly match
    game_port / game_port+1 / query_port: removes ANY existing ConanOps
    rule for this server id (whatever ports it was for -- so re-running
    setup or changing ports never leaves stale rules behind), removes
    legacy netsh-era rules named after `legacy_names`, then adds the
    three rules. With `exe_path`, also DISABLES (doesn't delete) any
    enabled inbound block rule Windows created for the server program
    when someone dismissed its "allow access?" popup -- block beats
    allow, so otherwise the new rules wouldn't help. All in one script
    -> at most one UAC prompt.

    Returns each rule's OBSERVED status afterward (read back from the
    firewall), not whether the add command claimed success."""
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
    """Removes every ConanOps rule for the given server id(s), whatever
    ports they were for, plus legacy netsh-era rules for `legacy_names`.
    Accepts a list so removing several servers is still ONE prompt.
    game_port/query_port are accepted for call compatibility and ignored
    -- rules are found by id, not by rebuilding their exact names."""
    ids = [server_ids] if isinstance(server_ids, str) else list(server_ids)
    legacy = [n for n in legacy_names if n]
    dirs = [d for d in program_dirs if d]
    if not ids and not legacy and not dirs:
        return True
    if dirs:
        # Also the allow/block rules Windows itself created for server
        # programs inside these folders (see cleanup.cleanup_script).
        import cleanup
        return _run_ps_privileged(cleanup.cleanup_script(ids, legacy, dirs)) == RUN_OK
    return _run_ps_privileged(_remove_script(ids, legacy) + "exit 0\n") == RUN_OK


# --------------------------------------------------------------------- #
# Minimal UPnP IGD client: SSDP discovery + SOAP.
# No third-party dependency -- just sockets and stdlib XML/HTTP.
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


_SSDP_MSEARCH = _msearch(_SEARCH_TARGETS[0])  # kept for anything importing it


@dataclass
class UpnpDevice:
    control_url: str
    service_type: str
    location: str = ""


# UPnP error codes worth naming
UPNP_CONFLICT = 718            # ConflictInMappingEntry
UPNP_NO_SUCH_ENTRY = 714       # NoSuchEntryInArray
UPNP_ARRAY_INDEX_INVALID = 713  # SpecifiedArrayIndexInvalid


def _ssdp_locations(timeout: float, local_ip: Optional[str]) -> List[str]:
    """Every distinct LOCATION that answers an IGD search within
    `timeout` -- not just the first responder (a smart TV or NAS can
    answer first). The multicast goes out the LAN adapter explicitly,
    so a VPN owning the default route doesn't swallow it."""
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
    # IP connection before PPP: on combo modems the PPP service is often
    # present but not the one actually connected.
    found.sort(key=lambda d: 0 if "WANIPConnection" in d.service_type else 1)
    return found


def _soap(device: UpnpDevice, action: str, args: List[Tuple[str, object]],
          timeout: float = 5.0) -> Tuple[Optional[Dict[str, str]], Optional[int]]:
    """Calls one SOAP action. Returns (response fields, None) on success
    or (None, upnp_error_code_or_None) on failure. Never raises."""
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
    """Finds the router's UPnP WAN connection service, if UPnP is on.
    Considers every responder, and prefers a service that reports
    itself Connected. Returns None (not an exception) if nothing
    usable responds -- a completely normal outcome."""
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
    # Pre-id mappings ("ConanOps Game Port", ...) count as ours only when
    # they point at THIS machine -- another PC on the LAN might be
    # running its own ConanOps.
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
        # Some IGDv2 routers refuse permanent (0) leases; a long lease is
        # renewed every time the server starts (see refresh_upnp_async).
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
    """Back-compat wrapper (external == internal port is all ConanOps uses)."""
    return map_port(device, external_port, internal_ip, description, protocol=protocol, timeout=timeout) == MAP_OK


def remove_upnp_mappings(server_id: str, ports: Iterable[int] = (), local_ip: Optional[str] = None,
                         device: Optional[UpnpDevice] = None, keep: Iterable[int] = ()) -> int:
    """Removes this server's router mappings. Lists the router's mapping
    table and deletes every entry tagged with this server's id (so stale
    ports from before a port change are caught too); for routers that
    don't support listing, falls back to checking `ports` one by one.
    Ports in `keep` are left alone. Returns how many were removed."""
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
    """Makes the router's forwards for this server exactly
    game/game+1/query -> internal_ip: removes this server's mappings for
    any other port (or pointing at an old IP), adds/refreshes the
    wanted ones, verifies them, and reads the router's own WAN address
    to detect double NAT / carrier-grade NAT."""
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
    """Best-effort attempt to forward all three ports via UPnP. Returns a
    dict describing what succeeded so the UI can show manual fallback
    instructions for whatever didn't."""
    return reconcile_upnp(server_id, internal_ip, game_port, query_port)


# Background refresh before each launch: re-points forwards after a DHCP
# change (preflight may have just re-detected bind_ip) and renews leases
# on routers that refused a permanent one. Throttled per configuration.
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
    """Used only for manual port-forwarding instructions and the
    double-NAT check -- never for -MULTIHOME."""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=timeout) as resp:
            ip = resp.read().decode("utf-8", errors="replace").strip()
    except Exception as e:  # noqa: BLE001
        _log.info(f"Couldn't detect public IP: {e}")
        return None
    return ip if network_utils.is_valid_ipv4(ip) else None


# --------------------------------------------------------------------- #
# The web version's own port (TCP), for phones on the home network.
# --------------------------------------------------------------------- #

WEB_RULE_PREFIX = "ConanOps-web-"


def web_rule_remove_script() -> str:
    return (
        "Get-NetFirewallRule -Group 'ConanOps' -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Name -like {_ps_str(WEB_RULE_PREFIX + '*')} }} | "
        "Remove-NetFirewallRule -ErrorAction SilentlyContinue\n"
    )


def allow_web_port(port: int) -> str:
    """Lets other devices on PRIVATE (home) networks reach the web version
    on `port` (TCP). Public networks -- cafes, dorm Wi-Fi marked public --
    stay blocked; the remote link doesn't need this rule at all. One
    permission prompt. Returns powershell.RUN_OK / RUN_DECLINED / RUN_FAILED."""
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
