"""
Background QThread worker for MainWindow's periodic health check.

Used to run directly in the QTimer callback: process_manager.is_running()
is fast, but the A2S query added for hung-server detection is a real
UDP round trip with its own timeout -- even querying THIS same
machine, a genuinely overloaded server can be slow enough that doing
this for every configured server directly on the UI thread would
freeze the whole window for however long the slowest of them takes.
Mirrors diagnostics_runner.py's identical reasoning.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QThread, Signal

import applog
import network_utils
import process_manager
from models import ServerConfig

_log = applog.get_logger(__name__)

# Local-machine query -- a healthy server answers almost instantly, so
# this can be short without risking false "unresponsive" verdicts from
# a query that just needed a bit longer to reach a remote server.
_QUERY_TIMEOUT_SECONDS = 1.5


class HealthCheckWorker(QThread):
    finished_check = Signal(list)  # List[Tuple[server_id, running, a2s_info_or_None]]

    def __init__(self, servers: List[ServerConfig], parent=None):
        super().__init__(parent)
        self._servers = list(servers)  # snapshot -- config could change while this runs

    def run(self) -> None:
        results: List[Tuple[str, bool, Optional[dict]]] = []
        for server in self._servers:
            if not server.install_dir:
                continue
            try:
                running = process_manager.is_running(server.install_dir)
                info = None
                if running:
                    info = network_utils.query_a2s_info(
                        server.bind_ip or "127.0.0.1", server.query_port, timeout=_QUERY_TIMEOUT_SECONDS,
                    )
                results.append((server.id, running, info))
            except Exception as e:  # noqa: BLE001 - one server's check failing shouldn't drop the rest
                _log.error(f"Health check failed for server {server.id} ({server.name}): {e}")
        self.finished_check.emit(results)
