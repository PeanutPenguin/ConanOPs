"""
Every Conan Exiles server setting that Funcom exposes in their own
in-game/ServerSettings.ini admin UI, organized into the same categories
their own settings screen uses (General, Progression, Day/Night,
Survival, Combat, Harvesting, Crafting, Building/Decay, Chat, Purge,
Pets & Hunger).

Source: the community-maintained Conan Exiles wiki's Server
Configuration page, cross-checked against a current hosting provider's
config generator. Key names, defaults, and sections reflect what those
sources documented at the time this was written.

DELIBERATELY EXCLUDED: Funcom's own wiki lists a further ~80 "unexposed
server settings" (accessible via the in-game console's
`GetAllServerSettings`) with an explicit caution: "These settings remain
hidden for a reason. These can have an extremely negative impact on your
gameplay experience. Use with caution." ConanOps does not surface those,
on the same reasoning Funcom didn't put them in their own UI. If you
need one of them anyway, it can still be hand-edited in the real `.ini`
file -- `ini_utils.apply_known_keys()` will never touch or remove a line
it doesn't recognize, so a hand-added unexposed setting sits there
untouched by anything ConanOps does.

Every value lives in `ServerConfig.gameplay`, a flat dict keyed by the
real ini key name (e.g. `server.gameplay["PVPEnabled"]`). Keys starting
with `__` (currently just `__description`) are ConanOps-only bookkeeping
that is never written to any `.ini` file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

DEFAULT_SECTION = "ServerSettings"  # ConanSandbox/Saved/Config/WindowsServer/ServerSettings.ini


@dataclass
class FieldSpec:
    key: str                       # the real ini key (or __-prefixed ConanOps-only key)
    label: str
    kind: str                      # "bool" | "int" | "float" | "choice" | "text"
    default: Any
    tooltip: str = ""
    section: str = DEFAULT_SECTION
    min: Optional[float] = None
    max: Optional[float] = None
    step: float = 1.0
    decimals: int = 1
    choices: Optional[List[Tuple[str, Any]]] = None  # [(label, value), ...] for kind="choice"


# --------------------------------------------------------------------- #
# Server Identity (Funcom's "General" section, minus ServerName/
# ServerPassword which live on the Network & Ports page since they're
# connection info, not gameplay rules -- and minus MaxPlayers, which
# ConanOps treats as a network setting rather than a gameplay one)
# --------------------------------------------------------------------- #
IDENTITY_FIELDS: List[FieldSpec] = [
    FieldSpec("__description", "Description (ConanOps note -- not written to any .ini)", "text", ""),
    FieldSpec("ServerMessageOfTheDay", "Message of the Day", "text", "",
              "Shown to players on the loading screen."),
    FieldSpec("AdminPassword", "Admin Password", "text", "",
              "Gives in-game admin rights to whoever enters it via Settings -> Server Settings -> Make Me Admin."),
    FieldSpec("IsBattlEyeEnabled", "BattlEye Anti-Cheat", "bool", True,
              "Funcom recommends leaving this on to reduce cheating."),
    FieldSpec("PVPEnabled", "PvP Enabled", "bool", True,
              "Player vs player combat is possible when on."),
    FieldSpec("RestrictPVPTime", "Time-Restrict PvP", "bool", False,
              "Limits PvP combat to set time windows (the specific weekday/weekend windows are an advanced, unexposed-by-Funcom setting; this just flips the restriction on/off)."),
    FieldSpec("PVPBlitzServer", "PvP Blitz (faster progression for PvP)", "bool", False,
              "Speeds up progression to get players into higher-tier PvP sooner."),
    FieldSpec("RestrictPVPBuildingDamageTime", "Time-Restrict PvP Building Damage", "bool", False,
              "Players can only damage others' structures during set time windows when on."),
    FieldSpec("ServerCommunity", "Community", "choice", 0,
              "Affects how your server is filtered in the in-game server list.",
              choices=[("Purist", 0), ("Relaxed", 1), ("Hardcore", 2), ("Role Playing", 3), ("Experimental", 4)]),
    FieldSpec("serverRegion", "Server Region", "choice", 1,
              "Affects how your server is filtered by region in the server list.",
              choices=[("Europe", 0), ("North America", 1), ("Asia", 2), ("Australia", 3), ("South America", 4), ("Japan", 5)]),
    FieldSpec("NoOwnership", "No Ownership", "bool", False,
              "When on, all players can loot/use/dismantle everything -- there's no ownership at all."),
    FieldSpec("ContainersIgnoreOwnership", "Containers Ignore Ownership", "bool", False,
              "All containers are open to everyone, though some can still be optionally locked."),
    FieldSpec("CanDamagePlayerOwnedStructures", "Can Damage Player-Owned Structures", "bool", True,
              "Players can attack and destroy other players' structures when on."),
    FieldSpec("bCanBeDamaged", "Players Can Be Damaged", "bool", True,
              "Players can take damage from other players when on."),
    FieldSpec("EnableSandStorm", "Enable Sandstorms", "bool", True,
              "Periodic sandstorms sweep across the Exiled Lands."),
    FieldSpec("clanMaxSize", "Clan Max Size", "int", 20,
              "Maximum members per clan.", min=1, max=100, step=1),
    FieldSpec("MaxNudity", "Maximum Nudity", "choice", 2,
              "Caps nudity server-wide regardless of a player's own client setting.",
              choices=[("None", 0), ("Partial", 1), ("Full", 2)]),
    FieldSpec("serverVoiceChat", "In-Game Voice Chat", "bool", True,
              "Enables the built-in voice chat."),
]

# --------------------------------------------------------------------- #
# Progression
# --------------------------------------------------------------------- #
PROGRESSION_FIELDS: List[FieldSpec] = [
    FieldSpec("PlayerXPRateMultiplier", "Player XP Rate Multiplier", "float", 1.0,
              "Multiplies all XP players receive, from every source.", min=0.0, max=20.0, step=0.1, decimals=1),
    FieldSpec("PlayerXPTimeMultiplier", "Player XP (Passive/Time) Multiplier", "float", 1.0,
              "Multiplies the passive XP players earn just for surviving over time.", min=0.0, max=20.0, step=0.1, decimals=1),
    FieldSpec("PlayerXPKillMultiplier", "Player XP (Kill) Multiplier", "float", 1.0,
              "Multiplies XP earned from killing monsters and players.", min=0.0, max=20.0, step=0.1, decimals=1),
    FieldSpec("PlayerXPHarvestMultiplier", "Player XP (Harvest) Multiplier", "float", 1.0,
              "Multiplies XP earned from harvesting.", min=0.0, max=20.0, step=0.1, decimals=1),
    FieldSpec("PlayerXPCraftMultiplier", "Player XP (Craft) Multiplier", "float", 1.0,
              "Multiplies XP earned from crafting.", min=0.0, max=20.0, step=0.1, decimals=1),
]

# --------------------------------------------------------------------- #
# Day / Night cycle
# --------------------------------------------------------------------- #
DAYNIGHT_FIELDS: List[FieldSpec] = [
    FieldSpec("DayCycleSpeedScale", "Day Cycle Speed", "float", 1.0,
              "Multiplies the entire 24-hour cycle's speed, on top of the individual settings below.", min=0.1, max=10.0, step=0.1, decimals=1),
    FieldSpec("DayTimeSpeedScale", "Daytime Speed", "float", 1.0,
              "Multiplies time spent in daytime hours (7:00-16:59 game time).", min=0.1, max=10.0, step=0.1, decimals=1),
    FieldSpec("NightTimeSpeedScale", "Nighttime Speed", "float", 1.0,
              "Multiplies time spent in nighttime hours (19:00-4:59 game time).", min=0.1, max=10.0, step=0.1, decimals=1),
    FieldSpec("DawnDuskSpeedScale", "Dawn/Dusk Speed", "float", 1.0,
              "Multiplies time spent in dawn (5:00-6:59) and dusk (17:00-18:59).", min=0.1, max=10.0, step=0.1, decimals=1),
    FieldSpec("UseClientCatchUpTime", "Use Catch-Up Time for New Players", "bool", True,
              "New characters start at a fixed time of day and play at that time until the server catches up to them."),
    FieldSpec("ClientCatchUpTime", "Catch-Up Time (24h, e.g. 1200 = noon)", "int", 1200,
              "The time of day new players start at, if Catch-Up Time is on. Avoid setting this to the darkest night hours.", min=0, max=2359, step=1),
]

# --------------------------------------------------------------------- #
# Survival
# --------------------------------------------------------------------- #
SURVIVAL_FIELDS: List[FieldSpec] = [
    FieldSpec("StaminaCostMultiplier", "Stamina Cost Multiplier", "float", 1.0,
              "Scales how much stamina every action costs.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("PlayerActiveThirstMultiplier", "Active Thirst Multiplier", "float", 1.0,
              "Scales thirst loss while actively playing.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("PlayerActiveHungerMultiplier", "Active Hunger Multiplier", "float", 1.0,
              "Scales hunger loss while actively playing.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("PlayerIdleThirstMultiplier", "Idle Thirst Multiplier", "float", 1.0,
              "Scales thirst loss while logged out/idle.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("PlayerIdleHungerMultiplier", "Idle Hunger Multiplier", "float", 1.0,
              "Scales hunger loss while logged out/idle.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("LogoutCharactersRemainInTheWorld", "Bodies Remain in World When Offline", "bool", True,
              "If off, a player's body disappears entirely from the world while they're offline instead of lying there unconscious."),
    FieldSpec("DropEquipmentOnDeath", "Drop Equipment on Death", "bool", True,
              "Killed players drop their equipped gear when they respawn."),
    FieldSpec("EverybodyCanLootCorpse", "Everybody Can Loot Corpses", "bool", True,
              "If off, only the player themselves can loot their own corpse."),
    FieldSpec("ThrallCorruptionRemovalMultiplier", "Thrall Corruption-Removal Multiplier", "float", 1.0,
              "Scales how fast thrall entertainers remove player corruption.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("PlayerCorruptionGainMultiplier", "Player Corruption Gain Multiplier", "float", 1.0,
              "Scales how much corruption players gain.", min=0.0, max=10.0, step=0.1, decimals=1),
]

# --------------------------------------------------------------------- #
# Combat
# --------------------------------------------------------------------- #
COMBAT_FIELDS: List[FieldSpec] = [
    FieldSpec("PlayerDamageMultiplier", "Player Damage Dealt Multiplier", "float", 1.0,
              "Scales damage a player deals.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("PlayerDamageTakenMultiplier", "Player Damage Taken Multiplier", "float", 1.0,
              "Scales damage a player receives.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("NPCDamageMultiplier", "NPC Damage Dealt Multiplier", "float", 1.0,
              "Scales damage NPCs/monsters deal.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("NPCDamageTakenMultiplier", "NPC Damage Taken Multiplier", "float", 1.0,
              "Scales damage NPCs/monsters receive.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("ThrallDamageToPlayersMultiplier", "Thrall Damage to Players Multiplier", "float", 1.0,
              "Scales damage thralls deal to players.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("NPCRespawnMultiplier", "NPC Respawn Speed Multiplier", "float", 1.0,
              "Scales how fast NPCs respawn after dying. Note: many NPCs don't respect this value.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("FriendlyFireDamageMultiplier", "Friendly Fire Damage Multiplier", "float", 0.0,
              "Scales damage dealt to clanmates/allies.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("BuildingDamageMultiplier", "Building Damage Multiplier", "float", 1.0,
              "Scales damage buildings receive.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("DurabilityMultiplier", "Item Durability-Loss Multiplier", "float", 1.0,
              "Scales durability lost per hit on weapons/tools/shields.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("ThrallWakeupTime", "Thrall Wakeup Time (seconds)", "int", 600,
              "How long a captured thrall stays unconscious.", min=0, max=7200, step=30),
    FieldSpec("AvatarLifetime", "Avatar Lifetime (seconds)", "int", 60,
              "How long a summoned Avatar of a god stays in the world.", min=0, max=600, step=10),
    FieldSpec("AvatarsDisabled", "Disable Avatars", "bool", False,
              "Prevents players from summoning Avatars at all."),
    FieldSpec("DisableLandclaimNotifications", "Disable Land Claim Notifications", "bool", False,
              "Suppresses the on-screen notifications for land claim events."),
]

# --------------------------------------------------------------------- #
# Harvesting
# --------------------------------------------------------------------- #
HARVESTING_FIELDS: List[FieldSpec] = [
    FieldSpec("ItemSpoilRateScale", "Item Spoil Rate", "float", 1.0,
              "Smaller values make food last longer before spoiling.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("HarvestAmountMultiplier", "Harvest Amount Multiplier", "float", 1.0,
              "Scales the amount of resources gathered per harvest.", min=0.0, max=20.0, step=0.1, decimals=1),
    FieldSpec("ResourceRespawnSpeedMultiplier", "Resource Respawn Speed Multiplier", "float", 1.0,
              "Scales how fast harvested resource nodes respawn.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("LandClaimRadiusMultiplier", "Land Claim Radius Multiplier", "float", 1.0,
              "Scales the radius of land claim around buildings, affecting resource/NPC respawn and others' ability to build nearby.", min=0.0, max=10.0, step=0.1, decimals=1),
]

# --------------------------------------------------------------------- #
# Crafting
# --------------------------------------------------------------------- #
CRAFTING_FIELDS: List[FieldSpec] = [
    FieldSpec("ItemConvertionMultiplier", "Crafting Time Multiplier", "float", 1.0,
              "Scales time to craft items. (Yes, 'Convertion' is Funcom's own misspelling in the actual ini key.)", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("ThrallConversionMultiplier", "Thrall Conversion Time Multiplier", "float", 1.0,
              "Scales time to convert a captured thrall on the Wheel of Pain.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("FuelBurnTimeMultiplier", "Fuel Burn Time Multiplier", "float", 1.0,
              "Scales how long fuel units burn in furnaces/campfires.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("CraftingCostMultiplier", "Crafting Cost Multiplier", "float", 1.0,
              "Scales the amount of resources required to craft an item.", min=0.0, max=10.0, step=0.1, decimals=1),
]

# --------------------------------------------------------------------- #
# Building & Decay
# --------------------------------------------------------------------- #
BUILDING_FIELDS: List[FieldSpec] = [
    FieldSpec("DisableBuildingAbandonment", "Disable Building Decay (buildings never decay)", "bool", False,
              "This is the real 'never decay' switch -- turning it on stops abandoned buildings from decaying entirely, regardless of the multiplier below."),
    FieldSpec("BuildingDecayTimeMultiplier", "Building Decay Time Multiplier", "float", 1.0,
              "Scales how long an abandoned building takes to decay. Has no effect if decay is disabled above.", min=0.0, max=10.0, step=0.1, decimals=1),
]

# --------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------- #
CHAT_FIELDS: List[FieldSpec] = [
    FieldSpec("ChatLocalRadius", "Local Chat Radius (cm)", "int", 5000,
              "How far local chat broadcasts, in centimeters.", min=100, max=100000, step=100),
    FieldSpec("ChatMaxMessageLength", "Max Chat Message Length", "int", 128,
              "Character limit per chat message.", min=16, max=1024, step=8),
    FieldSpec("ChatHasGlobal", "Enable Global Chat", "bool", True,
              "Turns the server-wide global chat channel on or off."),
]

# --------------------------------------------------------------------- #
# Purge
# --------------------------------------------------------------------- #
PURGE_FIELDS: List[FieldSpec] = [
    FieldSpec("EnablePurge", "Enable Purge", "bool", True,
              "Turning this off disables purge events completely."),
    FieldSpec("PurgeLevel", "Purge Level (difficulty)", "int", 6,
              "Higher is harder. 0 effectively disables purges.", min=0, max=10, step=1),
    FieldSpec("PurgePeriodicity", "Purges Per Day", "int", 1,
              "How many times a purge can trigger per real day (also depends on the settings below).", min=0, max=24, step=1),
    FieldSpec("RestrictPurgeTime", "Restrict Purge to Time Windows", "bool", False,
              "When on, purges only occur during the weekday/weekend windows below."),
    FieldSpec("PurgeTimeWeekdayStart", "Weekday Window Start (24h, e.g. 1800)", "int", 1800,
              "Weekday purge window start time.", min=0, max=2359, step=1),
    FieldSpec("PurgeTimeWeekdayEnd", "Weekday Window End (24h)", "int", 2300,
              "Weekday purge window end time.", min=0, max=2359, step=1),
    FieldSpec("PurgeTimeWeekendStart", "Weekend Window Start (24h)", "int", 1800,
              "Weekend purge window start time.", min=0, max=2359, step=1),
    FieldSpec("PurgeTimeWeekendEnd", "Weekend Window End (24h)", "int", 2300,
              "Weekend purge window end time.", min=0, max=2359, step=1),
    FieldSpec("PurgePreparationTime", "Purge Preparation Time (minutes)", "int", 5,
              "Time between the purge warning and the purge actually starting.", min=0, max=120, step=1),
    FieldSpec("PurgeDuration", "Purge Duration (minutes)", "int", 20,
              "Maximum time a purge lasts (can end sooner if all waves are cleared).", min=1, max=180, step=1),
    FieldSpec("MinPurgeOnlinePlayers", "Minimum Online Players for Purge", "int", 0,
              "0 means purges can happen even if the targeted clan is fully offline.", min=0, max=40, step=1),
    FieldSpec("AllowBuilding", "Allow Building During Purge", "bool", True,
              "Lets players build while a purge is in progress."),
    FieldSpec("ClanPurgeTrigger", "Purge Meter Trigger Value", "int", 3000,
              "Higher means a clan needs to be active longer before becoming eligible for a purge.", min=0, max=100000, step=100),
    FieldSpec("ClanScoreUpateFrenquency", "Purge Meter Update Interval (seconds)", "int", 300,
              "How often clan purge-meter scores are recalculated. (Funcom's own misspelling in the real ini key.)", min=10, max=3600, step=10),
    FieldSpec("PurgeNPCBuildingDamageMultiplier", "Purge NPC Building Damage Multiplier", "float", 10.0,
              "Scales damage purge NPCs deal to buildings.", min=0.0, max=50.0, step=1.0, decimals=1),
]

# --------------------------------------------------------------------- #
# Pets & Hunger
# --------------------------------------------------------------------- #
PETS_FIELDS: List[FieldSpec] = [
    FieldSpec("ToggleHungerSystemThralls", "Hunger System: Thralls", "bool", True,
              "Whether thralls need feeding."),
    FieldSpec("ToggleHungerSystemPets", "Hunger System: Pets", "bool", True,
              "Whether pets need feeding."),
    FieldSpec("FoodNutritionValue", "Food Nutrition Value", "float", 1.0,
              "How much nutrition a companion gains per feeding.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("StarvationTimeInMinutes", "Starvation Time (minutes, 100->0 hunger)", "int", 10080,
              "How long it takes a companion's hunger to fully deplete. Default is one week.", min=1, max=100000, step=60),
    FieldSpec("StarvationDamagePenaltyCap", "Starvation Damage Cap", "float", 1.0,
              "Max fraction of health starvation damage can remove (1.0 = up to 100%).", min=0.0, max=1.0, step=0.05, decimals=2),
    FieldSpec("AnimalPenCraftingTimeMultiplier", "Animal Pen Crafting Time Multiplier", "float", 1.0,
              "Scales breeding/crafting time in animal pens.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("FeedBoxRangeMultiplier", "Feed Box Range Multiplier", "float", 1.0,
              "Scales how far a food container can feed nearby companions.", min=0.0, max=10.0, step=0.1, decimals=1),
    FieldSpec("ExclusiveDiet", "Exclusive Diet", "bool", False,
              "If on, companions will only eat items on their specific diet list instead of any food."),
]

# --------------------------------------------------------------------- #
# All categories, in the order they appear in the Settings sub-nav.
# --------------------------------------------------------------------- #
CATEGORIES: List[Tuple[str, str, List[FieldSpec]]] = [
    ("progression", "Progression", PROGRESSION_FIELDS),
    ("daynight", "Day / Night Cycle", DAYNIGHT_FIELDS),
    ("survival", "Survival", SURVIVAL_FIELDS),
    ("combat", "Combat", COMBAT_FIELDS),
    ("harvesting", "Harvesting", HARVESTING_FIELDS),
    ("crafting", "Crafting", CRAFTING_FIELDS),
    ("building", "Building & Decay", BUILDING_FIELDS),
    ("chat", "Chat", CHAT_FIELDS),
    ("purge", "Purge", PURGE_FIELDS),
    ("pets", "Pets & Hunger", PETS_FIELDS),
]


def default_gameplay_dict() -> dict:
    """The full set of defaults for every field above, used to backfill
    any ServerConfig (old or new) that's missing some or all of them."""
    all_fields = IDENTITY_FIELDS + [f for _, _, fields in CATEGORIES for f in fields]
    return {f.key: f.default for f in all_fields}


ALL_FIELDS_BY_KEY = {
    f.key: f for f in IDENTITY_FIELDS + [f for _, _, fields in CATEGORIES for f in fields]
}
