"""
Background QThread worker for diagnostics.run_diagnostics().

Diagnostics now checks things that can genuinely take a few seconds --
a live Windows Firewall rule lookup per port (netsh), UPnP router
discovery (a socket-timeout-bound SSDP round trip), and a public-IP
HTTP lookup when UPnP isn't available -- on top of the pre-existing
2-second A2S query timeout when the server's running. Running all of
that directly on the UI thread (as run_diagnostics() used to be
called) would freeze the whole Diagnostics tab for however long the
slowest of those takes, which is exactly the kind of confusing,
"is it broken?" experience a diagnostics tool should never itself
cause. Mirrors the same QThread-worker pattern update_runner.py,
backup_runner.py, and network_setup_runner.py already use for their
own potentially-slow calls.
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
