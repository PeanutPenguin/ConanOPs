"""
A small time chart for the History and World pages: one line over time,
with gaps where there's no reading (ConanOps closed, server stopped), a
light fill underneath, and the value under the mouse in a tooltip.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, List, Optional, Sequence, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

Point = Tuple[float, Optional[float]]  # (unix time, value or None for a gap)


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    for step in (1, 2, 2.5, 5):
        for mag in range(-2, 12):
            n = step * 10 ** mag
            if n >= v:
                return n
    return v


def time_label(ts: float, span: float) -> str:
    d = datetime.fromtimestamp(ts)
    if span <= 36 * 3600:
        return d.strftime("%H:%M")
    if span <= 10 * 86400:
        return d.strftime("%a %H:%M")
    return d.strftime("%d %b")


class LineChart(QWidget):
    PAD_L, PAD_R, PAD_T, PAD_B = 44, 10, 10, 22

    def __init__(self, title: str, color: str = "#c9752f", text: str = "#8f8f8f", grid: str = "#2c2c2c",
                 fmt: Callable[[float], str] = lambda v: f"{v:g}", height: int = 150, parent=None):
        super().__init__(parent)
        self.title = title
        self._fmt = fmt
        self._points: List[Point] = []
        self._since = self._until = 0.0
        self._fixed_max: Optional[float] = None
        self.set_colors(color, text, grid)
        self.setMinimumHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self.setAccessibleName(title)

    def set_colors(self, color: str, text: str, grid: str) -> None:
        self._color, self._text, self._grid = QColor(color), QColor(text), QColor(grid)
        self.update()

    def set_points(self, points: Sequence[Point], since: float, until: float,
                   fixed_max: Optional[float] = None) -> None:
        self._points = sorted(points, key=lambda p: p[0])
        self._since, self._until = since, max(until, since + 1)
        self._fixed_max = fixed_max
        vals = [v for _t, v in self._points if v is not None]
        if vals:
            self.setAccessibleDescription(f"{self.title}: from {self._fmt(min(vals))} to {self._fmt(max(vals))}")
        else:
            self.setAccessibleDescription(f"{self.title}: no readings in this period")
        self.update()

    def has_data(self) -> bool:
        return any(v is not None for _t, v in self._points)

    def _plot(self) -> QRectF:
        return QRectF(self.PAD_L, self.PAD_T, max(10, self.width() - self.PAD_L - self.PAD_R),
                      max(10, self.height() - self.PAD_T - self.PAD_B))

    def _top(self) -> float:
        if self._fixed_max:
            return self._fixed_max
        vals = [v for _t, v in self._points if v is not None]
        return _nice_max(max(vals) * 1.1 if vals else 1.0)

    def _xy(self, rect: QRectF, ts: float, v: float, top: float) -> QPointF:
        x = rect.left() + (ts - self._since) / (self._until - self._since) * rect.width()
        y = rect.bottom() - (v / top) * rect.height() if top else rect.bottom()
        return QPointF(x, max(rect.top(), min(rect.bottom(), y)))

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt's naming
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self._plot()
        top = self._top()
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() * 0.85))
        p.setFont(font)
        # Grid and value labels.
        for i in range(4):
            y = rect.bottom() - rect.height() * i / 3
            p.setPen(QPen(self._grid, 1))
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            p.setPen(self._text)
            p.drawText(QRectF(0, y - 8, self.PAD_L - 6, 16), Qt.AlignRight | Qt.AlignVCenter,
                       self._fmt(top * i / 3))
        # Time labels.
        span = self._until - self._since
        for i, align in ((0, Qt.AlignLeft), (2, Qt.AlignHCenter), (4, Qt.AlignRight)):
            ts = self._since + span * i / 4
            x = rect.left() + rect.width() * i / 4
            box = QRectF(x - 60 if align != Qt.AlignLeft else x, rect.bottom() + 4, 120 if align == Qt.AlignHCenter
                         else 60, 16)
            if align == Qt.AlignRight:
                box = QRectF(x - 120, rect.bottom() + 4, 120, 16)
            p.drawText(box, align | Qt.AlignTop, time_label(ts, span))
        if not self.has_data():
            p.setPen(self._text)
            p.drawText(rect, Qt.AlignCenter, "No readings in this period yet")
            return
        # Lines, broken at gaps.
        runs: List[List[QPointF]] = [[]]
        for ts, v in self._points:
            if v is None:
                if runs[-1]:
                    runs.append([])
                continue
            runs[-1].append(self._xy(rect, ts, v, top))
        fill = QColor(self._color)
        fill.setAlpha(40)
        for run in runs:
            if not run:
                continue
            if len(run) == 1:
                p.setPen(Qt.NoPen)
                p.setBrush(self._color)
                p.drawEllipse(run[0], 2.5, 2.5)
                continue
            path = QPainterPath(run[0])
            for pt in run[1:]:
                path.lineTo(pt)
            area = QPainterPath(path)
            area.lineTo(QPointF(run[-1].x(), rect.bottom()))
            area.lineTo(QPointF(run[0].x(), rect.bottom()))
            area.closeSubpath()
            p.fillPath(area, fill)
            p.setPen(QPen(self._color, 1.8))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        rect = self._plot()
        pts = [(t, v) for t, v in self._points if v is not None]
        if not pts or not rect.contains(event.position()):
            QToolTip.hideText()
            return
        ts = self._since + (event.position().x() - rect.left()) / rect.width() * (self._until - self._since)
        t, v = min(pts, key=lambda p: abs(p[0] - ts))
        QToolTip.showText(event.globalPosition().toPoint(),
                          f"{datetime.fromtimestamp(t):%a %d %b %H:%M}\n{self.title}: {self._fmt(v)}", self)
