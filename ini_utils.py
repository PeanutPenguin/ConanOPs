"""
Read/write Conan Exiles' .ini files without clobbering keys ConanOps doesn't
know about. This is a line-based merge: known keys are updated in place or
appended under their [Section], every other line is left untouched, and the
original file is backed up first.
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
        # Two writes in the same second must not overwrite the older backup.
        n = 2
        while os.path.exists(f"{path}.{ts}_{n}.bak"):
            n += 1
        backup_path = f"{path}.{ts}_{n}.bak"
    shutil.copy2(path, backup_path)
    _prune_ini_backups(path)
    return backup_path


def _prune_ini_backups(path: str, keep: int = _MAX_INI_BACKUPS_PER_FILE) -> None:
    """Keeps only the newest `keep` .bak files for this ini path. Unpruned
    backups would pile up and get zipped into every server backup."""
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
    for the keys present in the file.

    Names are matched case-insensitively like Unreal's parser, since Unreal,
    Funcom's docs and ConanOps each use different casing for the same keys."""
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
    Updates existing keys in place, appends missing ones under their section
    (created at end of file if needed), and backs up the original first.
    Matching is case-insensitive; existing names keep their on-disk casing."""
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
            lines[i] = f"{km.group('key').strip()}={value}\n"

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
            # Shift later sections' recorded indices past the inserted lines.
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


# Engine.ini connection keys written by the Network & Ports page. Gameplay
# keys live in ini_field_specs.py.
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
