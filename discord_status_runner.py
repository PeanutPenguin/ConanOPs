"""QThread worker for webhooks.update_discord_status(), kept off the UI thread."""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QThread, Signal

import applog
import webhooks

_log = applog.get_logger(__name__)


class DiscordStatusWorker(QThread):
    finished_update = Signal(object)  # Optional[str] -- the message id to persist, or None on failure

    def __init__(self, webhook_url: str, message_id: str, content: str, parent=None, get_link_line=None):
        super().__init__(parent)
        self.webhook_url = webhook_url
        self.message_id = message_id
        self.content = content
        self.get_link_line = get_link_line  # called on this thread (the LAN lookup can be slow)

    def run(self) -> None:
        try:
            content = self.content
            if self.get_link_line:
                content = webhooks.with_link(content, self.get_link_line())
            result: Optional[str] = webhooks.update_discord_status(self.webhook_url, self.message_id, content)
        except Exception as e:  # noqa: BLE001 - always emit so the app doesn't wait forever
            _log.error(f"Discord status update failed unexpectedly: {e}")
            result = None
        self.finished_update.emit(result)
