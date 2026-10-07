"""
Background scheduler for automatic backups and restarts.

Runs on a QTimer ticking once a minute. For each configured server:

- Backup: if `backup_interval_hours` have elapsed since `last_backup_at`,
  take a scheduled backup.
- Restart: if `restart_enabled` and the current time falls inside
  [restart_start, restart_end) and today's date isn't already recorded
  in `last_restart_date` (so it only fires once per day, not once a
  minute for the whole window) -- restart, UNLESS anyone is currently
  online, in which case skip today and try again on the next check.

This module only decides *when*; it calls back into callables supplied by
the owner (main_window) to actually do the backup / restart, since those
need access to psutil process state, the log monitor's online-player set,
and the UI's toast/notification mechanism.
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

    # Independent of server-build updates -- a mod can get its own new
    # Workshop version on a completely different schedule than Funcom
    # ships a server build, and nothing else in this app ever
    # re-checks mods once they're downloaded except alongside an
    # actual server update. Checked far less often than a server
    # update (that's minutes; this is once a day) since a stale mod
    # file is a much smaller, slower-building problem than a stale
    # server build.
    _MOD_REFRESH_INTERVAL_HOURS = 24

    # Minimum gap between update-check ATTEMPTS for the same server when
    # checks keep failing (see _last_check_attempt above) -- short enough
    # that a real network blip clears quickly, long enough not to shell
    # out to SteamCMD every single minute while it's down.
    _MIN_RETRY_SECONDS = 300

    def __init__(self, get_servers: Callable[[], List[ServerConfig]],
                 is_online: Callable[[ServerConfig], bool],
                 on_state_changed: Callable[[], None],
                 is_running: Optional[Callable[[ServerConfig], bool]] = None,
                 is_locked: Optional[Callable[[ServerConfig], bool]] = None,
                 parent=None):
        """
        get_servers: callable -> list[ServerConfig] (current config)
        is_online: callable(server) -> bool, whether anyone is currently
                   connected (used to enforce the never-restart-while-
                   online rule)
        on_state_changed: called after last_backup_at/last_restart_date
                   are updated, so the owner can persist config
        is_running: callable(server) -> bool, whether the server process
                   is actually running right now. Used to skip a
                   scheduled restart for a server that isn't running (or
                   was never set up at all) -- "restart" only makes
                   sense for something that's currently up, and firing
                   it anyway used to mean the scheduler would silently
                   START a stopped server, or hit a wall of preflight
                   errors every day for a brand-new, never-configured
                   server. Defaults to "always running" (the pre-fix
                   behavior) if not provided, so existing callers/tests
                   that don't care about this distinction keep working.
        is_locked: callable(server) -> bool, whether the server is
                   currently under some OTHER exclusive automated
                   process's control (e.g. an active mod auto-bisect,
                   which stops and restarts the server itself, on its
                   own schedule, outside this one). A scheduled restart
                   firing on top of that would fight whatever that
                   other process is doing mid-test -- this is the same
                   check MainWindow's health check and watchdog use
                   (see _expected_stop) to avoid the same collision,
                   just plumbed through here too since this scheduler
                   is what actually decides to fire a restart in the
                   background, unprompted. Defaults to "never locked"
                   if not provided.
        """
        super().__init__(parent)
        self.get_servers = get_servers
        self.is_online = is_online
        self.on_state_changed = on_state_changed
        self.is_running = is_running if is_running is not None else (lambda server: True)
        self.is_locked = is_locked if is_locked is not None else (lambda server: False)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        # In-memory only (not persisted): tracks which (server_id, date)
        # we've already told the owner about today, so restart_skipped_online
        # fires once per day per server instead of every minute the window
        # stays open with someone online.
        self._skip_notified: set = set()
        # Wall-clock time (not persisted -- just for this run) of the
        # last update-check ATTEMPT per server id, success or failure.
        # server.last_update_check_at only gets stamped on a successful
        # check (see MainWindow._on_periodic_update_check), so a check
        # that keeps failing -- SteamCMD missing, network down -- would
        # otherwise leave last_update_check_at at "" forever, which made
        # `due` in _check_auto_update() True on every single 60-second
        # tick instead of backing off. See _MIN_RETRY_SECONDS.
        self._last_check_attempt: dict = {}
        # Rate-limits mod_refresh_due emission specifically (NOT a
        # success/failure distinction like _last_check_attempt's role
        # for update checks) -- without this, if MainWindow is already
        # busy handling a mod refresh for a server (this check, or one
        # triggered alongside a server update) when _check_mod_refresh
        # runs again a minute later, last_mod_check_at hasn't advanced
        # yet (it's only stamped once the refresh actually finishes),
        # so `due` would stay True and this would emit again on every
        # single tick until that refresh completes.
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
            # Under some other exclusive automated process's control
            # (an active mod auto-bisect, most likely) -- a backup
            # taken mid-run would capture the WORLD SAVE mid-test
            # (whatever mod subset happens to be loaded at that
            # moment), not the server's real, current state. Skip
            # this tick; the backup interval isn't reset, so it's
            # simply retried once the lock clears.
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
            # Deliberately NOT stamping last_backup_at here: this only
            # decides a backup is due and asks the owner to actually
            # run one (backup_due, handled off this thread). If that
            # backup fails, last_backup_at should stay as it was so the
            # next tick sees it as still-due and retries -- the owner
            # is the one who stamps it, and only once the backup has
            # actually succeeded (see MainWindow._handle_scheduled_backup).
            self.backup_due.emit(server)

    def _check_restart(self, server: ServerConfig, now: datetime) -> None:
        if not server.restart_enabled:
            return

        start = _parse_hhmm(server.restart_start)
        end = _parse_hhmm(server.restart_end)
        if not is_within_window(now.time(), start, end):
            return

        if not server.install_dir or not self.is_running(server):
            # Nothing configured, or configured but not currently
            # running -- there's nothing to restart. Deliberately not
            # stamped as "handled today": if the server gets started
            # partway through the window, later ticks should still be
            # able to fire for it.
            return

        if self.is_locked(server):
            # Under some other exclusive automated process's control
            # right now (an active mod auto-bisect, most likely) --
            # don't fight it. Also deliberately not stamped as "handled
            # today": a bisect is expected to finish well within the
            # rest of a typical restart window, so just try again next
            # tick rather than giving up on today's restart entirely.
            return

        # The "day" a window belongs to is the calendar day it STARTS
        # on, not whatever the clock reads at the moment we happen to
        # check. For a window that crosses midnight (e.g. 23:30-00:30),
        # the portion after midnight is still part of the previous
        # day's window -- stamping it with today's date would let the
        # same window fire again once the clock rolls over, since
        # today's date no longer matches what got stamped a few minutes
        # earlier on the other side of midnight.
        if start > end and now.time() < end:
            today = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            today = now.strftime("%Y-%m-%d")
        if server.last_restart_date == today:
            return  # already handled (fired or explicitly skipped) today

        if self.is_online(server):
            # Don't mark today as done -- keep checking every minute in
            # case the window is still open once players leave. Only stamp
            # "handled today" once the window has actually passed, which
            # we detect implicitly: if we're still in-window on a later
            # tick, we'll just check again. If the window closes with
            # people still online the whole time, nothing fires today,
            # which matches the spec's "skip that day and retry next time".
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
        """Decides only WHEN to check SteamCMD, on a throttled interval
        (checking on every 60-second tick would mean shelling out to
        SteamCMD every minute, which is slow and pointless). The actual
        SteamCMD call and the decision of whether to apply an update
        happen in MainWindow, since that's a blocking network operation
        that has no business running on this timer's thread."""
        if not server.auto_update or not server.steamcmd_dir:
            return
        if self.is_locked(server):
            # Under some other exclusive automated process's control
            # (an active mod auto-bisect, most likely) -- an update
            # firing mid-run would stop and relaunch the server using
            # whatever partial test mod list is on disk at that
            # moment, and race the bisect's own restart cycle. Skip
            # this tick; not stamped as checked, so it's simply
            # retried once the lock clears.
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

        # If the last ATTEMPT (successful or not) was too recent, don't
        # fire again yet -- see _last_check_attempt's comment. A check
        # that keeps succeeding never hits this: last_update_check_at
        # gets stamped, so `due` above goes False for the rest of the
        # interval and this code path isn't reached again until it's
        # genuinely due.
        last_attempt = self._last_check_attempt.get(server.id)
        if last_attempt is not None and (now - last_attempt).total_seconds() < self._MIN_RETRY_SECONDS:
            return

        self._last_check_attempt[server.id] = now
        # Same reasoning as _check_backup above: don't stamp
        # last_update_check_at here. If the SteamCMD check itself
        # fails (timeout, missing steamcmd.exe), stamping now would
        # make it wait a full interval before trying again; the
        # owner stamps it once the check has actually completed
        # (see MainWindow._on_periodic_update_check).
        self.update_check_due.emit(server)

    def _check_mod_refresh(self, server: ServerConfig, now: datetime) -> None:
        """Decides only WHEN to re-check enabled mods for a Workshop
        update, on its own ~24h interval -- same throttled-decision-
        only shape as _check_auto_update above (the actual SteamCMD
        download happens in MainWindow). Gated by the same
        server.auto_update toggle a server-build update check uses:
        if that's off, this stays off too, rather than adding a
        second automatic-download behavior nobody asked for."""
        if not server.auto_update or not server.steamcmd_dir:
            return
        if not any(m.get("enabled", True) for m in server.mods):
            return
        if self.is_locked(server):
            # Under some other exclusive automated process's control
            # (an active mod auto-bisect, most likely) -- this would
            # otherwise download into the same SteamCMD Workshop
            # folder a bisect run might currently depend on staying
            # stable for the duration of its own testing. Skip this
            # tick; retried once the lock clears.
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
