"""
How Server Settings is laid out, shared by the app and the web version:
which sections there are, which settings pages each one shows, and which
settings are shown up front. Everything else sits under "More options" (or,
on Gameplay, under its category) -- nothing is left out, and search finds all
of it. No Qt here.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import ini_field_specs

# Gameplay categories, in Funcom's order (progression first).
GAMEPLAY_PAGES: List[str] = [key for key, _label, _specs in ini_field_specs.CATEGORIES]

# (section key, label, sub-nav heading, one-line description, settings page keys)
SECTIONS: List[Tuple[str, str, str, str, List[str]]] = [
    ("identity", "Server Identity", "Server",
     "How the server shows up and who can do what.", ["identity"]),
    ("network", "Network & Ports", "Server",
     "Name, password and the ports players connect on.", ["network"]),
    ("gameplay", "Gameplay", "Server",
     "The settings people change most are first; every other one is in its category below.", GAMEPLAY_PAGES),
    ("backups", "Backups", "Automation",
     "When backups run and how many are kept, plus your saved backups.", ["backups"]),
    ("restart", "Restart Schedule", "Automation",
     "A daily restart in the quietest hours keeps the server healthy.", ["restart"]),
    ("updates", "Server Updates", "Automation",
     "Conan Exiles server builds from Steam: check, install, and automatic updates.", []),
    ("alerts", "RCON", "Automation",
     "Remote commands, used for saving before stops, kicks, bans and the Console.", ["alerts"]),
    ("diagnostics", "Diagnostics", "Help",
     "Checks the common reasons a server won't start or can't be reached.", []),
]
SECTION_KEYS = [s[0] for s in SECTIONS]

# Settings shown up front; anything else on that page goes under "More options".
# None = everything on the page is shown up front.
COMMON: Dict[str, Optional[List[str]]] = {
    "identity": ["ServerMessageOfTheDay", "AdminPassword", "PVPEnabled", "IsBattlEyeEnabled",
                 "ServerCommunity", "serverRegion"],
    "network": ["name", "password", "game_port", "query_port", "max_players"],
    "backups": ["backup_interval_hours", "backup_daily_keep", "backup_weekly_keep"],
    "restart": None,
    "alerts": ["rcon_enabled"],
}

# Gameplay's "most changed" settings, shown above the categories.
GAMEPLAY_COMMON: List[str] = [
    "PlayerXPRateMultiplier", "HarvestAmountMultiplier", "ItemConvertionMultiplier",
    "PlayerDamageMultiplier", "PlayerDamageTakenMultiplier", "DayCycleSpeedScale",
    "ItemSpoilRateScale", "DisableBuildingAbandonment", "EnablePurge",
]

# Old section/page keys (links, the dashboard's tiles) -> the section now showing them.
_ALIASES = {page: "gameplay" for page in GAMEPLAY_PAGES}


def section_for(key: str) -> str:
    """The section that shows a settings page (or section) key."""
    if key in SECTION_KEYS:
        return key
    return _ALIASES.get(key, SECTION_KEYS[0])


def is_common(page_key: str, field_key: str) -> bool:
    """True if a setting is shown up front rather than under "More options"."""
    if page_key in GAMEPLAY_PAGES:
        return field_key in GAMEPLAY_COMMON
    common = COMMON.get(page_key)
    return common is None or field_key in common


def category_title(page_key: str) -> str:
    return next((label for key, label, _s in ini_field_specs.CATEGORIES if key == page_key), "")


def short_help(text: str) -> str:
    """First sentence of a tooltip, for the one line under a setting."""
    text = " ".join((text or "").split())
    for end in (". ", "? ", "! "):
        i = text.find(end)
        if 0 < i < 140:
            return text[: i + 1]
    return text if len(text) <= 140 else text[:137].rstrip() + "…"
