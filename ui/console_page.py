from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QPlainTextEdit,
)

import rcon
from models import ServerConfig


class _RconWorker(QThread):
    result = Signal(str, bool)  # text, is_error

    def __init__(self, host: str, port: int, password: str, cmd: str, parent=None):
        super().__init__(parent)
        self.host, self.port, self.password, self.cmd = host, port, password, cmd

    def run(self) -> None:
        try:
            response = rcon.send_command(self.host, self.port, self.password, self.cmd)
            self.result.emit(response or "(no output)", False)
        except rcon.RconError as e:
            self.result.emit(str(e), True)
        except Exception as e:  # noqa: BLE001 - always emit so the UI never gets stuck waiting on this worker
            self.result.emit(f"Unexpected error: {e}", True)


class ConsolePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.server: Optional[ServerConfig] = None
        self._worker: Optional[_RconWorker] = None
        # See AccessPage._retiring_workers' comment -- same reasoning:
        # keep a reference alive until QThread.finished, not just until
        # our own result signal handler runs.
        self._retiring_workers: list = []

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(12)

        top = QHBoxLayout()
        self.title_label = QLabel("Console")
        self.title_label.setObjectName("PageTitle")
        top.addWidget(self.title_label)
        top.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setObjectName("Dim")
        top.addWidget(self.status_label)
        root.addLayout(top)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        root.addWidget(self.output, 1)

        input_row = QHBoxLayout()
        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText("Enter an RCON command…")
        self.command_edit.returnPressed.connect(self._send)
        send_btn = QPushButton("Send")
        send_btn.setObjectName("PrimaryButton")
        send_btn.clicked.connect(self._send)
        input_row.addWidget(self.command_edit, 1)
        input_row.addWidget(send_btn)
        root.addLayout(input_row)

    def set_server(self, server: ServerConfig) -> None:
        self.server = server
        self.output.clear()
        if not server.rcon_enabled:
            self.status_label.setText("RCON is disabled for this server (enable it on the RCON & Alerts settings page).")
            self.command_edit.setEnabled(False)
        else:
            # This is just the configured target, not a verified live
            # connection -- rcon.send_command() only actually connects
            # once a command is sent (see _RconWorker above), so
            # claiming "Connected" here would be true only by
            # coincidence. If the server's down or the port/password is
            # wrong, this label used to say "Connected" right up until
            # the first command failed.
            self.status_label.setText(f"RCON target: 127.0.0.1:{server.rcon_port} (connects when you send a command)")
            self.command_edit.setEnabled(True)

    def _send(self) -> None:
        cmd = self.command_edit.text().strip()
        if not cmd or not self.server or not self.server.rcon_enabled or self._worker:
            return
        self.output.appendPlainText(f"> {cmd}")
        self.command_edit.clear()
        self._worker = _RconWorker("127.0.0.1", self.server.rcon_port, self.server.rcon_password, cmd)
        self._worker.result.connect(self._on_result)
        self._worker.start()

    def _on_result(self, text: str, is_error: bool) -> None:
        prefix = "[error] " if is_error else ""
        self.output.appendPlainText(prefix + text)
        self._retire_worker(self._worker)
        self._worker = None

    def _retire_worker(self, worker: Optional[_RconWorker]) -> None:
        if worker is None:
            return
        self._retiring_workers.append(worker)
        worker.finished.connect(lambda w=worker: self._retiring_workers.remove(w) if w in self._retiring_workers else None)
