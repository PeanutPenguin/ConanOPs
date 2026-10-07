"""
Keeping the PC awake while servers are running.

Uses SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) -- the
standard "a program is busy, don't idle-sleep" request (the same one
media players and downloads use). It doesn't change anyone's power
settings, needs no admin rights, and ends automatically if ConanOps
exits. It stops IDLE sleep and hibernate; it can't stop someone pressing
the sleep button or closing a laptop lid.

The request belongs to the calling thread, so it must always be made
from the same (GUI) thread -- MainWindow does that from a QTimer.
"""
from __future__ import annotations

import sys

import applog

_log = applog.get_logger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

_current: bool = False


def set_keep_awake(on: bool) -> bool:
    """Turns the request on/off. Returns whether the call succeeded
    (always False off Windows)."""
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
