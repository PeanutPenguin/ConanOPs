"""
Tracks per-player sessions (join/leave times) and cumulative playtime for
one server, persisted to a small JSON file next to the app config. Also
produces the hourly activity histogram the Restart Schedule page uses to
suggest a quiet window.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class Session:
    name: str
    start: str   # ISO timestamp
    end: Optional[str] = None


class SessionTracker:
    def __init__(self, storage_path: str):
        self.storage_path = storage_path
        self.sessions: List[Session] = []
        self._active: Dict[str, Session] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.storage_path):
            return
        with open(self.storage_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        self.sessions = [Session(**s) for s in raw.get("sessions", [])]

        # Any session still open (end=None) when this file was last
        # written means ConanOps quit or crashed (or the server itself
        # crashed) while that player was connected -- we have no
        # reliable "they left" event for it. Left as-is, that session
        # would stay open forever: total_playtime_seconds() treats an
        # open session's end as "now" every time it's called, so its
        # counted duration would keep growing on every future load,
        # without bound, even long after the player actually
        # disconnected. Close it at load time using the file's own
        # last-modified time as a reasonable stand-in for when it
        # stopped being updated -- not exact, but bounded, and closer
        # to the truth than an ever-advancing "now". It's also
        # deliberately NOT re-added to self._active: if that player is
        # still genuinely online, the log monitor's own join line (or,
        # on first attach, its online-snapshot reconciliation) is what
        # re-establishes that, not stale state from a previous run.
        try:
            fallback_end = datetime.fromtimestamp(os.path.getmtime(self.storage_path)).isoformat()
        except OSError:
            fallback_end = datetime.now().isoformat()
        dangling = [s for s in self.sessions if s.end is None]
        if dangling:
            for s in dangling:
                s.end = fallback_end
            self._save()

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump({"sessions": [s.__dict__ for s in self.sessions]}, f, indent=2)

    def player_joined(self, name: str) -> None:
        if name in self._active:
            return
        s = Session(name=name, start=datetime.now().isoformat())
        self._active[name] = s
        self.sessions.append(s)
        self._save()

    def player_left(self, name: str) -> None:
        s = self._active.pop(name, None)
        if s:
            s.end = datetime.now().isoformat()
            self._save()

    def close_all_active(self) -> None:
        """Call when the server stops unexpectedly, so sessions don't
        stay open forever."""
        now = datetime.now().isoformat()
        for s in self._active.values():
            s.end = now
        self._active.clear()
        self._save()

    # ------------------------------------------------------------ stats --
    def total_playtime_seconds(self, name: str) -> float:
        total = 0.0
        for s in self.sessions:
            if s.name != name:
                continue
            start = datetime.fromisoformat(s.start)
            end = datetime.fromisoformat(s.end) if s.end else datetime.now()
            total += (end - start).total_seconds()
        return total

    def session_count(self, name: str) -> int:
        return sum(1 for s in self.sessions if s.name == name)

    def all_player_names(self) -> List[str]:
        return sorted({s.name for s in self.sessions})

    def hourly_activity_histogram(self) -> List[int]:
        """24 buckets counting how many sessions were active starting in
        each hour of day. A simplified approximation (by session start
        hour, not true overlap-across-hours) -- good enough to suggest a
        quiet restart window without needing a full interval-overlap
        calculation."""
        buckets = [0] * 24
        for s in self.sessions:
            start = datetime.fromisoformat(s.start)
            buckets[start.hour] += 1
        return buckets
