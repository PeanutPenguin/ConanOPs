"""Start with Windows: registers ConanOps via an HKCU Run-key value (no admin rights, nothing else to clean up).

Only launches at sign-in; it does nothing while the PC sits at the sign-in screen.
"""
from __future__ import annotations

import sys
from typing import Optional

import applog

_log = applog.get_logger(__name__)

_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "ConanOps"


def _command_line() -> str:
    """The Run-key command; each path is quoted separately so spaces don't split it."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    import os
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return f'"{sys.executable}" "{main_py}"'


def is_registered() -> bool:
    """True only if the entry exists and points at this install (not a stale old folder)."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _type = winreg.QueryValueEx(key, _VALUE_NAME)
            return value == _command_line()
    except (OSError, FileNotFoundError):
        return False


def register() -> None:
    """Adds or updates the Run-key entry. Raises OSError if the write fails
    (e.g. Group Policy); the caller must show that to the person."""
    if sys.platform != "win32":
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH) as key:
        winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _command_line())
    _log.info(f"Registered for Windows startup: {_command_line()}")


def unregister() -> None:
    """Removes the Run-key entry; fine if it doesn't exist."""
    if sys.platform != "win32":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _VALUE_NAME)
        _log.info("Unregistered from Windows startup.")
    except FileNotFoundError:
        pass
