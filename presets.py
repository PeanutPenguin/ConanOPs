"""
Presets: a few named choices for each group of settings ("Standard backups",
"Relaxed play style", "Hands-off"), so most people pick one and never see
the individual settings. The individual settings stay available in Advanced
mode; changing one there makes the group "Custom" unless the new values
match another preset.

Values are keyed by the same names the settings pages register their fields
under (ini keys for gameplay, ServerConfig attribute names, or App Settings
control names). No Qt here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

CUSTOM = "custom"


@dataclass
class Option:
    id: str
    name: str
    summary: str
    values: Optional[Dict[str, object]] = None  # None: the choice itself is the setting (see Group.choice_key)
    recommended: bool = False


@dataclass
class Group:
    key: str
    title: str
    help: str
    options: List[Option] = field(default_factory=list)
    # For groups whose choice IS one setting (e.g. mod_recovery_mode), that setting's name.
    choice_key: str = ""

    def option(self, option_id: str) -> Optional[Option]:
        return next((o for o in self.options if o.id == option_id), None)

    def keys(self) -> List[str]:
        """Every setting this group's presets set."""
        if self.choice_key:
            return [self.choice_key]
        out: List[str] = []
        for o in self.options:
            for k in (o.values or {}):
                if k not in out:
                    out.append(k)
        return out


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-6
    return str(a).strip() == str(b).strip()


def values_for(group: Group, option_id: str, resolve: Optional[Callable[[str, dict], dict]] = None) -> Dict[str, object]:
    """The settings an option sets. `resolve(option_id, values)` may adjust
    them for this server (e.g. the restart window from its player history)."""
    o = group.option(option_id)
    if o is None:
        return {}
    if group.choice_key:
        return {group.choice_key: o.id}
    values = dict(o.values or {})
    return resolve(option_id, values) if resolve else values


def match(group: Group, current: Dict[str, object], resolve: Optional[Callable[[str, dict], dict]] = None) -> str:
    """The option whose settings equal `current`, or CUSTOM."""
    for o in group.options:
        want = values_for(group, o.id, resolve)
        if want and all(k in current and _same(current[k], v) for k, v in want.items()):
            return o.id
    return CUSTOM


# ----------------------------------------------------------------- groups --
# Gameplay values are Funcom's ini keys. "Official" is Funcom's defaults.
SERVER_TYPE = Group(
    "server_type", "Server type",
    "Whether players can fight each other and damage each other's bases.",
    [Option("pve", "PvE", "Players can't fight or raid each other.",
            {"PVPEnabled": False, "CanDamagePlayerOwnedStructures": False}),
     Option("conflict", "PvE-Conflict", "Players can fight, but bases can't be damaged.",
            {"PVPEnabled": True, "CanDamagePlayerOwnedStructures": False}),
     Option("pvp", "PvP", "Fighting and raiding are allowed.",
            {"PVPEnabled": True, "CanDamagePlayerOwnedStructures": True})])

PLAY_STYLE = Group(
    "play_style", "Play style",
    "How fast players level, gather and craft, how much damage they take, how fast they get hungry and "
    "whether abandoned bases decay. Every individual gameplay setting is in Advanced.",
    [Option("official", "Official", "Funcom's standard settings.",
            {"PlayerXPRateMultiplier": 1.0, "HarvestAmountMultiplier": 1.0, "ItemConvertionMultiplier": 1.0,
             "PlayerDamageTakenMultiplier": 1.0, "PlayerActiveHungerMultiplier": 1.0,
             "PlayerActiveThirstMultiplier": 1.0, "DisableBuildingAbandonment": False}),
     Option("relaxed", "Relaxed", "2× XP and gathering, faster crafting, bases don't decay.",
            {"PlayerXPRateMultiplier": 2.0, "HarvestAmountMultiplier": 2.0, "ItemConvertionMultiplier": 0.5,
             "PlayerDamageTakenMultiplier": 1.0, "PlayerActiveHungerMultiplier": 0.8,
             "PlayerActiveThirstMultiplier": 0.8, "DisableBuildingAbandonment": True}, recommended=True),
     Option("boosted", "Boosted", "5× XP, 3× gathering, very fast crafting.",
            {"PlayerXPRateMultiplier": 5.0, "HarvestAmountMultiplier": 3.0, "ItemConvertionMultiplier": 0.3,
             "PlayerDamageTakenMultiplier": 0.8, "PlayerActiveHungerMultiplier": 0.5,
             "PlayerActiveThirstMultiplier": 0.5, "DisableBuildingAbandonment": True}),
     Option("hardcore", "Hardcore", "Slower progress, more damage, faster hunger.",
            {"PlayerXPRateMultiplier": 0.5, "HarvestAmountMultiplier": 0.8, "ItemConvertionMultiplier": 1.5,
             "PlayerDamageTakenMultiplier": 1.5, "PlayerActiveHungerMultiplier": 1.5,
             "PlayerActiveThirstMultiplier": 1.5, "DisableBuildingAbandonment": False})])

BACKUPS = Group(
    "backups", "Backups",
    "Copies of the world, so it can be put back after a problem. Older copies are removed automatically.",
    [Option("standard", "Standard", "Every 6 hours. Keeps a week of daily and a month of weekly copies.",
            {"backup_interval_hours": 6, "backup_daily_keep": 7, "backup_weekly_keep": 4,
             "backup_before_update": True}, recommended=True),
     Option("frequent", "Frequent", "Every 2 hours. Keeps two weeks of daily copies.",
            {"backup_interval_hours": 2, "backup_daily_keep": 14, "backup_weekly_keep": 8,
             "backup_before_update": True}),
     Option("light", "Light", "Once a day. Uses the least disk space.",
            {"backup_interval_hours": 24, "backup_daily_keep": 3, "backup_weekly_keep": 2,
             "backup_before_update": True})])

RESTART = Group(
    "restart", "Daily restart",
    "A restart once a day keeps the server running smoothly. It only happens when nobody is online; if "
    "someone stays on for the whole window, that day is skipped.",
    [Option("quiet", "Quietest hours", "When players are usually offline, from this server's history.",
            {"restart_enabled": True, "restart_start": "04:00", "restart_end": "06:00"}, recommended=True),
     Option("early", "Early morning", "Every night between 4 and 6 AM.",
            {"restart_enabled": True, "restart_start": "04:00", "restart_end": "06:00"}),
     Option("never", "Never", "Only restarts for updates and crashes.",
            {"restart_enabled": False})])

UPDATES = Group(
    "updates", "Game updates",
    "Players with a newer version of the game can't join an outdated server, so updates matter. A backup "
    "is always made first.",
    [Option("auto", "Automatic", "Installs updates when nobody is online.",
            {"auto_update": True, "auto_update_check_interval_hours": 6}, recommended=True),
     Option("tell", "Tell me", "Checks every few hours and lets you choose when to update.",
            {"auto_update": False, "auto_update_check_interval_hours": 6})])

MOD_RECOVERY = Group(
    "mod_recovery", "If a mod stops a server starting",
    "ConanOps finds the broken mod by itself, with the world protected, then does one of these. This "
    "applies to all your servers.",
    [Option("wait", "Wait for a fix", "Keeps the server stopped and starts it once the mod is updated. "
            "Nothing is lost.", recommended=True),
     Option("start_without", "Start without it", "Players get back sooner; that mod's items and "
            "buildings are removed (a backup is made first)."),
     Option("alert", "Just tell me", "Sends an alert and does nothing else.")],
    choice_key="mod_recovery_mode")

RUN = Group(
    "run", "How ConanOps runs",
    "Whether ConanOps starts by itself and keeps your servers going, so nobody has to be at the PC.",
    [Option("hands_off", "Hands-off", "Starts with Windows, reopens if it closes, keeps the PC awake and "
            "limits Windows update restarts to 4–6 AM.",
            {"start_with_windows": True, "keep_alive": True, "keep_awake": True, "update_window": True},
            recommended=True),
     Option("signed_in", "While I'm signed in", "Starts with Windows and keeps the PC awake. Windows "
            "restarts for updates whenever it likes.",
            {"start_with_windows": True, "keep_alive": False, "keep_awake": True, "update_window": False}),
     Option("manual", "Only when I open it", "Servers run only while ConanOps is open.",
            {"start_with_windows": False, "keep_alive": False, "keep_awake": True, "update_window": False})])

REMOTE = Group(
    "remote", "Use ConanOps from another device",
    "A web version of ConanOps that opens in a browser. Everyone signs in with the password you set below.",
    [Option("off", "Off", "Only this PC can control your servers.",
            {"web_on": False, "web_remote": False}, recommended=True),
     Option("wifi", "On my Wi-Fi", "From a phone or laptop at home.",
            {"web_on": True, "web_remote": False}),
     Option("anywhere", "From anywhere", "A secure link that also works on mobile data.",
            {"web_on": True, "web_remote": True})])

APP_UPDATES = Group(
    "app_updates", "ConanOps updates",
    "New versions of ConanOps itself. The previous version is kept in case the new one fails to start; your "
    "servers keep running while it updates.",
    [Option("auto", "Automatic", "Installs new versions by itself.",
            {"auto_check": True, "auto_install": True}, recommended=True),
     Option("ask", "Ask me first", "Shows a notice; you choose when.",
            {"auto_check": True, "auto_install": False}),
     Option("manual", "Only when I check", "Never looks for updates by itself.",
            {"auto_check": False, "auto_install": False})])

ALL = {g.key: g for g in (SERVER_TYPE, PLAY_STYLE, BACKUPS, RESTART, UPDATES, MOD_RECOVERY, RUN, REMOTE,
                          APP_UPDATES)}
