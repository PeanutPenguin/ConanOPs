"""
Read/write Conan Exiles' .ini config files (ServerSettings.ini, Engine.ini,
Game.ini) without clobbering keys ConanOps doesn't know about.

Design goal (from the spec): if Funcom adds a new key in an update, or the
person hand-edits the file, or a mod adds its own section, none of that
should be lost or corrupted just because ConanOps wrote to the file. So
this is a line-based merge, not "load into a dict and dump it back out":
- Known keys get their value updated in place if present.
- Known keys get appended under the right [Section] if missing entirely.
- Every other line is left byte-for-byte alone.
- A backup of the original file is written before any change.
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from typing import Dict, List, Tuple

_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
_KEY_RE = re.compile(r"^\s*(?P<key>[^=;#\[][^=]*?)\s*=\s*(?P<val>.*?)\s*$")


_MAX_INI_BACKUPS_PER_FILE = 5


def backup_ini(path: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"{path}.{ts}.bak"
    if os.path.exists(backup_path):
        # Settings can legitimately be applied more than once within
        # the same second (e.g. the Network & Ports page writes
        # Engine.ini AND Game.ini back to back, or several gameplay
        # keys get applied in one batch that touches this file twice).
        # Without a tiebreaker, the second backup_ini() call in that
        # window would overwrite the first backup with a copy of the
        # file taken AFTER the first change -- so the state from just
        # before that first change, which is exactly what a backup
        # is supposed to preserve, would be lost for good.
        n = 2
        while os.path.exists(f"{path}.{ts}_{n}.bak"):
            n += 1
        backup_path = f"{path}.{ts}_{n}.bak"
    shutil.copy2(path, backup_path)
    _prune_ini_backups(path)
    return backup_path


def _prune_ini_backups(path: str, keep: int = _MAX_INI_BACKUPS_PER_FILE) -> None:
    """Keeps only the newest `keep` .bak files for this ini path,
    deleting older ones. Every apply_known_keys() call makes a fresh
    timestamped backup and nothing ever cleaned the old ones up --
    over the life of a server (Settings gets applied a LOT: every
    gameplay tweak, every RCON toggle, every identity change) these
    piled up without bound in Config/WindowsServer/, and since
    backup_manager zips that whole folder into every server backup,
    every one of those .bak files rode along in every single backup
    archive too."""
    folder = os.path.dirname(path) or "."
    base = os.path.basename(path)
    try:
        candidates = [
            f for f in os.listdir(folder)
            if f.startswith(base + ".") and f.endswith(".bak")
        ]
    except OSError:
        return
    candidates.sort(reverse=True)  # the timestamp format sorts lexicographically = chronologically
    for stale in candidates[keep:]:
        try:
            os.remove(os.path.join(folder, stale))
        except OSError:
            pass  # best-effort cleanup -- not worth failing the actual ini write over


def read_known_keys(path: str, wanted: Dict[str, str]) -> Dict[str, str]:
    """wanted: {key_name: section_name}. Returns {key_name: raw_value_str}
    for whichever of those keys are actually present in the file.

    Section and key names are matched case-insensitively, the same way
    Unreal's own config parser (and classic Windows .ini semantics)
    treat them -- ConanOps' own maps consistently use one casing
    (e.g. "/script/engine.gamesession") while Unreal itself writes
    "/Script/Engine.GameSession", and Funcom's own published examples
    use a third casing again. Matching exact-case-only meant a key
    that already existed in the file, spelled with different casing
    than ConanOps expected, was never found here."""
    # {casefolded key: (original key name, casefolded section name)}
    wanted_cf = {k.casefold(): (k, section.casefold()) for k, section in wanted.items()}
    found: Dict[str, str] = {}
    if not os.path.exists(path):
        return found
    current_section_cf = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            m = _SECTION_RE.match(line)
            if m:
                current_section_cf = m.group("name").casefold()
                continue
            m = _KEY_RE.match(line)
            if not m:
                continue
            key_cf = m.group("key").strip().casefold()
            hit = wanted_cf.get(key_cf)
            if hit and hit[1] == current_section_cf:
                found[hit[0]] = m.group("val").strip()
    return found


def apply_known_keys(path: str, updates: Dict[str, Tuple[str, str]]) -> None:
    """updates: {key_name: (section_name, new_value_str)}.
    Updates existing lines in place; appends any missing key under its
    section (creating the section at end-of-file if it doesn't exist).
    Leaves every other line untouched. Backs up the original first.

    Section and key names are matched case-insensitively -- see
    read_known_keys()'s docstring for why. A key or section that
    already exists in the file keeps its on-disk casing (only its
    value line, and nothing else about it, is touched); a section
    or key that has to be newly appended uses the casing `updates`
    was given."""
    if os.path.exists(path):
        backup_ini(path)
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)

    lines: List[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

    # {casefolded key: (original key name, casefolded section name, value)}
    remaining = {k.casefold(): (k, section.casefold(), value) for k, (section, value) in updates.items()}
    current_section_cf = None
    # {casefolded section name: (last line index in that section, original on-disk casing)}
    section_end_index: Dict[str, Tuple[int, str]] = {}

    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m:
            current_section_cf = m.group("name").casefold()
            section_end_index[current_section_cf] = (i, m.group("name"))
            continue
        if current_section_cf is not None:
            _, orig_section = section_end_index[current_section_cf]
            section_end_index[current_section_cf] = (i, orig_section)
        km = _KEY_RE.match(line)
        if not km:
            continue
        key_cf = km.group("key").strip().casefold()
        hit = remaining.get(key_cf)
        if hit and hit[1] == current_section_cf:
            orig_key, _section_cf, value = hit
            del remaining[key_cf]
            # Keep the key's on-disk spelling -- only the value changes.
            lines[i] = f"{km.group('key').strip()}={value}\n"

    # Anything left in `remaining` needs to be appended under its section.
    by_section_cf: Dict[str, List[Tuple[str, str]]] = {}
    section_cf_to_requested_name: Dict[str, str] = {}
    for key_cf, (orig_key, section_cf, value) in remaining.items():
        by_section_cf.setdefault(section_cf, []).append((orig_key, value))
        section_cf_to_requested_name.setdefault(section_cf, next(
            sec for k2, (sec, v2) in updates.items() if k2 == orig_key
        ))

    for section_cf, kvs in by_section_cf.items():
        if section_cf in section_end_index:
            insert_at, _orig_section = section_end_index[section_cf]
            insert_at += 1
            new_lines = [f"{k}={v}\n" for k, v in kvs]
            lines[insert_at:insert_at] = new_lines
            # shift later sections' recorded indices (only matters if we
            # process multiple sections in one call with overlapping ranges)
            shift = len(new_lines)
            for sec_cf, (idx, orig_section) in list(section_end_index.items()):
                if idx >= insert_at:
                    section_end_index[sec_cf] = (idx + shift, orig_section)
        else:
            requested_name = section_cf_to_requested_name[section_cf]
            if lines and not lines[-1].endswith("\n"):
                lines.append("\n")
            lines.append(f"\n[{requested_name}]\n")
            for k, v in kvs:
                lines.append(f"{k}={v}\n")
            section_end_index[section_cf] = (len(lines) - 1, requested_name)

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)


# --------------------------------------------------------------------- #
# Conan-specific key/section maps. These are the ones ConanOps' Settings
# pages actually read and write; everything else in the files is left
# alone no matter what it is.
# --------------------------------------------------------------------- #

# --------------------------------------------------------------------- #
# NOTE: the authoritative list of Conan Exiles gameplay setting keys,
# their sections, defaults, and UI grouping now lives in
# ini_field_specs.py (built from the community wiki's Server
# Configuration page). The two constants below cover only the small,
# fixed set of connection-identity keys that ConanOps' Network & Ports
# page writes directly (see ui/main_window.py's _apply_network), which
# aren't part of that data-driven system since they need custom UI
# (port-conflict detection, IP auto-detect) rather than a generic field.
# --------------------------------------------------------------------- #

ENGINE_INI_MAP = {
    "Port": "URL",
    "GameServerQueryPort": "OnlineSubsystemSteam",
    "MultiHome": "URL",
    "ServerName": "OnlineSubsystemSteam",
    "ServerPassword": "OnlineSubsystemSteam",
}


def bool_to_ini(v: bool) -> str:
    return "True" if v else "False"


def ini_to_bool(v: str, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("true", "1", "yes")
