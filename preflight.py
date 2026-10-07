"""
Pre-flight validation: checks that everything a server needs is actually
where it should be, and auto-fixes what it safely can. Every fix is
returned in the result so the UI can show "auto-repaired: X" rather than
silently patching things.

Runs before: launch, scheduled auto-update, and on demand from the UI.
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

    # 1. Install folder
    if not os.path.isdir(server.install_dir):
        result.ok = False
        result.problems.append(f"Install folder not found: {server.install_dir}")
    else:
        # 2. Server exe present (catches the Enhanced folder-rename risk)
        exe = process_manager.server_exe_path(server.install_dir)
        if not os.path.exists(exe):
            result.ok = False
            result.problems.append(f"Server executable not found: {exe}")

    # 3. Log directory reachable (created on first run, so just note it,
    #    don't fail preflight over a folder that legitimately doesn't
    #    exist yet on a server that's never launched).
    log_dir = os.path.join(server.install_dir, "ConanSandbox", "Saved", "Logs")
    if os.path.isdir(server.install_dir) and not os.path.isdir(log_dir):
        result.repairs.append("Log directory did not exist yet (normal for a fresh install).")

    # 3b. Free space on the install drive. A world save that can't be
    #     written is how worlds get corrupted -- don't start into that.
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

    # 3c. Microsoft Visual C++ runtime. Only a definite "missing" fails
    #     here (the server can't start without it); an older version is
    #     left to Diagnostics, since it usually still works.
    if vcredist.status().state == vcredist.STATUS_MISSING:
        result.ok = False
        result.problems.append(
            "The Microsoft Visual C++ runtime the server needs isn't installed. Open Settings > Diagnostics "
            "and click \"Install Visual C++ Runtime\" (Windows will ask for permission once)."
        )

    # 4. SteamCMD present
    if not steamcmd.is_steamcmd_installed(server.steamcmd_dir):
        result.ok = False
        result.problems.append(f"SteamCMD not found at: {server.steamcmd_dir}")

    # 5. Install state healthy (StateFlags == 4 means fully installed)
    if os.path.isdir(server.install_dir):
        state = steamcmd.get_install_state(server.install_dir)
        if state is not None and state != 4:
            result.ok = False
            result.problems.append(f"SteamCMD reports install state {state} (expected 4 / fully installed).")

    # 6. Bind IP still matches a real local interface -- auto-repairable,
    #    but only when it's genuinely gone (the NIC it named no longer
    #    exists), not just because it isn't whichever interface the OS's
    #    default route happens to prefer right now. This respects an
    #    intentional choice on a multi-homed machine instead of silently
    #    overwriting it every time preflight runs (which happens before
    #    every launch, scheduled restart, and auto-update).
    local_ips = network_utils.list_local_ipv4s()
    if not server.bind_ip:
        new_ip = network_utils.get_local_ip()
        if not network_utils.is_usable_lan_ipv4(new_ip):
            # get_local_ip() only falls all the way through to loopback
            # when it genuinely couldn't detect a real LAN interface (no
            # default route, hostname lookup failed too) -- saving that
            # as the "auto-detected" bind IP used to be treated as a
            # successful repair, which would silently configure the
            # server to bind only to itself: unreachable from the LAN
            # or internet, with nothing anywhere saying so. Surface it
            # as a real problem instead so it doesn't launch this way.
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
    # else: local_ips came back empty (couldn't enumerate interfaces) or
    # bind_ip is still a valid local address -- leave it alone either way.

    # 7. Router forwards: refresh in the background before every launch,
    #    so a bind IP that just changed (DHCP, repaired above) gets its
    #    UPnP forwards re-pointed, and leases on routers that refuse
    #    permanent ones get renewed. Never blocks or fails preflight.
    if result.ok and network_setup.UPNP_REFRESH_ENABLED:
        try:
            network_setup.refresh_upnp_async(server)
        except Exception:  # noqa: BLE001 - best-effort
            pass

    return result
