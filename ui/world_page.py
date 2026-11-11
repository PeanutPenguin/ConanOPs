"""
The World page: clans, how much each has built, players who haven't been on
for a month, and how the world file has grown, all read from the newest
backup (world_report.py) so the live world is never touched.
"""
from __future__ import annotations

from datetime import datetime
from typing import Dict, Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton, QScrollArea, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

import applog
import world_report
from theme_config import load_theme
from ui.line_chart import LineChart

_log = applog.get_logger(__name__)


class WorldReportWorker(QThread):
    finished_report = Signal(object)  # world_report.WorldReport

    def __init__(self, destination: str, parent=None):
        super().__init__(parent)
        self.destination = destination

    def run(self) -> None:
        try:
            rep = world_report.build_report(self.destination)
        except Exception as e:  # noqa: BLE001
            _log.error(f"World report failed: {e}")
            rep = world_report.WorldReport(error=f"The report couldn't be made: {e}")
        self.finished_report.emit(rep)


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "unknown"
    days = int((datetime.now().timestamp() - ts) // 86400)
    when = datetime.fromtimestamp(ts).strftime("%d %b %Y")
    return f"{when} ({days} days ago)" if days >= 1 else f"{when} (today)"


def _num(n: Optional[int]) -> str:
    return "—" if n is None else f"{n:,}"


def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.verticalHeader().hide()
    t.setSortingEnabled(True)
    t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
    for i in range(1, len(headers)):
        t.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeToContents)
    t.setMinimumHeight(320)
    return t


class _NumItem(QTableWidgetItem):
    """Sorts by number, not text."""
    def __init__(self, value, text=None):
        super().__init__(text if text is not None else _num(value))
        self._v = value if value is not None else -1

    def __lt__(self, other):  # noqa: D401
        return self._v < getattr(other, "_v", 0)


class WorldPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.server = None
        self._worker: Optional[WorldReportWorker] = None
        self._reports: Dict[str, world_report.WorldReport] = {}
        palette = load_theme()

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

        intro = QLabel("Read from the newest backup, never the live world, so it's safe while people play. "
                       "Funcom doesn't publish how the world is stored, so anything ConanOps can't find in it "
                       "is left out.")
        intro.setObjectName("Dim")
        intro.setWordWrap(True)
        root.addWidget(intro)
        bar = QHBoxLayout()
        self.make_btn = QPushButton("Make Report")
        self.make_btn.setObjectName("PrimaryButton")
        self.make_btn.clicked.connect(lambda: self.make_report())
        bar.addWidget(self.make_btn)
        self.status = QLabel("")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        bar.addWidget(self.status, 1)
        root.addLayout(bar)

        self.content = QWidget()
        content = QVBoxLayout(self.content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(14)
        root.addWidget(self.content)

        tiles = QGridLayout()
        tiles.setSpacing(10)
        self.tiles = {}
        for i, (key, title) in enumerate((("characters", "Characters"), ("clans", "Clans"),
                                          ("pieces", "Building pieces"), ("inactive", "Inactive players"),
                                          ("idle_pieces", "Pieces of inactive players"), ("size", "World size"))):
            box = QFrame()
            box.setObjectName("Card")
            lay = QVBoxLayout(box)
            lay.setContentsMargins(14, 10, 14, 10)
            t = QLabel(title)
            t.setObjectName("Dim")
            v = QLabel("—")
            v.setObjectName("SectionTitle")
            lay.addWidget(t)
            lay.addWidget(v)
            self.tiles[key] = v
            tiles.addWidget(box, i // 3, i % 3)
        content.addLayout(tiles)

        self.clans_table = _table(["Clan", "Members", "Building pieces", "Last member online"])
        self.inactive_table = _table(["Player", "Clan", "Level", "Last online", "Own pieces"])
        self.growth_chart = LineChart("World size", palette.accent, palette.dim, palette.border,
                                      fmt=lambda v: f"{v:.0f} MB")
        for title, widget in (("Clans, by how much they've built", self.clans_table),
                              (f"Not on for {world_report.INACTIVE_DAYS}+ days", self.inactive_table),
                              ("World file size in each backup", self.growth_chart)):
            card = QFrame()
            card.setObjectName("Card")
            lay = QVBoxLayout(card)
            lay.setContentsMargins(16, 12, 16, 12)
            head = QLabel(title)
            head.setObjectName("SectionTitle")
            lay.addWidget(head)
            lay.addWidget(widget)
            content.addWidget(card)
        self.notes = QLabel("")
        self.notes.setObjectName("Dim")
        self.notes.setWordWrap(True)
        content.addWidget(self.notes)
        root.addStretch(1)
        self.content.hide()

    def set_server(self, server) -> None:
        self.server = server
        rep = self._reports.get(server.id) if server else None
        if rep is not None:
            self.show_report(rep)
        else:
            self.content.hide()
            self.status.setText("Make a report to see this server's clans, buildings and inactive players.")
        self.make_btn.setEnabled(self._worker is None)

    def make_report(self, server=None) -> str:
        """Starts a report for `server` (default: the one shown). Returns ""
        or why it can't start."""
        server = server or self.server
        if server is None:
            return "No server."
        if self._worker is not None:
            return "A report is already being made. Try again in a minute."
        if not server.backup_destination:
            return "This server has no backup folder yet."
        self._busy_id = server.id
        self.make_btn.setEnabled(False)
        if self.server is not None and self.server.id == server.id:
            self.status.setText("Unpacking the newest backup and reading the world… (a big world takes a minute)")
        worker = WorldReportWorker(server.backup_destination)
        worker.finished_report.connect(lambda rep, sid=server.id: self._done(sid, rep))
        self._worker = worker
        worker.start()
        return ""

    def is_busy(self, server_id: str) -> bool:
        return self._worker is not None and getattr(self, "_busy_id", "") == server_id

    def report_for(self, server_id: str):
        return self._reports.get(server_id)

    def _done(self, server_id: str, rep) -> None:
        if self._worker is not None:
            self._worker.wait(2000)
        self._worker = None
        self._reports[server_id] = rep
        self.make_btn.setEnabled(True)
        if self.server is not None and self.server.id == server_id:
            self.show_report(rep)

    def show_report(self, rep: world_report.WorldReport) -> None:
        if rep.error:
            self.status.setText(rep.error)
            self.content.hide()
            return
        when = rep.backup_when.strftime("%a %d %b %Y %H:%M") if rep.backup_when else "?"
        self.status.setText(f"From the backup made {when}.")
        self.tiles["characters"].setText(_num(rep.characters))
        self.tiles["clans"].setText(_num(rep.clan_count))
        self.tiles["pieces"].setText(_num(rep.pieces_total))
        self.tiles["inactive"].setText(_num(rep.inactive_count))
        self.tiles["idle_pieces"].setText(_num(rep.inactive_pieces))
        self.tiles["size"].setText(f"{rep.world_bytes / 1024 ** 2:,.0f} MB" if rep.world_bytes else "—")

        t = self.clans_table
        t.setSortingEnabled(False)
        t.setRowCount(len(rep.clans))
        for i, c in enumerate(rep.clans):
            t.setItem(i, 0, QTableWidgetItem(c.name))
            t.setItem(i, 1, _NumItem(c.members))
            t.setItem(i, 2, _NumItem(c.pieces))
            t.setItem(i, 3, _NumItem(c.last_active or 0, _ago(c.last_active) if c.last_active else "—"))
        t.setSortingEnabled(True)
        t.sortItems(2, Qt.DescendingOrder)

        t = self.inactive_table
        t.setSortingEnabled(False)
        t.setRowCount(len(rep.inactive))
        for i, p in enumerate(rep.inactive):
            t.setItem(i, 0, QTableWidgetItem(p.name))
            t.setItem(i, 1, QTableWidgetItem(p.clan or "—"))
            t.setItem(i, 2, _NumItem(p.level))
            t.setItem(i, 3, _NumItem(p.last_online or 0, _ago(p.last_online)))
            t.setItem(i, 4, _NumItem(p.pieces))
        t.setSortingEnabled(True)
        t.sortItems(3, Qt.AscendingOrder)

        pts = [(g.when.timestamp(), g.world_bytes / 1024 ** 2) for g in rep.growth]
        if pts:
            since, until = pts[0][0], pts[-1][0]
            if until - since < 3600:
                since -= 3600
            self.growth_chart.set_points(pts, since, until)
        else:
            self.growth_chart.set_points([], 0, 1)
        notes = list(rep.notes)
        if rep.inactive_count and rep.inactive_count > len(rep.inactive):
            notes.append(f"Showing the {len(rep.inactive)} who've been gone longest.")
        self.notes.setText("\n".join(notes))
        self.content.show()

    def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.wait(10_000)
