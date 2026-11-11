"""The weekly report (see weekly_report.py) and keeping server history tidy.

Part of MainWindow (see ui/main_window.py)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from PySide6.QtCore import QTimer

import applog
import backup_manager
import history
import weekly_report
from models import ServerConfig

_log = applog.get_logger('ui.main_window')


class ReportsMixin:
    _REPORT_TICK_MS = 60_000

    def _init_reports(self) -> None:
        self._report_timer = QTimer(self)
        self._report_timer.setInterval(self._REPORT_TICK_MS)
        self._report_timer.timeout.connect(self._reports_tick)
        self._history_pruned_on = ""

    def _start_reports(self) -> None:
        self._report_timer.start()

    def _reports_tick(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now()
        try:
            today = now.date().isoformat()
            if self._history_pruned_on != today:
                self._history_pruned_on = today
                for s in self.config.servers:
                    history.prune(s.id)
            c = self.config
            if weekly_report.is_due(now, c.weekly_report_enabled, c.weekly_report_day, c.weekly_report_hour,
                                    c.last_weekly_report):
                c.last_weekly_report = weekly_report.week_key(now)
                c.save()
                if c.alert_discord_url or c.alert_ntfy_url:
                    self._send_weekly_reports(now)
        except Exception as e:  # noqa: BLE001 - a report must never stop the app
            _log.error(f"Weekly report failed: {e}")

    def weekly_report_for(self, server: ServerConfig, now: Optional[datetime] = None) -> str:
        now = now or datetime.now()
        since = now - timedelta(days=7)
        summary = history.summary(server.id, since.timestamp(), now.timestamp())
        players = weekly_report.players_in(self._tracker_for(server).sessions, since, now)
        backups = backup_manager.list_backups(server.backup_destination) if server.backup_destination else []
        made = [b for b in backups if since <= b.when <= now]
        newest = max((b.when for b in backups), default=None)
        return weekly_report.report_text(summary, players, len(made), newest,
                                         backups_failed=summary.count("backup_failed"))

    def _send_weekly_reports(self, now: datetime) -> None:
        for server in list(self.config.servers):
            if not server.install_dir:
                continue
            try:
                text = self.weekly_report_for(server, now)
            except Exception as e:  # noqa: BLE001
                _log.error(f"Couldn't put together the weekly report for {server.name}: {e}")
                continue
            self._notify(server, text, title="Weekly Report")

    def send_weekly_report_now(self) -> str:
        """The "send now" button (app and web version)."""
        c = self.config
        if not (c.alert_discord_url or c.alert_ntfy_url):
            return "Add a Discord or ntfy link under Alerts first; that's where the report goes."
        servers = [s for s in self.config.servers if s.install_dir]
        if not servers:
            return "There are no servers to report on yet."
        self._send_weekly_reports(datetime.now())
        return f"Sent the report for {len(servers)} server{'s' if len(servers) != 1 else ''}."

    def _send_weekly_report_from_page(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.information(self, "Weekly report", self.send_weekly_report_now())
