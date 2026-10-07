"""QSlider that marks the last-applied ("committed") value with a ghost ring and
highlights the span to the live value while they differ."""
from __future__ import annotations

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import QSlider

GHOST_COLOR = QColor("#7d7263")
SPAN_COLOR = QColor("#e0b04a")
BG_COLOR = QColor("#120f0c")


class GhostSlider(QSlider):
    def __init__(self, minimum: int, maximum: int, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setMinimum(minimum)
        self.setMaximum(maximum)
        self._committed_value = minimum
        self.setFixedHeight(24)

    def set_committed_value(self, value: int) -> None:
        self._committed_value = value
        self.update()

    def _value_to_x(self, value: int) -> int:
        groove_margin = 8
        usable = self.width() - 2 * groove_margin
        span = self.maximum() - self.minimum()
        if span <= 0:
            return groove_margin
        frac = (value - self.minimum()) / span
        return groove_margin + int(frac * usable)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        y_center = self.height() // 2
        ghost_x = self._value_to_x(self._committed_value)
        live_x = self._value_to_x(self.value())
        # Must match the 14px handle size in ui/theme.py's QSS.
        handle_radius = 7

        if ghost_x != live_x:
            lo, hi = sorted((ghost_x, live_x))
            # Stop the strip at the live handle's edge; the ghost ring caps the other end.
            if live_x == hi:
                hi = max(lo, hi - handle_radius)
            else:
                lo = min(hi, lo + handle_radius)
            if hi > lo:
                painter.fillRect(QRect(lo, y_center - 3, hi - lo, 6), SPAN_COLOR)

            # Only drawn when values differ, so it never covers the real handle.
            painter.setPen(QPen(GHOST_COLOR, 2))
            painter.setBrush(BG_COLOR)
            painter.drawEllipse(ghost_x - handle_radius, y_center - handle_radius, handle_radius * 2, handle_radius * 2)

        painter.end()
