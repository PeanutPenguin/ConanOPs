"""
Step-by-step guide that folds out under a "How do I...?" link; each step's
details fold out too.
"""
from __future__ import annotations

from typing import List, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget


class _Step(QFrame):
    def __init__(self, number: int, title: str, body: str, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(10)
        badge = QLabel(str(number))
        badge.setObjectName("StepBadge")
        badge.setFixedSize(24, 24)
        badge.setAlignment(Qt.AlignCenter)
        top.addWidget(badge)
        self.toggle = QToolButton()
        self.toggle.setObjectName("StepToggle")
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setArrowType(Qt.RightArrow)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.toggled.connect(self._set_open)
        top.addWidget(self.toggle)
        top.addStretch(1)
        lay.addLayout(top)
        self.body = QLabel(body)
        self.body.setObjectName("Dim")
        self.body.setWordWrap(True)
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.setContentsMargins(34, 0, 0, 2)
        self.body.hide()
        lay.addWidget(self.body)

    def _set_open(self, on: bool) -> None:
        self.toggle.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.body.setVisible(on)


class FoldOutGuide(QWidget):
    def __init__(self, title: str, intro: str, steps: List[Tuple[str, str]], parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        self.toggle = QToolButton()
        self.toggle.setObjectName("GuideToggle")
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setArrowType(Qt.RightArrow)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.toggled.connect(self._set_open)
        root.addWidget(self.toggle, 0, Qt.AlignLeft)
        self.panel = QWidget()
        pl = QVBoxLayout(self.panel)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        intro_label = QLabel(intro)
        intro_label.setObjectName("Dim")
        intro_label.setWordWrap(True)
        pl.addWidget(intro_label)
        self.steps = [_Step(i, t, b) for i, (t, b) in enumerate(steps, 1)]
        for step in self.steps:
            pl.addWidget(step)
        self.panel.hide()
        root.addWidget(self.panel)

    def _set_open(self, on: bool) -> None:
        self.toggle.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.panel.setVisible(on)
