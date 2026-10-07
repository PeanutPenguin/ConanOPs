"""Unattended ("background") mode: keeps servers managed when nobody is signed in.

A Task Scheduler task (S4U logon, RunLevel Limited, so it shares files with the
window) runs at startup and every 2 minutes, starting `ConanOps.exe --background`
only if no ConanOps process exists. When a window opens, it drops a handoff file;
the background instance exits within ~2s without stopping any server.
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

# Disabled: S4U tasks can break DPAPI for the account, which ConanOps (and
# browsers) use to encrypt saved passwords. Shown only so it can be turned off.
AVAILABLE = False

TASK_NAME = "ConanOps Background"
TASK_PATH = "\\ConanOps\\"
BACKGROUND_FLAG = "--background"
REPEAT_MINUTES = 2

_OWNER_FILE = "instance.json"
_HANDOFF_FILE = "handoff.request"


def _app_command() -> tuple:
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
        # Not Start-Process -Wait: that also waits for descendants (the game
        # servers), keeping the task "Running" after ConanOps exits.
        check = (
            f"if (-not (Get-Process -Name {powershell.ps_str(process_name)} -ErrorAction SilentlyContinue)) "
            f"{{ Start-Process -FilePath {powershell.ps_str(exe)} -ArgumentList {powershell.ps_str(BACKGROUND_FLAG)} "
            f"-WorkingDirectory {powershell.ps_str(workdir)} -PassThru | ForEach-Object {{ $_.WaitForExit() }} }}"
        )
        args = f'-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -Command "{check}"'
        return powershell.powershell_exe(), args, workdir
    workdir = os.path.dirname(os.path.abspath(extra[0]))
    args = " ".join(f'"{a}"' for a in [*extra, BACKGROUND_FLAG])
    return exe, args, workdir


def _current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def register() -> str:
    """Creates/updates the task (one UAC prompt). Returns powershell.RUN_OK / RUN_DECLINED / RUN_FAILED."""
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
        # Default task priority 7 (below normal) would be inherited by servers; 4 = normal.
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
        # No Stop-ScheduledTask: it can kill the whole process tree, including servers.
        "if ($t) { $t | Unregister-ScheduledTask -Confirm:$false }\n"
        "exit 0\n"
    )
    outcome = powershell.run_privileged(script)
    _log.info(f"Background task removal: {outcome}")
    return outcome


def status() -> Optional[dict]:
    """{"exists", "state", "matches"}, or None if the check couldn't run."""
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


def stop_other_instances() -> int:
    """Ends every other ConanOps process (not the game servers) so the uninstaller can remove files."""
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
