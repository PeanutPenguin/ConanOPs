"""A page made of tabs (e.g. Players: Players | Access), each an existing page."""
from __future__ import annotations

from typing import List, Tuple

from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QPushButton, QStackedWidget, QVBoxLayout, QWidget


class TabbedPage(QWidget):
    def __init__(self, tabs: List[Tuple[str, str, QWidget]], parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        strip = QFrame()
        strip.setObjectName("TabStrip")
        row = QHBoxLayout(strip)
        row.setContentsMargins(24, 4, 24, 0)
        row.setSpacing(4)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.stack = QStackedWidget()
        self.tabs = {}
        self.buttons = {}
        for key, label, page in tabs:
            btn = QPushButton(label.replace("&", "&&"))
            btn.setObjectName("TabButton")
            btn.setCheckable(True)
            btn.clicked.connect(lambda _=False, k=key: self.show_tab(k))
            self._group.addButton(btn)
            row.addWidget(btn)
            self.stack.addWidget(page)
            self.tabs[key] = page
            self.buttons[key] = btn
        row.addStretch(1)
        root.addWidget(strip)
        root.addWidget(self.stack, 1)
        if tabs:
            self.show_tab(tabs[0][0])

    def show_tab(self, key: str) -> None:
        if key in self.tabs:
            self.buttons[key].setChecked(True)
            self.stack.setCurrentWidget(self.tabs[key])

    def current_tab(self) -> str:
        page = self.stack.currentWidget()
        return next((k for k, p in self.tabs.items() if p is page), "")
