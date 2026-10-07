"""
Settings > Diagnostics tab: runs diagnostics.run_diagnostics() on the active
server and lists problems and fixes in plain language. Not a SettingsPageBase
since nothing here is applied.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QFrame,
)

import diagnostics
import diagnostics_runner
import network_setup
import network_utils
from models import ServerConfig
from ui.workers import keep_until_finished

# Status -> (pill object name, pill text); colors come from the theme.
_STATUS_PILL = {
    diagnostics.STATUS_OK: ("PillOn", "Pass"),
    diagnostics.STATUS_WARNING: ("PillWarn", "Check"),
    diagnostics.STATUS_ERROR: ("PillBad", "Fail"),
}


def _result_row(result: diagnostics.DiagnosticResult, on_guide: Optional[Callable[[], None]] = None,
                action_label: str = "View Port Forwarding Guide") -> QFrame:
    row = QFrame()
    row.setObjectName("Card")
    layout = QVBoxLayout(row)
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(4)

    top = QHBoxLayout()
    pill_name, pill_text = _STATUS_PILL[result.status]
    symbol = QLabel(pill_text)
    symbol.setObjectName(pill_name)
    symbol.setAlignment(Qt.AlignCenter)
    symbol.setFixedWidth(52)
    title = QLabel(result.title)
    title.setStyleSheet("font-weight: 600;")
    top.addWidget(symbol)
    top.addWidget(title)
    if result.detail:
        # Hover cue; the tooltip is on the whole row.
        info_cue = QLabel("ⓘ")
        info_cue.setObjectName("Muted")
        info_cue.setToolTip(result.detail)
        top.addWidget(info_cue)
    top.addStretch(1)
    layout.addLayout(top)

    message = QLabel(result.message)
    message.setObjectName("Muted")
    message.setWordWrap(True)
    message.setContentsMargins(28, 0, 0, 0)
    layout.addWidget(message)

    if on_guide is not None:
        # Only for a failed "Router UPnP" result: port forwarding needs a walkthrough.
        guide_row = QHBoxLayout()
        guide_row.setContentsMargins(28, 4, 0, 0)
        guide_btn = QPushButton(action_label)
        guide_btn.clicked.connect(on_guide)
        guide_row.addWidget(guide_btn)
        guide_row.addStretch(1)
        layout.addLayout(guide_row)

    if result.detail:
        # Set on each child too: Qt children don't inherit a parent's tooltip.
        row.setToolTip(result.detail)
        symbol.setToolTip(result.detail)
        title.setToolTip(result.detail)
        message.setToolTip(result.detail)

    return row


class DiagnosticsPage(QWidget):
    def __init__(self, get_active_server: Callable[[], Optional[ServerConfig]],
                 get_reserved_ports: Callable[[], set], parent=None):
        super().__init__(parent)
        self.get_active_server = get_active_server
        self.get_reserved_ports = get_reserved_ports
        self._result_rows: list = []
        self._worker: Optional[diagnostics_runner.DiagnosticsWorker] = None
        self._last_server: Optional[ServerConfig] = None
        # Keeps finished workers referenced until QThread.finished fires
        # ("QThread: Destroyed while thread is still running").
        self._retiring_workers: list = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 12)
        self.title_label = title = QLabel("Diagnostics")
        title.setObjectName("PageTitle")
        header.addWidget(title)
        header.addSpacing(16)
        self.last_run_label = QLabel("")
        self.last_run_label.setObjectName("Muted")
        header.addWidget(self.last_run_label)
        header.addStretch(1)
        self.run_btn = QPushButton("Run Diagnostics")
        self.run_btn.setObjectName("PrimaryButton")
        self.run_btn.clicked.connect(self._run)
        header.addWidget(self.run_btn)
        root.addLayout(header)

        note = QLabel(
            "Checks common reasons a server won't start or can't be reached -- missing "
            "files, port conflicts, a stale network address, an unwritable backup folder -- "
            "and tells you what to do about each one. Read-only: this never changes "
            "anything on its own."
        )
        note.setObjectName("Dim")
        note.setWordWrap(True)
        note.setContentsMargins(0, 0, 0, 12)
        root.addWidget(note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.results_container = QWidget()
        self.results_layout = QVBoxLayout(self.results_container)
        self.results_layout.setContentsMargins(0, 0, 8, 24)
        self.results_layout.setSpacing(10)
        self.results_layout.addStretch(1)
        scroll.setWidget(self.results_container)
        root.addWidget(scroll, 1)

        self.placeholder = QLabel("Click \"Run Diagnostics\" to check this server.")
        self.placeholder.setObjectName("Dim")
        self.results_layout.insertWidget(0, self.placeholder)

    def _clear_results(self) -> None:
        for row in self._result_rows:
            self.results_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._result_rows = []

    def _run(self) -> None:
        if self._worker is not None:
            return  # already running
        server = self.get_active_server()
        if server is None:
            return
        self._clear_results()
        self.placeholder.setVisible(False)
        self.run_btn.setEnabled(False)
        self.run_btn.setText("Running…")
        self.last_run_label.setText("")
        self._last_server = server  # so the port-forwarding guide button knows which server's ports/IP to show

        reserved_ports = self.get_reserved_ports()
        self._worker = diagnostics_runner.DiagnosticsWorker(server, reserved_ports=reserved_ports, parent=self)
        self._worker.finished_diagnostics.connect(self._on_diagnostics_finished)
        worker = self._worker
        keep_until_finished(self._retiring_workers, worker)
        self._worker.start()

    def _on_diagnostics_finished(self, results: list) -> None:
        self._worker = None
        self.run_btn.setEnabled(True)
        self.run_btn.setText("Run Diagnostics")

        for result in results:
            needs_guide = result.title == "Router UPnP" and result.status != diagnostics.STATUS_OK
            on_guide = self._open_port_forwarding_guide if needs_guide else None
            label = "View Port Forwarding Guide"
            if result.title == diagnostics.AUTO_SIGN_IN_TITLE and result.status != diagnostics.STATUS_OK:
                import windows_update
                on_guide, label = windows_update.open_sign_in_settings, "Open Sign-in Settings"
            if result.title == diagnostics.VC_RUNTIME_TITLE and result.status != diagnostics.STATUS_OK:
                on_guide, label = self._install_vc_runtime, "Install Visual C++ Runtime"
            row = _result_row(result, on_guide=on_guide, action_label=label)
            self.results_layout.insertWidget(self.results_layout.count() - 1, row)
            self._result_rows.append(row)

        n_errors = sum(1 for r in results if r.status == diagnostics.STATUS_ERROR)
        n_warnings = sum(1 for r in results if r.status == diagnostics.STATUS_WARNING)
        if n_errors == 0 and n_warnings == 0:
            summary = "No issues found."
        elif n_errors > 0:
            summary = f"{n_errors} problem{'' if n_errors == 1 else 's'} found"
            if n_warnings:
                summary += f", {n_warnings} more worth a look"
            summary += "."
        else:
            summary = f"{n_warnings} thing{'' if n_warnings == 1 else 's'} worth a look."
        self.last_run_label.setText(f"Last run at {datetime.now().strftime('%I:%M %p')} -- {summary}")

    def on_server_switched(self) -> None:
        """Clears results from the previous server."""
        self._clear_results()
        self.placeholder.setVisible(True)
        self.last_run_label.setText("")

    def _install_vc_runtime(self) -> None:
        """Installs the VC++ runtime in the background, then re-runs Diagnostics."""
        if getattr(self, "_vc_worker", None) is not None:
            return
        import vcredist
        from PySide6.QtCore import QThread, Signal
        from PySide6.QtWidgets import QMessageBox

        class _VcWorker(QThread):
            done = Signal(str)

            def run(self):
                try:
                    outcome = vcredist.install()
                except Exception as e:  # noqa: BLE001
                    import applog
                    applog.get_logger(__name__).error(f"Visual C++ runtime install failed: {e}")
                    outcome = vcredist.INSTALL_FAILED
                self.done.emit(outcome)

        self.last_run_label.setText("Installing the Visual C++ runtime… (click Yes if Windows asks)")
        self._vc_worker = _VcWorker(self)

        def finished(outcome):
            self._vc_worker = None
            messages = {
                vcredist.INSTALL_OK: None,
                vcredist.INSTALL_OK_RESTART: "Installed. Windows may need a restart before the server can use it.",
                vcredist.INSTALL_DECLINED: None,
                vcredist.INSTALL_DOWNLOAD_FAILED: "Couldn't download the Visual C++ runtime from Microsoft -- check "
                                                  "the internet connection and try again.",
                vcredist.INSTALL_FAILED: "Installing the Visual C++ runtime failed -- see conanops.log.",
            }
            msg = messages.get(outcome)
            if msg:
                QMessageBox.information(self, "Visual C++ Runtime", msg)
            self._run()

        self._vc_worker.done.connect(finished)
        keep_until_finished(self._retiring_workers, self._vc_worker)
        self._vc_worker.start()

    def _open_port_forwarding_guide(self) -> None:
        if self._last_server is None:
            return
        if getattr(self, "_guide_worker", None) is not None:
            return  # already looking things up
        server = self._last_server
        # Router and public-IP lookups run off the GUI thread.
        import network_setup_runner
        self._guide_worker = network_setup_runner.GuideInfoWorker(server.bind_ip or None, parent=self)

        def show(info, srv=server):
            self._guide_worker = None
            router_ip, public_ip = info
            from ui.port_forwarding_guide_dialog import PortForwardingGuideDialog
            dialog = PortForwardingGuideDialog(
                srv.game_port, srv.query_port, srv.bind_ip or "", router_ip, public_ip, parent=self,
            )
            dialog.exec()

        self._guide_worker.finished_info.connect(show)
        self._guide_worker.start()
