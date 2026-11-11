"""Records each server's history (samples and events) as things happen.

Part of MainWindow (see ui/main_window.py)."""
from __future__ import annotations

from typing import Optional

import applog
import history
from models import ServerConfig

_log = applog.get_logger('ui.main_window')


class HistoryMixin:
    def _record_event(self, server: ServerConfig, kind: str, detail: str = "") -> None:
        history.record_event(server.id, kind, detail)
        page = getattr(self, "history_page", None)
        if page is not None and self.config.active_server_id == server.id:
            page.history_changed()

    def _record_sample(self, server: ServerConfig, running: bool, info: Optional[dict],
                       stats: Optional[dict]) -> None:
        cpu = history.cpu_percent(server.id, stats.get("pid") if stats else None,
                                  stats.get("cpu_seconds") if stats else None)
        players = None
        if running:
            online = self._online_by_server.get(server.id)
            players = info.get("players") if info else (len(online) if online is not None else None)
        if history.record_sample(server.id, running, players, cpu, stats.get("mem_mb") if stats else None):
            page = getattr(self, "history_page", None)
            if page is not None and self.config.active_server_id == server.id:
                page.history_changed()
