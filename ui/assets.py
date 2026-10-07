"""
Bundled assets (assets/ at the project root) and the small widgets that
draw them: the app icon, the Geist fonts, sidebar/nav line icons, and
the animated rattlesnake loading spinner.

Everything here degrades gracefully: a missing asset file (a partial
copy of the app, a broken PyInstaller bundle) falls back to a plain
drawn placeholder or the system font rather than raising -- nothing
about how the app WORKS depends on these.

PyInstaller: bundle the folder with  --add-data "assets;assets"  (see
main.py's docstring). asset_path() looks in sys._MEIPASS first.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from typing import Dict, List, Optional

from PySide6.QtCore import QByteArray, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QLabel

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def asset_path(*parts: str) -> str:
    base = getattr(sys, "_MEIPASS", None) or _PROJECT_ROOT
    return os.path.join(base, "assets", *parts)


# ------------------------------------------------------------------ fonts --

_fonts_loaded = False


def load_fonts() -> List[str]:
    """Registers the bundled Geist / Geist Mono fonts with Qt (once).
    Returns the family names that loaded. Needs a QGuiApplication."""
    global _fonts_loaded
    families: List[str] = []
    if _fonts_loaded:
        return families
    folder = asset_path("fonts")
    if os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith((".ttf", ".otf")):
                font_id = QFontDatabase.addApplicationFont(os.path.join(folder, name))
                if font_id >= 0:
                    families.extend(QFontDatabase.applicationFontFamilies(font_id))
    _fonts_loaded = True
    return sorted(set(families))


# --------------------------------------------------------------- app icon --

def app_icon() -> QIcon:
    """The ConanOps icon (window, taskbar, tray). Falls back to a plain
    drawn badge if the asset is missing."""
    icon = QIcon()
    for name in ("conanops.ico", "conanops-icon-512.png"):
        path = asset_path(name)
        if os.path.exists(path):
            icon.addFile(path)
    if icon.isNull():
        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor(0, 0, 0, 0))
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor("#1e2433"))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(2, 2, 60, 60, 14, 14)
        p.end()
        icon = QIcon(pixmap)
    return icon


def app_icon_pixmap(size: int) -> QPixmap:
    """The icon at an exact pixel size, scaled smoothly from the large
    PNG (crisp on high-DPI screens)."""
    path = asset_path("conanops-icon-512.png")
    pm = QPixmap(path) if os.path.exists(path) else app_icon().pixmap(512, 512)
    return pm.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)


# ------------------------------------------------------------- line icons --

# 24x24 stroke icons, drawn with currentColor (substituted at render time).
_ICON_PATHS: Dict[str, str] = {
    "dashboard": '<rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/>'
                 '<rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/>',
    "players": '<circle cx="9" cy="8" r="3.5"/><path d="M2.5 20c.8-3.5 3.4-5.5 6.5-5.5s5.7 2 6.5 5.5"/>'
               '<path d="M16 4.5a3.5 3.5 0 010 7M18 14.8c1.8.7 3 2.5 3.5 5.2"/>',
    "mods": '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z"/><path d="M4 7.5l8 4.5 8-4.5M12 12v9"/>',
    "updates": '<path d="M20 12a8 8 0 11-2.3-5.7"/><path d="M20 4v5h-5"/>',
    "access": '<path d="M12 3l7 3v5c0 4.5-3 7.8-7 10-4-2.2-7-5.5-7-10V6z"/>',
    "console": '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
    "settings": '<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12"/><circle cx="16" cy="6" r="2"/>'
                '<circle cx="10" cy="12" r="2"/><circle cx="18" cy="18" r="2"/>',
    "app": '<circle cx="12" cy="12" r="3"/><path d="M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1'
           'M16.6 16.6l2.1 2.1M5.3 18.7l2.1-2.1M16.6 7.4l2.1-2.1"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "power": '<path d="M12 3v8"/><path d="M6.3 6.8a8 8 0 1011.4 0"/>',
    "play": '<path d="M7 4.5v15l12-7.5z"/>',
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 018 0v3"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
    "up": '<path d="M6 15l6-6 6 6"/>',
    "down": '<path d="M6 9l6 6 6-6"/>',
    "trash": '<path d="M5 7h14M10 7V4h4v3M7 7l1 13h8l1-13"/>',
}


def _render_svg(svg: str, size: int) -> QPixmap:
    from PySide6.QtSvg import QSvgRenderer  # local: keeps importing this module cheap
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    ratio = 2  # render at 2x so icons stay sharp on high-DPI displays
    pm = QPixmap(size * ratio, size * ratio)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    renderer.render(p)
    p.end()
    pm.setDevicePixelRatio(ratio)
    return pm


@lru_cache(maxsize=128)
def line_icon(name: str, color: str, checked_color: str = "", size: int = 18) -> QIcon:
    """A stroke icon in `color`; with checked_color, a checkable
    button's checked state uses that color instead (sidebar nav)."""
    body = _ICON_PATHS.get(name, "")

    def svg(c: str) -> str:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{c}" '
                f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">{body}</svg>')

    icon = QIcon()
    icon.addPixmap(_render_svg(svg(color), size), QIcon.Normal, QIcon.Off)
    if checked_color:
        icon.addPixmap(_render_svg(svg(checked_color), size), QIcon.Normal, QIcon.On)
        icon.addPixmap(_render_svg(svg(checked_color), size), QIcon.Active, QIcon.On)
    return icon


# --------------------------------------------------------- loading spinner --

@lru_cache(maxsize=8)
def _loading_frames(size: int) -> tuple:
    folder = asset_path("loading")
    if not os.path.isdir(folder):
        return ()
    frames = []
    for name in sorted(n for n in os.listdir(folder) if n.endswith(".png")):
        pm = QPixmap(os.path.join(folder, name))
        if not pm.isNull():
            frames.append(pm.scaled(size * 2, size * 2, Qt.KeepAspectRatio, Qt.SmoothTransformation))
    for pm in frames:
        pm.setDevicePixelRatio(2)
    return tuple(frames)


class LoadingSpinner(QLabel):
    """The rattlesnake loading animation at any size. Only animates
    while visible, so a hidden spinner costs nothing. If the frames are
    missing it simply shows nothing."""

    FRAME_MS = 33  # the source animation's own frame duration

    def __init__(self, size: int = 24, parent=None):
        super().__init__(parent)
        self._frames = _loading_frames(size)
        self._index = 0
        self.setFixedSize(QSize(size, size))
        self.setObjectName("TransparentRow")
        self.setAccessibleName("Loading")
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._advance)
        if self._frames:
            self.setPixmap(self._frames[0])

    def _advance(self) -> None:
        if not self._frames:
            return
        self._index = (self._index + 1) % len(self._frames)
        self.setPixmap(self._frames[self._index])

    def showEvent(self, event) -> None:  # noqa: N802 -- Qt's naming
        super().showEvent(event)
        if self._frames:
            self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    def is_animating(self) -> bool:
        return self._timer.isActive()


def frame_count() -> int:
    return len(_loading_frames(24))


def pixmap_or_none(path: str) -> Optional[QPixmap]:
    return QPixmap(path) if os.path.exists(path) else None
