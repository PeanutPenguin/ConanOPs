"""
Tails the newest server log on a QThread and emits signals for status
reports, player joins/leaves and world saves.
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
    """Replay a log's join/leave lines to find who's online now, for
    attaching to an already-running server. Best-effort."""
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
        self._attached_once = False

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        while self._running:
            latest = find_latest_log(self.install_dir)
            if latest and latest != self._current_log:
                self._current_log = latest
                if not self._attached_once:
                    # First attach (not a restart we saw): rebuild who's
                    # already online, or restarts could kick them unwarned.
                    self._attached_once = True
                    online = _reconstruct_online_players(latest)
                    if online:
                        self.initial_online_snapshot.emit(online)
                else:
                    # A new log file means a restart: skip history and clear
                    # the online set, so players whose leave line was never
                    # written (crash) don't block restarts forever.
                    self.initial_online_snapshot.emit(set())
                try:
                    self._position = os.path.getsize(latest)
                except OSError:
                    self._position = 0
            elif latest and latest == self._current_log:
                # Conan may reuse the filename across restarts; a file
                # smaller than our position was recreated, so start over.
                try:
                    current_size = os.path.getsize(latest)
                except OSError:
                    current_size = 0
                if current_size < self._position:
                    self._position = 0
                    self.initial_online_snapshot.emit(set())

            if self._current_log and os.path.exists(self._current_log):
                self._read_new_lines()

            # Sleep in small steps so stop() takes effect quickly; callers
            # wait() with a timeout, and destroying a running QThread crashes.
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

        # Only consume complete lines, so a name caught mid-write isn't
        # split. Byte offsets keep _position exact across UTF-8 sequences.
        last_newline = new_data.rfind(b"\n")
        if last_newline == -1:
            return  # no complete line yet
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
