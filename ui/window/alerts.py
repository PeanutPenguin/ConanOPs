"""Alerts (tray, Discord, ntfy) and the Discord live status messages.

Part of MainWindow (see ui/main_window.py); split out to keep each file focused."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QSystemTrayIcon,
)

from models import ServerConfig
import discord_status_runner
import webhooks
import applog


from ui.window.common import _NotifyWorker

_log = applog.get_logger('ui.main_window')


class AlertsMixin:
    def _notify(self, server: ServerConfig, message: str, title: str = "ConanOps") -> None:
        full_title = f"{title} — {server.name}"
        if self.tray_icon and self.tray_icon.isVisible():
            self.tray_icon.showMessage(full_title, message, QSystemTrayIcon.Information, 5000)

        worker = _NotifyWorker(self.config.alert_discord_url, self.config.alert_ntfy_url, message, full_title, server.name,
                               get_link_line=lambda sid=server.id: self.web_link_line(sid))
        worker.finished_notify.connect(self._on_notify_finished)
        self._notify_workers.append(worker)
        worker.finished_notify.connect(lambda *_, w=worker: self._notify_workers.remove(w) if w in self._notify_workers else None,
                                       Qt.QueuedConnection)
        self._retire_worker(worker)
        worker.start()

    def web_link_line(self, server_id: str = "") -> str:
        """Link to the web version for Discord messages: always the
        from-anywhere (Cloudflare) link, never the home-network one. "" when
        that link isn't up (web version or from-anywhere off, or still
        connecting). With server_id the link opens that server's dashboard."""
        try:
            web = getattr(self, "web_control", None)
            if web is None or not web.is_running:
                return ""
            remote = getattr(getattr(self, "web_tunnel", None), "url", "") or ""
            if not remote:
                return ""
            suffix = f"/?server={server_id}#/dashboard" if server_id else ""
            return webhooks.web_link_line(remote.rstrip("/") + suffix)
        except Exception as e:  # noqa: BLE001 - a missing link must never stop an alert
            _log.warning(f"Couldn't work out the web link for an alert: {e}")
            return ""

    # ------------------------------------------------- Discord commands --
    def _sync_discord_bot(self) -> None:
        """Starts, restarts or stops the !status/!restart bot to match App Settings."""
        import discord_bot
        want = (self.config.discord_bot_token.strip(), self.config.discord_bot_channel_id.strip())
        bot = getattr(self, "_discord_bot", None)
        if bot is not None and (bot.token, bot.channel_id) == want and bot.isRunning():
            return
        if bot is not None:
            bot.stop()
            self._retire_worker(bot)
            self._discord_bot = None
        self.discord_bot_problem = ""
        if not all(want):
            return
        bot = discord_bot.DiscordBotWorker(*want)
        bot.command.connect(self._on_discord_command)
        bot.problem.connect(self._on_discord_bot_problem)
        self._discord_bot = bot
        bot.start()

    def _on_discord_bot_problem(self, text: str) -> None:
        self.discord_bot_problem = text
        _log.warning(f"Discord commands: {text}")
        page = getattr(self, "app_settings_page", None)
        if page is not None and hasattr(page, "show_discord_bot_problem"):
            page.show_discord_bot_problem(text)

    def _discord_status_rows(self) -> list:
        rows = []
        for s in self.config.servers:
            if not s.install_dir:
                continue
            names = sorted(self._online_by_server.get(s.id, set()) or ())
            rows.append({"name": s.name, "running": bool(self._known_running.get(s.id)),
                         "players": len(names), "names": names,
                         "max_players": getattr(s, "max_players", 0), "busy": self.server_busy(s)})
        return rows

    def _on_discord_command(self, message_id: str, author_id: str, author: str, content: str) -> None:
        import discord_bot
        bot = getattr(self, "_discord_bot", None)
        parsed = discord_bot.parse_command(content)
        if bot is None or parsed is None:
            return
        cmd, arg = parsed
        if cmd == "help":
            reply = discord_bot.HELP
        elif cmd == "status":
            reply = discord_bot.status_text(self._discord_status_rows(), self.web_link_line())
        elif author_id not in discord_bot.admin_ids(self.config.discord_bot_admin_ids):
            reply = "Only people on ConanOps' allowed list can restart the server."
        else:
            server, why = discord_bot.pick_server(self.config.servers, arg)
            if server is None:
                reply = why
            else:
                problem = self.restart_server(server)
                reply = (f"Can't restart {server.name}: {problem}" if problem
                         else f"Restarting **{server.name}** -- players get a warning and the world is saved first.")
                if not problem:
                    _log.info(f"{server.name}: restart requested from Discord by {author} ({author_id})")
        bot.reply(message_id, reply)

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
                get_link_line=lambda sid=server.id: self.web_link_line(sid),
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
        self._sync_discord_bot()
        if self.config.discord_status_enabled and self.config.alert_discord_url:
            QTimer.singleShot(1000, self._check_discord_status_all)
