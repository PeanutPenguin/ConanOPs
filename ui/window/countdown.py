"""In-game warnings before a restart that players would otherwise get kicked
by: the daily restart, a game or mod update, a scheduled event.

With players online and the server's "when players are online" set to warn,
ConanOps counts down in game chat (by default 15, 5 and 1 minutes before)
and then goes ahead. If everyone leaves first, it goes ahead straight away.
Set to wait (or with RCON off, since warnings need it), the restart waits
until nobody is online.

Part of MainWindow (see ui/main_window.py)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QTimer

import applog
import banlist_manager
from models import ServerConfig

_log = applog.get_logger('ui.main_window')

NOW = "now"
COUNTING_DOWN = "countdown"
WAITING = "wait"


@dataclass
class Countdown:
    server_id: str
    what: str                      # "restart", "update", "mod update", an event's name...
    deadline: float                # time.monotonic() it goes ahead
    then: Callable[[], None]
    marks: List[int] = field(default_factory=list)  # minutes still to announce, largest first
    started: float = 0.0


def plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def warning_text(what: str, minutes: int) -> str:
    return (f"Server {what} in {plural(minutes, 'minute')}. Please find a safe place and log off; "
            "you can rejoin once it's back.")


class CountdownMixin:
    _COUNTDOWN_TICK_MS = 5000

    def _init_countdowns(self) -> None:
        self._countdowns: Dict[str, Countdown] = {}
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(self._COUNTDOWN_TICK_MS)
        self._countdown_timer.timeout.connect(self._tick_countdowns)

    def countdown_left(self, server_id: str) -> Optional[int]:
        """Seconds until a counted-down restart goes ahead, or None."""
        c = self._countdowns.get(server_id)
        return None if c is None else max(0, int(c.deadline - time.monotonic()))

    def countdown_what(self, server_id: str) -> str:
        c = self._countdowns.get(server_id)
        return c.what if c else ""

    def _players_online(self, server: ServerConfig) -> bool:
        return bool(self._online_by_server.get(server.id))

    def can_warn(self, server: ServerConfig) -> bool:
        return server.when_players_online == "warn" and server.rcon_enabled

    def _with_warning(self, server: ServerConfig, what: str, then: Callable[[], None]) -> str:
        """Runs `then` now when nobody's online (returns NOW). With players
        on, counts down in game chat first (COUNTING_DOWN), or, set to wait,
        does nothing (WAITING) so the caller tries again later."""
        if not self._players_online(server) or not self._known_running.get(server.id, False):
            then()
            return NOW
        if not self.can_warn(server):
            return WAITING
        if server.id in self._countdowns:
            return COUNTING_DOWN  # one countdown at a time; the first one wins
        marks = sorted({int(m) for m in server.warning_minutes if int(m) > 0}, reverse=True) or [15, 5, 1]
        now = time.monotonic()
        c = Countdown(server.id, what, now + marks[0] * 60, then, marks, now)
        self._countdowns[server.id] = c
        self._announce(server, c)
        if not self._countdown_timer.isActive():
            self._countdown_timer.start()
        self._notify(server, f"Players are online, so they're being warned in game: {what} in "
                             f"{plural(marks[0], 'minute')}.", title="Restart Coming Up")
        self._record_event(server, "announcement", warning_text(what, marks[0]))
        self._refresh_chrome()
        return COUNTING_DOWN

    def _announce(self, server: ServerConfig, c: Countdown) -> None:
        """Broadcasts every mark that's due (normally just the next one)."""
        left = c.deadline - time.monotonic()
        due = [m for m in c.marks if left <= m * 60 + 2]
        if not due:
            return
        minutes = min(due)
        c.marks = [m for m in c.marks if m not in due]
        self._broadcast(server, warning_text(c.what, max(1, minutes)))

    def _broadcast(self, server: ServerConfig, text: str) -> None:
        def send(srv=server, msg=text):
            return banlist_manager.broadcast_message(srv, msg)
        self._run_op(send, on_error=lambda e, n=server.name: _log.warning(f"Couldn't warn players on {n}: {e}"))

    def cancel_countdown(self, server_id: str, why: str = "") -> bool:
        c = self._countdowns.pop(server_id, None)
        if c is None:
            return False
        server = next((s for s in self.config.servers if s.id == server_id), None)
        if server is not None and why:
            self._notify(server, f"The {c.what} countdown was called off: {why}", title="Countdown Cancelled")
        if not self._countdowns:
            self._countdown_timer.stop()
        self._refresh_chrome()
        return True

    def _tick_countdowns(self) -> None:
        for sid, c in list(self._countdowns.items()):
            server = next((s for s in self.config.servers if s.id == sid), None)
            if server is None:
                self._countdowns.pop(sid, None)
                continue
            if not self._known_running.get(sid, False):
                self.cancel_countdown(sid, "the server stopped.")
                continue
            everyone_left = not self._players_online(server)
            if everyone_left or time.monotonic() >= c.deadline:
                self._countdowns.pop(sid, None)
                if not everyone_left:
                    self._broadcast(server, f"Server {c.what} now.")
                try:
                    c.then()
                except Exception as e:  # noqa: BLE001 - one failure mustn't stop the other countdowns
                    _log.error(f"After the {c.what} countdown on {server.name}: {e}")
                continue
            self._announce(server, c)
        if not self._countdowns:
            self._countdown_timer.stop()
        self._refresh_chrome()
