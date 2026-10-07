"""
QSS stylesheet for ConanOps. All colors and fonts come from a ThemePalette
(theme_config.py), so custom themes restyle everything.

Object names other modules rely on -- keep these stable:
  Sidebar, AppTitle, AppVersion, SectionLabel, NavButton, ServerRow,
  ServerRemoveButton, ContentHeader, HeaderTitle, HeaderSubtitle,
  Card, InfoBox, WarnBox, PageTitle, SectionTitle, Muted, Dim,
  StatValue, StatLabel, ErrorText, PrimaryButton, DangerButton,
  PillOn / PillOff / PillWarn / PillBad, TransparentRow, ColorSwatch.

Checkboxes render as switches using generated indicator images, since QSS
can't draw a switch knob.
"""
from __future__ import annotations

import os
import tempfile

from theme_config import ThemePalette, DEFAULT_PALETTE


def _hex_to_rgb(hex_color: str) -> tuple:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _shade(hex_color: str, factor: float) -> str:
    """factor < 1 darkens toward black, > 1 lightens toward white."""
    r, g, b = _hex_to_rgb(hex_color)
    if factor < 1:
        r, g, b = (int(c * factor) for c in (r, g, b))
    else:
        t = factor - 1
        r, g, b = (int(c + (255 - c) * t) for c in (r, g, b))
    return f"#{r:02x}{g:02x}{b:02x}"


def _rgba(hex_color: str, alpha: int) -> str:
    r, g, b = _hex_to_rgb(hex_color)
    return f"rgba({r}, {g}, {b}, {alpha})"


def _luminance(hex_color: str) -> float:
    def ch(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _hex_to_rgb(hex_color)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _mix(a: str, b: str, t: float) -> str:
    """a blended toward b by t (0..1)."""
    ra, ga, ba = _hex_to_rgb(a)
    rb, gb, bb = _hex_to_rgb(b)
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in ((ra, rb), (ga, gb), (ba, bb)))


# The mockup's exact button colors, used while accent and red are defaults.
_MOCKUP_ACCENT = "#c9752f"
_MOCKUP_RED = "#f28b80"
_MOCKUP_BUTTONS = {
    "fill": "#a9581c", "fill_hover": "#b8631f", "accent_text": "#f0a66a",
    "danger_bg": "#2a1b1a", "danger_bg_hover": "#341f1d", "danger_border": "#6b2c27", "danger_text": "#f19a91",
}


def button_colors(accent: str, red: str, bg: str) -> dict:
    colors = {}
    if accent.lower() == _MOCKUP_ACCENT:
        colors.update({k: _MOCKUP_BUTTONS[k] for k in ("fill", "fill_hover", "accent_text")})
    else:
        fill = primary_fill(accent)
        colors.update({"fill": fill, "fill_hover": _shade(fill, 1.08), "accent_text": _shade(accent, 1.3)})
    if red.lower() == _MOCKUP_RED:
        colors.update({k: _MOCKUP_BUTTONS[k] for k in ("danger_bg", "danger_bg_hover", "danger_border", "danger_text")})
    else:
        colors.update({
            "danger_bg": _mix(bg, _shade(red, 0.55), 0.18),
            "danger_bg_hover": _mix(bg, _shade(red, 0.55), 0.28),
            "danger_border": _shade(red, 0.44),
            "danger_text": _shade(red, 1.08),
        })
    return colors


def primary_fill(accent: str) -> str:
    """The accent, darkened until white text on it reaches 4.5:1 contrast."""
    fill = accent
    for _ in range(20):
        if (1.05) / (_luminance(fill) + 0.05) >= 4.5:
            return fill
        fill = _shade(fill, 0.93)
    return fill


def _switch_images(palette: ThemePalette) -> dict:
    """PNG switch images (1x and @2x). PNG, not SVG: Qt re-renders SVG
    indicators on every paint, which stuttered on pages with many switches."""
    folder = os.path.join(tempfile.gettempdir(), "conanops-ui")
    os.makedirs(folder, exist_ok=True)
    on_fill = button_colors(palette.accent, palette.red, palette.bg)["fill"]
    specs = {
        "on": (on_fill, "#ffffff", True),
        "off": ("#3a3a3a", "#d6d6d6", False),
        "on_disabled": (_shade(on_fill, 0.6), "#9a9a9a", True),
        "off_disabled": ("#2c2c2c", "#6a6a6a", False),
    }
    paths = {}
    for key, (track, knob, right) in specs.items():
        base = os.path.join(folder, f"switch-{key}-{track.lstrip('#')}")
        for scale, suffix in ((1, ""), (2, "@2x")):
            path = f"{base}{suffix}.png"
            if not os.path.exists(path):
                _draw_switch(path, track, knob, right, scale)
        paths[key] = f"{base}.png".replace("\\", "/")
    # Windows' native radio is near-invisible on dark backgrounds.
    for key, (ring, dot) in {
        "radio_on": (on_fill, on_fill), "radio_off": ("#6a6a6a", None),
        "radio_on_disabled": (_shade(on_fill, 0.6), _shade(on_fill, 0.6)), "radio_off_disabled": ("#3a3a3a", None),
    }.items():
        base = os.path.join(folder, f"{key}-{ring.lstrip('#')}")
        for scale, suffix in ((1, ""), (2, "@2x")):
            path = f"{base}{suffix}.png"
            if not os.path.exists(path):
                _draw_radio(path, ring, dot, scale)
        paths[key] = f"{base}.png".replace("\\", "/")
    return paths


def _draw_radio(path: str, ring: str, dot, scale: int) -> None:
    try:
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter, QPen
        size = 18 * scale
        img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        pen = QPen(QColor(ring))
        pen.setWidthF(1.6 * scale)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        inset = 1.2 * scale
        p.drawEllipse(QRectF(inset, inset, size - 2 * inset, size - 2 * inset))
        if dot:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(dot))
            r = 4.5 * scale
            p.drawEllipse(QRectF(size / 2 - r, size / 2 - r, 2 * r, 2 * r))
        p.end()
        img.save(path, "PNG")
    except Exception:  # noqa: BLE001 - cosmetic
        pass


def _draw_switch(path: str, track: str, knob: str, right: bool, scale: int) -> None:
    try:
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter
        w, h = 34 * scale, 20 * scale
        img = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(track))
        p.drawRoundedRect(QRectF(0, 0, w, h), h / 2, h / 2)
        p.setBrush(QColor(knob))
        r = 7 * scale
        cx = (24 if right else 10) * scale
        p.drawEllipse(QRectF(cx - r, h / 2 - r, 2 * r, 2 * r))
        p.end()
        img.save(path, "PNG")
    except Exception:  # noqa: BLE001 - cosmetic; a missing image just shows Qt's default checkbox
        pass


def build_stylesheet(palette: ThemePalette = DEFAULT_PALETTE) -> str:
    BG = palette.bg
    PANEL = palette.panel
    SUNK = palette.panel_alt
    BORDER = palette.border
    TEXT = palette.ivory
    MUTED = palette.muted
    DIM = palette.dim
    ACCENT = palette.accent
    GREEN, RED, YELLOW = palette.green, palette.red, palette.yellow
    BTN = button_colors(ACCENT, RED, BG)
    ACCENT_TEXT = BTN["accent_text"]
    FILL = BTN["fill"]
    FILL_HOVER = BTN["fill_hover"]
    # Mockup: buttons #232323 / hover #2c2c2c / border #3a3a3a; inputs
    # border #363636; nav text #c9c9c9.
    CONTROL = _shade(PANEL, 0.95)
    CONTROL_HOVER = _shade(PANEL, 1.03)
    BUTTON_BORDER = _shade(BORDER, 1.05)
    CONTROL_BORDER = _shade(BORDER, 1.03)
    NAV_TEXT = _mix(MUTED, TEXT, 0.55)
    FONT_DISPLAY = palette.font_display
    FONT_UI = palette.font_ui
    FONT_MONO = palette.font_mono
    sw = _switch_images(palette)
    return f"""
    QWidget {{
        background-color: {BG};
        color: {TEXT};
        font-family: {FONT_UI};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background-color: {BG}; }}
    QToolTip {{
        background-color: {PANEL}; color: {TEXT};
        border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 8px;
    }}

    /* ------------------------------------------------------ sidebar -- */
    #Sidebar {{
        background-color: {SUNK};
        border-right: 1px solid {BORDER};
    }}
    #Sidebar QLabel, #Sidebar QWidget#TransparentRow {{ background: transparent; }}
    #AppTitle {{
        font-family: {FONT_DISPLAY};
        font-weight: 600;
        font-size: 16px;
        color: {TEXT};
    }}
    #AppVersion {{ color: {DIM}; font-size: 12px; }}
    #SectionLabel {{
        color: {DIM};
        font-size: 12px;
        font-weight: 600;
        padding: 10px 12px 4px 12px;
    }}
    QPushButton#NavButton, QPushButton#ServerRow {{
        text-align: left;
        border: none;
        border-radius: 8px;
        padding: 0px 12px;
        min-height: 40px;
        background: transparent;
        color: {NAV_TEXT};
        font-size: 14px;
        font-weight: 500;
    }}
    QPushButton#NavButton:hover, QPushButton#ServerRow:hover {{
        background-color: {PANEL};
        color: {TEXT};
    }}
    QPushButton#NavButton:checked {{
        background-color: {_rgba(ACCENT, 38)};
        color: {ACCENT_TEXT};
    }}
    QPushButton#ServerRow:checked {{
        background-color: {PANEL};
        color: {TEXT};
        font-weight: 600;
    }}
    QPushButton#NavButton:disabled {{ color: {_shade(DIM, 0.7)}; }}
    QPushButton#ServerRemoveButton {{
        border: none; border-radius: 6px; padding: 2px; min-height: 0px;
        background: transparent; color: {DIM};
    }}
    QPushButton#ServerRemoveButton:hover {{ background-color: {PANEL}; color: {RED}; }}
    QLabel#ServerPlayers {{ color: {DIM}; font-size: 12px; }}
    QPushButton#ServerRow {{ padding-left: 30px; }}
    QLabel#ServerDot {{ background-color: #5a5a5a; border-radius: 4px; }}
    QLabel#ServerDot[running="true"] {{ background-color: {GREEN}; }}
    QLabel#SidebarNote {{ color: {MUTED}; font-size: 12px; }}
    QLabel#TileTitle {{ font-weight: 600; font-size: 14px; background: transparent; }}

    /* ------------------------------------------------------- header -- */
    #ContentHeader {{
        background-color: {BG};
        border-bottom: 1px solid {BORDER};
    }}
    #ContentHeader QLabel {{ background: transparent; }}
    QLabel#HeaderTitle {{
        font-family: {FONT_DISPLAY};
        font-size: 22px;
        font-weight: 600;
        color: {TEXT};
    }}
    QLabel#HeaderSubtitle {{ color: {MUTED}; font-size: 13px; }}

    /* ------------------------------------------------------ buttons -- */
    QPushButton {{
        background-color: {CONTROL};
        border: 1px solid {BUTTON_BORDER};
        border-radius: 8px;
        padding: 0px 14px;
        min-height: 38px;
        color: {_mix(TEXT, "#ffffff", 0.0)};
        font-size: 13px;
        font-weight: 500;
    }}
    QPushButton:hover {{ background-color: {CONTROL_HOVER}; }}
    QPushButton:pressed {{ background-color: {SUNK}; }}
    QPushButton:focus {{ border: 1px solid {ACCENT_TEXT}; }}
    QPushButton:disabled {{ color: {DIM}; background-color: {_shade(CONTROL, 0.92)}; border-color: {BORDER}; }}

    /* Square icon-only buttons (header lock, mod row arrows/delete). */
    QPushButton#IconButton {{ padding: 0px; min-width: 38px; max-width: 38px; min-height: 38px; max-height: 38px; }}

    QPushButton#PrimaryButton, QPushButton[primary="true"] {{
        background-color: {FILL};
        border: 1px solid {FILL};
        color: #ffffff;
        font-weight: 600;
    }}
    QPushButton#PrimaryButton:hover, QPushButton[primary="true"]:hover {{ background-color: {FILL_HOVER}; border-color: {FILL_HOVER}; }}
    QPushButton#PrimaryButton:disabled, QPushButton[primary="true"]:disabled {{ background-color: {BORDER}; border-color: {BORDER}; color: {DIM}; }}

    QPushButton#DangerButton {{
        background-color: {BTN["danger_bg"]};
        border: 1px solid {BTN["danger_border"]};
        color: {BTN["danger_text"]};
    }}
    QPushButton#DangerButton:hover {{ background-color: {BTN["danger_bg_hover"]}; }}

    QPushButton#ColorSwatch {{
        border: 1px solid {BUTTON_BORDER};
        border-radius: 8px;
        padding: 0;
        min-height: 0px;
    }}

    /* ------------------------------------------------- surfaces/text -- */
    QFrame#Card {{
        background-color: {PANEL};
        border: 1px solid {BORDER};
        border-radius: 10px;
    }}
    QFrame#Card QLabel, QFrame#Card QCheckBox {{ background: transparent; }}
    QFrame#InfoBox {{
        background-color: {PANEL};
        border: 1px solid {BORDER};
        border-radius: 10px;
    }}
    QFrame#WarnBox {{
        background-color: {_rgba(YELLOW, 22)};
        border: 1px solid {_rgba(YELLOW, 80)};
        border-radius: 10px;
    }}
    QFrame#InfoBox QLabel, QFrame#WarnBox QLabel {{ background: transparent; }}
    QLabel#WarnNote {{
        background-color: {_rgba(YELLOW, 22)};
        border: 1px solid {_rgba(YELLOW, 80)};
        border-radius: 10px;
        padding: 10px 14px;
        color: {TEXT};
    }}
    QLabel#OkNote {{
        background-color: {_rgba(GREEN, 18)};
        border: 1px solid {_rgba(GREEN, 70)};
        border-radius: 10px;
        padding: 10px 14px;
        color: {TEXT};
    }}
    QWidget#TransparentRow {{ background: transparent; }}
    QFrame#SettingRow {{ background: transparent; border-bottom: 1px solid {_shade(BORDER, 0.92)}; }}
    QFrame#SettingRowLast {{ background: transparent; border: none; }}
    QFrame#SettingRow QLabel, QFrame#SettingRowLast QLabel, QFrame#SettingRow QCheckBox, QFrame#SettingRowLast QCheckBox {{
        background: transparent; font-size: 14px; color: {TEXT};
    }}
    QLabel#SettingValue {{ color: {MUTED}; font-family: {FONT_MONO}; font-size: 12px; }}
    GhostSlider, QSlider {{ background: transparent; }}

    QLabel#PageTitle, QLabel#SectionTitle {{
        font-family: {FONT_DISPLAY};
        font-weight: 600;
        font-size: 17px;
        color: {TEXT};
    }}
    QLabel#Muted {{ color: {MUTED}; }}
    QLabel#Dim {{ color: {DIM}; }}
    QToolButton#GuideToggle {{ background: transparent; border: none; color: {ACCENT_TEXT}; font-weight: 600;
        padding: 2px 0; }}
    QToolButton#GuideToggle:hover {{ text-decoration: underline; }}
    QToolButton#StepToggle {{ background: transparent; border: none; color: {TEXT}; font-weight: 600;
        text-align: left; padding: 2px 0; }}
    QLabel#StepBadge {{ background: {_mix(PANEL, ACCENT, 0.2)}; color: {ACCENT_TEXT}; border-radius: 12px;
        font-weight: 600; }}
    QLabel#ErrorText {{ color: {RED}; }}
    QLabel#StatLabel {{ color: {MUTED}; font-size: 12px; font-weight: 500; }}
    QLabel#StatValue {{
        font-size: 22px;
        font-weight: 600;
        color: {TEXT};
    }}

    QLabel#PillOn, QLabel#PillOff, QLabel#PillWarn, QLabel#PillBad {{
        border-radius: 9px;
        padding: 2px 10px;
        font-size: 11px;
        font-weight: 600;
        max-height: 18px;
    }}
    QLabel#PillOn {{ color: {GREEN}; background-color: {_rgba(GREEN, 31)}; }}
    QLabel#PillOff {{ color: {MUTED}; background-color: {_rgba(MUTED, 31)}; }}
    QLabel#PillWarn {{ color: {YELLOW}; background-color: {_rgba(YELLOW, 33)}; }}
    QLabel#PillBad {{ color: {RED}; background-color: {_rgba(RED, 33)}; }}

    /* ------------------------------------------------------- inputs -- */
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit {{
        background-color: {SUNK};
        border: 1px solid {CONTROL_BORDER};
        border-radius: 8px;
        padding: 7px 10px;
        color: {TEXT};
        selection-background-color: {FILL};
    }}
    QTextEdit, QPlainTextEdit {{
        background-color: {SUNK};
        border: 1px solid {BORDER};
        border-radius: 10px;
        padding: 8px 10px;
        color: #c8c8c8;
        font-family: {FONT_MONO};
        font-size: 12px;
        selection-background-color: {FILL};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
        border: 1px solid {ACCENT};
    }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {{ color: {DIM}; }}
    QLineEdit[dirty="true"], QSpinBox[dirty="true"], QDoubleSpinBox[dirty="true"], QComboBox[dirty="true"] {{
        border: 1px solid {ACCENT};
    }}
    QComboBox::drop-down {{ border: none; width: 24px; }}
    QComboBox QAbstractItemView {{
        background-color: {PANEL};
        border: 1px solid {BORDER};
        selection-background-color: {_rgba(ACCENT, 50)};
        selection-color: {TEXT};
        outline: none;
    }}
    QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
        width: 0; border: none;
    }}

    QCheckBox {{ spacing: 10px; background: transparent; }}
    QCheckBox::indicator {{ width: 34px; height: 20px; }}
    QCheckBox::indicator:unchecked {{ image: url("{sw['off']}"); }}
    QCheckBox::indicator:checked {{ image: url("{sw['on']}"); }}
    QCheckBox::indicator:unchecked:disabled {{ image: url("{sw['off_disabled']}"); }}
    QCheckBox::indicator:checked:disabled {{ image: url("{sw['on_disabled']}"); }}
    QCheckBox:disabled {{ color: {DIM}; }}
    QRadioButton {{ spacing: 10px; background: transparent; min-height: 26px; }}
    QRadioButton::indicator {{ width: 18px; height: 18px; }}
    QRadioButton::indicator:unchecked {{ image: url("{sw['radio_off']}"); }}
    QRadioButton::indicator:checked {{ image: url("{sw['radio_on']}"); }}
    QRadioButton::indicator:unchecked:disabled {{ image: url("{sw['radio_off_disabled']}"); }}
    QRadioButton::indicator:checked:disabled {{ image: url("{sw['radio_on_disabled']}"); }}
    QRadioButton:disabled {{ color: {DIM}; }}

    /* Setup wizard */
    QWizard {{ background-color: {BG}; }}
    QWizardPage {{ background: transparent; }}
    QFrame#WizardSteps {{ background-color: {SUNK}; border-right: 1px solid {BORDER}; }}
    QFrame#WizardSteps QLabel {{ background: transparent; }}
    QLabel#StepBadge {{
        border-radius: 12px; font-size: 12px; font-weight: 600;
        color: {MUTED}; background-color: {PANEL}; border: 1px solid {BUTTON_BORDER};
    }}
    QLabel#StepBadge[state="current"] {{ color: #ffffff; background-color: {FILL}; border-color: {FILL}; }}
    QLabel#StepBadge[state="done"] {{ color: {GREEN}; background-color: {_rgba(GREEN, 31)}; border-color: {_rgba(GREEN, 80)}; }}
    QLabel#StepLabel {{ color: {MUTED}; font-size: 14px; }}
    QLabel#StepLabel[state="current"] {{ color: {ACCENT_TEXT}; font-weight: 600; }}
    QLabel#StepLabel[state="done"] {{ color: {TEXT}; }}

    QSlider {{ background: transparent; }}
    QSlider::groove:horizontal {{ height: 6px; background: {CONTROL_BORDER}; border-radius: 3px; }}
    QSlider::handle:horizontal {{
        background: {TEXT}; border: 2px solid {FILL};
        width: 14px; height: 14px; margin: -5px 0; border-radius: 8px;
    }}
    QSlider::sub-page:horizontal {{ background: {FILL}; border-radius: 3px; }}

    /* -------------------------------------------------- lists/tables -- */
    QListWidget, QTableWidget, QTreeWidget {{
        background-color: {PANEL};
        border: 1px solid {BORDER};
        border-radius: 10px;
        outline: none;
        padding: 4px;
    }}
    QListWidget::item {{
        padding: 9px 10px;
        border-radius: 6px;
        border-bottom: 1px solid {_shade(BORDER, 0.92)};
    }}
    QListWidget::item:hover {{ background-color: {CONTROL_HOVER}; }}
    QListWidget::item:selected, QTableWidget::item:selected {{
        background-color: {_rgba(ACCENT, 38)};
        color: {TEXT};
    }}
    QTableWidget {{ gridline-color: {BORDER}; padding: 0; }}
    QTableWidget::item {{ padding: 6px 10px; border-bottom: 1px solid {_shade(BORDER, 0.92)}; }}
    QListWidget#ModList {{ padding: 0; }}
    QListWidget#ModList QLabel, QListWidget#ModList QCheckBox, QListWidget#ModList QWidget#TransparentRow {{
        background: transparent;
    }}
    QListWidget#ModList::item {{ padding: 0; border-radius: 0; }}
    QListWidget#ModList::item, QListWidget#ModList::item:selected, QListWidget#ModList::item:hover {{
        color: transparent;
    }}
    QListWidget#ModList::item:selected {{ background-color: {_rgba(ACCENT, 26)}; }}
    QLabel#ModName {{ font-weight: 600; font-size: 14px; color: {TEXT}; }}
    QLabel#ModName[tone="dim"] {{ color: {DIM}; }}
    QLabel#ModName[tone="warn"] {{ color: {YELLOW}; }}
    QLabel#ModName[tone="bad"] {{ color: {RED}; }}
    QHeaderView {{ background: transparent; }}
    QHeaderView::section {{
        background-color: {PANEL};
        color: {MUTED};
        border: none;
        border-bottom: 1px solid {BORDER};
        padding: 10px 10px;
        font-size: 12px;
        font-weight: 600;
    }}
    QTableCornerButton::section {{ background-color: {PANEL}; border: none; }}

    QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; top: -1px; }}
    QTabBar::tab {{
        background: transparent; color: {MUTED};
        padding: 8px 14px; border: none; border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:selected {{ color: {ACCENT_TEXT}; border-bottom: 2px solid {ACCENT}; }}

    QProgressBar {{
        background-color: {SUNK}; border: 1px solid {BORDER}; border-radius: 6px;
        text-align: center; color: {TEXT}; min-height: 14px;
    }}
    QProgressBar::chunk {{ background-color: {FILL}; border-radius: 5px; }}

    QGroupBox {{
        border: 1px solid {BORDER}; border-radius: 10px;
        margin-top: 14px; padding: 12px; background-color: {PANEL};
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; color: {MUTED}; }}

    QMenu {{
        background-color: {PANEL}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px;
    }}
    QMenu::item {{ padding: 7px 18px; border-radius: 6px; background: transparent; }}
    QMenu::item:selected {{ background-color: {_rgba(ACCENT, 38)}; }}
    QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

    QScrollArea {{ border: none; background: transparent; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {CONTROL_BORDER}; border-radius: 4px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: {DIM}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {CONTROL_BORDER}; border-radius: 4px; min-width: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
    """
