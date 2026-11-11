"""
Which version of each Workshop mod is downloaded, so ConanOps can tell when
the Workshop has a newer one.

SteamCMD records it in steamapps/workshop/appworkshop_440900.acf (each
item's "timeupdated", the Workshop's own time for that version). If that
file is missing or doesn't list a mod, the time its files were downloaded
stands in: a version can't be newer than the download that fetched it.
"""
from __future__ import annotations

import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

import applog

_log = applog.get_logger(__name__)

CONAN_APP_ID = "440900"
# Workshop times and file times come from different clocks.
SLACK_SECONDS = 120

_ITEM_RE = re.compile(r'"(\d+)"\s*\{([^{}]*)\}', re.S)
_TIME_RE = re.compile(r'"timeupdated"\s*"(\d+)"', re.I)


def _workshop_dir(steamcmd_dir: str) -> str:
    return os.path.join(steamcmd_dir, "steamapps", "workshop")


def _manifest_times(steamcmd_dir: str) -> Dict[str, int]:
    path = os.path.join(_workshop_dir(steamcmd_dir), f"appworkshop_{CONAN_APP_ID}.acf")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return {}
    start = text.find('"WorkshopItemsInstalled"')
    if start < 0:
        return {}
    brace = text.find("{", start)
    if brace < 0:
        return {}
    # The block ends at the brace that closes it.
    depth, end = 0, len(text)
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    out: Dict[str, int] = {}
    for m in _ITEM_RE.finditer(text[brace + 1:end]):
        t = _TIME_RE.search(m.group(2))
        if t:
            out[m.group(1)] = int(t.group(1))
    return out


def _files_time(steamcmd_dir: str, workshop_id: str) -> Optional[int]:
    folder = os.path.join(_workshop_dir(steamcmd_dir), "content", CONAN_APP_ID, workshop_id)
    newest = None
    try:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                try:
                    t = os.path.getmtime(os.path.join(root, name))
                except OSError:
                    continue
                newest = t if newest is None else max(newest, t)
    except OSError:
        return None
    return None if newest is None else int(newest)


def installed_times(steamcmd_dir: str, workshop_ids: Iterable[str]) -> Dict[str, Optional[int]]:
    """Workshop id -> the version time of what's downloaded (None: not downloaded)."""
    ids = [str(i) for i in workshop_ids]
    manifest = _manifest_times(steamcmd_dir) if steamcmd_dir else {}
    out: Dict[str, Optional[int]] = {}
    for wid in ids:
        if not steamcmd_dir:
            out[wid] = None
        elif wid in manifest:
            out[wid] = manifest[wid]
        else:
            out[wid] = _files_time(steamcmd_dir, wid)
    return out


def newer_on_workshop(steamcmd_dir: str, workshop_ids: Iterable[str], items: dict
                      ) -> List[Tuple[str, int]]:
    """(id, Workshop time) for each mod with a newer version than the one
    downloaded. A mod that isn't downloaded at all isn't counted here (the
    next start downloads it), nor is one the Workshop didn't answer for."""
    ids = [str(i) for i in workshop_ids]
    have = installed_times(steamcmd_dir, ids)
    out = []
    for wid in ids:
        item = items.get(wid)
        latest = int(getattr(item, "time_updated", 0) or 0) if item is not None else 0
        current = have.get(wid)
        if latest and current is not None and latest > current + SLACK_SECONDS:
            out.append((wid, latest))
    return out
