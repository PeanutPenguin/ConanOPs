"""Alerts (tray, Discord, ntfy) and the Discord live status messages.

Part of MainWindow (see ui/main_window.py); split out to keep each file focused."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QSystemTrayIcon,
)

from models import ServerConfig
import discord_status_runner
import applog


from ui.window.common import _NotifyWorker

_log = applog.get_logger('ui.main_window')


class AlertsMixin:
    def _notify(self, server: ServerConfig, message: str, title: str = "ConanOps") -> None:
        full_title = f"{title} — {server.name}"
        if self.tray_icon and self.tray_icon.isVisible():
            self.tray_icon.showMessage(full_title, message, QSystemTrayIcon.Information, 5000)

        worker = _NotifyWorker(self.config.alert_discord_url, self.config.alert_ntfy_url, message, full_title, server.name)
        worker.finished_notify.connect(self._on_notify_finished)
        self._notify_workers.append(worker)
        worker.finished_notify.connect(lambda *_, w=worker: self._notify_workers.remove(w) if w in self._notify_workers else None)
        self._retire_worker(worker)
        worker.start()

    def _on_notify_finished(self, ok: bool, server_name: str, title: str, message: str) -> None:
        if not ok:
            _log.warning(f"Alert delivery failed for {server_name}: {title} -- {message}")
            if self.tray_icon and self.tray_icon.isVisible():
                self.tray_icon.showMessage(
                    "Alert Delivery Failed",
                    f"Couldn't reach Discord/ntfy for \"{title}\" -- see conanops.log.",
                    QSystemTrayIcon.Warning, 5000,
                )

    def _check_discord_status_all(self) -> None:
        url = self.config.alert_discord_url
        if not (self.config.discord_status_enabled and url):
            return
        for server in self.config.servers:
            if not server.install_dir:
                continue
            if server.id in self._discord_status_workers:
                continue  # previous update still in flight -- skip this tick rather than overlap
            content = self._build_discord_status_message(server)
            worker = discord_status_runner.DiscordStatusWorker(
                url, server.discord_status_message_id, content, parent=self,
            )
            worker.finished_update.connect(lambda message_id, srv=server: self._on_discord_status_finished(srv, message_id))
            self._discord_status_workers[server.id] = worker
            self._retire_worker(worker)
            worker.start()

    def _build_discord_status_message(self, server: ServerConfig) -> str:
        """Built from cached state; no queries of its own."""
        now_str = datetime.now().strftime("%H:%M")
        if self._known_running.get(server.id, False):
            online = self._online_by_server.get(server.id, set())
            return f"**{server.name}** — 🟢 Online, {len(online)} player(s) connected. _(updated {now_str})_"
        return f"**{server.name}** — 🔴 Offline. _(updated {now_str})_"

    def _on_discord_status_finished(self, server: ServerConfig, message_id: Optional[str]) -> None:
        self._discord_status_workers.pop(server.id, None)
        if message_id is not None:
            self._discord_status_failures.pop(server.id, None)
            if message_id != server.discord_status_message_id:
                server.discord_status_message_id = message_id
                self.config.save()
            return

        # Drop the message id (so a fresh one is posted) only after several
        # failures in a row; one failure may just be a network blip.
        streak = self._discord_status_failures.get(server.id, 0) + 1
        self._discord_status_failures[server.id] = streak
        if streak >= self._DISCORD_STATUS_FAILURE_RESET_THRESHOLD and server.discord_status_message_id:
            server.discord_status_message_id = ""
            self.config.save()
            self._discord_status_failures[server.id] = 0

    def alerts_changed(self, discord_url_changed: bool) -> None:
        """App Settings → Alerts were saved (here or from the web)."""
        if discord_url_changed:
            # Live status messages live in the old link's channel: post
            # fresh ones in the new channel instead of editing old ones.
            for s in self.config.servers:
                s.discord_status_message_id = ""
            self.config.save()
        self._refresh_chrome()
        if self.config.discord_status_enabled and self.config.alert_discord_url:
            QTimer.singleShot(1000, self._check_discord_status_all)
