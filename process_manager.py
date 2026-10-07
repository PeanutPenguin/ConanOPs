"""
Launching, finding, and stopping the Conan Exiles server process.
-MULTIHOME must be the local LAN IP, never the public IP.
"""
from __future__ import annotations

import proc_utils
import os
import subprocess
import sys
import time
from typing import Optional

import psutil

from models import ServerConfig
from proc_utils import hidden_gui_window_kwargs

EXE_RELATIVE_PATH = os.path.join("ConanSandbox", "Binaries", "Win64", "ConanSandboxServer-Win64-Shipping.exe")
PROCESS_NAME = "ConanSandboxServer-Win64-Shipping.exe"


def server_exe_path(install_dir: str) -> str:
    return os.path.join(install_dir, EXE_RELATIVE_PATH)


def build_launch_args(server: ServerConfig) -> list:
    exe = server_exe_path(server.install_dir)
    args = [
        exe,
        "-log",
        f"-Port={server.game_port}",
        f"-QueryPort={server.query_port}",
        f"-MaxPlayers={server.max_players}",
    ]
    if server.bind_ip:
        args.append(f"-MULTIHOME={server.bind_ip}")
    return args


def find_running_pid(install_dir: str) -> Optional[int]:
    """Matches name AND exe path, so two servers on one machine aren't confused."""
    target_exe = os.path.normcase(os.path.abspath(server_exe_path(install_dir)))
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            if proc.info["name"] != PROCESS_NAME:
                continue
            exe = proc.info.get("exe")
            if exe and os.path.normcase(os.path.abspath(exe)) == target_exe:
                return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def is_running(install_dir: str) -> bool:
    return find_running_pid(install_dir) is not None


def game_ini_path(install_dir: str) -> str:
    return os.path.join(install_dir, "ConanSandbox", "Saved", "Config", "WindowsServer", "Game.ini")


def sync_rcon_ini(server: ServerConfig) -> bool:
    """Makes Game.ini's RCON settings match ConanOps' before launch. Only
    writes (and backs up) when something differs. Returns True if it wrote."""
    import ini_utils  # local: keeps this module's import graph minimal
    path = game_ini_path(server.install_dir)
    wanted = {
        "RconEnabled": ("RconPlugin", "1" if server.rcon_enabled else "0"),
        "RconPassword": ("RconPlugin", server.rcon_password),
        "RconPort": ("RconPlugin", str(server.rcon_port)),
    }
    if not server.rcon_enabled and not os.path.exists(path):
        return False  # nothing to turn off, and no reason to create the file just for that
    try:
        current = ini_utils.read_known_keys(path, {k: sec for k, (sec, _v) in wanted.items()}) if os.path.exists(path) else {}
        if all(str(current.get(k, "")).strip() == v for k, (_sec, v) in wanted.items()):
            return False
        ini_utils.apply_known_keys(path, wanted)
        return True
    except OSError:
        return False  # never block a launch over this


def launch(server: ServerConfig) -> subprocess.Popen:
    exe = server_exe_path(server.install_dir)
    if not os.path.exists(exe):
        raise FileNotFoundError(f"Server executable not found: {exe}")
    sync_rcon_ini(server)
    args = build_launch_args(server)
    kwargs = dict(cwd=os.path.dirname(exe), env=proc_utils.child_env(), **hidden_gui_window_kwargs())
    if sys.platform == "win32":
        # Break out of the keep-alive task's job object so ending the task
        # can't kill the server. Not every job allows it, so fall back.
        try:
            flags = kwargs.get("creationflags", 0) | 0x01000000  # CREATE_BREAKAWAY_FROM_JOB
            return subprocess.Popen(args, **dict(kwargs, creationflags=flags))
        except OSError:
            pass
    return subprocess.Popen(args, **kwargs)


# After `saveworld`: how long the world DB must stay unwritten to count as
# saved (big saves pause between writes), and the max wait. Off the UI thread.
SAVE_QUIET_SECONDS = 5.0
SAVE_MAX_WAIT_SECONDS = 120.0
_SAVE_POLL_SECONDS = 0.5


def _latest_world_save_mtime(install_dir: str) -> float:
    import mod_manager  # local import: keeps this module's import graph minimal
    saved_dir = os.path.join(install_dir, "ConanSandbox", "Saved")
    latest = 0.0
    for path in mod_manager.world_save_files(saved_dir):
        try:
            latest = max(latest, os.path.getmtime(path))
        except OSError:
            continue
    return latest


def _wait_for_save_to_settle(install_dir: str, pid: int, quiet: float = SAVE_QUIET_SECONDS,
                             max_wait: float = SAVE_MAX_WAIT_SECONDS) -> None:
    """Returns once the world DB is unchanged for `quiet` seconds, the
    process exits, or `max_wait` passes."""
    deadline = time.monotonic() + max_wait
    last_mtime = _latest_world_save_mtime(install_dir)
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return
        time.sleep(_SAVE_POLL_SECONDS)
        mtime = _latest_world_save_mtime(install_dir)
        now = time.monotonic()
        if mtime != last_mtime:
            last_mtime, last_change = mtime, now
        elif now - last_change >= quiet:
            return


def graceful_stop(server: ServerConfig, timeout: float = 15.0) -> bool:
    """If RCON is enabled, `saveworld` and wait for the save to finish
    writing before stop(), since terminate() is a hard TerminateProcess
    on Windows. `saveworld` does not make the server exit."""
    pid = find_running_pid(server.install_dir)
    if pid is None:
        return True

    if server.rcon_enabled:
        import rcon  # local import: process_manager stays usable without RCON present
        try:
            rcon.send_command("127.0.0.1", server.rcon_port, server.rcon_password, "saveworld")
            _wait_for_save_to_settle(server.install_dir, pid)
        except rcon.RconError:
            pass  # best-effort -- fall through to the hard stop either way

    return stop(server.install_dir, timeout=timeout)


def stop(install_dir: str, timeout: float = 10.0) -> bool:
    pid = find_running_pid(install_dir)
    if pid is None:
        return True
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        proc.wait(timeout=timeout)
        return True
    except psutil.TimeoutExpired:
        try:
            proc.kill()
            # Wait for real exit so ports are free before a relaunch.
            proc.wait(timeout=5.0)
            return True
        except psutil.TimeoutExpired:
            return True  # still didn't confirm exit, but nothing more this function can do -- caller's own backstop (if any) takes over
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return True
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True


def restart(server: ServerConfig) -> subprocess.Popen:
    graceful_stop(server)
    return launch(server)
