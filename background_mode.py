"""
Unattended ("background") mode: keeps servers managed when nobody is
signed into Windows -- after a power cut, a Windows Update restart, or
someone signing out.

How it works
------------
A Task Scheduler task ("\\ConanOps\\ConanOps Background") runs as the
person's own account with "run whether the user is logged on or not"
(S4U logon -- no password is stored). It fires at startup and then every
2 minutes. Each run is a tiny PowerShell check: if no ConanOps process
exists, it starts `ConanOps.exe --background` and waits on it. While
that instance runs, the task stays "Running", so the 2-minute repeats
are skipped (MultipleInstances = IgnoreNew). The ConanOps.exe onefile
bundle is therefore only unpacked when it's actually needed, not every
2 minutes.

The background instance is the normal app with no window: same
scheduler, watchdog, auto-resume, updates and backups. Servers it starts
live in the background session, so they survive people signing in and
out.

Handoff
-------
Only one ConanOps runs at a time (main.py's lock file). When someone
opens ConanOps normally while a background instance holds the lock, the
new window drops a handoff request file; the background instance sees
it within ~2 seconds and exits WITHOUT stopping any server, and the
window takes over (it finds the already-running servers by their exe
path). When the window is closed or the person signs out, the task
starts a background instance again within 2 minutes.

Registering the task needs Administrator rights (one permission
prompt). The task itself runs with normal rights (RunLevel Limited), the
same as the window, so both can read and write the same files.
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

# Off for now: tasks that run with "Do not store password" (S4U) can
# break Windows' data protection (DPAPI) for the account -- ConanOps
# encrypts RCON/server passwords with it, and the same breakage can hit
# browsers' saved passwords and other apps. The setting is only shown to
# someone who already turned it on, so they can turn it back off.
AVAILABLE = False

TASK_NAME = "ConanOps Background"
TASK_PATH = "\\ConanOps\\"
BACKGROUND_FLAG = "--background"
REPEAT_MINUTES = 2

_OWNER_FILE = "instance.json"
_HANDOFF_FILE = "handoff.request"


# --------------------------------------------------------------------- #
# What the task runs
# --------------------------------------------------------------------- #

def _app_command() -> tuple:
    """(executable, argument list) that starts ConanOps itself."""
    if getattr(sys, "frozen", False):
        return sys.executable, []
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    return exe, [main_py]


def task_action() -> tuple:
    """(execute, arguments, working_directory) for the scheduled task."""
    import ntpath  # Windows path rules, whatever this runs on (tests run on Linux)
    exe, extra = _app_command()
    if getattr(sys, "frozen", False):
        workdir = ntpath.dirname(exe)
        process_name = ntpath.splitext(ntpath.basename(exe))[0]
        # WaitForExit() on the started process only -- NOT Start-Process
        # -Wait, which in Windows PowerShell also waits for every
        # descendant, i.e. the game servers, and would keep the task
        # "Running" (blocking the 2-minute restarts) long after ConanOps
        # itself handed off and exited.
        check = (
            f"if (-not (Get-Process -Name {powershell.ps_str(process_name)} -ErrorAction SilentlyContinue)) "
            f"{{ Start-Process -FilePath {powershell.ps_str(exe)} -ArgumentList {powershell.ps_str(BACKGROUND_FLAG)} "
            f"-WorkingDirectory {powershell.ps_str(workdir)} -PassThru | ForEach-Object {{ $_.WaitForExit() }} }}"
        )
        args = f'-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command "{check}"'
        return powershell.powershell_exe(), args, workdir
    # Running from source: the lock file alone keeps this to one instance.
    workdir = os.path.dirname(os.path.abspath(extra[0]))
    args = " ".join(f'"{a}"' for a in [*extra, BACKGROUND_FLAG])
    return exe, args, workdir


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


# --------------------------------------------------------------------- #
# Task registration (elevated) and status (read-only)
# --------------------------------------------------------------------- #

def register() -> str:
    """Creates/updates the task. Returns powershell.RUN_OK / RUN_DECLINED
    / RUN_FAILED. One permission prompt."""
    if sys.platform != "win32":
        return powershell.RUN_FAILED
    execute, arguments, workdir = task_action()
    q = powershell.ps_str
    script = (
        f"$action = New-ScheduledTaskAction -Execute {q(execute)} -Argument {q(arguments)} -WorkingDirectory {q(workdir)}\n"
        "$atStartup = New-ScheduledTaskTrigger -AtStartup\n"
        f"$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
        f"-RepetitionInterval (New-TimeSpan -Minutes {REPEAT_MINUTES})\n"
        "$atStartup.Repetition = $repeat.Repetition\n"
        f"$principal = New-ScheduledTaskPrincipal -UserId {q(_current_user())} -LogonType S4U -RunLevel Limited\n"
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable\n"
        # Task Scheduler runs tasks at below-normal priority (7) by default,
        # which the game servers would inherit. 4 = normal.
        "$settings.Priority = 4\n"
        f"Register-ScheduledTask -TaskName {q(TASK_NAME)} -TaskPath {q(TASK_PATH)} -Action $action "
        "-Trigger @($atStartup, $repeat) -Principal $principal -Settings $settings "
        "-Description 'Keeps ConanOps managing your Conan Exiles servers when nobody is signed in.' "
        "-Force | Out-Null\n"
        "exit 0\n"
    )
    outcome = powershell.run_privileged(script)
    _log.info(f"Background task registration: {outcome}")
    return outcome


def unregister() -> str:
    if sys.platform != "win32":
        return powershell.RUN_OK
    q = powershell.ps_str
    script = (
        f"$t = Get-ScheduledTask -TaskName {q(TASK_NAME)} -TaskPath {q(TASK_PATH)} -ErrorAction SilentlyContinue\n"
        # No Stop-ScheduledTask: stopping a running task can end its whole
        # process tree, which would include servers a background instance
        # started. (This runs from the window, so no background instance
        # is running anyway.)
        "if ($t) { $t | Unregister-ScheduledTask -Confirm:$false }\n"
        "exit 0\n"
    )
    outcome = powershell.run_privileged(script)
    _log.info(f"Background task removal: {outcome}")
    return outcome


def status() -> Optional[dict]:
    """{"exists": bool, "state": str, "matches": bool} or None if the
    check couldn't run (or the task isn't visible to this account)."""
    if sys.platform != "win32":
        return None
    q = powershell.ps_str
    script = (
        f"$t = Get-ScheduledTask -TaskName {q(TASK_NAME)} -TaskPath {q(TASK_PATH)} -ErrorAction SilentlyContinue\n"
        "if (-not $t) { '{\"exists\":false}'; exit 0 }\n"
        "$a = $t.Actions | Select-Object -First 1\n"
        "@{ exists = $true; state = [string]$t.State; execute = [string]$a.Execute; "
        "arguments = [string]$a.Arguments } | ConvertTo-Json -Compress\n"
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
        return {"exists": False, "state": "", "matches": False}
    execute, arguments, _wd = task_action()
    matches = (str(data.get("execute", "")).lower() == execute.lower()
               and str(data.get("arguments", "")) == arguments)
    return {"exists": True, "state": data.get("state", ""), "matches": matches}


# --------------------------------------------------------------------- #
# Which instance owns the app right now, and handoff
# --------------------------------------------------------------------- #

def _path(name: str) -> str:
    return os.path.join(conanops_paths.no_space_root(), name)


def write_owner(mode: str) -> None:
    try:
        os.makedirs(conanops_paths.no_space_root(), exist_ok=True)
        tmp = _path(_OWNER_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"mode": mode, "pid": os.getpid()}, f)
        os.replace(tmp, _path(_OWNER_FILE))
    except OSError as e:
        _log.warning(f"Couldn't record instance owner: {e}")


def read_owner() -> Optional[dict]:
    try:
        with open(_path(_OWNER_FILE), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def clear_owner() -> None:
    owner = read_owner()
    if owner and owner.get("pid") == os.getpid():
        try:
            os.remove(_path(_OWNER_FILE))
        except OSError:
            pass


def pid_alive(pid) -> bool:
    try:
        import psutil
        return bool(pid) and psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001
        return False


def background_instance_running() -> bool:
    owner = read_owner()
    return bool(owner and owner.get("mode") == "background" and pid_alive(owner.get("pid")))


def request_handoff() -> None:
    try:
        with open(_path(_HANDOFF_FILE), "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        _log.warning(f"Couldn't request handoff: {e}")


def handoff_requested() -> bool:
    return os.path.exists(_path(_HANDOFF_FILE))


def clear_handoff() -> None:
    try:
        os.remove(_path(_HANDOFF_FILE))
    except OSError:
        pass


# --------------------------------------------------------------------- #
# Uninstall
# --------------------------------------------------------------------- #

def stop_other_instances() -> int:
    """Ends every other ConanOps process (window or background) --
    never the game servers, which are separate processes and keep
    running. Used by the uninstaller so the app's files aren't locked."""
    import psutil
    me = os.getpid()
    skip = {me}
    try:
        skip.add(psutil.Process(me).ppid())  # onefile builds: our own bootloader
    except Exception:  # noqa: BLE001
        pass
    exe_name = os.path.basename(sys.executable).lower() if getattr(sys, "frozen", False) else "conanops.exe"
    stopped = 0
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["pid"] in skip or (proc.info["name"] or "").lower() != exe_name:
                continue
            proc.terminate()
            stopped += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return stopped
