"""
Tails the newest server log file and parses it for:
  - periodic status reports (players, fps, cpu)
  - player join / leave events
  - world-save confirmations

Runs as a QThread so the UI never blocks on file IO. Emits Qt signals the
Dashboard/Players pages can connect to.
"""
from __future__ import annotations

import glob
import os
import re
import time
from typing import Optional

from PySide6.QtCore import QThread, Signal

STATUS_RE = re.compile(
    r"LogServerStats:.*?Players=(?P<players>\d+).*?FPS=(?P<fps>[\d.]+).*?CPU=(?P<cpu>[\d.]+)"
)
JOIN_RE = re.compile(r"LogNet: Join succeeded: (?P<name>.+)")
LEAVE_RE = re.compile(r"LogNet: Player disconnected: (?P<name>.+)")
SAVE_RE = re.compile(r"LogSave: World save completed")


def find_latest_log(install_dir: str) -> Optional[str]:
    log_dir = os.path.join(install_dir, "ConanSandbox", "Saved", "Logs")
    if not os.path.isdir(log_dir):
        return None
    candidates = glob.glob(os.path.join(log_dir, "*.log"))
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _reconstruct_online_players(log_path: str) -> set:
    """Replays a log file's join/leave lines from the start to figure
    out who's currently online, for the case where we're attaching to
    a log that's already been running for a while (see run()'s
    initial-attach handling). Best-effort: a rotated/truncated log
    missing someone's join line just means they won't show up here
    until their next join/leave event."""
    online: set = set()
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = JOIN_RE.search(line)
                if m:
                    online.add(m.group("name").strip())
                    continue
                m = LEAVE_RE.search(line)
                if m:
                    online.discard(m.group("name").strip())
    except OSError:
        return set()
    return online


class LogMonitor(QThread):
    status_update = Signal(dict)     # {"players": int, "fps": float, "cpu": float}
    player_joined = Signal(str)
    player_left = Signal(str)
    world_saved = Signal()
    log_line = Signal(str)
    initial_online_snapshot = Signal(set)  # {name, ...} -- who's already online when we first attach

    def __init__(self, install_dir: str, poll_interval: float = 1.0, parent=None):
        super().__init__(parent)
        self.install_dir = install_dir
        self.poll_interval = poll_interval
        self._running = True
        self._current_log: Optional[str] = None
        self._position = 0
        self._attached_once = False  # False until we've picked an initial log to watch

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        while self._running:
            latest = find_latest_log(self.install_dir)
            if latest and latest != self._current_log:
                self._current_log = latest
                if not self._attached_once:
                    # This is our FIRST look at this server's log, not a
                    # (re)start we witnessed ourselves -- e.g. ConanOps
                    # just launched, or this server's monitor is only
                    # now being created, while the server has already
                    # been running for a while with people connected.
                    # Seeking straight to end-of-file (the restart
                    # behavior below) would silently treat everyone
                    # already online as offline, since we'd never see
                    # their join line -- which then meant restarts and
                    # auto-updates could kick them without warning,
                    # thinking nobody was online at all. Scan the whole
                    # existing file first to reconstruct who's actually
                    # online right now.
                    self._attached_once = True
                    online = _reconstruct_online_players(latest)
                    if online:
                        self.initial_online_snapshot.emit(online)
                else:
                    # A genuine (re)start: Conan reuses the same log
                    # filename, so a NEW file path here means the
                    # process actually restarted -- seek to end so we
                    # only see new activity, not the whole history.
                    # Whoever was online before the restart is gone now
                    # -- without this, a player whose LEAVE line never
                    # made it into the old log (process killed, crash,
                    # power loss) would stay stuck "online" forever,
                    # which blocks the never-restart-while-online check
                    # and auto-updates indefinitely.
                    self.initial_online_snapshot.emit(set())
                try:
                    self._position = os.path.getsize(latest)
                except OSError:
                    self._position = 0
            elif latest and latest == self._current_log:
                # Same filename as last tick -- Conan reuses its log
                # file name across restarts rather than creating a
                # fresh one, so a restart doesn't show up as a "new"
                # path above at all. Detect it a different way: if the
                # file is now SMALLER than our last read position, it
                # was truncated/recreated out from under us, and
                # sticking with the old (now out-of-range) position
                # would mean silently skipping everything from the
                # start of the new log up to that old byte offset --
                # including the new process's startup and any early
                # join lines -- once the file grows past it again.
                try:
                    current_size = os.path.getsize(latest)
                except OSError:
                    current_size = 0
                if current_size < self._position:
                    self._position = 0
                    # Same reasoning as the restart branch above: a
                    # truncated/recreated log means whoever we thought
                    # was online is stale information now.
                    self.initial_online_snapshot.emit(set())

            if self._current_log and os.path.exists(self._current_log):
                self._read_new_lines()

            # Sleep in small increments rather than one
            # time.sleep(self.poll_interval) call, so stop() (which
            # just flips self._running) takes effect within a fraction
            # of a second instead of only being noticed on the NEXT
            # loop iteration. Callers join this thread with a fixed
            # wait() timeout after calling stop() -- with the default
            # poll_interval of 1s, a single uninterruptible sleep could
            # leave the thread still running well past a shorter wait()
            # call, at which point discarding the QThread wrapper while
            # its underlying thread is still alive is a crash risk
            # ("QThread: Destroyed while thread is still running").
            slept = 0.0
            step = 0.1
            while self._running and slept < self.poll_interval:
                time.sleep(step)
                slept += step

    def _read_new_lines(self) -> None:
        try:
            with open(self._current_log, "rb") as f:
                f.seek(self._position)
                new_data = f.read()
        except OSError:
            return

        if not new_data:
            return

        # Only consume up to the last complete line. Reading in text
        # mode up to EOF (the previous behavior) could catch a line
        # the game process was still in the middle of writing --
        # "LogNet: Join succeeded: Bo" instead of "...Bob" -- and
        # advancing the read position past it meant the rest of that
        # name ("b\n") got treated as the start of the NEXT line
        # instead. A player caught mid-write like that would register
        # under a truncated name, so their real LEAVE line later never
        # matches and clears them from the online set. Byte offsets
        # (not f.tell() in text mode) also make self._position exact
        # regardless of any multi-byte UTF-8 sequences straddling a
        # read boundary.
        last_newline = new_data.rfind(b"\n")
        if last_newline == -1:
            return  # no complete line yet -- wait for more data
        complete = new_data[:last_newline + 1]
        self._position += len(complete)

        text = complete.decode("utf-8", errors="replace")
        for line in text.splitlines():
            self.log_line.emit(line)

            m = STATUS_RE.search(line)
            if m:
                self.status_update.emit({
                    "players": int(m.group("players")),
                    "fps": float(m.group("fps")),
                    "cpu": float(m.group("cpu")),
                })
                continue

            m = JOIN_RE.search(line)
            if m:
                self.player_joined.emit(m.group("name").strip())
                continue

            m = LEAVE_RE.search(line)
            if m:
                self.player_left.emit(m.group("name").strip())
                continue

            if SAVE_RE.search(line):
                self.world_saved.emit()
