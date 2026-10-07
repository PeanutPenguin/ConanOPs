"""
"Run with admin rights": ConanOps runs elevated without a permission
(UAC) prompt each time, so nothing it does -- from the app or from the
web version -- ever waits for someone to click "Yes" at the PC.

How it works
------------
Turning it on registers a Task Scheduler task, "ConanOps (admin)", that
runs ConanOps as the signed-in person with highest privileges. Windows
asks for permission ONCE, to create the task. After that:

  * Whenever ConanOps starts without admin rights (from the Start menu,
    at sign-in, or reopened by keep-alive) it asks Task Scheduler to
    start the task -- which needs no prompt -- and closes; the copy the
    task starts has admin rights. A copy started that way carries
    --elevated, so it never tries again (no loops).
  * If the task can't start, ConanOps just keeps running normally.
  * Copies ConanOps starts itself (after an update) inherit the rights.

The task has no trigger of its own -- it only runs when ConanOps starts
it -- and runs only while that person is signed in. Turning the option
off (or Delete Everything / uninstalling) removes it.

Trade-off: anything that can change the files in ConanOps' folder could
then run with admin rights without a prompt.
"""
from __future__ import annotations

import json
import ntpath
import os
import subprocess
import sys
import time
from typing import List, Optional

import applog
import conanops_paths
import powershell
import proc_utils

_log = applog.get_logger(__name__)

TASK_NAME = "ConanOps (admin)"
TASK_PATH = "\\"
ELEVATED_FLAG = "--elevated"
_SWITCH_FILE = "admin-mode.on"
_ARGS_FILE = "admin-launch-args.json"
_QUIT_FILE = "quit.request"
# Flags handed on to the elevated copy. Anything else is dropped.
_PASSED_FLAGS = ("--keep-alive",)


def _path(name: str) -> str:
    return os.path.join(conanops_paths.no_space_root(), name)


def switch_file() -> str:
    return _path(_SWITCH_FILE)


def is_on() -> bool:
    """Read before the config is loaded, so it's a file, not a setting."""
    return os.path.exists(switch_file())


def _app_command() -> tuple:
    """(exe, arguments, working folder) the task runs."""
    if getattr(sys, "frozen", False):
        return sys.executable, ELEVATED_FLAG, ntpath.dirname(sys.executable)
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    here = os.path.dirname(os.path.abspath(__file__))
    return exe, f'"{os.path.join(here, "main.py")}" {ELEVATED_FLAG}', here


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def register_script() -> str:
    """Comment line first: it's what shows up if the web version has to
    report that this needs the PC."""
    q = powershell.ps_str
    exe, args, workdir = _app_command()
    return (
        "# Run ConanOps with admin rights\n"
        f"$action = New-ScheduledTaskAction -Execute {q(exe)} -Argument {q(args)} -WorkingDirectory {q(workdir)}\n"
        f"$principal = New-ScheduledTaskPrincipal -UserId {q(_current_user())} -LogonType Interactive -RunLevel Highest\n"
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances Parallel\n"
        # Task Scheduler's default priority (7, below normal) would be
        # inherited by ConanOps and, through it, the game servers.
        "$settings.Priority = 4\n"
        f"Register-ScheduledTask -TaskName {q(TASK_NAME)} -TaskPath {q(TASK_PATH)} -Action $action "
        "-Principal $principal -Settings $settings "
        "-Description 'Starts ConanOps with admin rights when ConanOps asks for it. Turn off in ConanOps > App Settings.' "
        "-Force | Out-Null\n"
        "exit 0\n"
    )


def unregister_script() -> str:
    q = powershell.ps_str
    # Never Stop-ScheduledTask: that could end ConanOps and its servers.
    return (
        "# Turn off running ConanOps with admin rights\n"
        f"Get-ScheduledTask -TaskName {q(TASK_NAME)} -TaskPath {q(TASK_PATH)} -ErrorAction SilentlyContinue "
        "| Unregister-ScheduledTask -Confirm:$false -ErrorAction SilentlyContinue\n"
    )


def _write_switch() -> bool:
    try:
        os.makedirs(conanops_paths.no_space_root(), exist_ok=True)
        with open(switch_file(), "w", encoding="utf-8") as f:
            f.write("ConanOps starts itself with admin rights while this file exists.\n")
        return True
    except OSError as e:
        _log.error(f"Couldn't write the admin-mode switch: {e}")
        return False


def _remove_switch() -> None:
    try:
        os.remove(switch_file())
    except FileNotFoundError:
        pass
    except OSError as e:
        _log.warning(f"Couldn't remove the admin-mode switch: {e}")


def enable() -> str:
    """Registers the task (one permission prompt unless already elevated).
    Returns a powershell.RUN_* outcome."""
    if sys.platform != "win32":
        return powershell.RUN_FAILED
    outcome = powershell.run_privileged(register_script(), timeout=120)
    if outcome == powershell.RUN_OK and not _write_switch():
        return powershell.RUN_FAILED
    return outcome


def disable() -> str:
    """Stops using admin rights from the next start. The switch file goes
    first, so even if removing the task fails, it's no longer used."""
    _remove_switch()
    if sys.platform != "win32":
        return powershell.RUN_OK
    st = status()
    if st is not None and not st.get("exists"):
        return powershell.RUN_OK
    return powershell.run_privileged(unregister_script() + "exit 0\n", timeout=120)


def status() -> Optional[dict]:
    """{"exists", "matches"} -- matches is False when the task starts a
    different (moved or reinstalled) copy of ConanOps. None if Task
    Scheduler couldn't be read."""
    if sys.platform != "win32":
        return None
    q = powershell.ps_str
    proc = powershell.run_readonly(
        f"$t = Get-ScheduledTask -TaskName {q(TASK_NAME)} -TaskPath {q(TASK_PATH)} -ErrorAction SilentlyContinue\n"
        "if (-not $t) { '{\"exists\":false}'; exit 0 }\n"
        "$a = $t.Actions | Select-Object -First 1\n"
        "@{ exists = $true; execute = [string]$a.Execute; arguments = [string]$a.Arguments; "
        "level = [string]$t.Principal.RunLevel } | ConvertTo-Json -Compress\n"
        "exit 0\n")
    if proc is None or proc.returncode != 0:
        return None
    try:
        data = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    if not data.get("exists"):
        return {"exists": False, "matches": False}
    exe, args, _wd = _app_command()
    return {"exists": True, "matches": (str(data.get("execute", "")).lower() == exe.lower()
                                         and str(data.get("arguments", "")) == args
                                         and str(data.get("level", "")) == "Highest")}


def ensure(enabled: bool) -> Optional[str]:
    """Background startup check. Elevated: silently re-registers a
    missing or outdated task. Returns a problem to show, or None."""
    if sys.platform != "win32" or not enabled:
        return None
    try:
        st = status()
        if st is None or st.get("matches"):
            if st is not None and not is_on():
                _write_switch()
            return None
        if proc_utils.is_admin():
            return None if enable() == powershell.RUN_OK else "Couldn't repair the admin-rights task."
        return ("ConanOps' admin-rights task is missing or points at an old copy of ConanOps, so it's running "
                "without admin rights. Turn \"Run with admin rights\" off and on again to fix it.")
    except Exception as e:  # noqa: BLE001 - best-effort
        _log.warning(f"Admin-mode check failed: {e}")
        return None


# --------------------------------------------------------------------- #
# Starting the elevated copy
# --------------------------------------------------------------------- #

def should_relaunch(argv: List[str]) -> bool:
    return (sys.platform == "win32" and ELEVATED_FLAG not in argv and is_on()
            and not proc_utils.is_admin() and task_points_here())


def task_points_here() -> bool:
    """Whether the task starts THIS copy of ConanOps. A task left pointing
    at a moved or older copy must not be used: it would start that copy
    (with admin rights) instead of this one. Fast (schtasks, no
    PowerShell), since it runs at every start."""
    import xml.etree.ElementTree as ET
    try:
        proc = subprocess.run(["schtasks.exe", "/Query", "/TN", TASK_PATH + TASK_NAME, "/XML"], capture_output=True,
                              timeout=10, **proc_utils.hidden_window_kwargs())
    except (OSError, subprocess.TimeoutExpired) as e:
        _log.warning(f"Couldn't read the admin-rights task: {e}")
        return False
    if proc.returncode != 0:
        _log.warning("The admin-rights task is missing; running without admin rights.")
        return False
    raw = proc.stdout
    text = None
    for enc in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(enc)
            if "<Task" in text:
                break
        except UnicodeDecodeError:
            continue
    try:
        root = ET.fromstring(text or "")
    except ET.ParseError:
        return False
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    command = (root.findtext(".//t:Exec/t:Command", default="", namespaces=ns) or "").strip().strip('"')
    arguments = (root.findtext(".//t:Exec/t:Arguments", default="", namespaces=ns) or "").strip()
    exe, args, _wd = _app_command()
    ok = command.lower() == exe.lower() and arguments == args
    if not ok:
        _log.warning(f"The admin-rights task starts {command!r}, not this copy ({exe!r}); running without admin "
                     f"rights. Turn \"Run with admin rights\" off and on again to fix it.")
    return ok


def take_launch_args() -> List[str]:
    """In the elevated copy: the flags the copy that started it had."""
    path = _path(_ARGS_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        os.remove(path)
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or time.time() - float(data.get("time", 0)) > 120:
        return []
    return [a for a in data.get("args", []) if a in _PASSED_FLAGS]


def _run_task() -> bool:
    try:
        proc = subprocess.run(["schtasks.exe", "/Run", "/TN", TASK_PATH + TASK_NAME], capture_output=True,
                              text=True, timeout=20, **proc_utils.hidden_window_kwargs())
    except (OSError, subprocess.TimeoutExpired) as e:
        _log.warning(f"Couldn't start the admin-rights task: {e}")
        return False
    if proc.returncode != 0:
        _log.warning(f"Admin-rights task didn't start (code {proc.returncode}): {(proc.stderr or proc.stdout).strip()}")
    return proc.returncode == 0


def relaunch_elevated(argv: List[str], lock, lock_path: str, wait_seconds: float = 20.0) -> bool:
    """Called with the single-instance `lock` held. Hands over to the
    elevated copy: releases the lock, starts the task, and returns True
    once the new copy has taken the lock (this copy should then exit).
    Returns False -- with the lock held again -- if that didn't happen,
    so this copy carries on without admin rights."""
    from PySide6.QtCore import QLockFile
    try:
        with open(_path(_ARGS_FILE), "w", encoding="utf-8") as f:
            json.dump({"time": time.time(), "args": [a for a in argv[1:] if a in _PASSED_FLAGS]}, f)
    except OSError:
        pass
    lock.unlock()
    if _run_task():
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            time.sleep(0.3)
            probe = QLockFile(lock_path)
            if probe.tryLock(0):
                probe.unlock()
                continue
            _log.info("Handed over to ConanOps running with admin rights.")
            return True
        _log.warning("The admin-rights copy of ConanOps didn't start in time; continuing without admin rights.")
    if not lock.tryLock(10_000):
        # The elevated copy turned up after all.
        return True
    try:
        os.remove(_path(_ARGS_FILE))
    except OSError:
        pass
    return False


def restart_elevated() -> bool:
    """From a running (unelevated) ConanOps: start the elevated copy. The
    caller closes this one (releasing the lock) only if this returns True;
    the new copy waits for the lock."""
    try:
        with open(_path(_ARGS_FILE), "w", encoding="utf-8") as f:
            json.dump({"time": time.time(), "args": []}, f)
    except OSError:
        pass
    return _run_task()


# --------------------------------------------------------------------- #
# Closing an elevated copy from an unelevated one (the uninstaller)
# --------------------------------------------------------------------- #
# A program without admin rights can't end one that has them, so the
# uninstaller asks: it writes a file the elevated copy checks for.

def request_quit() -> None:
    try:
        with open(_path(_QUIT_FILE), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        _log.warning(f"Couldn't ask ConanOps to close: {e}")


def quit_requested() -> bool:
    return os.path.exists(_path(_QUIT_FILE))


def clear_quit_request() -> None:
    try:
        os.remove(_path(_QUIT_FILE))
    except OSError:
        pass
