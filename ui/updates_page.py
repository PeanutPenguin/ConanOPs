from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QFrame, QTextEdit, QTableWidget, QTableWidgetItem, QHeaderView, QSpinBox,
    QMessageBox,
)

import changelog
from models import ServerConfig
from update_runner import CheckWorker, UpdateWorker


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
        self._check_worker: Optional[CheckWorker] = None
        self._update_worker: Optional[UpdateWorker] = None
        # See ui/access_page.py's AccessPage._retiring_workers comment.
        self._retiring_workers: list = []
        self._pending_changelog: Optional[changelog.ChangelogInfo] = None

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
        if worker is None:
            return
        self._retiring_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._retiring_workers.remove(w) if w in self._retiring_workers else None)

    def set_server(self, server: ServerConfig) -> None:
        self.server = server
        self.installed_label.setText(server.installed_buildid or "Unknown")
        self.auto_update_check.blockSignals(True)
        self.auto_update_check.setChecked(server.auto_update)
        self.auto_update_check.blockSignals(False)
        self.interval_spin.blockSignals(True)
        self.interval_spin.setValue(server.auto_update_check_interval_hours)
        self.interval_spin.blockSignals(False)
        self.pending_frame.hide()

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

    def check_now(self) -> None:
        if not self.server or self._check_worker:
            return
        server = self.server
        self.check_btn.setEnabled(False)
        self.check_btn.setText("Checking…")
        self._check_worker = CheckWorker(server.steamcmd_dir)
        self._check_worker.finished_check.connect(
            lambda latest, info, srv=server: self._on_check_finished(srv, latest, info)
        )
        self._check_worker.start()

    def _on_check_finished(self, server: ServerConfig, latest_buildid, info: changelog.ChangelogInfo) -> None:
        self._check_worker = None
        # Same reasoning as _on_update_finished: don't let a check that
        # was kicked off for one server paint its result onto a
        # different server's page if the person switched away while it
        # was running.
        if self.server is not server:
            return
        self.check_btn.setEnabled(True)
        self.check_btn.setText("Check Now")

        if not latest_buildid or latest_buildid == server.installed_buildid:
            self.pending_frame.hide()
            self._set_build_state("● Up to date" if latest_buildid else "● Couldn't check", "PillOn" if latest_buildid else "PillWarn")
            return
        self._set_build_state(f"● Build {latest_buildid} available", "PillWarn")

        self._pending_changelog = info
        # installed_buildid/latest_buildid are bare Steam build ids, not
        # dotted version strings -- pass "" for both so this only goes
        # on the changelog's own keyword check (see is_major_update's
        # docstring); comparing two build ids as if they were versions
        # made every update show up as MAJOR UPDATE.
        is_major = changelog.is_major_update("", "", info.body if info.fetched_ok else "")
        label = "MAJOR UPDATE" if is_major else "Update"
        self.pending_label.setText(f"{label}: Build {latest_buildid} available")
        self.changelog_view.setPlainText(
            info.body if info.fetched_ok and info.body else "No changelog text available yet."
        )
        self._latest_buildid = latest_buildid
        self.pending_frame.show()

    def _set_build_state(self, text: str, pill: str) -> None:
        self.build_state_label.setText(text)
        self.build_state_label.setObjectName(pill)
        self.build_state_label.style().unpolish(self.build_state_label)
        self.build_state_label.style().polish(self.build_state_label)

    def _update_now(self) -> None:
        if not self.server or self._update_worker:
            return
        server = self.server  # captured now -- see _on_update_finished's comment
        if self.on_update_now_guard and self.on_update_now_guard(server):
            QMessageBox.information(
                self, "Update already in progress",
                f"An update for \"{server.name}\" is already running (started automatically or from "
                f"another tab) -- wait for it to finish before starting another.",
            )
            return

        # The backup now happens inside UpdateWorker, AFTER the server has
        # stopped -- a backup taken first would copy a live database.

        # A manual update used to run straight against a live server --
        # stop it first, the same as the automatic update path already
        # did, so files being validated/replaced aren't also open and
        # being written to by the running process at the same time.
        was_running = self.on_stop_before_update(server) if self.on_stop_before_update else False

        self.update_now_btn.setEnabled(False)
        self.update_now_btn.setText("Updating…")
        self._update_worker = UpdateWorker(
            server.steamcmd_dir, server.install_dir,
            stop_server=server if was_running else None,
            backup_server=server if server.backup_before_update else None,
            backup_destination=server.backup_destination if server.backup_before_update else "",
        )
        self._update_worker.finished_update.connect(
            lambda result, srv=server, wr=was_running: self._on_update_finished(srv, result, wr)
        )
        self._update_worker.start()

    def _on_update_finished(self, server: ServerConfig, result, was_running: bool) -> None:
        self._retire_worker(self._update_worker)
        self._update_worker = None

        if self.on_relaunch_after_update:
            self.on_relaunch_after_update(server, was_running, result)

        if result.success:
            server.installed_buildid = result.installed_buildid or server.installed_buildid
            if self.on_update_applied:
                self.on_update_applied(server, server.installed_buildid)

        # `server` is the one THIS update was actually for, captured
        # when the button was clicked -- not necessarily self.server
        # anymore, since the person may have switched to a different
        # server in the sidebar while the update was running. Only
        # touch this page's own widgets (button state, installed-build
        # label, history table) if we're still looking at that same
        # server; otherwise those widgets belong to whatever server IS
        # showing now, and overwriting them with this update's result
        # would display the wrong server's build id.
        if self.server is not server:
            return
        self.update_now_btn.setEnabled(True)
        self.update_now_btn.setText("Back Up && Update Now")
        if result.success:
            self.installed_label.setText(server.installed_buildid or "Unknown")
            self._add_history_row("just now", f"→ {server.installed_buildid}", "succeeded")
            self.pending_frame.hide()
        else:
            self._add_history_row("just now", "update attempt", "failed")

    def _add_history_row(self, when: str, change: str, result: str) -> None:
        # Always insert at the top (index 0) so History reads newest-first.
        self.history_table.insertRow(0)
        self.history_table.setItem(0, 0, QTableWidgetItem(when))
        self.history_table.setItem(0, 1, QTableWidgetItem(change))
        self.history_table.setItem(0, 2, QTableWidgetItem(result))
