"""
"Reopen ConanOps if it stops": a scheduled task that keeps ConanOps
itself running while someone is signed into Windows.

How it works
------------
A Task Scheduler task ("ConanOps Keep-Alive", in the root folder) runs
as the signed-in person -- "only when the user is logged on", so no
password is stored and Windows' password protection (DPAPI) is never
involved, unlike the retired background mode. It starts a tiny hidden
PowerShell watcher at sign-in. Every minute the watcher checks whether
ConanOps is running; if it has been gone for two checks in a row (so a
self-update's quick restart doesn't count), it starts ConanOps again
with --keep-alive, which opens quietly in the tray.

The watcher stops relaunching when:
  * the person chose Quit from the tray (a "user-quit" marker, cleared
    the next time ConanOps starts any other way), or
  * keep-alive is turned off, ConanOps is uninstalled or everything is
    deleted (the "keep-alive.on" switch file is gone -- the watcher then
    exits on its own).

The task repeats every 10 minutes too, so a watcher that somehow died
comes back (MultipleInstances IgnoreNew stops duplicates). Registering
a task that runs only as yourself needs no administrator rights.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Optional

import applog
import conanops_paths
import powershell

_log = applog.get_logger(__name__)

TASK_NAME = "ConanOps Keep-Alive"
TASK_PATH = "\\"
FLAG = "--keep-alive"
CHECK_SECONDS = 60
REPEAT_MINUTES = 10

_SWITCH_FILE = "keep-alive.on"
_QUIT_MARKER = "user-quit"


def _path(name: str) -> str:
    return os.path.join(conanops_paths.no_space_root(), name)


def switch_file() -> str:
    return _path(_SWITCH_FILE)


def quit_marker() -> str:
    return _path(_QUIT_MARKER)


# --------------------------------------------------------------------- #
# Deliberate quit
# --------------------------------------------------------------------- #

def mark_user_quit() -> None:
    try:
        os.makedirs(conanops_paths.no_space_root(), exist_ok=True)
        with open(quit_marker(), "w", encoding="utf-8") as f:
            f.write("ConanOps was closed on purpose; keep-alive won't reopen it.\n")
    except OSError as e:
        _log.warning(f"Couldn't record the deliberate quit: {e}")


def clear_user_quit() -> None:
    try:
        os.remove(quit_marker())
    except OSError:
        pass


# --------------------------------------------------------------------- #
# The task
# --------------------------------------------------------------------- #

def _app_command() -> tuple:
    """(exe, arguments) that start ConanOps quietly."""
    if getattr(sys, "frozen", False):
        return sys.executable, FLAG
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return exe, f'"{main_py}" {FLAG}'


def watcher_script() -> str:
    """The PowerShell the task runs. Kept short and readable on purpose:
    anyone looking at the task in Task Scheduler can see what it does."""
    import ntpath
    exe, args = _app_command()
    q = powershell.ps_str
    workdir = ntpath.dirname(exe) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    return (
        f"$exe = {q(exe)}; $on = {q(switch_file())}; $quit = {q(quit_marker())}; $gone = 0; "
        f"while (Test-Path -LiteralPath $on) {{ "
        f"Start-Sleep -Seconds {CHECK_SECONDS}; "
        f"if (Test-Path -LiteralPath $quit) {{ $gone = 0; continue }}; "
        f"$running = Get-Process -ErrorAction SilentlyContinue | Where-Object {{ $_.Path -and ($_.Path -ieq $exe) }}; "
        f"if ($running) {{ $gone = 0; continue }}; "
        f"$gone++; "
        f"if ($gone -ge 2 -and (Test-Path -LiteralPath $exe)) {{ "
        f"Start-Process -FilePath $exe -ArgumentList {q(args)} -WorkingDirectory {q(workdir)}; $gone = 0 }} }}"
    )


def task_arguments() -> str:
    return ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command "'
            + watcher_script().replace('"', '\\"') + '"')


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def _register_script() -> str:
    q = powershell.ps_str
    return (
        f"$action = New-ScheduledTaskAction -Execute {q(powershell.powershell_exe())} -Argument {q(task_arguments())}\n"
        f"$logon = New-ScheduledTaskTrigger -AtLogOn -User {q(_current_user())}\n"
        f"$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
        f"-RepetitionInterval (New-TimeSpan -Minutes {REPEAT_MINUTES})\n"
        f"$principal = New-ScheduledTaskPrincipal -UserId {q(_current_user())} -LogonType Interactive -RunLevel Limited\n"
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable\n"
        # Task Scheduler's default priority (7, below normal) would be
        # inherited by ConanOps and, through it, the game servers.
        "$settings.Priority = 4\n"
        f"Register-ScheduledTask -TaskName {q(TASK_NAME)} -TaskPath {q(TASK_PATH)} -Action $action "
        "-Trigger @($logon, $repeat) -Principal $principal -Settings $settings "
        "-Description 'Reopens ConanOps if it stops while you are signed in. Turn off in ConanOps > App Settings.' "
        "-Force | Out-Null\n"
        f"Start-ScheduledTask -TaskName {q(TASK_NAME)} -TaskPath {q(TASK_PATH)} -ErrorAction SilentlyContinue\n"
        "exit 0\n"
    )


def enable() -> bool:
    """Turns keep-alive on: writes the switch file and registers (or
    refreshes) the task. No permission prompt. True on success."""
    if sys.platform != "win32":
        return False
    try:
        os.makedirs(conanops_paths.no_space_root(), exist_ok=True)
        with open(switch_file(), "w", encoding="utf-8") as f:
            f.write("Delete this file to stop ConanOps' keep-alive watcher.\n")
    except OSError as e:
        _log.error(f"Couldn't write the keep-alive switch: {e}")
        return False
    proc = powershell.run_readonly(_register_script(), timeout=60)
    ok = proc is not None and proc.returncode == 0
    if not ok:
        _log.error(f"Couldn't register the keep-alive task: {(proc.stderr if proc else 'PowerShell unavailable')}")
    return ok


def unregister_script() -> str:
    q = powershell.ps_str
    # Never Stop-ScheduledTask: ending a task can end everything it
    # started, which could include ConanOps and the game servers. The
    # watcher exits by itself once its switch file is gone.
    return (
        f"Get-ScheduledTask -TaskName {q(TASK_NAME)} -TaskPath {q(TASK_PATH)} -ErrorAction SilentlyContinue "
        "| Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue\n"
    )


def disable() -> bool:
    try:
        os.remove(switch_file())
    except FileNotFoundError:
        pass
    except OSError as e:
        _log.warning(f"Couldn't remove the keep-alive switch: {e}")
    if sys.platform != "win32":
        return True
    proc = powershell.run_readonly(unregister_script() + "exit 0\n", timeout=60)
    return proc is not None and proc.returncode == 0


def status() -> Optional[dict]:
    """{"exists": bool, "matches": bool} -- matches is False when the
    task points at an old ConanOps location (moved or reinstalled).
    None if Task Scheduler couldn't be read."""
    if sys.platform != "win32":
        return None
    q = powershell.ps_str
    script = (
        f"$t = Get-ScheduledTask -TaskName {q(TASK_NAME)} -TaskPath {q(TASK_PATH)} -ErrorAction SilentlyContinue\n"
        "if (-not $t) { '{\"exists\":false}'; exit 0 }\n"
        "$a = $t.Actions | Select-Object -First 1\n"
        "@{ exists = $true; arguments = [string]$a.Arguments } | ConvertTo-Json -Compress\n"
        "exit 0\n"
    )
    proc = powershell.run_readonly(script)
    if proc is None or proc.returncode != 0:
        return None
    try:
        data = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    if not data.get("exists"):
        return {"exists": False, "matches": False}
    return {"exists": True, "matches": str(data.get("arguments", "")) == task_arguments()}


def ensure(enabled: bool) -> None:
    """Called in the background at startup: makes the task match the
    setting -- re-registers it if it's missing or points at an old copy
    of ConanOps, removes it if the setting is off."""
    if sys.platform != "win32":
        return
    try:
        st = status()
        if enabled:
            if not os.path.exists(switch_file()) or st is None or not st.get("matches"):
                enable()
        elif st and st.get("exists"):
            disable()
    except Exception as e:  # noqa: BLE001 - best-effort
        _log.warning(f"Keep-alive check failed: {e}")
