"""
Per-player sessions and playtime for one server, saved to JSON, plus the
hourly activity histogram used to suggest a quiet restart window.
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

        # Sessions left open mean ConanOps or the server crashed. Close them
        # at the file's mtime so playtime doesn't grow forever. They are not
        # re-added to _active; the log monitor re-establishes who's online.
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
        """Call when the server stops unexpectedly."""
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
        """24 buckets of session counts by start hour (an approximation)."""
        buckets = [0] * 24
        for s in self.sessions:
            start = datetime.fromisoformat(s.start)
            buckets[start.hour] += 1
        return buckets
