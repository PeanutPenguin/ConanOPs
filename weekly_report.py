"""
The weekly report: one short message per server about the past seven days,
sent with the alerts (Discord, ntfy) at the day and hour set in App Settings.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Tuple

import history


def week_key(when: datetime) -> str:
    year, week, _day = when.isocalendar()
    return f"{year}-W{week:02d}"


def is_due(now: datetime, enabled: bool, day: int, hour: int, last_sent: str) -> bool:
    """Due once this week's day and hour have passed and it hasn't gone out
    this week, so a PC that was off at that time sends it when it's back."""
    if not enabled or last_sent == week_key(now):
        return False
    slot = (now - timedelta(days=now.weekday())).replace(hour=hour, minute=0, second=0, microsecond=0)
    slot += timedelta(days=day)
    return now >= slot


def players_in(sessions: Iterable, since: datetime, until: datetime) -> Tuple[int, float]:
    """(different players, hours played) from session_tracker sessions."""
    names = set()
    seconds = 0.0
    for s in sessions:
        try:
            start = datetime.fromisoformat(s.start)
            end = datetime.fromisoformat(s.end) if s.end else until
        except (TypeError, ValueError):
            continue
        a, b = max(start, since), min(end, until)
        if b <= a:
            continue
        names.add(s.name)
        seconds += (b - a).total_seconds()
    return len(names), seconds / 3600.0


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def report_text(summary: "history.Summary", players: Tuple[int, float], backups_made: int,
                newest_backup: Optional[datetime], backups_failed: int = 0) -> str:
    lines: List[str] = []
    up = summary.uptime_percent
    if up is None:
        lines.append("Uptime: not recorded (ConanOps wasn't running).")
    else:
        lines.append(f"Uptime: {up:.1f}%")
    crashes = summary.count("crash")
    hangs = summary.count("hang")
    problems = []
    if crashes:
        problems.append(_n(crashes, "crash"))
    if hangs:
        problems.append(f"stopped responding {_n(hangs, 'time')}")
    lines.append("Problems: " + (", ".join(problems) if problems else "none"))
    restarts = summary.count("restart") + summary.count("scheduled_restart") + summary.count("watchdog")
    lines.append(f"Restarts: {restarts}" + (f" ({summary.count('watchdog')} by the watchdog)"
                                            if summary.count("watchdog") else ""))
    updates = summary.count("update") + summary.count("mod_update")
    if updates or summary.count("update_failed"):
        lines.append(f"Updates: {summary.count('update')} game, {summary.count('mod_update')} mod"
                     + (f", {summary.count('update_failed')} failed" if summary.count("update_failed") else ""))
    count, hours = players
    peak = summary.peak_players
    lines.append(f"Players: {_n(count, 'different player')}, {hours:.0f} hours played, "
                 f"most at once {peak}")
    if summary.peak_mem_mb:
        cpu = f", average CPU {summary.avg_cpu:.0f}%" if summary.avg_cpu is not None else ""
        lines.append(f"Memory: up to {summary.peak_mem_mb / 1024:.1f} GB{cpu}")
    if newest_backup is not None:
        lines.append(f"Backups: {backups_made} made this week, newest {newest_backup:%a %d %b %H:%M}"
                     + (f", {backups_failed} failed" if backups_failed else ""))
    else:
        lines.append("Backups: none -- check the Backups page")
    return "\n".join(lines)
