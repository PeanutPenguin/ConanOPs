"""
GhostSlider: a QSlider that also draws a small ring at the last-applied
("committed") value. While the live value matches the committed one, the
ghost sits exactly under the handle and is invisible; drag away from it
and the ghost stays put while a highlighted strip shows the span between
old and new value.
"""
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
        # Must match the handle's rendered size in ui/theme.py's QSS
        # (`QSlider::handle:horizontal { width: 14px; height: 14px; }`)
        # -- used to keep our custom overlay from drawing into the
        # handle's circle rather than stopping cleanly at its edge.
        handle_radius = 7

        if ghost_x != live_x:
            lo, hi = sorted((ghost_x, live_x))
            # Inset whichever edge sits at the live handle so the flat
            # rectangle stops at the edge of its circle instead of
            # cutting across the middle of it -- the ghost-side edge
            # doesn't need this since the ghost ring below is drawn on
            # top of it and caps that end on its own.
            if live_x == hi:
                hi = max(lo, hi - handle_radius)
            else:
                lo = min(hi, lo + handle_radius)
            if hi > lo:
                painter.fillRect(QRect(lo, y_center - 3, hi - lo, 6), SPAN_COLOR)

            # Ghost ring -- only drawn when there's an actual pending
            # change to show. When the live value matches the committed
            # one, this is skipped entirely so the real handle (already
            # painted by super().paintEvent() above) shows through
            # untouched, rather than being covered by an opaque "ghost"
            # that isn't actually a ghost -- it was previously drawn
            # every time, including when it exactly overlapped the real
            # handle, replacing its ivory circle with a dark one.
            painter.setPen(QPen(GHOST_COLOR, 2))
            painter.setBrush(BG_COLOR)
            painter.drawEllipse(ghost_x - handle_radius, y_center - handle_radius, handle_radius * 2, handle_radius * 2)

        painter.end()
