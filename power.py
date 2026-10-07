"""Keeps the PC from idle-sleeping while servers run, via SetThreadExecutionState.

No admin or power-setting changes; ends when ConanOps exits. The request is per
thread, so always call from the GUI thread (MainWindow uses a QTimer).
"""
from __future__ import annotations

import sys

import applog

_log = applog.get_logger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_current: bool = False


def set_keep_awake(on: bool) -> bool:
    """Returns whether the call succeeded (always False off Windows)."""
    global _current
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ok = bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except Exception as e:  # noqa: BLE001
        _log.warning(f"Couldn't change the keep-awake request: {e}")
        return False
    if ok and on != _current:
        _log.info("Keeping the PC awake while servers run." if on else "No longer keeping the PC awake.")
    if ok:
        _current = on
    return ok


def is_keeping_awake() -> bool:
    return _current
