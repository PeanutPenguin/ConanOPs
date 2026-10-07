"""
User-editable color theme in ~/ConanOps/theme.json, separate from server
config so themes can be shared as files. Missing or invalid keys fall back
to DEFAULT_PALETTE one field at a time; unknown keys are ignored.
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
    # Geist is bundled (assets/fonts); fallbacks matter only for custom fonts.
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

# Old defaults that older versions saved to every theme.json; a field still
# holding one is treated as not customized.
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

# Display order and labels for the theme editor; fonts are edited as text.
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
            continue
        if isinstance(value, str) and LEGACY_DEFAULTS.get(key, "").lower() == value.strip().lower():
            continue  # see LEGACY_DEFAULTS
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
