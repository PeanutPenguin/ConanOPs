from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QPushButton, QMessageBox, QInputDialog,
)

from ui import table_cells
from models import ServerConfig
from session_tracker import SessionTracker


def _fmt_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h {m:02d}m"


class PlayersPage(QWidget):
    """Player session history plus Kick/Ban actions. Ban asks for a SteamID64
    because Conan's log only gives display names."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tracker: Optional[SessionTracker] = None
        self.server: Optional[ServerConfig] = None
        self._online_names: set = set()
        self.on_kick: Optional[Callable[[ServerConfig, str], None]] = None
        self.on_ban: Optional[Callable[[ServerConfig, str, str], None]] = None  # (server, player_name, steam_id)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        top = QHBoxLayout()
        self.title_label = QLabel("Players")
        self.title_label.setObjectName("PageTitle")
        top.addWidget(self.title_label)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search players…")
        self.search_edit.setFixedWidth(360)
        self.search_edit.textChanged.connect(self._refresh_table)
        top.addWidget(self.search_edit)
        top.addStretch(1)
        root.addLayout(top)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Player", "Status", "Total Playtime", "Sessions", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        table_cells.fit_button_column(self.table, 4, min_width=170)
        root.addWidget(self.table, 1)

    def set_tracker(self, tracker: SessionTracker) -> None:
        self.tracker = tracker
        self._refresh_table()

    def set_server(self, server: Optional[ServerConfig]) -> None:
        self.server = server
        self._refresh_table()

    def set_online_players(self, names: set) -> None:
        """Set by main_window on join/leave; SessionTracker doesn't track who's online now."""
        self._online_names = set(names)
        self._refresh_table()

    def _refresh_table(self) -> None:
        self.table.setRowCount(0)
        if not self.tracker:
            return
        query = self.search_edit.text().strip().lower()
        names = [n for n in self.tracker.all_player_names() if query in n.lower()]
        for name in names:
            row = table_cells.add_row(self.table)
            is_online = name in self._online_names
            self.table.setItem(row, 0, QTableWidgetItem(name))
            status = QTableWidgetItem("● Online" if is_online else "Offline")
            status.setForeground(QColor("#5fd38a" if is_online else "#8f8f8f"))
            self.table.setItem(row, 1, status)
            self.table.setItem(row, 2, QTableWidgetItem(_fmt_duration(self.tracker.total_playtime_seconds(name))))
            self.table.setItem(row, 3, QTableWidgetItem(str(self.tracker.session_count(name))))

            kick_btn = QPushButton("Kick")
            kick_btn.setEnabled(is_online)
            kick_btn.clicked.connect(lambda _=False, n=name: self._handle_kick(n))

            ban_btn = QPushButton("Ban…")
            ban_btn.setObjectName("DangerButton")
            ban_btn.clicked.connect(lambda _=False, n=name: self._handle_ban(n))

            actions = table_cells.button_cell(kick_btn, ban_btn)
            self.table.setCellWidget(row, 4, actions)
        table_cells.size_button_column(self.table, 4, min_width=170)

    def _handle_kick(self, name: str) -> None:
        if not self.server or not self.on_kick:
            return
        if not self.server.rcon_enabled:
            QMessageBox.information(
                self, "RCON Not Enabled",
                "Kicking a player live needs RCON. Enable it in Server Settings → RCON first "
                "(and give the server a moment to restart with it on).",
            )
            return
        reply = QMessageBox.question(
            self, "Kick Player", f"Kick \"{name}\" from the server now?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        self.on_kick(self.server, name)

    def _handle_ban(self, name: str) -> None:
        if not self.server or not self.on_ban:
            return
        steam_id, ok = QInputDialog.getText(
            self, "Ban Player",
            f"Ban \"{name}\" -- enter their SteamID64 (Conan's own server log doesn't include this, "
            f"only their display name, so ConanOps can't fill it in automatically):",
        )
        if not ok or not steam_id.strip():
            return
        self.on_ban(self.server, name, steam_id.strip())
