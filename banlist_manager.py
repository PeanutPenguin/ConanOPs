"""
Whitelist and ban management.

ConanOps keeps the authoritative list (SteamID64 strings) on the
ServerConfig and pushes it to the running server via RCON commands when
changed, since that takes effect immediately without a restart. A local
text file is also written as a human-readable backup/reference.

Caveat, stated plainly: the exact RCON command names for
ban/kick/whitelist can vary by Conan Exiles version, and this module's
command strings (`banplayer`, `unbanplayer`, `kickplayer`) reflect the
commonly-documented set at the time this was written. If a command
doesn't take effect, checking the current in-game `listcommands` RCON
output against these strings is the first thing to try -- same category
of caveat as mod_manager's file format, not something pre-flight checks
can catch.
"""
from __future__ import annotations

import os
from typing import List

import ini_utils
import rcon
from models import ServerConfig


def _list_path(install_dir: str, kind: str) -> str:
    # These are the actual filenames Conan Exiles itself reads from
    # ConanSandbox/Saved/ -- "whitelist.txt" and "blacklist.txt" (ban),
    # one SteamID64 per line, no other formatting. A previous version
    # wrote to "ConanOps_whitelist.txt"/"ConanOps_banlist.txt" instead,
    # which the server has no reason to ever look at, so nothing
    # written there took effect even after a restart.
    filename = "whitelist.txt" if kind == "whitelist" else "blacklist.txt"
    return os.path.join(install_dir, "ConanSandbox", "Saved", filename)


def set_whitelist_enabled(server: ServerConfig) -> None:
    """Writes EnableWhitelist to ServerSettings.ini -- whitelist.txt
    alone does nothing; the server only enforces it when this flag is
    also on. Previously nothing wrote this key at all, so toggling
    "Whitelist-only mode" in the UI never actually changed server
    behavior."""
    if not server.install_dir:
        return
    settings_ini = os.path.join(server.install_dir, "ConanSandbox", "Saved", "Config", "WindowsServer", "ServerSettings.ini")
    ini_utils.apply_known_keys(settings_ini, {
        "EnableWhitelist": ("ServerSettings", ini_utils.bool_to_ini(server.whitelist_enabled)),
    })


def write_list_file(install_dir: str, kind: str, ids: List[str]) -> None:
    path = _list_path(install_dir, kind)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(ids) + ("\n" if ids else ""))


def add_whitelist(server: ServerConfig, steam_id: str) -> None:
    if steam_id not in server.whitelist_ids:
        server.whitelist_ids.append(steam_id)
    write_list_file(server.install_dir, "whitelist", server.whitelist_ids)


def remove_whitelist(server: ServerConfig, steam_id: str) -> None:
    server.whitelist_ids = [i for i in server.whitelist_ids if i != steam_id]
    write_list_file(server.install_dir, "whitelist", server.whitelist_ids)


def ban_player(server: ServerConfig, steam_id: str, host: str = "127.0.0.1") -> str:
    """Adds to the local ban bookkeeping and, if RCON is enabled, issues
    the ban immediately. Returns a human-readable status string."""
    if steam_id not in server.banned_ids:
        server.banned_ids.append(steam_id)
    write_list_file(server.install_dir, "banlist", server.banned_ids)

    if not server.rcon_enabled:
        return "Added to ban list. RCON is disabled, so this takes effect on next server restart."
    try:
        response = rcon.send_command(host, server.rcon_port, server.rcon_password, f"banplayer {steam_id}")
        return f"Banned immediately via RCON. Server response: {response or '(no output)'}"
    except rcon.RconError as e:
        return f"Added to ban list, but RCON command failed: {e}"


def unban_player(server: ServerConfig, steam_id: str, host: str = "127.0.0.1") -> str:
    server.banned_ids = [i for i in server.banned_ids if i != steam_id]
    write_list_file(server.install_dir, "banlist", server.banned_ids)

    if not server.rcon_enabled:
        return "Removed from ban list. RCON is disabled, so this takes effect on next server restart."
    try:
        response = rcon.send_command(host, server.rcon_port, server.rcon_password, f"unbanplayer {steam_id}")
        return f"Unbanned immediately via RCON. Server response: {response or '(no output)'}"
    except rcon.RconError as e:
        return f"Removed from ban list, but RCON command failed: {e}"


def kick_player(server: ServerConfig, player_name: str, host: str = "127.0.0.1") -> str:
    if not server.rcon_enabled:
        return "RCON is disabled -- can't kick a player live. Enable RCON in RCON & Alerts settings."
    try:
        response = rcon.send_command(host, server.rcon_port, server.rcon_password, f"kickplayer {player_name}")
        return f"Kicked. Server response: {response or '(no output)'}"
    except rcon.RconError as e:
        return f"Kick failed: {e}"


def broadcast_message(server: ServerConfig, message: str, host: str = "127.0.0.1") -> bool:
    """Sends an in-game broadcast to everyone currently connected --
    used to warn players a restart is about to happen (see
    MainWindow._warn_then_restart) rather than just disconnecting them
    with zero notice. Returns whether the RCON command was sent
    successfully; best-effort by design, since a restart that's about
    to happen shouldn't be blocked on a broadcast failing to send --
    worst case, players just don't get the heads-up this time.

    Same caveat as this module's own docstring already states for
    kickplayer/banplayer/unbanplayer: `broadcast` is a commonly-
    documented RCON command for Conan Exiles, not an officially
    published one, so it's exactly the kind of thing to check against
    live `listcommands` RCON output first if it doesn't seem to work."""
    if not server.rcon_enabled:
        return False
    try:
        rcon.send_command(host, server.rcon_port, server.rcon_password, f"broadcast {message}")
        return True
    except rcon.RconError:
        return False
