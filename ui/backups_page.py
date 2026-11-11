from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QFileDialog,
)

import backup_manager
from ui import table_cells
from models import ServerConfig


class BackupsPage(QWidget):
    def __init__(self, parent=None, embedded: bool = False):
        super().__init__(parent)
        self.server: Optional[ServerConfig] = None
        self.on_restore_confirmed = None  # set by main_window: callable(server, backup_entry)
        self.on_back_up_now = None  # set by main_window: callable(server) -> "" when started, else why not

        root = QVBoxLayout(self)
        # embedded: shown inside another page, so skip our own title and margins.
        root.setContentsMargins(0, 0, 0, 0) if embedded else root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        top = QHBoxLayout()
        if not embedded:
            self.title_label = QLabel("Backups")
            self.title_label.setObjectName("PageTitle")
            top.addWidget(self.title_label)
        top.addStretch(1)
        self.backup_now_btn = QPushButton("Back Up Now")
        self.backup_now_btn.setObjectName("PrimaryButton")
        self.backup_now_btn.clicked.connect(self._back_up_now)
        top.addWidget(self.backup_now_btn)
        self.import_btn = QPushButton("Import External Backup…")
        self.import_btn.clicked.connect(self._import_external)
        top.addWidget(self.import_btn)
        root.addLayout(top)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["When", "Trigger", "Size", ""])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        table_cells.fit_button_column(self.table, 3)
        for col in (1, 2):  # keep "before update" on one line
            self.table.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.setWordWrap(False)
        self.table.setMinimumHeight(220)
        root.addWidget(self.table, 1)

        # Backups are added/pruned from elsewhere too, so re-read the folder when it changes.
        self._signature = None
        self._watch = QTimer(self)
        self._watch.setInterval(4000)
        self._watch.timeout.connect(self._refresh_if_changed)

    def set_server(self, server: ServerConfig) -> None:
        self.server = server
        self.refresh()

    def _folder_signature(self):
        dest = self.server.backup_destination if self.server else ""
        if not dest or not os.path.isdir(dest):
            return (dest,)
        try:
            with os.scandir(dest) as it:
                return (dest,) + tuple(sorted((e.name, e.stat().st_size, e.stat().st_mtime_ns)
                                              for e in it if e.name.lower().endswith(".zip")))
        except OSError:
            return (dest,)

    def _refresh_if_changed(self) -> None:
        if self._folder_signature() != self._signature:
            self.refresh()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_if_changed()
        self._watch.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._watch.stop()

    def refresh(self) -> None:
        self._signature = self._folder_signature()
        self.table.setRowCount(0)
        if not self.server or not self.server.backup_destination:
            return
        entries = backup_manager.list_backups(self.server.backup_destination)
        for entry in entries:
            row = table_cells.add_row(self.table)
            self.table.setItem(row, 0, QTableWidgetItem(entry.when.strftime("%Y-%m-%d %H:%M")))
            self.table.setItem(row, 1, QTableWidgetItem(entry.trigger))
            self.table.setItem(row, 2, QTableWidgetItem(entry.size_label))
            restore_btn = QPushButton("Restore")
            restore_btn.clicked.connect(lambda _=False, e=entry: self._confirm_restore(e))
            self.table.setCellWidget(row, 3, table_cells.button_cell(restore_btn))
        table_cells.size_button_column(self.table, 3)

    def _back_up_now(self) -> None:
        if not self.server:
            return
        if not self.server.backup_destination:
            QMessageBox.warning(
                self, "No destination set",
                "Set a backup destination folder for this server first, on the Settings -> Backups page.",
            )
            return
        if self.on_back_up_now is None:
            return
        why = self.on_back_up_now(self.server)
        if why:
            QMessageBox.warning(self, "Backup not started", why)

    def set_busy(self, busy: bool) -> None:
        """While a backup is being made: the button says so and can't start another."""
        self.backup_now_btn.setEnabled(not busy)
        self.backup_now_btn.setText("Backing Up…" if busy else "Back Up Now")

    def _confirm_restore(self, entry) -> None:
        if not self.server:
            return
        reply = QMessageBox.warning(
            self,
            "Restore this backup?",
            f"This stops {self.server.name}, replaces the current world with the "
            f"backup from {entry.when.strftime('%Y-%m-%d %H:%M')}, and restarts. "
            "Anything built since then will be lost. A safety copy of the current "
            "world is taken first.",
            QMessageBox.Cancel | QMessageBox.Yes,
            QMessageBox.Cancel,
        )
        if reply == QMessageBox.Yes and self.on_restore_confirmed:
            self.on_restore_confirmed(self.server, entry)
            self.refresh()

    def _import_external(self) -> None:
        if not self.server:
            return
        if not self.server.backup_destination:
            QMessageBox.information(
                self, "No destination set",
                "Set a backup destination folder for this server first, on the Settings -> Backups page.",
            )
            return

        path, _filter = QFileDialog.getOpenFileName(
            self, "Import a backup from another server", "", "Backup zip files (*.zip)"
        )
        if not path:
            return

        proceed = QMessageBox.warning(
            self,
            "Import external backup?",
            "This copies the selected zip into this server's own backup list, so you can "
            "restore from it like any other backup here.\n\n"
            "It only brings over the world save (and, if present, server config files) from "
            "that zip -- it does NOT copy over the other server's ConanOps settings (rates, "
            "mods, schedules, etc.). If you want this server to match that one, you'll still "
            "need to set that up separately on the Settings pages.\n\n"
            "Continue?",
            QMessageBox.Cancel | QMessageBox.Yes,
            QMessageBox.Cancel,
        )
        if proceed != QMessageBox.Yes:
            return

        try:
            backup_manager.import_external_backup(path, self.server.backup_destination)
        except backup_manager.ImportValidationError as e:
            QMessageBox.warning(self, "Can't import this file", str(e))
            return
        except OSError as e:
            QMessageBox.critical(self, "Import failed", f"Couldn't copy the file: {e}")
            return

        self.refresh()
        QMessageBox.information(
            self, "Imported",
            "The backup was imported and added to the list below. Use Restore on it when you're ready.",
        )
