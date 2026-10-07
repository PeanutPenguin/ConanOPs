"""
Steers Windows Update restarts into ConanOps' restart window, since Windows
otherwise restarts mid-session without saving the world. set_active_hours()
covers everything outside the window (Windows caps active hours at 18h);
MainWindow uses reboot_pending() + restart_pc() to restart in the window
when nobody is online, after stopping servers.
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
    """Whether Windows is waiting to restart for updates (no admin needed)."""
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
    """(start_hour, end_hour) from the window's end (rounded up) to its start
    (rounded down), capped at 18 hours. None if there's no room."""
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
    """Writes active hours (one UAC prompt) and turns off "automatically
    adjust active hours", which would move them again."""
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
    """Schedules a restart (shutdown /g); no admin rights needed."""
    if sys.platform != "win32":
        return False
    try:
        proc = subprocess.run(
            # /g, not /r: with Automatic Restart Sign-On it signs back in, so
            # ConanOps (started at sign-in) brings the servers back.
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
    """Current active-hours values ({name: int or None}), saved before we
    change them so restore_script() can put them back exactly."""
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
    """Elevated PowerShell restoring `original`, removing values that didn't exist."""
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


# Automatic Restart Sign-On ("Use my sign-in info..."). Without it, a PC
# restarted for updates waits at sign-in and ConanOps never starts.

AUTO_SIGN_IN_ON = "on"
AUTO_SIGN_IN_OFF = "off"
AUTO_SIGN_IN_BLOCKED = "blocked"   # disabled by policy; can't be changed here
AUTO_SIGN_IN_UNKNOWN = "unknown"   # unset or older Windows

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
    """Reads the setting (no admin). Unset is UNKNOWN, since the default
    differs between Windows editions."""
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
