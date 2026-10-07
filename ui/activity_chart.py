"""
Bar chart of player activity by hour of day (24 bars), used on the
Restart Schedule page. The hours inside the current restart window get
a tinted full-height column plus an accent bar, so the window stays
visible even when its bars are tiny -- which they should be, since the
point is to restart when nobody's around.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget


def hour_name(h: int) -> str:
    return f"{12 if h % 12 == 0 else h % 12} {'AM' if h < 12 else 'PM'}"


def window_hours(start_h: Optional[int], end_h: Optional[int]) -> List[int]:
    """Hours covered by a start-end window, wrapping past midnight
    (22 -> 2 covers 22, 23, 0, 1). Empty if either end is unknown;
    start == end means a one-hour window."""
    if start_h is None or end_h is None:
        return []
    if start_h == end_h:
        return [start_h]
    hours, h = [], start_h
    while h != end_h:
        hours.append(h)
        h = (h + 1) % 24
    return hours


class ActivityChart(QWidget):
    PLOT_HEIGHT = 150
    AXIS_W = 26
    LABEL_H = 20

    def __init__(self, accent: str = "#c9752f", bar: str = "#4a4a4a", text: str = "#8f8f8f",
                 grid: str = "#2c2c2c", parent=None):
        super().__init__(parent)
        self._values: List[int] = [0] * 24
        self._window: List[int] = []
        self.set_colors(accent, bar, text, grid)
        self.setMinimumHeight(self.PLOT_HEIGHT + self.LABEL_H + 8)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.setAccessibleName("Player activity by hour")

    def set_colors(self, accent: str, bar: str, text: str, grid: str) -> None:
        self._accent, self._bar, self._text, self._grid = (QColor(c) for c in (accent, bar, text, grid))
        self.update()

    def set_values(self, values: Sequence[int]) -> None:
        vals = list(values)[:24]
        self._values = vals + [0] * (24 - len(vals))
        self.setAccessibleDescription(self.summary())
        self.update()

    def set_window(self, start_h: Optional[int], end_h: Optional[int]) -> None:
        self._window = window_hours(start_h, end_h)
        self.update()

    def has_data(self) -> bool:
        return sum(self._values) > 0

    def quietest_and_busiest(self) -> tuple:
        if not self.has_data():
            return None, None
        quiet = min(range(24), key=lambda h: (self._values[h], h))
        busy = max(range(24), key=lambda h: (self._values[h], -h))
        return quiet, busy

    def summary(self) -> str:
        quiet, busy = self.quietest_and_busiest()
        if quiet is None:
            return "Not enough session history yet."
        return (f"Quietest {hour_name(quiet)}–{hour_name((quiet + 1) % 24)} "
                f"({self._values[quiet]}) · Busiest {hour_name(busy)}–{hour_name((busy + 1) % 24)} ({self._values[busy]})")

    # ---------------------------------------------------------- geometry --
    def _plot_rect(self) -> QRectF:
        return QRectF(self.AXIS_W + 6, 4, max(1, self.width() - self.AXIS_W - 8), self.PLOT_HEIGHT)

    def _bar_rect(self, h: int, value: float, top: float) -> QRectF:
        plot = self._plot_rect()
        slot = plot.width() / 24
        gap = min(4.0, slot * 0.18)
        x = plot.left() + h * slot + gap / 2
        height = max(3.0, value / top * plot.height()) if top else 3.0
        return QRectF(x, plot.bottom() - height, slot - gap, height)

    def _top(self) -> int:
        peak = max(self._values) if self._values else 0
        if peak <= 0:
            return 1
        # A round axis maximum: the peak itself up to 5, then the next
        # multiple of 5 (or of 10 past 50) so the half-way gridline lands
        # on a whole-ish number.
        if peak <= 5:
            return peak
        unit = 5 if peak <= 50 else 10
        return -(-peak // unit) * unit

    # ------------------------------------------------------------- paint --
    def paintEvent(self, event) -> None:  # noqa: N802 -- Qt's naming
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        plot = self._plot_rect()
        top = self._top()
        small = QFont(self.font())
        small.setPixelSize(11)
        p.setFont(small)

        # Gridlines + y-axis labels at 0, half and top.
        for frac, label in ((0.0, "0"), (0.5, str(top / 2).rstrip("0").rstrip(".")), (1.0, str(top))):
            y = plot.bottom() - frac * plot.height()
            p.setPen(self._grid)
            p.drawLine(int(plot.left()), int(y), int(plot.right()), int(y))
            p.setPen(self._text)
            p.drawText(QRectF(0, y - 8, self.AXIS_W, 16), Qt.AlignRight | Qt.AlignVCenter, label)

        p.setPen(Qt.NoPen)
        tint = QColor(self._accent)
        tint.setAlpha(46)
        for h in range(24):
            in_window = h in self._window
            if in_window:
                col = self._bar_rect(h, top, top)
                p.setBrush(tint)
                p.drawRoundedRect(col, 3, 3)
            p.setBrush(self._accent if in_window else self._bar)
            p.drawRoundedRect(self._bar_rect(h, self._values[h], top), 3, 3)

        p.setPen(self._text)
        label_y = plot.bottom() + 6
        for h, align in ((0, Qt.AlignLeft), (6, Qt.AlignHCenter), (12, Qt.AlignHCenter),
                         (18, Qt.AlignHCenter), (23, Qt.AlignRight)):
            bar = self._bar_rect(h, 0, top)
            box = QRectF(bar.center().x() - 30, label_y, 60, self.LABEL_H)
            if align == Qt.AlignLeft:
                box.moveLeft(bar.left())
            elif align == Qt.AlignRight:
                box.moveRight(bar.right())
            p.drawText(box, align | Qt.AlignTop, hour_name(h))

        if not self.has_data():
            p.setPen(self._text)
            p.drawText(plot, Qt.AlignCenter, "Not enough session history yet")
        p.end()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        plot = self._plot_rect()
        x = event.position().x()
        if plot.left() <= x <= plot.right():
            h = min(23, int((x - plot.left()) / (plot.width() / 24)))
            n = self._values[h]
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"{hour_name(h)}–{hour_name((h + 1) % 24)}: {n} session{'s' if n != 1 else ''} started",
                self,
            )
        super().mouseMoveEvent(event)
