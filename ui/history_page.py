"""
The History page: a server's players, CPU and memory over the last day,
week, month or three months, with totals for the period and a timeline of
what happened (starts, crashes, restarts, updates, backups, events).
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import List, Optional

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QHeaderView,
)

import history
from theme_config import load_theme
from ui.line_chart import LineChart

RANGES = [("24h", "Last 24 hours", 86400), ("7d", "7 days", 7 * 86400), ("30d", "30 days", 30 * 86400),
          ("90d", "90 days", 90 * 86400)]
CHART_POINTS = 240


def chart_points(rows: List[history.Sample], since: float, until: float, attr: str, scale: float = 1.0):
    """(time, value) pairs with a gap (None) wherever readings stop: the
    server was down, or ConanOps wasn't running."""
    step = max(history.SAMPLE_EVERY_SECONDS * 2.5, (until - since) / CHART_POINTS * 2.5)
    out = []
    prev = None
    for r in rows:
        if prev is not None and r.ts - prev > step:
            out.append((prev + 1, None))
        v = getattr(r, attr)
        out.append((r.ts, (v / scale) if (r.up and v is not None) else None))
        prev = r.ts
    return out


def _tile(title: str):
    box = QFrame()
    box.setObjectName("Card")
    lay = QVBoxLayout(box)
    lay.setContentsMargins(14, 10, 14, 10)
    lay.setSpacing(2)
    t = QLabel(title)
    t.setObjectName("Dim")
    v = QLabel("—")
    v.setObjectName("SectionTitle")
    lay.addWidget(t)
    lay.addWidget(v)
    return box, v


class HistoryPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = None
        self._range = 86400
        self._dirty = False
        palette = load_theme()
        self._red = palette.red

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)
        body = QWidget()
        scroll.setWidget(body)
        root = QVBoxLayout(body)
        root.setContentsMargins(28, 20, 28, 24)
        root.setSpacing(14)

        bar = QHBoxLayout()
        self.range_group = QButtonGroup(self)
        self.range_buttons = {}
        for key, label, seconds in RANGES:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(seconds == self._range)
            b.clicked.connect(lambda _c=False, s=seconds: self.set_range(s))
            self.range_group.addButton(b)
            self.range_buttons[key] = b
            bar.addWidget(b)
        bar.addStretch(1)
        self.caption = QLabel("")
        self.caption.setObjectName("Dim")
        bar.addWidget(self.caption)
        root.addLayout(bar)

        tiles = QGridLayout()
        tiles.setSpacing(10)
        self.tiles = {}
        for i, (key, title) in enumerate((("uptime", "Uptime"), ("peak", "Most players at once"),
                                          ("avg", "Average players"), ("cpu", "Average CPU"),
                                          ("mem", "Most memory"), ("problems", "Crashes and freezes"))):
            box, value = _tile(title)
            self.tiles[key] = value
            tiles.addWidget(box, i // 3, i % 3)
        root.addLayout(tiles)

        self.players_chart = LineChart("Players", palette.accent, palette.dim, palette.border,
                                       fmt=lambda v: f"{v:.0f}")
        self.cpu_chart = LineChart("CPU", palette.yellow, palette.dim, palette.border, fmt=lambda v: f"{v:.0f}%")
        self.mem_chart = LineChart("Memory", palette.green, palette.dim, palette.border,
                                   fmt=lambda v: f"{v:.1f} GB")
        for title, chart in (("Players online", self.players_chart), ("CPU (share of the whole PC)", self.cpu_chart),
                             ("Memory used by the server", self.mem_chart)):
            card = QFrame()
            card.setObjectName("Card")
            lay = QVBoxLayout(card)
            lay.setContentsMargins(16, 12, 16, 12)
            head = QLabel(title)
            head.setObjectName("SectionTitle")
            lay.addWidget(head)
            lay.addWidget(chart)
            root.addWidget(card)

        events_card = QFrame()
        events_card.setObjectName("Card")
        ev = QVBoxLayout(events_card)
        ev.setContentsMargins(16, 12, 16, 12)
        head = QHBoxLayout()
        t = QLabel("Timeline")
        t.setObjectName("SectionTitle")
        head.addWidget(t)
        head.addStretch(1)
        self.problems_only = QCheckBox("Problems only")
        self.problems_only.toggled.connect(lambda _on: self.refresh())
        head.addWidget(self.problems_only)
        ev.addLayout(head)
        self.events_table = QTableWidget(0, 3)
        self.events_table.setHorizontalHeaderLabels(["When", "What", "Details"])
        self.events_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.events_table.verticalHeader().hide()
        self.events_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.events_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.events_table.horizontalHeader().setStretchLastSection(True)
        self.events_table.setMinimumHeight(260)
        ev.addWidget(self.events_table)
        self.no_events = QLabel("Nothing has happened in this period yet.")
        self.no_events.setObjectName("Dim")
        ev.addWidget(self.no_events)
        root.addWidget(events_card)
        root.addStretch(1)

        # history_changed() is called on every sample and event; redraw at most every few seconds.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(3000)
        self._refresh_timer.timeout.connect(self.refresh)

    def set_server(self, server) -> None:
        self.server = server
        self.refresh()

    def set_range(self, seconds: int) -> None:
        self._range = seconds
        for key, _label, s in RANGES:
            self.range_buttons[key].setChecked(s == seconds)
        self.refresh()

    def history_changed(self) -> None:
        if self.isVisible():
            if not self._refresh_timer.isActive():
                self._refresh_timer.start()
        else:
            self._dirty = True

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._dirty:
            self._dirty = False
            self.refresh()

    def refresh(self, now: Optional[float] = None) -> None:
        if self.server is None:
            return
        until = time.time() if now is None else now
        since = until - self._range
        sid = self.server.id
        rows = history.samples(sid, since, until, max_points=CHART_POINTS)
        self.players_chart.set_points(chart_points(rows, since, until, "players"), since, until)
        self.cpu_chart.set_points(chart_points(rows, since, until, "cpu"), since, until)
        self.mem_chart.set_points(chart_points(rows, since, until, "mem_mb", 1024.0), since, until)
        s = history.summary(sid, since, until)
        up = s.uptime_percent
        self.tiles["uptime"].setText("—" if up is None else f"{up:.1f}%")
        self.tiles["peak"].setText(str(s.peak_players) if rows else "—")
        self.tiles["avg"].setText("—" if s.avg_players is None else f"{s.avg_players:.1f}")
        self.tiles["cpu"].setText("—" if s.avg_cpu is None else f"{s.avg_cpu:.0f}%")
        self.tiles["mem"].setText("—" if s.peak_mem_mb is None else f"{s.peak_mem_mb / 1024:.1f} GB")
        self.tiles["problems"].setText(str(s.count("crash") + s.count("hang")))
        watched = s.sampled_seconds / max(1.0, until - since) * 100
        self.caption.setText(f"ConanOps was watching {watched:.0f}% of this time" if rows else
                             "ConanOps records a reading every 5 minutes while it's open")
        kinds = sorted(history.PROBLEM_KINDS) if self.problems_only.isChecked() else None
        events = history.events(sid, since, limit=500, kinds=kinds)
        self.events_table.setRowCount(len(events))
        for i, e in enumerate(events):
            when = QTableWidgetItem(datetime.fromtimestamp(e.ts).strftime("%a %d %b %H:%M"))
            what = QTableWidgetItem(e.label)
            if e.problem:
                what.setForeground(QColor(self._red))
            self.events_table.setItem(i, 0, when)
            self.events_table.setItem(i, 1, what)
            self.events_table.setItem(i, 2, QTableWidgetItem(e.detail))
        self.events_table.setVisible(bool(events))
        self.no_events.setVisible(not events)
