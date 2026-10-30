"""
"Can people on the internet see this server?" from outside the PC.

Two independent signals, neither of which needs a server of our own:

1. Steam's public list of servers at an internet address (ISteamApps/
   GetServersAtAddress). A server appears there once it has registered with
   Steam, which needs working outbound traffic and its query port answering
   Steam. Not being listed after a few minutes is a reliable sign of a
   problem. Being listed is a good sign but not proof that players can join:
   some routers let Steam's replies back in without a port forward.
2. A status query sent to the PC's own public address. Only a router that
   forwards the port answers it (and only if it supports "NAT loopback"), so
   an answer proves the forward works; no answer proves nothing.

No Qt; call off the GUI thread.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

import applog

_log = applog.get_logger(__name__)

API_URL = "https://api.steampowered.com/ISteamApps/GetServersAtAddress/v1/"
CONAN_APP_IDS = {440900, 443030}  # the game (servers list under it) and the dedicated server

LISTED = "listed"
NOT_LISTED = "not_listed"
UNKNOWN = "unknown"  # Steam couldn't be asked


@dataclass
class SteamListing:
    state: str
    entries: List[dict] = field(default_factory=list)  # every server Steam lists at that address
    match: Optional[dict] = None                       # the one that is this server
    error: str = ""


def _fetch(public_ip: str, timeout: float) -> dict:
    url = API_URL + "?" + urllib.parse.urlencode({"addr": public_ip, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": "ConanOps"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def steam_listing(public_ip: str, game_port: int, query_port: int, timeout: float = 6.0) -> SteamListing:
    """Whether Steam lists a server with these ports at `public_ip`."""
    if not public_ip:
        return SteamListing(UNKNOWN, error="the public address isn't known")
    try:
        data = _fetch(public_ip, timeout)
    except Exception as e:  # noqa: BLE001 - offline, blocked, Steam down: all "couldn't ask"
        _log.info(f"Steam server list lookup failed: {e}")
        return SteamListing(UNKNOWN, error=str(e))
    resp = data.get("response") or {}
    if not resp.get("success", False):
        return SteamListing(UNKNOWN, error=str(resp.get("message") or "Steam said the request failed"))
    entries = [e for e in resp.get("servers") or [] if isinstance(e, dict)]
    for e in entries:
        try:
            addr_port = int(str(e.get("addr", "")).rsplit(":", 1)[-1])
        except ValueError:
            addr_port = -1
        same_port = int(e.get("gameport") or -1) == int(game_port) or addr_port == int(query_port)
        app_ok = not e.get("appid") or int(e.get("appid")) in CONAN_APP_IDS
        if same_port and app_ok:
            return SteamListing(LISTED, entries=entries, match=e)
    return SteamListing(NOT_LISTED, entries=entries)


def answers_on_public_address(public_ip: str, query_port: int, timeout: float = 2.0) -> bool:
    """True if the server answered a status query sent to the public address."""
    if not public_ip:
        return False
    try:
        import network_utils
        return network_utils.query_a2s_info(public_ip, query_port, timeout=timeout) is not None
    except Exception:  # noqa: BLE001
        return False
