"""
Whether each mod is right for the game build the server is on, from the
evidence there is, strongest first:

1. Proof from this server: the server started and answered with exactly this
   version of the mod on exactly this game build (or failed to, right after
   the mod changed). Recorded per server in ServerConfig.mod_proof.
2. The Workshop: the mod's version tag (Enhanced / Legacy) and whether it has
   been updated since the last game patch that required mods to be rebuilt.

A Workshop date can only say a mod is *probably* fine or *probably* stale;
proof says what actually happened here. Nothing can say two mods agree with
each other before they are run together, which is what the proof records.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import applog
import steam_workshop_api as swa

_log = applog.get_logger(__name__)

CURRENT = "current"        # updated since the last breaking patch
WORKING = "working"        # older, but ran on this build here
UNVERIFIED = "unverified"  # older (or untagged) and not yet run on this build
FAILED = "failed"          # the server failed right after this version arrived
LEGACY = "legacy"
REMOVED = "removed"

LABELS = {
    CURRENT: "Updated for current patch",
    WORKING: "Working on this build",
    UNVERIFIED: "Not verified on this build",
    FAILED: "Failed on this build",
    LEGACY: "Legacy",
    REMOVED: "Not on the Workshop",
}
PROBLEMS = (FAILED, LEGACY, REMOVED)

# Workshop times and file times come from different clocks (see mod_versions).
SLACK_SECONDS = 120

# Dates of game patches that required mods to be rebuilt, kept in the
# project's repository so every copy of ConanOps learns of a new one without
# an update of its own. Small hotfixes don't belong in it.
PATCHES_URL = "https://raw.githubusercontent.com/PeanutPenguin/ConanOPs/main/mod_patches.json"


def proof_for(build: str, mod_time: Optional[int], ok: bool) -> dict:
    return {"build": str(build or ""), "time": int(mod_time or 0), "ok": bool(ok)}


def proof_applies(proof, build: str, workshop_time: int) -> bool:
    """Is this proof about the server's current game build and the mod's
    current Workshop version? A newer version on the Workshop, or a new game
    build, hasn't been proven by an older run."""
    if not isinstance(proof, dict) or not build or str(proof.get("build", "")) != str(build):
        return False
    try:
        proven_time = int(proof.get("time", 0) or 0)
    except (TypeError, ValueError):
        return False
    return bool(proven_time) and proven_time + SLACK_SECONDS >= int(workshop_time or 0)


def verdict(info, checked: bool, proof=None, build: str = "") -> Tuple[str, str]:
    """(verdict, why) for one mod. info: its steam_workshop_api.WorkshopItem
    from the last check, or None; checked: whether that check asked about it.
    ("", "") when nothing is known yet."""
    if info is None:
        return (REMOVED, "The Workshop no longer lists it (removed or made private), so players can't "
                         "download it.") if checked else ("", "")
    if info.status == swa.STATUS_LEGACY:
        return LEGACY, "Tagged for the game before the Enhanced update."
    if proof_applies(proof, build, info.time_updated):
        if not proof.get("ok"):
            return FAILED, "The server failed right after this version of the mod arrived, on this game build."
        if info.status == swa.STATUS_UPDATED:
            return CURRENT, "Updated since the last game patch, and the server has run with it on this build."
        return WORKING, ("Not updated since the last game patch that needed mods rebuilt, but the server has "
                         "started and answered with this version on this game build.")
    if info.status == swa.STATUS_UPDATED:
        return CURRENT, "Updated since the last game patch that needed mods rebuilt."
    if info.status == swa.STATUS_STALE:
        return UNVERIFIED, ("Not updated since the last game patch that needed mods rebuilt, and this server "
                            "hasn't run with it on the current game build yet.")
    return UNVERIFIED, "It has no version tag, and this server hasn't run with it on the current game build yet."


# ------------------------------------------------------------- load order --

def order_problems(mods: List[dict], items: dict) -> List[Tuple[str, str]]:
    """(mod id, required mod id) for every enabled mod listed before a mod it
    requires. A required mod has to load first. Needs the Workshop's
    "required items", which Steam only gives with an API key."""
    position = {m["id"]: i for i, m in enumerate(mods) if m.get("enabled", True)}
    out = []
    for m in mods:
        info = items.get(m["id"]) if m["id"] in position else None
        for need in (getattr(info, "children", None) or []):
            if need in position and position[need] > position[m["id"]]:
                out.append((m["id"], need))
    return out


def fixed_order(mods: List[dict], items: dict) -> List[dict]:
    """The same mods with each required mod moved to just before the first
    mod that needs it; everything else keeps its place."""
    out = list(mods)
    for _ in range(len(out) * len(out) + 1):  # each pass fixes one; a cycle can't loop forever
        problems = order_problems(out, items)
        if not problems:
            break
        mod_id, need = problems[0]
        need_at = next(i for i, m in enumerate(out) if m["id"] == need)
        moved = out.pop(need_at)
        out.insert(next(i for i, m in enumerate(out) if m["id"] == mod_id), moved)
    return out


# ------------------------------------------------- the last breaking patch --

def _valid_date(text) -> str:
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def latest_patch_date(document) -> str:
    """Newest date in a mod_patches.json document ("" if none is usable)."""
    patches = document.get("breaking_patches", []) if isinstance(document, dict) else []
    dates = [_valid_date(p.get("date", "")) for p in patches if isinstance(p, dict)]
    return max([d for d in dates if d], default="")


def fetch_latest_patch_date(url: str = PATCHES_URL, timeout: float = 10.0) -> str:
    """The newest breaking-patch date published for ConanOps ("" if it
    couldn't be fetched or read). Never raises."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ConanOps"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return latest_patch_date(json.loads(resp.read(200_000).decode("utf-8")))
    except (urllib.error.URLError, OSError, ValueError) as e:
        _log.info(f"Couldn't fetch the list of game patches that need mods rebuilt: {e}")
        return ""


def effective_cutoff(own: str, published: str) -> str:
    """The later of the date set in App Settings and the published one."""
    return max([d for d in (_valid_date(own), _valid_date(published)) if d], default=swa.DEFAULT_CUTOFF_DATE)
