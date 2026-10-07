from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QListWidget, QCheckBox, QMessageBox, QFrame,
)

import applog
import banlist_manager
from models import ServerConfig
from ui.workers import keep_until_finished

_log = applog.get_logger(__name__)


class _BanlistActionWorker(QThread):
    """Runs a ban/unban off the GUI thread (it may do a blocking RCON call)."""
    finished_action = Signal(str, str)  # status message, action label ("Ban"/"Unban")

    def __init__(self, fn, server, steam_id, action_label, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._server = server
        self._steam_id = steam_id
        self._action_label = action_label

    def run(self) -> None:
        # Always emit, or the Ban/Unban controls stay disabled.
        try:
            status = self._fn(self._server, self._steam_id)
        except Exception as e:  # noqa: BLE001 - always emit so the UI re-enables
            _log.error(f"{self._action_label} failed unexpectedly for {self._steam_id}: {e}")
            status = f"{self._action_label} failed unexpectedly: {e}"
        self.finished_action.emit(status, self._action_label)


class AccessPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.server: Optional[ServerConfig] = None
        self.on_changed = None  # callable(server) -- persist config
        self._action_worker: Optional[_BanlistActionWorker] = None
        # Finished workers stay referenced until QThread.finished fires;
        # destroying a QThread whose thread is still running crashes.
        self._retiring_workers: list = []

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        self.title_label = title = QLabel("Access Control")
        title.setObjectName("PageTitle")
        root.addWidget(title)

        mode_card = QFrame()
        mode_card.setObjectName("Card")
        mode = QHBoxLayout(mode_card)
        mode.setContentsMargins(18, 14, 18, 14)
        mode_text = QVBoxLayout()
        mode_text.setSpacing(2)
        mode_title = QLabel("Whitelist-only mode")
        mode_title.setStyleSheet("font-weight: 600; font-size: 14px;")
        mode_text.addWidget(mode_title)
        mode_note = QLabel("Only players on the whitelist can join.")
        mode_note.setObjectName("Muted")
        mode_text.addWidget(mode_note)
        mode.addLayout(mode_text, 1)
        self.whitelist_only_check = QCheckBox()
        self.whitelist_only_check.setAccessibleName("Whitelist-only mode (only listed players can join)")
        self.whitelist_only_check.toggled.connect(self._toggle_whitelist_mode)
        mode.addWidget(self.whitelist_only_check)
        root.addWidget(mode_card)

        columns = QHBoxLayout()
        columns.setSpacing(18)

        # --- Whitelist column ---
        wl_col = QVBoxLayout()
        wl_col.setSpacing(10)
        wl_title = QLabel("Whitelist")
        wl_title.setObjectName("SectionTitle")
        wl_col.addWidget(wl_title)
        self.whitelist_list = QListWidget()
        wl_row = QHBoxLayout()
        self.whitelist_edit = QLineEdit()
        self.whitelist_edit.setPlaceholderText("SteamID64")
        wl_add = QPushButton("Add")
        wl_add.clicked.connect(self._add_whitelist)
        wl_remove = QPushButton("Remove Selected")
        wl_remove.clicked.connect(self._remove_whitelist)
        wl_row.addWidget(self.whitelist_edit)
        wl_row.addWidget(wl_add)
        wl_col.addLayout(wl_row)
        wl_col.addWidget(self.whitelist_list, 1)
        wl_col.addWidget(wl_remove, 0, Qt.AlignLeft)
        columns.addLayout(wl_col)

        # --- Ban list column ---
        ban_col = QVBoxLayout()
        ban_col.setSpacing(10)
        ban_title = QLabel("Ban List")
        ban_title.setObjectName("SectionTitle")
        ban_col.addWidget(ban_title)
        self.ban_list = QListWidget()
        ban_row = QHBoxLayout()
        self.ban_edit = QLineEdit()
        self.ban_edit.setPlaceholderText("SteamID64")
        ban_add = QPushButton("Ban")
        ban_add.setObjectName("DangerButton")
        ban_add.clicked.connect(self._add_ban)
        ban_remove = QPushButton("Unban Selected")
        ban_remove.clicked.connect(self._remove_ban)
        ban_row.addWidget(self.ban_edit)
        ban_row.addWidget(ban_add)
        ban_col.addLayout(ban_row)
        ban_col.addWidget(self.ban_list, 1)
        ban_col.addWidget(ban_remove, 0, Qt.AlignLeft)
        columns.addLayout(ban_col)

        root.addLayout(columns, 1)

        note = QLabel(
            "Bans and unbans apply immediately if RCON is enabled (RCON & Alerts settings); "
            "otherwise they take effect on the next restart."
        )
        note.setObjectName("Dim")
        note.setWordWrap(True)
        root.addWidget(note)

    def set_server(self, server: ServerConfig) -> None:
        self.server = server
        self.whitelist_only_check.setChecked(server.whitelist_enabled)
        self._refresh()

    def _refresh(self) -> None:
        self.whitelist_list.clear()
        self.ban_list.clear()
        if not self.server:
            return
        self.whitelist_list.addItems(self.server.whitelist_ids)
        self.ban_list.addItems(self.server.banned_ids)

    def _persist(self) -> None:
        if self.on_changed and self.server:
            self.on_changed(self.server)

    def _toggle_whitelist_mode(self, on: bool) -> None:
        if self.server:
            self.server.whitelist_enabled = on
            banlist_manager.set_whitelist_enabled(self.server)
            self._persist()

    def _add_whitelist(self) -> None:
        sid = self.whitelist_edit.text().strip()
        if not sid or not self.server:
            return
        banlist_manager.add_whitelist(self.server, sid)
        self.whitelist_edit.clear()
        self._persist()
        self._refresh()

    def _remove_whitelist(self) -> None:
        item = self.whitelist_list.currentItem()
        if not item or not self.server:
            return
        banlist_manager.remove_whitelist(self.server, item.text())
        self._persist()
        self._refresh()

    def _add_ban(self) -> None:
        sid = self.ban_edit.text().strip()
        if not sid or not self.server or self._action_worker:
            return
        self.ban_edit.clear()
        self._run_banlist_action(banlist_manager.ban_player, sid, "Ban")

    def _remove_ban(self) -> None:
        item = self.ban_list.currentItem()
        if not item or not self.server or self._action_worker:
            return
        self._run_banlist_action(banlist_manager.unban_player, item.text(), "Unban")

    def _run_banlist_action(self, fn, steam_id: str, label: str) -> None:
        self.ban_edit.setEnabled(False)
        self.ban_list.setEnabled(False)
        self._action_worker = _BanlistActionWorker(fn, self.server, steam_id, label)
        self._action_worker.finished_action.connect(self._on_banlist_action_finished)
        self._action_worker.start()

    def _on_banlist_action_finished(self, status: str, label: str) -> None:
        self._retire_worker(self._action_worker)
        self._action_worker = None
        self.ban_edit.setEnabled(True)
        self.ban_list.setEnabled(True)
        self._persist()
        self._refresh()
        QMessageBox.information(self, label, status)

    def _retire_worker(self, worker: Optional[_BanlistActionWorker]) -> None:
        keep_until_finished(self._retiring_workers, worker)
