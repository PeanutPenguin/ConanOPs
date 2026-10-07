"""
Prunes Conan Exiles' own log files (ConanSandbox/Saved/Logs/*.log) --
distinct from ConanOps' own log (applog.py, which already rotates
itself via Python's RotatingFileHandler). The game server's own logs
are never pruned by Conan Exiles itself: each launch just adds one
more *.log file (Unreal Engine's convention -- the previous run's log
gets renamed to a "-backup-<timestamp>" name and a fresh one starts),
so over a long-running, frequently-restarted server these accumulate
indefinitely with nothing ever deleting the old ones.
"""
from __future__ import annotations

import glob
import os
from typing import List

import applog

_log = applog.get_logger(__name__)

# Keep at least this many of the most recent log files regardless of
# total size, so a fresh restart never leaves someone with zero
# history to look back at, even if a single log file happens to be
# unusually large on its own.
DEFAULT_MIN_KEEP = 3
DEFAULT_MAX_TOTAL_MB = 500.0


def logs_dir(install_dir: str) -> str:
    return os.path.join(install_dir, "ConanSandbox", "Saved", "Logs")


def prune_old_logs(install_dir: str, max_total_mb: float = DEFAULT_MAX_TOTAL_MB, min_keep: int = DEFAULT_MIN_KEEP) -> List[str]:
    """Deletes the OLDEST *.log files in this server's Logs folder
    until the remaining total is under max_total_mb -- but always
    keeps at least `min_keep` of the most recent ones, even if that
    means staying over the size cap (a single very large log
    shouldn't mean deleting every log this server has). Returns the
    list of paths actually deleted (mainly for logging/testing --
    callers don't need to do anything with it). Best-effort: a file
    that can't be deleted (still open, permissions) is skipped rather
    than treated as fatal."""
    log_dir = logs_dir(install_dir)
    if not os.path.isdir(log_dir):
        return []
    paths = glob.glob(os.path.join(log_dir, "*.log"))
    if len(paths) <= min_keep:
        return []

    paths_by_age = sorted(paths, key=os.path.getmtime, reverse=True)  # newest first
    protected = set(paths_by_age[:min_keep])

    total_bytes = sum(os.path.getsize(p) for p in paths_by_age)
    max_bytes = max_total_mb * 1024 * 1024
    deleted: List[str] = []
    for p in reversed(paths_by_age):  # oldest first -- least valuable to lose
        if total_bytes <= max_bytes:
            break
        if p in protected:
            continue
        try:
            size = os.path.getsize(p)
            os.remove(p)
            total_bytes -= size
            deleted.append(p)
        except OSError as e:
            _log.warning(f"Couldn't delete old game server log {p}: {e}")
    if deleted:
        _log.info(f"Pruned {len(deleted)} old game server log file(s) from {log_dir}.")
    return deleted
