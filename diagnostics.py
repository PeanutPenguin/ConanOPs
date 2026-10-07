"""Read-only "why isn't this server working?" checks for the Settings > Diagnostics tab.

Covers install files, runtime prerequisites, ports, firewall (checked live),
router forwarding, the running process/RCON, and backup folders. Never changes
anything; preflight.py does the automatic repairs.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import List, Optional

import network_setup
import network_utils
import proc_utils
import process_manager
import steamcmd
import vcredist
import windows_update
import backup_manager
from models import ServerConfig

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"

VC_RUNTIME_TITLE = "Visual C++ runtime"
AUTO_SIGN_IN_TITLE = "Sign back in after updates"

# Below the error threshold, backups and update downloads are likely to fail.
LOW_DISK_SPACE_ERROR_GB = 1.0
LOW_DISK_SPACE_WARNING_GB = 5.0


@dataclass
class DiagnosticResult:
    title: str
    status: str  # STATUS_OK / STATUS_WARNING / STATUS_ERROR
    message: str
    detail: str = ""  # technical detail, shown on hover


def _check_writable(path: str, label: str, results: List[DiagnosticResult]) -> None:
    if not os.path.isdir(path):
        return  # existence is checked by the caller
    if os.access(path, os.W_OK):
        results.append(DiagnosticResult(
            label, STATUS_OK, f"{label} is writable.",
            detail=f"Checked with os.access(\"{path}\", os.W_OK).",
        ))
    else:
        results.append(DiagnosticResult(
            label, STATUS_ERROR,
            f"{label} ({path}) isn't writable by ConanOps. Check the folder's permissions, "
            f"or that it isn't marked read-only.",
            detail=f"os.access(\"{path}\", os.W_OK) returned False. Common causes: the folder or a "
                    f"parent is marked read-only, the current Windows user account doesn't have Write "
                    f"permission in the folder's security settings, or it's on a read-only network share.",
        ))


def _check_disk_space(path: str, results: List[DiagnosticResult]) -> None:
    try:
        usage = shutil.disk_usage(path)
    except OSError as e:
        results.append(DiagnosticResult(
            "Disk space", STATUS_WARNING, f"Couldn't check free disk space for {path}: {e}",
            detail=f"shutil.disk_usage(\"{path}\") raised {type(e).__name__}: {e}",
        ))
        return
    free_gb = usage.free / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)
    detail = (
        f"shutil.disk_usage(\"{path}\") -- total {total_gb:.1f} GB, free {free_gb:.2f} GB. "
        f"Thresholds: error below {LOW_DISK_SPACE_ERROR_GB:.0f} GB, warning below {LOW_DISK_SPACE_WARNING_GB:.0f} GB free."
    )
    if free_gb < LOW_DISK_SPACE_ERROR_GB:
        results.append(DiagnosticResult(
            "Disk space", STATUS_ERROR,
            f"Only {free_gb:.1f} GB free on this drive -- updates and backups will likely "
            f"fail until space is freed up.",
            detail=detail,
        ))
    elif free_gb < LOW_DISK_SPACE_WARNING_GB:
        results.append(DiagnosticResult(
            "Disk space", STATUS_WARNING,
            f"Only {free_gb:.1f} GB free on this drive -- worth freeing up space before it "
            f"becomes a problem for updates or backups.",
            detail=detail,
        ))
    else:
        results.append(DiagnosticResult("Disk space", STATUS_OK, f"{free_gb:.0f} GB free on this drive.", detail=detail))


def run_diagnostics(server: ServerConfig, reserved_ports: Optional[set] = None) -> List[DiagnosticResult]:
    """Runs every check, most fundamental first (install, network, runtime, backups)."""
    results: List[DiagnosticResult] = []
    reserved_ports = reserved_ports or set()

    # ------------------------------------------------------- setup/install --
    if not server.install_dir:
        results.append(DiagnosticResult(
            "Install folder", STATUS_ERROR,
            "No install folder is configured for this server yet. Run the setup wizard "
            "(Dashboard, \"Set Up Server\") before anything else here will matter.",
            detail="server.install_dir is empty in ConanOps' saved configuration for this server.",
        ))
        return results  # nothing else is meaningful to check yet

    if not os.path.isdir(server.install_dir):
        results.append(DiagnosticResult(
            "Install folder", STATUS_ERROR,
            f"The configured install folder doesn't exist: {server.install_dir} "
            f"-- it may have been moved or deleted. Re-run the setup wizard to reinstall.",
            detail=f"os.path.isdir(\"{server.install_dir}\") returned False.",
        ))
        return results

    _check_writable(server.install_dir, "Install folder permissions", results)
    _check_disk_space(server.install_dir, results)

    if server.steamcmd_dir and not steamcmd.is_steamcmd_installed(server.steamcmd_dir):
        results.append(DiagnosticResult(
            "SteamCMD", STATUS_ERROR,
            f"SteamCMD isn't installed at {server.steamcmd_dir}. Go to Updates and click "
            f"Update Now to install it, or re-run the setup wizard.",
            detail=f"steamcmd.is_steamcmd_installed(\"{server.steamcmd_dir}\") returned False -- "
                    f"steamcmd.exe wasn't found in that folder.",
        ))
    else:
        results.append(DiagnosticResult(
            "SteamCMD", STATUS_OK, "SteamCMD is installed.",
            detail=f"steamcmd.exe found under {server.steamcmd_dir}.",
        ))

    exe_path = process_manager.server_exe_path(server.install_dir)
    if not os.path.exists(exe_path):
        results.append(DiagnosticResult(
            "Server files", STATUS_ERROR,
            f"The server executable isn't present at {exe_path}. The server files may not "
            f"have finished downloading -- go to Updates and click Update Now.",
            detail=f"os.path.exists(\"{exe_path}\") returned False.",
        ))
    else:
        state = steamcmd.get_install_state(server.install_dir)
        if state is not None and state != 4:
            results.append(DiagnosticResult(
                "Server files", STATUS_WARNING,
                f"SteamCMD reports the install isn't fully complete (state {state}). "
                f"Go to Updates and click Update Now to finish or repair it.",
                detail=f"Read from the appmanifest .acf file's StateFlags in {server.install_dir}\\steamapps. "
                        f"4 means fully installed; anything else means an update, validation, or download "
                        f"is incomplete or was interrupted.",
            ))
        else:
            results.append(DiagnosticResult(
                "Server files", STATUS_OK, "Server files look installed correctly.",
                detail=f"Executable found at {exe_path}; appmanifest StateFlags reports 4 (fully installed).",
            ))

    rt = vcredist.status()
    if rt.state == vcredist.STATUS_MISSING:
        results.append(DiagnosticResult(
            VC_RUNTIME_TITLE, STATUS_ERROR,
            "The Microsoft Visual C++ runtime the Conan server needs isn't installed, so the server can't "
            "start. Click the button below to install it.",
            detail="No VC++ 2015-2022 x64 runtime registry entry, and vcruntime140.dll / vcruntime140_1.dll / "
                   "msvcp140.dll aren't all in System32.",
        ))
    elif rt.state == vcredist.STATUS_OUTDATED:
        results.append(DiagnosticResult(
            VC_RUNTIME_TITLE, STATUS_WARNING,
            f"The Microsoft Visual C++ runtime is an older version ({rt.version_text}). If the server fails to "
            f"start or crashes on launch, install the latest one with the button below.",
            detail=f"Installed {rt.version_text}; ConanOps treats "
                   f"{'.'.join(map(str, vcredist.MIN_VERSION))} or newer as current.",
        ))
    elif rt.state == vcredist.STATUS_OK:
        results.append(DiagnosticResult(
            VC_RUNTIME_TITLE, STATUS_OK, "The Microsoft Visual C++ runtime is installed.",
            detail=f"Version: {rt.version_text}.",
        ))

    sign_in = windows_update.auto_sign_in_status()
    if sign_in == windows_update.AUTO_SIGN_IN_OFF:
        results.append(DiagnosticResult(
            AUTO_SIGN_IN_TITLE, STATUS_WARNING,
            "Windows won't sign you back in after an update restart, so the PC waits at the sign-in screen and "
            "your servers stay down until someone signs in. Turn on \"Use my sign-in info to automatically "
            "finish setting up after an update\" (button below).",
            detail="UserARSO OptOut = 1 for this account.",
        ))
    elif sign_in == windows_update.AUTO_SIGN_IN_BLOCKED:
        results.append(DiagnosticResult(
            AUTO_SIGN_IN_TITLE, STATUS_WARNING,
            "A policy on this PC (usually a work or school one) stops Windows signing you back in after "
            "update restarts, so servers stay down after one until someone signs in.",
            detail="HKLM Policies\\System DisableAutomaticRestartSignOn = 1.",
        ))
    elif sign_in == windows_update.AUTO_SIGN_IN_ON:
        results.append(DiagnosticResult(
            AUTO_SIGN_IN_TITLE, STATUS_OK,
            "Windows signs you back in after update restarts, so ConanOps and your servers come back by themselves.",
        ))
    else:
        results.append(DiagnosticResult(
            AUTO_SIGN_IN_TITLE, STATUS_WARNING,
            "ConanOps couldn't tell whether Windows signs you back in after update restarts. Check that "
            "\"Use my sign-in info to automatically finish setting up after an update\" is on (button below) -- "
            "otherwise servers stay down after an update restart until someone signs in.",
            detail="No UserARSO OptOut value for this account; Windows' default applies.",
        ))

    # ------------------------------------------------------------- network --
    if server.game_port == server.query_port:
        results.append(DiagnosticResult(
            "Ports", STATUS_ERROR,
            "Game port and Query port are set to the same number -- they need to be "
            "different. Fix this on the Network & Ports settings tab.",
            detail=f"Both are set to {server.game_port}.",
        ))
    elif server.query_port == server.game_port + 1:
        results.append(DiagnosticResult(
            "Ports", STATUS_ERROR,
            f"The Query port ({server.query_port}) is the port right above the Game port, which the "
            f"server also uses for itself. Pick a different Query port on the Network & Ports settings tab.",
            detail=f"Game port {server.game_port} implies {server.game_port + 1} is also bound by the server.",
        ))
    else:
        results.append(DiagnosticResult(
            "Ports", STATUS_OK, "Game and Query ports are set to different numbers.",
            detail=f"Game port {server.game_port}, Query port {server.query_port}.",
        ))

    pid = process_manager.find_running_pid(server.install_dir)
    running = pid is not None
    ports_to_check = [
        ("Game", server.game_port),
        ("Game+1", server.game_port + 1),  # Conan's own second UDP port, bound right above the game port
        ("Query", server.query_port),
    ]
    for label, port in ports_to_check:
        if port in reserved_ports:
            results.append(DiagnosticResult(
                f"{label} port ({port})", STATUS_ERROR,
                f"Port {port} is also used by another configured ConanOps server. "
                f"Change one of them on the Network & Ports settings tab.",
                detail=f"Port {port}/UDP appears in the reserved-ports set built from every OTHER "
                        f"configured server's Game/Game+1/Query ports.",
            ))
        elif not running and not network_utils.is_udp_port_free(port):
            results.append(DiagnosticResult(
                f"{label} port ({port})", STATUS_WARNING,
                f"Port {port} is currently in use by something else on this machine, "
                f"and this server isn't running -- it likely won't be able to start "
                f"until that's freed up.",
                detail=f"network_utils.is_udp_port_free({port}) returned False while this server's own "
                        f"process wasn't running (so it can't be this server holding it).",
            ))
        else:
            results.append(DiagnosticResult(
                f"{label} port ({port})", STATUS_OK, f"Port {port} looks available.",
                detail="Not in another server's reserved-ports set, and (if this server isn't "
                        "currently running) not detected as in use by anything else locally.",
            ))

    if server.bind_ip:
        local_ips = network_utils.list_local_ipv4s()
        if local_ips and server.bind_ip not in local_ips:
            results.append(DiagnosticResult(
                "Bind IP", STATUS_WARNING,
                f"The configured bind IP ({server.bind_ip}) doesn't match any current "
                f"network interface on this machine. This gets corrected automatically "
                f"the next time the server starts or restarts, but if you're troubleshooting "
                f"right now, that mismatch may be why it isn't reachable.",
                detail=f"Configured: {server.bind_ip}. Currently detected local IPv4 addresses: "
                        f"{', '.join(sorted(local_ips)) or '(none detected)'}.",
            ))
        else:
            results.append(DiagnosticResult(
                "Bind IP", STATUS_OK, f"Bind IP ({server.bind_ip}) matches a current network interface.",
                detail=f"{server.bind_ip} is in the currently detected local IPv4 addresses: "
                        f"{', '.join(sorted(local_ips))}.",
            ))
    else:
        results.append(DiagnosticResult(
            "Bind IP", STATUS_WARNING,
            "No bind IP is configured yet -- one gets auto-detected the next time the server starts.",
            detail="server.bind_ip is empty in ConanOps' saved configuration for this server.",
        ))

    # Listed just before the firewall rules, which depend on it.
    if proc_utils.is_admin():
        results.append(DiagnosticResult(
            "Administrator rights", STATUS_OK,
            "ConanOps is running with Administrator rights -- Windows Firewall rules can be added/removed.",
            detail="proc_utils.is_admin() -> ctypes IsUserAnAdmin() returned True.",
        ))
    else:
        results.append(DiagnosticResult(
            "Administrator rights", STATUS_WARNING,
            "ConanOps isn't running as Administrator. Windows Firewall rule changes will show a "
            "permission prompt when needed (on the setup wizard's Networking step, or the Network & "
            "Ports settings tab) -- if that prompt gets dismissed or denied, the rules below won't "
            "be added.",
            detail="proc_utils.is_admin() -> ctypes IsUserAnAdmin() returned False.",
        ))

    # Checked live: antivirus or a firewall reset can remove rules behind our back.
    fw_status = network_setup.firewall_status(server.id, server.game_port, server.query_port)
    fw_ports = network_setup._port_labels(server.game_port, server.query_port)
    for r, (label, port) in zip(fw_status, fw_ports):
        rn = network_setup.rule_name(server.id, label, port)
        if not r.checked:
            results.append(DiagnosticResult(
                f"Firewall rule: {r.message}", STATUS_WARNING,
                "Couldn't read Windows Firewall to check this rule.",
                detail=f"Get-NetFirewallRule for '{rn}' didn't run successfully (PowerShell unavailable or blocked).",
            ))
        elif r.success:
            results.append(DiagnosticResult(
                f"Firewall rule: {r.message}", STATUS_OK, "Rule is present and active.",
                detail=f"Windows Firewall has an enabled inbound allow rule named '{rn}'.",
            ))
        else:
            results.append(DiagnosticResult(
                f"Firewall rule: {r.message}", STATUS_ERROR,
                "No active Windows Firewall rule for this port -- incoming connections are likely blocked. "
                "Click \"Repair Networking\" on the Network & Ports settings tab to add it (click Yes if "
                "Windows asks for permission).",
                detail=f"No enabled inbound allow rule named '{rn}' in the '{network_setup.RULE_GROUP}' group.",
            ))

    env = network_setup.firewall_environment(process_manager.server_exe_path(server.install_dir))
    if env.get("checked"):
        if env["block_rules"]:
            results.append(DiagnosticResult(
                "Firewall: server program blocked", STATUS_ERROR,
                "Windows Firewall has a rule BLOCKING the Conan server program itself -- usually created "
                "when the \"allow this app?\" popup was dismissed the first time the server started. A "
                "block rule overrides ConanOps' allow rules. Click \"Repair Networking\" on the Network & "
                "Ports settings tab to turn it off.",
                detail="Enabled inbound block rule(s) for the server exe: " + ", ".join(env["block_rules"]),
            ))
        if env["third_party"]:
            names = ", ".join(env["third_party"])
            results.append(DiagnosticResult(
                "Third-party firewall", STATUS_WARNING,
                f"{names} is managing this PC's firewall. ConanOps' rules go into Windows Firewall, which "
                f"{names} may ignore -- if friends can't connect, allow incoming UDP ports "
                f"{server.game_port}, {server.game_port + 1} and {server.query_port} (or the Conan server "
                f"program) in {names}'s own settings.",
                detail="Windows Security Center (root/SecurityCenter2 FirewallProduct) lists: " + names,
            ))

    # Router: UPnP present, our three ports forwarded here, and double NAT.
    try:
        device = network_setup.discover_igd(timeout=2.0, local_ip=server.bind_ip or None)
    except Exception:  # noqa: BLE001
        device = None
    public_ip = network_setup.get_public_ip(timeout=2.0)
    public_text = public_ip or "(couldn't detect)"
    manual_text = (
        f"UDP {server.game_port}, {server.game_port + 1} (Conan's own second port, right above the game "
        f"port), and {server.query_port}, forwarded to this PC's local IP "
        f"({server.bind_ip or '(not yet detected)'})"
    )
    if device is not None:
        missing = []
        for label, port in fw_ports:
            entry = network_setup.get_port_mapping(device, port, "UDP")
            if not entry or (server.bind_ip and entry.get("NewInternalClient") not in ("", server.bind_ip)):
                missing.append(port)
        if missing:
            results.append(DiagnosticResult(
                "Router UPnP", STATUS_WARNING,
                f"Your router supports automatic forwarding, but port(s) {', '.join(map(str, missing))} "
                f"aren't forwarded to this PC. Click \"Repair Networking\" on the Network & Ports settings "
                f"tab, or forward {manual_text} manually.",
                detail=f"UPnP control URL {device.control_url} ({device.service_type}); "
                       f"GetSpecificPortMappingEntry found no mapping to {server.bind_ip or 'this PC'} for: "
                       f"{', '.join(map(str, missing))}.",
            ))
        else:
            results.append(DiagnosticResult(
                "Router UPnP", STATUS_OK,
                "Your router is forwarding all three ports to this PC.",
                detail=f"UPnP control URL {device.control_url} ({device.service_type}).",
            ))
        external_ip = network_setup.get_external_ip(device)
        double_nat = bool(external_ip and network_utils.is_non_public_ipv4(external_ip)) or bool(
            external_ip and public_ip and external_ip != public_ip
        )
        if double_nat:
            results.append(DiagnosticResult(
                "Double NAT / CGNAT", STATUS_ERROR,
                f"Your router's own internet address ({external_ip}) isn't your public address "
                f"({public_text}). There's another router/modem in front of it, or your ISP uses "
                f"carrier-grade NAT -- forwarding on this router alone can't make the server reachable. "
                f"Put the ISP modem in bridge mode, forward the same ports on it too, or ask your ISP for "
                f"a public IP address.",
                detail=f"UPnP GetExternalIPAddress returned {external_ip}; api.ipify.org reports {public_text}.",
            ))
    else:
        results.append(DiagnosticResult(
            "Router UPnP", STATUS_WARNING,
            f"No UPnP-capable router found -- ports need to be forwarded manually in your router's "
            f"settings: {manual_text}. Your public IP is {public_text}. Most online \"open port\" checkers "
            f"only test TCP, so they can't confirm these UDP ports; the reliable test is a friend outside "
            f"your network connecting to {public_text}:{server.game_port}.",
            detail="SSDP M-SEARCH for an InternetGatewayDevice (sent from the server's LAN adapter) got no "
                   "usable response -- either UPnP is disabled in the router's settings, or the router "
                   "doesn't support it at all.",
        ))

    # -------------------------------------------------------- runtime state --
    if running:
        info = network_utils.query_a2s_info(server.bind_ip or "127.0.0.1", server.query_port, timeout=2.0)
        if info is None:
            results.append(DiagnosticResult(
                "Running state", STATUS_WARNING,
                "The server process is running, but it isn't answering status queries yet. "
                "That's normal for the first minute or two after starting -- if it's been "
                "much longer than that, it may be stuck; check the Console tab, or consider "
                "restarting it from the Dashboard.",
                detail=f"Process found (pid {pid}). A2S_INFO query to "
                        f"{server.bind_ip or '127.0.0.1'}:{server.query_port} (2s timeout) got no response.",
            ))
        else:
            results.append(DiagnosticResult(
                "Running state", STATUS_OK,
                f"The server is running and responding -- \"{info.get('name', server.name)}\", "
                f"{info.get('players', '?')}/{info.get('max_players', '?')} players connected.",
                detail=f"Process found (pid {pid}). A2S_INFO query to "
                        f"{server.bind_ip or '127.0.0.1'}:{server.query_port} succeeded: {info}.",
            ))

        if server.rcon_enabled:
            results.append(_check_rcon(server))
    else:
        results.append(DiagnosticResult(
            "Running state", STATUS_WARNING,
            "The server isn't currently running. If you expected it to be, check the "
            "Dashboard for a Start/Restart button, or the Console tab for anything logged "
            "before it stopped.",
            detail=f"process_manager.find_running_pid(\"{server.install_dir}\") found no matching process.",
        ))
        if server.rcon_enabled:
            results.append(DiagnosticResult(
                "RCON", STATUS_WARNING, "Can't check RCON -- the server isn't currently running."
            ))

    # -------------------------------------------------------------- backups --
    # The backup source; a missing Saved folder is why backups fail silently.
    saved = backup_manager.saved_dir(server.install_dir)
    if not os.path.isdir(saved):
        results.append(DiagnosticResult(
            "World save", STATUS_ERROR,
            f"The world save folder doesn't exist yet: {saved} -- this is why backups are "
            f"failing (\"couldn't find the server's Saved folder\"). Conan Exiles creates this "
            f"itself the first time the server actually starts up, so if it's never been "
            f"started successfully, start it and check back here or on the Console tab. If "
            f"it HAS been started before, this usually means the configured install folder "
            f"doesn't match where the server is actually running from -- check it above, or "
            f"use \"Open Server Folder\" on the Server Identity settings tab to see what's "
            f"really there.",
            detail=f"os.path.isdir(\"{saved}\") returned False.",
        ))
    else:
        results.append(DiagnosticResult(
            "World save", STATUS_OK, "World save folder exists.", detail=f"Found at {saved}.",
        ))

    if server.backup_destination:
        if os.path.isdir(server.backup_destination):
            if os.access(server.backup_destination, os.W_OK):
                results.append(DiagnosticResult(
                    "Backup folder", STATUS_OK, "Backup folder exists and is writable.",
                    detail=f"os.path.isdir and os.access(..., os.W_OK) both true for {server.backup_destination}.",
                ))
            else:
                results.append(DiagnosticResult(
                    "Backup folder", STATUS_ERROR,
                    f"The backup folder ({server.backup_destination}) isn't writable -- "
                    f"backups will fail until this is fixed.",
                    detail=f"os.access(\"{server.backup_destination}\", os.W_OK) returned False.",
                ))
            _check_disk_space(server.backup_destination, results)
        else:
            results.append(DiagnosticResult(
                "Backup folder", STATUS_OK,
                "Backup folder doesn't exist yet -- it'll be created automatically the "
                "first time a backup runs.",
                detail=f"os.path.isdir(\"{server.backup_destination}\") returned False -- not created yet.",
            ))
    else:
        results.append(DiagnosticResult(
            "Backup folder", STATUS_WARNING,
            "No backup destination is configured yet -- set one on the Backups settings "
            "tab so scheduled and pre-update backups can actually run.",
            detail="server.backup_destination is empty in ConanOps' saved configuration for this server.",
        ))

    return results


def _check_rcon(server: ServerConfig) -> DiagnosticResult:
    """Caller ensures the server is running and RCON is enabled."""
    import rcon
    try:
        with rcon.RconClient("127.0.0.1", server.rcon_port, server.rcon_password, timeout=3.0):
            pass
        return DiagnosticResult(
            "RCON", STATUS_OK, f"RCON connected and authenticated on port {server.rcon_port}.",
            detail=f"TCP connect to 127.0.0.1:{server.rcon_port} succeeded, and SERVERDATA_AUTH "
                    f"authenticated with the configured RCON password.",
        )
    except rcon.RconAuthError:
        return DiagnosticResult(
            "RCON", STATUS_ERROR,
            f"RCON connected on port {server.rcon_port}, but authentication failed -- the RCON "
            f"password configured in ConanOps doesn't match the server's. Check it on the RCON & "
            f"Alerts settings tab.",
            detail=f"TCP connect to 127.0.0.1:{server.rcon_port} succeeded, but SERVERDATA_AUTH_RESPONSE "
                    f"indicated the password was rejected.",
        )
    except rcon.RconError as e:
        return DiagnosticResult(
            "RCON", STATUS_ERROR,
            f"Couldn't connect to RCON on port {server.rcon_port}: {e}. Backups that checkpoint the "
            f"world save via RCON before copying it, and any RCON-based alerts, won't work until "
            f"this is fixed.",
            detail=f"TCP connect to 127.0.0.1:{server.rcon_port} raised: {e}",
        )
