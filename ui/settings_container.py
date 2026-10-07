from __future__ import annotations

from typing import List, Tuple

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QPushButton, QStackedWidget,
    QButtonGroup, QScrollArea, QFrame, QLabel,
)

# Sub-nav group headings, shown above the entry with that key.
GROUP_HEADINGS = {"diagnostics": "Server", "progression": "Gameplay", "backups": "Automation"}


class SettingsContainer(QWidget):
    """Sub-navigation for the Settings area, built from an ordered list of
    (key, label, page_widget) tuples."""

    def __init__(self, entries: List[Tuple[str, str, QWidget]], parent=None):
        super().__init__(parent)

        root = QHBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 20)
        root.setSpacing(24)

        subnav_scroll = QScrollArea()
        subnav_scroll.setWidgetResizable(True)
        subnav_scroll.setFrameShape(QFrame.NoFrame)
        subnav_scroll.setFixedWidth(210)

        subnav_container = QWidget()
        subnav = QVBoxLayout(subnav_container)
        subnav.setSpacing(2)
        subnav.setContentsMargins(0, 0, 0, 0)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.stack = QStackedWidget()
        self.pages = {}

        for key, label, widget in entries:
            if key in GROUP_HEADINGS:
                heading = QLabel(GROUP_HEADINGS[key])
                heading.setObjectName("SectionLabel")
                subnav.addWidget(heading)
            # Qt treats a single & as a shortcut marker.
            btn = QPushButton(label.replace("&", "&&"))
            btn.setObjectName("NavButton")
            btn.setCheckable(True)
            btn.setProperty("sub_key", key)
            btn.clicked.connect(lambda _=False, k=key: self._select(k))
            self._group.addButton(btn)
            subnav.addWidget(btn)
            self.stack.addWidget(widget)
            self.pages[key] = widget
            # Each page shows the total pending count across all tabs, and
            # Apply on any tab applies every tab (see apply_all()).
            if hasattr(widget, "dirty_changed"):
                widget.dirty_changed.connect(self._recompute_total_pending)
            if hasattr(widget, "set_apply_all_hook"):
                widget.set_apply_all_hook(self.apply_all)

        subnav.addStretch(1)
        subnav_scroll.setWidget(subnav_container)
        root.addWidget(subnav_scroll)
        root.addWidget(self.stack, 1)

        if entries:
            self._select(entries[0][0])

    def _recompute_total_pending(self) -> None:
        total = sum(p.dirty_count() for p in self.pages.values() if hasattr(p, "dirty_count"))
        for p in self.pages.values():
            if hasattr(p, "set_total_pending"):
                p.set_total_pending(total)

    def apply_all(self) -> None:
        """Applies every page's pending changes. A page whose can_apply() is
        False is skipped and its changes stay pending."""
        for p in self.pages.values():
            if not hasattr(p, "dirty_count") or not hasattr(p, "apply_own"):
                continue
            if p.dirty_count() <= 0:
                continue
            if hasattr(p, "can_apply") and not p.can_apply():
                continue
            p.apply_own()

    def _select(self, key: str) -> None:
        for btn in self._group.buttons():
            btn.setChecked(btn.property("sub_key") == key)
        self.stack.setCurrentWidget(self.pages[key])
