"""
"Start with Windows": registers ConanOps to launch automatically when
the person logs into Windows, via a HKEY_CURRENT_USER Run-key entry --
NOT the Startup folder, and NOT a Scheduled Task, both of which do the
same thing but need either a file to manage or elevated rights to set
up. A per-user Run key needs neither: no admin rights, and it's a
single registry value, so there's nothing else to clean up if it's
ever unregistered.

This only affects whether Windows launches ConanOps automatically at
LOGIN -- it does NOT make Windows skip the login screen or launch
ConanOps before anyone's signed in. A PC that reboots and sits at the
login screen with nobody there to sign in won't start ConanOps at all
via this mechanism; that's a separate, bigger problem (auto sign-in,
or running as a background Windows service) this module doesn't solve.
"""
from __future__ import annotations

import sys
from typing import Optional

import applog

_log = applog.get_logger(__name__)

_RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "ConanOps"


def _command_line() -> str:
    """What the registry value actually launches. For a packaged
    build, sys.executable IS ConanOps.exe -- no arguments needed, same
    reasoning as self_update.py's relaunch logic. For a source
    install, this has to invoke the same interpreter against main.py,
    quoted the way Windows' registry Run-key launcher expects (each
    path individually quoted, not the whole line -- an unquoted space
    in either path would otherwise split it into two arguments)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    import os
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return f'"{sys.executable}" "{main_py}"'


def is_registered() -> bool:
    """Whether the Run-key entry currently exists AND points at this
    same install -- not just whether SOME "ConanOps" entry exists.
    Checking the target, not just presence, matters if ConanOps was
    ever reinstalled to a different folder: a stale entry pointing at
    a now-deleted old location wouldn't actually start anything, so it
    shouldn't read as "registered" here (see register()'s docstring
    for how that stale entry gets cleaned up rather than left behind)."""
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
    """Adds (or updates, if it already exists but pointed somewhere
    else -- e.g. ConanOps was moved or reinstalled to a new folder)
    the Run-key entry. No admin rights needed: HKEY_CURRENT_USER is
    writable by the signed-in user account itself. Raises OSError if
    the registry write itself fails for some other reason (permissions
    lockdown via Group Policy, say) -- the caller (App Settings' Start-
    with-Windows checkbox) is responsible for showing that to the
    person rather than silently leaving the checkbox in a state that
    doesn't match reality."""
    if sys.platform != "win32":
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH) as key:
        winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _command_line())
    _log.info(f"Registered for Windows startup: {_command_line()}")


def unregister() -> None:
    """Removes the Run-key entry. Safe to call even if it was never
    registered, or was already removed -- both are treated as success,
    not an error, since the end state (\"not registered\") is exactly
    what the caller wants either way."""
    if sys.platform != "win32":
        return
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, _VALUE_NAME)
        _log.info("Unregistered from Windows startup.")
    except FileNotFoundError:
        pass  # already not registered -- nothing to do
