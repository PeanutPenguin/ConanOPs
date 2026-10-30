"""
Server Settings as the app shows it since 1.0.14: four sections, each led by
presets (presets.py), with the individual settings shown only in Advanced
mode. The settings pages themselves are unchanged and still hold, validate
and save every value; this only arranges them.

  Server        -- server type, name, password, player limit
                   (Advanced: ports, every identity setting, RCON)
  Gameplay      -- play style (Advanced: every gameplay setting)
  Automation    -- backups, daily restart, game updates, broken-mod handling
  Check for Problems -- Diagnostics
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

import presets
from ui.preset_group import PresetBinding, PresetGroup, page_reader, page_signals, page_writer


def take_form(page) -> QWidget:
    """A settings page's form, lifted out of the page's own scroll area so it
    can sit in a section with other things. The page keeps managing it."""
    form = page.form_container
    scroll = form.parentWidget()
    while scroll is not None and not isinstance(scroll, QScrollArea):
        scroll = scroll.parentWidget()
    if scroll is not None and scroll.widget() is form:
        scroll.takeWidget()
        scroll.hide()
    page.header_widget.hide()
    form.layout().setContentsMargins(0, 0, 0, 0)
    return form


def take_scroll_content(widget) -> QWidget:
    """The inside of a widget that scrolls by itself (GameplaySection)."""
    scroll = getattr(widget, "scroll", None)
    if isinstance(scroll, QScrollArea) and scroll.widget() is not None:
        inner = scroll.takeWidget()
        scroll.hide()
        if inner.layout() is not None:
            inner.layout().setContentsMargins(0, 0, 0, 0)
        return inner
    return widget


def titled_card(title: str, body: QWidget) -> QFrame:
    card = QFrame()
    card.setObjectName("Card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(18, 16, 18, 16)
    lay.setSpacing(10)
    head = QLabel(title)
    head.setObjectName("SectionTitle")
    lay.addWidget(head)
    lay.addWidget(body)
    return card


def titled(title: str, body: QWidget) -> QWidget:
    """A heading over a settings form that already draws its own card."""
    box = QWidget()
    box.setObjectName("TransparentRow")
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 6, 0, 0)
    lay.setSpacing(8)
    head = QLabel(title)
    head.setObjectName("SectionTitle")
    lay.addWidget(head)
    lay.addWidget(body)
    return box


class Column(QScrollArea):
    """A scrolling column of cards; some shown only in Advanced mode."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        self.layout_ = QVBoxLayout(inner)
        self.layout_.setContentsMargins(0, 0, 8, 16)
        self.layout_.setSpacing(14)
        self.setWidget(inner)
        self._advanced_only: List[QWidget] = []
        self._simple_only: List[QWidget] = []
        self.pickers: List[PresetGroup] = []
        self.on_set_advanced: List[Callable[[bool], None]] = []

    def add(self, widget: QWidget, advanced_only: bool = False, simple_only: bool = False) -> QWidget:
        self.layout_.addWidget(widget)
        if advanced_only:
            self._advanced_only.append(widget)
        if simple_only:
            self._simple_only.append(widget)
        if isinstance(widget, PresetGroup):
            self.pickers.append(widget)
        return widget

    def finish(self) -> None:
        self.layout_.addStretch(1)

    def set_advanced(self, on: bool) -> None:
        for w in self._advanced_only:
            w.setVisible(on)
        for w in self._simple_only:
            w.setVisible(not on)
        for p in self.pickers:
            p.set_advanced(on)
        for fn in self.on_set_advanced:
            fn(on)


class ServerSections:
    """Builds the four section widgets from the existing settings pages."""

    def __init__(self, *, identity, network, rcon, gameplay_section, backups, restart, updates_page,
                 diagnostics, all_pages: List, request_advanced: Callable[[], None]):
        self.request_advanced = request_advanced
        self.bindings: List[PresetBinding] = []
        self.columns: Dict[str, QWidget] = {}
        pages = list(all_pages)

        # ----------------------------------------------------------- server --
        col = Column()
        self.server_type = self._picker(col, presets.SERVER_TYPE)
        self._bind_pages(self.server_type, pages)
        net_form = take_form(network)
        col.add(titled("Name and password", net_form))
        col.add(titled("More server settings", take_form(identity)), advanced_only=True)
        col.add(titled("Remote commands (RCON)", take_form(rcon)), advanced_only=True)
        col.on_set_advanced.append(lambda on: network.set_simple(not on))
        col.finish()
        self.columns["server"] = col

        # --------------------------------------------------------- gameplay --
        col = Column()
        details = take_scroll_content(gameplay_section)
        self.play_style = self._picker(col, presets.PLAY_STYLE, details=details)
        self._bind_pages(self.play_style, pages)
        col.finish()
        self.columns["gameplay"] = col

        # ------------------------------------------------------- automation --
        col = Column()
        backups_list = backups.backups_widget
        backups.form_layout.removeWidget(backups_list)
        backups.divider.hide()
        self.backups = self._picker(col, presets.BACKUPS, details=take_form(backups))
        self._bind_pages(self.backups, pages)
        col.add(titled_card("Saved backups", backups_list))

        def restart_resolve(option_id: str, values: dict) -> dict:
            suggested = getattr(restart, "_suggested", None)
            if option_id == "quiet" and suggested:
                values = dict(values, restart_start=suggested[0], restart_end=suggested[1])
            return values
        self.restart = self._picker(col, presets.RESTART, details=take_form(restart))
        self._bind_pages(self.restart, pages, resolve=restart_resolve)

        self.updates = self._picker(col, presets.UPDATES)
        self.bindings.append(PresetBinding(
            self.updates,
            read=lambda: {"auto_update": updates_page.auto_update_check.isChecked(),
                          "auto_update_check_interval_hours": updates_page.interval_spin.value()},
            write=lambda v: (updates_page.auto_update_check.setChecked(bool(v["auto_update"])),
                             updates_page.interval_spin.setValue(int(v["auto_update_check_interval_hours"]))),
            signals=[updates_page.auto_update_check.toggled, updates_page.interval_spin.valueChanged,
                     updates_page.settings_shown]))
        updates_page.layout().setContentsMargins(0, 0, 0, 0)
        col.add(titled_card("Game version", updates_page))
        col.on_set_advanced.append(lambda on: updates_page.set_simple(not on))
        # Bound later to App Settings (bind_mod_recovery): it's one setting for every server.
        self.mod_recovery = self._picker(col, presets.MOD_RECOVERY)
        col.finish()
        self.columns["automation"] = col

        # -------------------------------------------------------- problems --
        self.columns["diagnostics"] = diagnostics

    def _picker(self, col: Column, group, details: Optional[QWidget] = None) -> PresetGroup:
        picker = PresetGroup(group, details=details)
        picker.advanced_requested.connect(self.request_advanced)
        col.add(picker)
        return picker

    def _bind_pages(self, picker: PresetGroup, pages: List, resolve=None) -> None:
        keys = picker.group.keys()
        self.bindings.append(PresetBinding(picker, page_reader(pages, keys), page_writer(pages),
                                           page_signals(pages, keys), resolve=resolve))

    def bind_mod_recovery(self, combo) -> None:
        """Keeps the broken-mod preset and App Settings' saved choice in step."""
        def write(values: dict) -> None:
            idx = combo.findData(values.get("mod_recovery_mode"))
            if idx >= 0:
                combo.setCurrentIndex(idx)
        self.bindings.append(PresetBinding(
            self.mod_recovery, read=lambda: {"mod_recovery_mode": combo.currentData()}, write=write,
            signals=[combo.currentIndexChanged]))

    def set_advanced(self, on: bool) -> None:
        for col in self.columns.values():
            if isinstance(col, Column):
                col.set_advanced(on)

    def entries(self) -> list:
        """(key, label, heading, blurb, widget) for SettingsContainer."""
        import settings_layout
        return [(key, label, heading, blurb, self.columns[key])
                for key, label, heading, blurb, _pages in settings_layout.DESKTOP_SECTIONS]
