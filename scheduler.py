"""
Once-a-minute scheduler deciding when each server is due for a backup,
daily restart (skipped while anyone is online), update check or mod
refresh. It only emits signals; MainWindow does the actual work.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from models import ServerConfig


def _parse_hhmm(s: str) -> dtime:
    try:
        h, m = s.split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError):
        return dtime(0, 0)


def is_within_window(now: dtime, start: dtime, end: dtime) -> bool:
    """Handles windows that cross midnight (e.g. 23:30-00:30)."""
    if start <= end:
        return start <= now < end
    return now >= start or now < end


class Scheduler(QObject):
    backup_due = Signal(object)                 # ServerConfig
    restart_due = Signal(object)                # ServerConfig
    restart_skipped_online = Signal(object)      # ServerConfig
    update_check_due = Signal(object)            # ServerConfig -- time to ask SteamCMD for the latest build
    mod_refresh_due = Signal(object)             # ServerConfig -- time to re-check enabled mods for Workshop updates

    # Mods update on their own schedule, separate from server builds.
    _MOD_REFRESH_INTERVAL_HOURS = 24

    # Backoff between attempts while checks keep failing.
    _MIN_RETRY_SECONDS = 300

    def __init__(self, get_servers: Callable[[], List[ServerConfig]],
                 is_online: Callable[[ServerConfig], bool],
                 on_state_changed: Callable[[], None],
                 is_running: Optional[Callable[[ServerConfig], bool]] = None,
                 is_locked: Optional[Callable[[ServerConfig], bool]] = None,
                 parent=None):
        """
        is_online: whether anyone is connected (never restart while online).
        on_state_changed: called after last_restart_date changes, to persist config.
        is_running: restarts only fire for a running server (default: always running).
        is_locked: server is under another automated process (e.g. mod
                   auto-bisect); scheduled actions wait (default: never locked).
        """
        super().__init__(parent)
        self.get_servers = get_servers
        self.is_online = is_online
        self.on_state_changed = on_state_changed
        self.is_running = is_running if is_running is not None else (lambda server: True)
        self.is_locked = is_locked if is_locked is not None else (lambda server: False)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        # (server_id, date) pairs already notified, so restart_skipped_online fires once a day.
        self._skip_notified: set = set()
        # Last update-check attempt per server. last_update_check_at is only
        # stamped on success, so without this a failing check would retry every tick.
        self._last_check_attempt: dict = {}
        # last_mod_check_at is stamped only when a refresh finishes, so this
        # stops re-emitting every tick while one is still running.
        self._last_mod_check_attempt: dict = {}

    def start(self, interval_ms: int = 60_000) -> None:
        self._timer.start(interval_ms)

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        now = datetime.now()
        for server in self.get_servers():
            self._check_backup(server, now)
            self._check_restart(server, now)
            self._check_auto_update(server, now)
            self._check_mod_refresh(server, now)

    def _check_backup(self, server: ServerConfig, now: datetime) -> None:
        if not server.backup_destination:
            return
        if self.is_locked(server):
            # A backup during e.g. a mod bisect would capture a mid-test world save; retry later.
            return
        if server.last_backup_at:
            try:
                last = datetime.fromisoformat(server.last_backup_at)
            except ValueError:
                last = None
        else:
            last = None

        due = last is None or (now - last).total_seconds() >= server.backup_interval_hours * 3600
        if due:
            # The owner stamps last_backup_at only on success, so a failed backup is retried.
            self.backup_due.emit(server)

    def _check_restart(self, server: ServerConfig, now: datetime) -> None:
        if not server.restart_enabled:
            return

        start = _parse_hhmm(server.restart_start)
        end = _parse_hhmm(server.restart_end)
        if not is_within_window(now.time(), start, end):
            return

        if not server.install_dir or not self.is_running(server):
            # Not stamped as handled, so a server started later in the window still restarts.
            return

        if self.is_locked(server):
            # Don't fight e.g. a mod bisect; not stamped, so retried next tick.
            return

        # A window belongs to the day it starts on, so one crossing midnight
        # can't fire twice.
        if start > end and now.time() < end:
            today = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            today = now.strftime("%Y-%m-%d")
        if server.last_restart_date == today:
            return  # already handled (fired or explicitly skipped) today

        if self.is_online(server):
            # Not stamped: keep checking in case players leave before the window ends.
            skip_key = (server.id, today)
            if skip_key not in self._skip_notified:
                self._skip_notified.add(skip_key)
                self.restart_skipped_online.emit(server)
            return

        server.last_restart_date = today
        self._skip_notified.discard((server.id, today))
        self.on_state_changed()
        self.restart_due.emit(server)

    def _check_auto_update(self, server: ServerConfig, now: datetime) -> None:
        """Decides only when an update check is due; MainWindow runs SteamCMD."""
        if not server.auto_update or not server.steamcmd_dir:
            return
        if self.is_locked(server):
            # An update mid-bisect would race its restarts; retried once unlocked.
            return
        if server.last_update_check_at:
            try:
                last = datetime.fromisoformat(server.last_update_check_at)
            except ValueError:
                last = None
        else:
            last = None

        interval_hours = max(1, server.auto_update_check_interval_hours)
        due = last is None or (now - last).total_seconds() >= interval_hours * 3600
        if not due:
            return

        last_attempt = self._last_check_attempt.get(server.id)
        if last_attempt is not None and (now - last_attempt).total_seconds() < self._MIN_RETRY_SECONDS:
            return

        self._last_check_attempt[server.id] = now
        # The owner stamps last_update_check_at once the check completes.
        self.update_check_due.emit(server)

    def _check_mod_refresh(self, server: ServerConfig, now: datetime) -> None:
        """Decides when enabled mods are due a Workshop re-check (daily).
        Uses the same auto_update toggle as server updates."""
        if not server.auto_update or not server.steamcmd_dir:
            return
        if not any(m.get("enabled", True) for m in server.mods):
            return
        if self.is_locked(server):
            # A bisect needs the Workshop folder to stay unchanged.
            return

        if server.last_mod_check_at:
            try:
                last = datetime.fromisoformat(server.last_mod_check_at)
            except ValueError:
                last = None
        else:
            last = None

        due = last is None or (now - last).total_seconds() >= self._MOD_REFRESH_INTERVAL_HOURS * 3600
        if not due:
            return

        last_attempt = self._last_mod_check_attempt.get(server.id)
        if last_attempt is not None and (now - last_attempt).total_seconds() < self._MIN_RETRY_SECONDS:
            return

        self._last_mod_check_attempt[server.id] = now
        self.mod_refresh_due.emit(server)
