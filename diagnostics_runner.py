"""
Background QThread worker for diagnostics.run_diagnostics().

Firewall lookups (netsh), UPnP discovery, public-IP lookups and A2S queries
can each take seconds, so they must stay off the UI thread.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QThread, Signal

import applog
import diagnostics
from models import ServerConfig

_log = applog.get_logger(__name__)


class DiagnosticsWorker(QThread):
    finished_diagnostics = Signal(object)  # List[diagnostics.DiagnosticResult]

    def __init__(self, server: ServerConfig, reserved_ports: Optional[set] = None, parent=None):
        super().__init__(parent)
        self.server = server
        self.reserved_ports = reserved_ports

    def run(self) -> None:
        try:
            results: List[diagnostics.DiagnosticResult] = diagnostics.run_diagnostics(
                self.server, reserved_ports=self.reserved_ports,
            )
        except Exception as e:  # noqa: BLE001 - always emit so the button doesn't stay stuck on "Running..."
            _log.error(f"Diagnostics failed unexpectedly: {e}")
            results = [diagnostics.DiagnosticResult(
                "Diagnostics", diagnostics.STATUS_ERROR, f"Diagnostics failed unexpectedly: {e}",
            )]
        self.finished_diagnostics.emit(results)
