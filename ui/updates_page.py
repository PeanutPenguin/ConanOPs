from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QFrame, QTextEdit, QTableWidget, QTableWidgetItem, QHeaderView, QSpinBox,
    QMessageBox,
)

import changelog
from models import ServerConfig
from update_runner import CheckWorker, UpdateWorker
from ui.workers import keep_until_finished


class UpdatesPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.server: Optional[ServerConfig] = None
        self.on_before_update = None   # callable(server) -> run backup etc, called before update
        self.on_update_applied = None  # callable(server, new_buildid)
        self.on_auto_update_setting_changed = None  # callable(server) -> persist auto_update / interval
        self.on_update_now_guard = None      # callable(server) -> bool: True if an update is already in flight elsewhere
        self.on_stop_before_update = None    # callable(server) -> bool: stops the server if running, returns was_running
        self.on_relaunch_after_update = None  # callable(server, was_running) -> None
        # Keyed by server id so work continues across server switches.
        self._check_workers: Dict[str, CheckWorker] = {}
        self._update_workers: Dict[str, UpdateWorker] = {}
        self._state: Dict[str, dict] = {}
        # See ui/access_page.py's AccessPage._retiring_workers comment.
        self._retiring_workers: list = []

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(16)

        top = QHBoxLayout()
        self.title_label = QLabel("Updates")
        self.title_label.setObjectName("PageTitle")
        top.addWidget(self.title_label)
        top.addStretch(1)
        self.check_btn = QPushButton("Check Now")
        self.check_btn.clicked.connect(self.check_now)
        top.addWidget(self.check_btn)
        root.addLayout(top)

        tiles = QHBoxLayout()
        tiles.setSpacing(12)

        def tile(title_text: str) -> tuple:
            frame = QFrame()
            frame.setObjectName("Card")
            lay = QVBoxLayout(frame)
            lay.setContentsMargins(16, 14, 16, 14)
            lay.setSpacing(8)
            t = QLabel(title_text)
            t.setObjectName("StatLabel")
            lay.addWidget(t)
            tiles.addWidget(frame, 1)
            return frame, lay

        _f, build_lay = tile("Installed build")
        self.installed_label = QLabel("Installed: —")
        self.installed_label.setObjectName("StatValue")
        build_lay.addWidget(self.installed_label)
        self.build_state_label = QLabel("● Not checked yet")
        self.build_state_label.setObjectName("PillOff")
        build_lay.addWidget(self.build_state_label, 0, Qt.AlignLeft)
        build_lay.addStretch(1)

        _f, auto_lay = tile("Auto-update")
        self.auto_update_check = QCheckBox("Install new builds automatically")
        self.auto_update_check.toggled.connect(self._on_auto_update_toggled)
        auto_lay.addWidget(self.auto_update_check)
        every = QHBoxLayout()
        every_lbl = QLabel("Check every")
        every_lbl.setObjectName("Muted")
        every.addWidget(every_lbl)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(1, 48)
        self.interval_spin.setSuffix(" h")
        self.interval_spin.setFixedWidth(90)
        self.interval_spin.valueChanged.connect(self._on_interval_changed)
        every.addWidget(self.interval_spin)
        every.addStretch(1)
        auto_lay.addLayout(every)
        auto_lay.addStretch(1)
        root.addLayout(tiles)

        auto_note = QLabel(
            "When Auto-update is on, ConanOps checks SteamCMD on this schedule and, if a "
            "new build is found while nobody is online, backs up and updates immediately. "
            "It also always checks right after a scheduled restart."
        )
        auto_note.setObjectName("Dim")
        auto_note.setWordWrap(True)
        root.addWidget(auto_note)

        self.pending_frame = QFrame()
        self.pending_frame.setObjectName("Card")
        self.pending_frame.hide()
        p_layout = QVBoxLayout(self.pending_frame)
        p_top = QHBoxLayout()
        self.pending_label = QLabel("")
        p_top.addWidget(self.pending_label)
        p_top.addStretch(1)
        self.update_now_btn = QPushButton("Back Up && Update Now")
        self.update_now_btn.setObjectName("PrimaryButton")
        self.update_now_btn.clicked.connect(self._update_now)
        p_top.addWidget(self.update_now_btn)
        p_layout.addLayout(p_top)
        self.changelog_view = QTextEdit()
        self.changelog_view.setReadOnly(True)
        self.changelog_view.setFixedHeight(120)
        p_layout.addWidget(self.changelog_view)
        root.addWidget(self.pending_frame)

        history_title = QLabel("Update History")
        history_title.setObjectName("SectionTitle")
        root.addWidget(history_title)
        self.history_table = QTableWidget(0, 3)
        self.history_table.setHorizontalHeaderLabels(["When", "Change", "Result"])
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self.history_table, 1)

    def _retire_worker(self, worker: Optional[UpdateWorker]) -> None:
        keep_until_finished(self._retiring_workers, worker)

    def state_for(self, server: ServerConfig) -> dict:
        """What's known about a server's updates: "build_state" (text,
        pill), "pending" (headline or ""), "latest", "changelog",
        "history" (newest first: when, change, result), "checking",
        "updating"."""
        st = self._state.setdefault(server.id, {
            "build_state": ("● Not checked yet", "PillOff"), "pending": "", "latest": "", "changelog": "",
            "history": [],
        })
        st["checking"] = server.id in self._check_workers
        st["updating"] = server.id in self._update_workers
        return st

    def _render(self) -> None:
        server = self.server
        if server is None:
            return
        st = self.state_for(server)
        self.installed_label.setText(server.installed_buildid or "Unknown")
        self._set_build_state(*st["build_state"])
        self.check_btn.setEnabled(not st["checking"])
        self.check_btn.setText("Checking…" if st["checking"] else "Check Now")
        self.update_now_btn.setEnabled(not st["updating"])
        self.update_now_btn.setText("Updating…" if st["updating"] else "Back Up && Update Now")
        if st["pending"]:
            self.pending_label.setText(st["pending"])
            self.changelog_view.setPlainText(st["changelog"])
            self.pending_frame.show()
        else:
            self.pending_frame.hide()
        self.history_table.setRowCount(0)
        for when, change, result in st["history"]:
            row = self.history_table.rowCount()
            self.history_table.insertRow(row)
            for col, text in enumerate((when, change, result)):
                self.history_table.setItem(row, col, QTableWidgetItem(text))

    def set_server(self, server: ServerConfig) -> None:
        self.server = server
        self.refresh_settings()
        self._render()

    def refresh_settings(self) -> None:
        """Re-shows the saved auto-update settings after an outside change."""
        if not self.server:
            return
        self.auto_update_check.blockSignals(True)
        self.auto_update_check.setChecked(self.server.auto_update)
        self.auto_update_check.blockSignals(False)
        self.interval_spin.blockSignals(True)
        self.interval_spin.setValue(self.server.auto_update_check_interval_hours)
        self.interval_spin.blockSignals(False)

    def _on_auto_update_toggled(self, on: bool) -> None:
        if self.server:
            self.server.auto_update = on
            if self.on_auto_update_setting_changed:
                self.on_auto_update_setting_changed(self.server)

    def _on_interval_changed(self, value: int) -> None:
        if self.server:
            self.server.auto_update_check_interval_hours = value
            if self.on_auto_update_setting_changed:
                self.on_auto_update_setting_changed(self.server)

    def check_now(self, server: Optional[ServerConfig] = None) -> bool:
        """Starts a check for `server` (default: the one shown). False if
        one is already running for it."""
        server = server or self.server
        if not server or server.id in self._check_workers:
            return False
        worker = CheckWorker(server.steamcmd_dir)
        worker.finished_check.connect(lambda latest, info, srv=server: self._on_check_finished(srv, latest, info))
        self._check_workers[server.id] = worker
        worker.start()
        if self.server is server:
            self._render()
        return True

    def _on_check_finished(self, server: ServerConfig, latest_buildid, info: changelog.ChangelogInfo) -> None:
        self._retire_worker(self._check_workers.pop(server.id, None))
        self.record_check(server, latest_buildid, info)

    def record_check(self, server: ServerConfig, latest_buildid, info) -> None:
        """A check's result (from this page or an automatic one)."""
        st = self.state_for(server)
        if not latest_buildid or latest_buildid == server.installed_buildid:
            st.update(pending="", latest="", changelog="",
                      build_state=("● Up to date", "PillOn") if latest_buildid else ("● Couldn't check", "PillWarn"))
        else:
            # Build ids aren't version strings, so only the keyword check is used.
            body = getattr(info, "body", "") if getattr(info, "fetched_ok", False) else ""
            is_major = changelog.is_major_update("", "", body)
            st.update(
                build_state=(f"● Build {latest_buildid} available", "PillWarn"),
                pending=f"{'MAJOR UPDATE' if is_major else 'Update'}: Build {latest_buildid} available",
                latest=latest_buildid,
                changelog=body or "No changelog text available yet.",
            )
        st["checked_at"] = datetime.now().isoformat(timespec="minutes")
        if self.server is server:
            self._render()

    def _set_build_state(self, text: str, pill: str) -> None:
        self.build_state_label.setText(text)
        self.build_state_label.setObjectName(pill)
        self.build_state_label.style().unpolish(self.build_state_label)
        self.build_state_label.style().polish(self.build_state_label)

    def _update_now(self) -> None:
        self.update_now(self.server)

    def update_now(self, server: Optional[ServerConfig], quiet: bool = False) -> str:
        """Backs up, stops (if running), updates and restarts `server`.
        Returns "" when started, else why not (also shown in a dialog
        unless quiet)."""
        if not server:
            return "No server."
        if server.id in self._update_workers or (self.on_update_now_guard and self.on_update_now_guard(server)):
            msg = (f"An update for \"{server.name}\" is already running (started automatically or from "
                   f"another tab) -- wait for it to finish before starting another.")
            if not quiet:
                QMessageBox.information(self, "Update already in progress", msg)
            return msg

        # Stop first so files aren't in use; UpdateWorker backs up after the
        # stop, since a backup of a live database is unsafe.
        was_running = self.on_stop_before_update(server) if self.on_stop_before_update else False
        worker = UpdateWorker(
            server.steamcmd_dir, server.install_dir,
            stop_server=server if was_running else None,
            backup_server=server if server.backup_before_update else None,
            backup_destination=server.backup_destination if server.backup_before_update else "",
        )
        worker.finished_update.connect(
            lambda result, srv=server, wr=was_running: self._on_update_finished(srv, result, wr)
        )
        self._update_workers[server.id] = worker
        worker.start()
        if self.server is server:
            self._render()
        return ""

    def _on_update_finished(self, server: ServerConfig, result, was_running: bool) -> None:
        self._retire_worker(self._update_workers.pop(server.id, None))

        if self.on_relaunch_after_update:
            self.on_relaunch_after_update(server, was_running, result)

        if result.success:
            server.installed_buildid = result.installed_buildid or server.installed_buildid
            if self.on_update_applied:
                self.on_update_applied(server, server.installed_buildid)
        self.record_result(server, result.success)

    def record_result(self, server: ServerConfig, success: bool, automatic: bool = False) -> None:
        """Adds an update attempt to the server's history (manual or
        automatic) and clears its pending notice after a success."""
        st = self.state_for(server)
        when = datetime.now().strftime("%Y-%m-%d %H:%M")
        if success:
            st["history"].insert(0, (when, f"→ {server.installed_buildid}" + (" (automatic)" if automatic else ""),
                                     "succeeded"))
            st.update(pending="", latest="", changelog="", build_state=("● Up to date", "PillOn"))
        else:
            st["history"].insert(0, (when, "automatic update attempt" if automatic else "update attempt", "failed"))
        del st["history"][50:]
        if self.server is server:
            self._render()
