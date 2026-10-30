"""
A preset picker for one group of settings (presets.py): a card per preset,
a "?" that shows the longer explanation, a "Custom" badge when the settings
match no preset, and the individual settings underneath, shown only in
Advanced mode.

PresetBinding connects a picker to wherever its settings live -- a settings
page's staged fields (saved with the Save bar) or App Settings controls
(saved immediately) -- and keeps the picker in step when they change.
"""
from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

import presets as presets_mod

_COLUMNS = {2: 2, 3: 3, 4: 4}


class PresetCard(QPushButton):
    """A preset choice: a checkable button laid out like a small card. Grows
    to fit its wrapped text (a QPushButton doesn't do that by itself)."""

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt name
        super().resizeEvent(event)
        lay = self.layout()
        if lay is not None and lay.hasHeightForWidth():
            want = lay.heightForWidth(self.width())
            if want > 0 and want != self.minimumHeight():
                self.setMinimumHeight(want)


class PresetGroup(QFrame):
    chosen = Signal(str)            # an option id, picked by the person
    advanced_requested = Signal()   # "Fine-tune in Advanced"

    def __init__(self, group: presets_mod.Group, details: Optional[QWidget] = None,
                 note: str = "", parent=None, details_elsewhere: bool = False):
        """details: the individual settings, shown under the presets in Advanced
        mode. details_elsewhere: they're shown separately (below this card), so
        only offer the "Fine-tune in Advanced" link."""
        super().__init__(parent)
        self.group = group
        self.setObjectName("Card")
        self._current = ""
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        self.title_label = QLabel(group.title)
        self.title_label.setObjectName("SectionTitle")
        head.addWidget(self.title_label)
        self.help_btn = QPushButton("?")
        self.help_btn.setObjectName("HelpButton")
        self.help_btn.setCheckable(True)
        self.help_btn.setAccessibleName(f"What is {group.title}?")
        self.help_btn.setToolTip(group.help)
        head.addWidget(self.help_btn)
        self.custom_badge = QLabel("Custom")
        self.custom_badge.setObjectName("CustomBadge")
        self.custom_badge.setToolTip("These settings don't match any of the choices below.")
        self.custom_badge.hide()
        head.addWidget(self.custom_badge)
        head.addStretch(1)
        lay.addLayout(head)

        self.help_label = QLabel(group.help)
        self.help_label.setObjectName("Muted")
        self.help_label.setWordWrap(True)
        self.help_label.hide()
        self.help_btn.toggled.connect(self.help_label.setVisible)
        lay.addWidget(self.help_label)

        grid = QGridLayout()
        grid.setSpacing(10)
        cols = _COLUMNS.get(len(group.options), 3)
        self._buttons: Dict[str, QPushButton] = {}
        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(False)  # so "Custom" can leave none checked
        for i, opt in enumerate(group.options):
            btn = PresetCard()
            btn.setObjectName("PresetCard")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setAccessibleName(f"{opt.name}: {opt.summary}")
            inner = QVBoxLayout(btn)
            inner.setContentsMargins(14, 12, 14, 12)
            inner.setSpacing(4)
            name = QLabel(opt.name)
            name.setObjectName("PresetName")
            inner.addWidget(name)
            if opt.recommended:
                rec = QLabel("Recommended")
                rec.setObjectName("PresetRecommended")
                inner.addWidget(rec)
            summary = QLabel(opt.summary)
            summary.setObjectName("PresetSummary")
            summary.setWordWrap(True)
            inner.addWidget(summary)
            inner.addStretch(1)
            for child in btn.findChildren(QLabel):
                child.setAttribute(Qt.WA_TransparentForMouseEvents)
            btn.setMinimumHeight(inner.sizeHint().height() + 8)
            btn.setSizePolicy(btn.sizePolicy().horizontalPolicy(), QSizePolicy.Minimum)
            btn.clicked.connect(lambda _=False, oid=opt.id: self._pick(oid))
            self._button_group.addButton(btn)
            grid.addWidget(btn, i // cols, i % cols)
            self._buttons[opt.id] = btn
        for c in range(cols):
            grid.setColumnStretch(c, 1)
        lay.addLayout(grid)

        if note:
            note_label = QLabel(note)
            note_label.setObjectName("Dim")
            note_label.setWordWrap(True)
            lay.addWidget(note_label)

        self.details = details
        self.details_box = None
        self.advanced_link = None
        if details is not None:
            self.details_box = QFrame()
            self.details_box.setObjectName("TransparentRow")
            dl = QVBoxLayout(self.details_box)
            dl.setContentsMargins(0, 6, 0, 0)
            dl.setSpacing(8)
            caption = QLabel("Details -- changing one makes this Custom")
            caption.setObjectName("Dim")
            dl.addWidget(caption)
            dl.addWidget(details)
            lay.addWidget(self.details_box)
        if details is not None or details_elsewhere:
            self.advanced_link = QPushButton("Fine-tune in Advanced")
            self.advanced_link.setObjectName("LinkButton")
            self.advanced_link.setCursor(Qt.PointingHandCursor)
            self.advanced_link.clicked.connect(self.advanced_requested.emit)
            lay.addWidget(self.advanced_link, 0, Qt.AlignLeft)
        self.set_advanced(False)

    def _pick(self, option_id: str) -> None:
        self.set_current(option_id)
        self.chosen.emit(option_id)

    def set_current(self, option_id: str) -> None:
        """Shows which preset the settings match (presets.CUSTOM: none)."""
        self._current = option_id
        for oid, btn in self._buttons.items():
            btn.setChecked(oid == option_id)
        self.custom_badge.setVisible(option_id == presets_mod.CUSTOM)

    def current(self) -> str:
        return self._current

    def set_advanced(self, on: bool) -> None:
        if self.details_box is not None:
            self.details_box.setVisible(on)
        if self.advanced_link is not None:
            self.advanced_link.setVisible(not on)

    def buttons(self) -> Dict[str, QPushButton]:
        return dict(self._buttons)


class PresetBinding(QObject):
    """Keeps a PresetGroup and the settings it stands for in step.
    read() -> {setting: value}; write({setting: value}) applies a preset."""

    def __init__(self, picker: PresetGroup, read: Callable[[], dict], write: Callable[[dict], None],
                 signals: Iterable = (), resolve: Optional[Callable[[str, dict], dict]] = None, parent=None):
        super().__init__(parent or picker)
        self.picker, self.read, self.write, self.resolve = picker, read, write, resolve
        self._writing = False
        picker.chosen.connect(self._on_chosen)
        for sig in signals:
            sig.connect(lambda *_: self.refresh())
        self.refresh()

    def _on_chosen(self, option_id: str) -> None:
        values = presets_mod.values_for(self.picker.group, option_id, self.resolve)
        self._writing = True
        try:
            self.write(values)
        finally:
            self._writing = False
        self.refresh()

    def refresh(self) -> None:
        if self._writing:
            return
        try:
            current = self.read()
        except Exception:  # noqa: BLE001 - a page mid-rebuild; try again on the next change
            return
        self.picker.set_current(presets_mod.match(self.picker.group, current, self.resolve))


# ------------------------------------------------------- page helpers --
def page_owner(pages: List, key: str):
    """The settings page that registered `key`, or None."""
    return next((p for p in pages if key in getattr(p, "_fields", {})), None)


def page_reader(pages: List, keys: Iterable[str]) -> Callable[[], dict]:
    keys = list(keys)

    def read() -> dict:
        out = {}
        for k in keys:
            p = page_owner(pages, k)
            if p is not None:
                out[k] = p._getters[k](p._fields[k])
        return out
    return read


def page_writer(pages: List) -> Callable[[dict], None]:
    """Sets staged values on the pages (saved with the Save bar)."""
    def write(values: dict) -> None:
        for k, v in values.items():
            p = page_owner(pages, k)
            if p is not None:
                p._setters[k](p._fields[k], v)
    return write


def page_signals(pages: List, keys: Iterable[str]) -> list:
    """dirty_changed of every page that owns one of `keys` (fires on edits and loads)."""
    out, seen = [], set()
    for k in keys:
        p = page_owner(pages, k)
        if p is not None and id(p) not in seen:
            seen.add(id(p))
            out.append(p.dirty_changed)
    return out
