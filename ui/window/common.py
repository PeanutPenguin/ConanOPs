"""Helpers shared by MainWindow and its parts (ui/window/)."""
from __future__ import annotations

import os

from PySide6.QtCore import QThread, Signal

from models import ServerConfig
import conanops_paths
import webhooks
import applog


_log = applog.get_logger('ui.main_window')


APP_INSTALL_DIR = conanops_paths.app_install_dir()


def _sessions_path_for(server: ServerConfig) -> str:
    base = os.path.join(os.path.expanduser("~"), "ConanOps", "sessions")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, f"{server.id}.json")


class _OpWorker(QThread):
    """Runs one blocking server operation (stop, restart) off the UI thread;
    graceful_stop can wait ~2 minutes for a big world save."""
    done = Signal(object)
    failed = Signal(object)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as e:  # noqa: BLE001 - handed to the caller's on_error
            self.failed.emit(e)
            return
        self.done.emit(result)


class _NotifyWorker(QThread):
    """Sends a server's webhook alerts off the GUI thread (blocking HTTP POSTs
    with 5s timeouts each)."""
    finished_notify = Signal(bool, str, str, str)  # ok, server_name, title, message

    def __init__(self, discord_url: str, ntfy_url: str, message: str, title: str, server_name: str, parent=None,
                 get_link_line=None):
        super().__init__(parent)
        self.get_link_line = get_link_line  # called here, off the GUI thread (the LAN lookup can be slow)
        self.discord_url = discord_url
        self.ntfy_url = ntfy_url
        self.message = message
        self.title = title
        self.server_name = server_name

    def run(self) -> None:
        try:
            link = self.get_link_line() if (self.get_link_line and self.discord_url) else ""
            ok = webhooks.notify(self.discord_url, self.ntfy_url, self.message, title=self.title, link_line=link)
        except Exception as e:  # noqa: BLE001 - always emit so this worker gets cleared from _notify_workers
            _log.error(f"Alert delivery raised unexpectedly: {e}")
            ok = False
        self.finished_notify.emit(ok, self.server_name, self.title, self.message)
