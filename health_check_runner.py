"""
Background QThread worker for MainWindow's periodic health check.

The A2S query used for hung-server detection is a UDP round trip that an
overloaded server can answer slowly, so it must not run on the UI thread.
"""
from __future__ import annotations

from typing import List, Optional, Tuple

from PySide6.QtCore import QThread, Signal

import applog
import network_utils
import process_manager
from models import ServerConfig

_log = applog.get_logger(__name__)

# Local query: a healthy server answers almost instantly.
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
