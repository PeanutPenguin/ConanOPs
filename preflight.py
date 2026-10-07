"""
Pre-flight validation before launch, auto-update, or on demand. Auto-fixes
what it safely can and reports every repair so nothing is patched silently.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from models import ServerConfig
import network_setup
import network_utils
import process_manager
import steamcmd
import vcredist


MIN_FREE_BYTES_TO_LAUNCH = 1024 ** 3


@dataclass
class CheckResult:
    ok: bool
    problems: List[str] = field(default_factory=list)
    repairs: List[str] = field(default_factory=list)


def run_preflight(server: ServerConfig) -> CheckResult:
    result = CheckResult(ok=True)

    if not os.path.isdir(server.install_dir):
        result.ok = False
        result.problems.append(f"Install folder not found: {server.install_dir}")
    else:
        # Also catches the Enhanced folder-rename risk.
        exe = process_manager.server_exe_path(server.install_dir)
        if not os.path.exists(exe):
            result.ok = False
            result.problems.append(f"Server executable not found: {exe}")

    # The log folder is created on first run, so a missing one is only noted.
    log_dir = os.path.join(server.install_dir, "ConanSandbox", "Saved", "Logs")
    if os.path.isdir(server.install_dir) and not os.path.isdir(log_dir):
        result.repairs.append("Log directory did not exist yet (normal for a fresh install).")

    # A world save that can't be written corrupts the world.
    if os.path.isdir(server.install_dir):
        try:
            import shutil
            free = shutil.disk_usage(server.install_dir).free
            if free < MIN_FREE_BYTES_TO_LAUNCH:
                result.ok = False
                result.problems.append(
                    f"Only {free / 1024 ** 3:.2f} GB free on the server's drive -- not enough to safely save "
                    f"the world. Free up space (at least {MIN_FREE_BYTES_TO_LAUNCH / 1024 ** 3:.0f} GB) first."
                )
        except OSError:
            pass

    # Only a definitely missing VC++ runtime fails; an old one usually still works.
    if vcredist.status().state == vcredist.STATUS_MISSING:
        result.ok = False
        result.problems.append(
            "The Microsoft Visual C++ runtime the server needs isn't installed. Open Settings > Diagnostics "
            "and click \"Install Visual C++ Runtime\" (Windows will ask for permission once)."
        )

    if not steamcmd.is_steamcmd_installed(server.steamcmd_dir):
        result.ok = False
        result.problems.append(f"SteamCMD not found at: {server.steamcmd_dir}")

    # StateFlags == 4 means fully installed.
    if os.path.isdir(server.install_dir):
        state = steamcmd.get_install_state(server.install_dir)
        if state is not None and state != 4:
            result.ok = False
            result.problems.append(f"SteamCMD reports install state {state} (expected 4 / fully installed).")

    # Re-detect the bind IP only if its interface is gone, so an intentional
    # choice on a multi-homed machine is respected.
    local_ips = network_utils.list_local_ipv4s()
    if not server.bind_ip:
        new_ip = network_utils.get_local_ip()
        if not network_utils.is_usable_lan_ipv4(new_ip):
            # Loopback would make the server unreachable from the network.
            result.ok = False
            result.problems.append(
                "Couldn't auto-detect a real network interface for the Bind IP (only loopback/127.0.0.1 "
                "was found) -- set it manually on the Network & Ports settings page."
            )
        else:
            server.bind_ip = new_ip
            result.repairs.append(f"Bind IP wasn't set; auto-detected as {new_ip}.")
    elif local_ips and server.bind_ip not in local_ips:
        old = server.bind_ip
        new_ip = network_utils.get_local_ip()
        if not network_utils.is_usable_lan_ipv4(new_ip):
            result.ok = False
            result.problems.append(
                f"Bind IP {old} is no longer a local interface, and a replacement couldn't be auto-detected "
                "(only loopback/127.0.0.1 was found) -- set it manually on the Network & Ports settings page."
            )
        else:
            server.bind_ip = new_ip
            result.repairs.append(f"Bind IP was stale ({old}, no longer a local interface); re-detected as {new_ip}.")
    # Refresh UPnP forwards in the background so a changed bind IP is
    # re-pointed and expiring leases renewed. Never blocks preflight.
    if result.ok and network_setup.UPNP_REFRESH_ENABLED:
        try:
            network_setup.refresh_upnp_async(server)
        except Exception:  # noqa: BLE001 - best-effort
            pass

    return result
