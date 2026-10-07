"""
Windows Update restarts, on ConanOps' terms.

Windows restarts the PC on its own after installing updates, at a time
of its choosing outside "active hours" -- often in the middle of a play
session, and with the game world not saved. Two things here:

  * set_active_hours(): moves Windows' active hours to cover everything
    except ConanOps' chosen restart window, so Windows' own automatic
    restart is steered toward that window. (Windows caps active hours
    at 18 hours, so a short window still leaves some extra hours where
    Windows could pick -- ConanOps tries to restart first.)
  * reboot_pending() + restart_pc(): MainWindow checks every minute;
    inside the window, with an update restart pending and nobody online,
    it saves and stops every server and restarts the PC itself. Servers
    come back via auto-resume after the restart.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Optional, Tuple

import applog
import powershell
from proc_utils import hidden_window_kwargs

_log = applog.get_logger(__name__)

_PENDING_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
)

MAX_ACTIVE_HOURS = 18


def reboot_pending() -> bool:
    """Whether Windows is waiting to restart to finish installing updates.
    Reading these keys needs no admin rights."""
    if sys.platform != "win32":
        return False
    import winreg
    for path in _PENDING_KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
                return True
        except OSError:
            continue
    return False


def _hour_of(hhmm: str, round_up: bool = False) -> int:
    h, m = (int(x) for x in hhmm.split(":"))
    if round_up and m > 0:
        h += 1
    return h % 24


def compute_active_hours(window_start: str, window_end: str) -> Optional[Tuple[int, int]]:
    """Active hours (start_hour, end_hour) that avoid the restart window
    [window_start, window_end): they begin at the window's end (rounded
    up to the hour) and run until the window's start (rounded down),
    capped at Windows' 18-hour maximum. None if there's no room."""
    try:
        active_start = _hour_of(window_end, round_up=True)
        active_end = _hour_of(window_start)
    except (ValueError, AttributeError):
        return None
    length = (active_end - active_start) % 24
    if length == 0:
        return None
    if length > MAX_ACTIVE_HOURS:
        active_end = (active_start + MAX_ACTIVE_HOURS) % 24
    return active_start, active_end


def set_active_hours(start_hour: int, end_hour: int) -> str:
    """Writes Windows Update's active hours (needs Administrator rights:
    one permission prompt). Also turns off "automatically adjust active
    hours", which would otherwise move them again."""
    if sys.platform != "win32":
        return powershell.RUN_FAILED
    script = (
        "$k = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'\n"
        "if (-not (Test-Path $k)) { New-Item -Path $k -Force | Out-Null }\n"
        f"Set-ItemProperty -Path $k -Name ActiveHoursStart -Value {int(start_hour) % 24} -Type DWord\n"
        f"Set-ItemProperty -Path $k -Name ActiveHoursEnd -Value {int(end_hour) % 24} -Type DWord\n"
        "Set-ItemProperty -Path $k -Name SmartActiveHoursState -Value 0 -Type DWord\n"
        "exit 0\n"
    )
    outcome = powershell.run_privileged(script)
    _log.info(f"Set Windows Update active hours {start_hour}:00-{end_hour}:00: {outcome}")
    return outcome


def restart_pc(delay_seconds: int = 60, reason: str = "ConanOps: restarting to finish Windows updates.") -> bool:
    """Schedules a restart (shutdown /g). Standard user accounts
    are allowed to restart their own PC; no admin rights needed."""
    if sys.platform != "win32":
        return False
    try:
        proc = subprocess.run(
            # /g instead of /r: a full restart that, when Windows' "Use my
            # sign-in info to automatically finish setting up after an
            # update" (Automatic Restart Sign-On) is on, signs the person
            # back in and locks the screen -- so ConanOps (started at
            # sign-in) brings the servers back without anyone at the PC.
            ["shutdown", "/g", "/t", str(int(delay_seconds)), "/c", reason[:500]],
            capture_output=True, text=True, timeout=15, **hidden_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _log.error(f"Couldn't schedule the restart: {e}")
        return False
    if proc.returncode != 0:
        _log.error(f"shutdown /r failed ({proc.returncode}): {(proc.stderr or proc.stdout).strip()}")
    return proc.returncode == 0


_ACTIVE_HOURS_KEY = r"SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
_ACTIVE_HOURS_VALUES = ("ActiveHoursStart", "ActiveHoursEnd", "SmartActiveHoursState")


def read_active_hours() -> dict:
    """The current active-hours registry values ({name: int or None},
    None = not set), read BEFORE ConanOps first changes them so
    restore_script() can put them back exactly. Needs no admin rights."""
    out = {name: None for name in _ACTIVE_HOURS_VALUES}
    if sys.platform != "win32":
        return out
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ACTIVE_HOURS_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            for name in _ACTIVE_HOURS_VALUES:
                try:
                    out[name] = int(winreg.QueryValueEx(key, name)[0])
                except (OSError, ValueError, TypeError):
                    pass
    except OSError:
        pass
    return out


def restore_script(original: dict) -> str:
    """PowerShell (elevated) that puts the active-hours values back to
    `original` -- removing any value that didn't exist before."""
    lines = ["try {\n$k = 'HKLM:\\SOFTWARE\\Microsoft\\WindowsUpdate\\UX\\Settings'\n",
             "if (Test-Path $k) {\n"]
    for name in _ACTIVE_HOURS_VALUES:
        value = (original or {}).get(name)
        if value is None:
            lines.append(f"  Remove-ItemProperty -Path $k -Name {name} -ErrorAction SilentlyContinue\n")
        else:
            lines.append(f"  Set-ItemProperty -Path $k -Name {name} -Value {int(value)} -Type DWord\n")
    lines.append("}\n} catch {}\n")
    return "".join(lines)


def restore_active_hours(original: dict) -> str:
    """Puts Windows Update's active hours back (one permission prompt)."""
    if sys.platform != "win32":
        return powershell.RUN_FAILED
    return powershell.run_privileged(restore_script(original) + "exit 0\n")


# --------------------------------------------------------------------- #
# "Use my sign-in info to automatically finish setting up after an
# update" (Automatic Restart Sign-On). Without it, a PC that restarts
# for updates sits at the sign-in screen and ConanOps -- which starts
# when someone signs in -- doesn't come back until someone does.
# --------------------------------------------------------------------- #

AUTO_SIGN_IN_ON = "on"
AUTO_SIGN_IN_OFF = "off"
AUTO_SIGN_IN_BLOCKED = "blocked"   # turned off by a policy (work/school PC); can't be changed here
AUTO_SIGN_IN_UNKNOWN = "unknown"   # couldn't tell (setting never touched, or older Windows)

SIGN_IN_SETTINGS_URI = "ms-settings:signinoptions"


def _current_user_sid() -> str:
    try:
        proc = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                              timeout=10, **hidden_window_kwargs())
        m = re.search(r'"(S-1-[0-9-]+)"', proc.stdout or "")
        return m.group(1) if m else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def auto_sign_in_status() -> str:
    """Reads the setting without needing admin rights. Only reports OFF
    when Windows positively says so -- an unset value means Windows'
    default, which differs between editions, so that's UNKNOWN."""
    if sys.platform != "win32":
        return AUTO_SIGN_IN_UNKNOWN
    import winreg

    def read(path, name):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                return int(winreg.QueryValueEx(k, name)[0])
        except (OSError, ValueError, TypeError):
            return None

    if read(r"SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System", "DisableAutomaticRestartSignOn") == 1:
        return AUTO_SIGN_IN_BLOCKED
    sid = _current_user_sid()
    if not sid:
        return AUTO_SIGN_IN_UNKNOWN
    opt_out = read(rf"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\UserARSO\{sid}", "OptOut")
    if opt_out == 1:
        return AUTO_SIGN_IN_OFF
    if opt_out == 0:
        return AUTO_SIGN_IN_ON
    return AUTO_SIGN_IN_UNKNOWN


def open_sign_in_settings() -> None:
    if sys.platform == "win32":
        try:
            os.startfile(SIGN_IN_SETTINGS_URI)  # noqa: S606 - a fixed Settings URI
        except OSError as e:
            _log.warning(f"Couldn't open sign-in settings: {e}")
