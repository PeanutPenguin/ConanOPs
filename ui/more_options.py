"""A "More options" fold-out: a header that opens a body of less-used settings."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QHBoxLayout, QToolButton, QVBoxLayout, QWidget


class MoreOptions(QFrame):
    def __init__(self, title: str = "More options", count: int = 0, parent=None, card: bool = True):
        super().__init__(parent)
        self.setObjectName("Card" if card else "TransparentRow")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(14 if card else 0, 4, 18 if card else 0, 4)
        self.toggle = QToolButton()
        self.toggle.setObjectName("FoldToggle")
        self.toggle.setText(title)
        self.toggle.setCheckable(True)
        self.toggle.setArrowType(Qt.RightArrow)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setCursor(Qt.PointingHandCursor)
        self.toggle.setMinimumHeight(40)
        self.toggle.toggled.connect(self._set_open)
        head.addWidget(self.toggle)
        head.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("Dim")
        head.addWidget(self.count_label)
        root.addLayout(head)
        self.body = QWidget()
        self.body.setObjectName("TransparentRow")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 6)
        self.body_layout.setSpacing(0)
        self.body.hide()
        root.addWidget(self.body)
        self.set_count(count)

    def set_count(self, n: int) -> None:
        self.count_label.setText(f"{n} setting{'' if n == 1 else 's'}" if n else "")

    def _set_open(self, on: bool) -> None:
        self.toggle.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.body.setVisible(on)

    def set_open(self, on: bool) -> None:
        self.toggle.setChecked(on)

    def is_open(self) -> bool:
        return self.toggle.isChecked()


def open_folds_around(widget: QWidget) -> None:
    """Opens every MoreOptions the widget sits in, so it can be seen."""
    w = widget.parentWidget()
    while w is not None:
        if isinstance(w, MoreOptions):
            w.set_open(True)
        w = w.parentWidget()
