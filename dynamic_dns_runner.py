"""Background QThread worker for dynamic_dns.update() -- a real HTTP
round trip, so this can't run directly on the UI thread (same
reasoning as every other *_runner.py module in this app)."""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

import applog
import dynamic_dns

_log = applog.get_logger(__name__)


class DuckDnsWorker(QThread):
    finished_update = Signal(object)  # dynamic_dns.DuckDnsResult

    def __init__(self, domain: str, token: str, parent=None):
        super().__init__(parent)
        self.domain = domain
        self.token = token

    def run(self) -> None:
        try:
            result = dynamic_dns.update(self.domain, self.token)
        except Exception as e:  # noqa: BLE001 - always emit so the app doesn't wait forever
            _log.error(f"DuckDNS update failed unexpectedly: {e}")
            result = dynamic_dns.DuckDnsResult(ok=False, message=f"DuckDNS update failed unexpectedly: {e}")
        self.finished_update.emit(result)
