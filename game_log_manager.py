"""
Prunes Conan Exiles' own server logs (ConanSandbox/Saved/Logs/*.log). The game
never deletes them; each launch adds another backup log.
"""
from __future__ import annotations

import glob
import os
from typing import List

import applog

_log = applog.get_logger(__name__)

# Always keep a few recent logs, even if one is unusually large.
DEFAULT_MIN_KEEP = 3
DEFAULT_MAX_TOTAL_MB = 500.0


def logs_dir(install_dir: str) -> str:
    return os.path.join(install_dir, "ConanSandbox", "Saved", "Logs")


def prune_old_logs(install_dir: str, max_total_mb: float = DEFAULT_MAX_TOTAL_MB, min_keep: int = DEFAULT_MIN_KEEP) -> List[str]:
    """Delete the oldest *.log files until under max_total_mb, always
    keeping the newest `min_keep`. Best-effort; returns deleted paths."""
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
    for p in reversed(paths_by_age):  # oldest first
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
