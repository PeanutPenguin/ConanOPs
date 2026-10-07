"""
User-editable color theme for ConanOps, stored as its own JSON file
separately from server config (~/ConanOps/theme.json) so a theme --
including one someone else made -- can be dropped in and used just by
placing the file, no code changes needed.

A missing file, or a file missing/invalid on some keys, falls back to
DEFAULT_PALETTE field-by-field -- a partial file (just `{"accent":
"#3f7fd6"}`) is enough to override only what you actually want
changed. Unknown keys are ignored rather than erroring, so an older
theme.json stays loadable if new fields get added later.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, fields, asdict
from typing import Optional

import applog

_log = applog.get_logger(__name__)

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


@dataclass
class ThemePalette:
    # Charcoal + ember defaults (the 9router-inspired redesign). Fonts
    # are bundled with the app (assets/fonts, loaded by ui.assets), so
    # Geist is always available; the fallbacks only matter for a
    # theme.json that names a font that isn't installed.
    bg: str = "#1d1d1d"          # page background
    panel: str = "#252525"       # cards / tiles
    panel_alt: str = "#181818"   # sidebar, inputs, log views
    border: str = "#303030"
    ivory: str = "#ececec"       # primary text
    muted: str = "#a3a3a3"       # secondary text
    dim: str = "#8f8f8f"         # captions, section labels
    accent: str = "#c9752f"
    green: str = "#5fd38a"
    red: str = "#f28b80"
    yellow: str = "#f0b54a"
    font_display: str = "Geist, 'Segoe UI', sans-serif"
    font_ui: str = "Geist, 'Segoe UI', sans-serif"
    font_mono: str = "'Geist Mono', Consolas, monospace"


DEFAULT_PALETTE = ThemePalette()

# The defaults before the redesign. Older versions of ConanOps wrote the
# whole palette to theme.json on every Apply, so most people's file is
# full of these exact values even though they never chose them. Any
# field still holding its OLD default is read as "not customized" and
# gets the new default instead; fields someone actually changed are
# kept. (Accent is the same in both, so it's unaffected either way.)
LEGACY_DEFAULTS = {
    "bg": "#1c1712",
    "panel": "#1c1712",
    "panel_alt": "#241d17",
    "border": "#34291d",
    "ivory": "#f2ece1",
    "muted": "#a89a86",
    "dim": "#7d7263",
    "green": "#6fbf73",
    "red": "#e2574c",
    "yellow": "#e0b04a",
    "font_display": "Cinzel, Georgia, serif",
    "font_ui": "'IBM Plex Sans', Segoe UI, sans-serif",
    "font_mono": "'IBM Plex Mono', Consolas, monospace",
}

# Human-readable labels for the fields a color-picker UI should show,
# in display order. Font fields are edited as plain text, not a color
# picker, so they're listed separately.
COLOR_FIELD_LABELS = [
    ("accent", "Accent"),
    ("bg", "Background"),
    ("panel", "Panel"),
    ("panel_alt", "Panel (alt)"),
    ("border", "Border"),
    ("ivory", "Text (primary)"),
    ("muted", "Text (muted)"),
    ("dim", "Text (dim)"),
    ("green", "Success"),
    ("yellow", "Warning"),
    ("red", "Error / Danger"),
]
FONT_FIELD_LABELS = [
    ("font_display", "Display font (headings)"),
    ("font_ui", "UI font (body text)"),
    ("font_mono", "Monospace font (console, values)"),
]

_VALID_FIELDS = {f.name for f in fields(ThemePalette)}
_COLOR_FIELDS = {name for name, _ in COLOR_FIELD_LABELS}


def _theme_path() -> str:
    return os.path.join(os.path.expanduser("~"), "ConanOps", "theme.json")


def theme_file_path() -> str:
    return _theme_path()


def load_theme(path: Optional[str] = None) -> ThemePalette:
    path = path or _theme_path()
    palette = ThemePalette()
    if not os.path.exists(path):
        return palette
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _log.warning(f"Couldn't read theme.json ({e}) -- using the default theme.")
        return palette
    if not isinstance(raw, dict):
        _log.warning("theme.json isn't a JSON object -- using the default theme.")
        return palette

    for key, value in raw.items():
        if key not in _VALID_FIELDS:
            continue  # unknown/future key -- ignore rather than error
        if isinstance(value, str) and LEGACY_DEFAULTS.get(key, "").lower() == value.strip().lower():
            continue  # an old default nobody chose -- see LEGACY_DEFAULTS
        if key in _COLOR_FIELDS:
            if isinstance(value, str) and _HEX_RE.match(value):
                setattr(palette, key, value)
            else:
                _log.warning(f"theme.json: '{key}' isn't a valid #rrggbb color -- keeping the default.")
        else:
            if isinstance(value, str) and value.strip():
                setattr(palette, key, value)
    return palette


def save_theme(palette: ThemePalette, path: Optional[str] = None) -> None:
    path = path or _theme_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(palette), f, indent=2)
        f.write("\n")
