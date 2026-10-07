"""
Whitelist and ban management.

The lists (SteamID64 strings) live on ServerConfig, are written to the files
the server reads, and are pushed live over RCON when enabled. The RCON command
names are community-documented, not official; if one stops working, compare
against the server's `listcommands` output.
"""
from __future__ import annotations

import os
from typing import List

import ini_utils
import rcon
from models import ServerConfig


def _list_path(install_dir: str, kind: str) -> str:
    # The exact files Conan Exiles reads from ConanSandbox/Saved/: one SteamID64 per line.
    filename = "whitelist.txt" if kind == "whitelist" else "blacklist.txt"
    return os.path.join(install_dir, "ConanSandbox", "Saved", filename)


def set_whitelist_enabled(server: ServerConfig) -> None:
    """Writes EnableWhitelist to ServerSettings.ini; the server ignores
    whitelist.txt unless this flag is on."""
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
    """Adds to the ban list and bans live via RCON if enabled. Returns a status string."""
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
    """Best-effort in-game broadcast (e.g. a restart warning). Returns whether
    it was sent; callers must not block a restart on failure."""
    if not server.rcon_enabled:
        return False
    try:
        rcon.send_command(host, server.rcon_port, server.rcon_password, f"broadcast {message}")
        return True
    except rcon.RconError:
        return False
