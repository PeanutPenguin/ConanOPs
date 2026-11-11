"""
Each server's history: a sample every few minutes (running, players, CPU,
memory) and a timeline of what happened (starts, crashes, restarts, updates,
backups). Kept in one small SQLite file per server, beside the player
sessions, so it survives restarts and can be charted over weeks.

Only the window's thread writes; reads for the web version run there too.
Every function here catches its own errors: history is never allowed to
break the thing it records.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import applog

_log = applog.get_logger(__name__)

SAMPLE_EVERY_SECONDS = 5 * 60
KEEP_SAMPLES_DAYS = 90
KEEP_EVENTS_DAYS = 365

# Kinds of event, with how each is described.
EVENT_LABELS = {
    "start": "Started",
    "stop": "Stopped",
    "restart": "Restarted",
    "scheduled_restart": "Daily restart",
    "crash": "Crashed",
    "hang": "Stopped responding",
    "watchdog": "Restarted by the watchdog",
    "update": "Game updated",
    "update_failed": "Game update failed",
    "mod_update": "Mods updated",
    "backup": "Backup made",
    "backup_failed": "Backup failed",
    "restore": "Backup restored",
    "restore_failed": "Restore failed",
    "event": "Scheduled event",
    "announcement": "Announcement",
    "takeover": "Taken over by ConanOps",
}
PROBLEM_KINDS = {"crash", "hang", "update_failed", "backup_failed", "restore_failed"}

_lock = threading.Lock()
_connections: Dict[str, sqlite3.Connection] = {}
_last_sample: Dict[str, float] = {}
_cpu_prev: Dict[str, tuple] = {}  # server id -> (pid, cpu seconds, wall time)


def history_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "ConanOps", "history")


def history_file(server_id: str) -> str:
    return os.path.join(history_dir(), f"{server_id}.sqlite")


def _db(server_id: str) -> Optional[sqlite3.Connection]:
    con = _connections.get(server_id)
    if con is not None:
        return con
    try:
        os.makedirs(history_dir(), exist_ok=True)
        con = sqlite3.connect(history_file(server_id), check_same_thread=False, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS samples (ts INTEGER NOT NULL, up INTEGER NOT NULL, "
                    "players INTEGER, cpu REAL, mem_mb REAL)")
        con.execute("CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts)")
        con.execute("CREATE TABLE IF NOT EXISTS events (ts INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT)")
        con.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts)")
        con.commit()
    except (sqlite3.Error, OSError) as e:
        _log.warning(f"Server history isn't available for {server_id}: {e}")
        return None
    _connections[server_id] = con
    return con


def close(server_id: Optional[str] = None) -> None:
    """Closes one server's history file (before deleting it), or all of them."""
    with _lock:
        for sid in [server_id] if server_id else list(_connections):
            con = _connections.pop(sid, None)
            if con is not None:
                try:
                    con.close()
                except sqlite3.Error:
                    pass
            _last_sample.pop(sid, None)
            _cpu_prev.pop(sid, None)


def cpu_percent(server_id: str, pid: Optional[int], cpu_seconds: Optional[float],
                now: Optional[float] = None) -> Optional[float]:
    """The server's CPU use since the last reading, as a share of the whole PC."""
    now = time.time() if now is None else now
    if pid is None or cpu_seconds is None:
        _cpu_prev.pop(server_id, None)
        return None
    prev = _cpu_prev.get(server_id)
    _cpu_prev[server_id] = (pid, cpu_seconds, now)
    if not prev or prev[0] != pid or now <= prev[2]:
        return None
    cores = os.cpu_count() or 1
    return max(0.0, min(100.0, (cpu_seconds - prev[1]) / (now - prev[2]) / cores * 100.0))


def record_sample(server_id: str, up: bool, players: Optional[int] = None, cpu: Optional[float] = None,
                  mem_mb: Optional[float] = None, now: Optional[float] = None, force: bool = False) -> bool:
    """At most one sample per SAMPLE_EVERY_SECONDS per server. True if stored."""
    now = time.time() if now is None else now
    if not force and now - _last_sample.get(server_id, 0.0) < SAMPLE_EVERY_SECONDS - 5:
        return False
    with _lock:
        con = _db(server_id)
        if con is None:
            return False
        try:
            con.execute("INSERT INTO samples VALUES (?, ?, ?, ?, ?)",
                        (int(now), 1 if up else 0, players, None if cpu is None else round(cpu, 1),
                         None if mem_mb is None else round(mem_mb)))
            con.commit()
        except sqlite3.Error as e:
            _log.warning(f"Couldn't record history for {server_id}: {e}")
            return False
    _last_sample[server_id] = now
    return True


def record_event(server_id: str, kind: str, detail: str = "", now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    with _lock:
        con = _db(server_id)
        if con is None:
            return
        try:
            con.execute("INSERT INTO events VALUES (?, ?, ?)", (int(now), kind, (detail or "")[:500]))
            con.commit()
        except sqlite3.Error as e:
            _log.warning(f"Couldn't record a history event for {server_id}: {e}")


@dataclass
class Sample:
    ts: int
    up: bool
    players: Optional[int]
    cpu: Optional[float]
    mem_mb: Optional[float]


@dataclass
class Event:
    ts: int
    kind: str
    detail: str

    @property
    def label(self) -> str:
        return EVENT_LABELS.get(self.kind, self.kind.replace("_", " ").capitalize())

    @property
    def problem(self) -> bool:
        return self.kind in PROBLEM_KINDS


def samples(server_id: str, since: float, until: Optional[float] = None, max_points: int = 0) -> List[Sample]:
    """Samples from `since`, oldest first. With max_points, evenly thinned
    by averaging neighbours (the highest player count is kept)."""
    until = time.time() if until is None else until
    with _lock:
        con = _db(server_id)
        if con is None:
            return []
        try:
            rows = con.execute("SELECT ts, up, players, cpu, mem_mb FROM samples WHERE ts >= ? AND ts <= ? "
                               "ORDER BY ts", (int(since), int(until))).fetchall()
        except sqlite3.Error:
            return []
    out = [Sample(r[0], bool(r[1]), r[2], r[3], r[4]) for r in rows]
    if max_points and len(out) > max_points:
        out = _thin(out, max_points)
    return out


def _avg(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _thin(rows: List[Sample], max_points: int) -> List[Sample]:
    size = len(rows) / max_points
    out = []
    for i in range(max_points):
        chunk = rows[int(i * size):int((i + 1) * size)] or [rows[min(int(i * size), len(rows) - 1)]]
        players = [r.players for r in chunk if r.players is not None]
        out.append(Sample(chunk[-1].ts, any(r.up for r in chunk), max(players) if players else None,
                          _avg(r.cpu for r in chunk), _avg(r.mem_mb for r in chunk)))
    return out


def events(server_id: str, since: float = 0, limit: int = 500, kinds=None) -> List[Event]:
    """Newest first."""
    with _lock:
        con = _db(server_id)
        if con is None:
            return []
        try:
            if kinds:
                marks = ",".join("?" * len(kinds))
                rows = con.execute(f"SELECT ts, kind, detail FROM events WHERE ts >= ? AND kind IN ({marks}) "
                                   "ORDER BY ts DESC LIMIT ?", (int(since), *kinds, int(limit))).fetchall()
            else:
                rows = con.execute("SELECT ts, kind, detail FROM events WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                                   (int(since), int(limit))).fetchall()
        except sqlite3.Error:
            return []
    return [Event(r[0], r[1], r[2] or "") for r in rows]


@dataclass
class Summary:
    since: float
    until: float
    sampled_seconds: float = 0.0   # time ConanOps was watching
    up_seconds: float = 0.0
    peak_players: int = 0
    avg_players: Optional[float] = None
    avg_cpu: Optional[float] = None
    peak_mem_mb: Optional[float] = None
    counts: Dict[str, int] = None

    @property
    def uptime_percent(self) -> Optional[float]:
        if self.sampled_seconds <= 0:
            return None
        return 100.0 * self.up_seconds / self.sampled_seconds

    def count(self, kind: str) -> int:
        return (self.counts or {}).get(kind, 0)


def summary(server_id: str, since: float, until: Optional[float] = None) -> Summary:
    """Totals for a period. Each sample stands for the time until the next
    one (at most two sample intervals, so time ConanOps wasn't running isn't
    counted either way)."""
    until = time.time() if until is None else until
    rows = samples(server_id, since, until)
    out = Summary(since=since, until=until, counts={})
    cap = SAMPLE_EVERY_SECONDS * 2
    for i, r in enumerate(rows):
        nxt = rows[i + 1].ts if i + 1 < len(rows) else min(until, r.ts + SAMPLE_EVERY_SECONDS)
        span = max(0.0, min(cap, nxt - r.ts))
        out.sampled_seconds += span
        if r.up:
            out.up_seconds += span
    players = [r.players for r in rows if r.up and r.players is not None]
    out.peak_players = max(players) if players else 0
    out.avg_players = _avg(players)
    out.avg_cpu = _avg(r.cpu for r in rows if r.up)
    mems = [r.mem_mb for r in rows if r.mem_mb is not None]
    out.peak_mem_mb = max(mems) if mems else None
    for e in events(server_id, since, limit=100000):
        if e.ts <= until:
            out.counts[e.kind] = out.counts.get(e.kind, 0) + 1
    return out


def prune(server_id: str, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    with _lock:
        con = _db(server_id)
        if con is None:
            return
        try:
            con.execute("DELETE FROM samples WHERE ts < ?", (int(now - KEEP_SAMPLES_DAYS * 86400),))
            con.execute("DELETE FROM events WHERE ts < ?", (int(now - KEEP_EVENTS_DAYS * 86400),))
            con.commit()
        except sqlite3.Error as e:
            _log.warning(f"Couldn't thin out history for {server_id}: {e}")
