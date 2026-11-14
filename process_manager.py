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

import applog

from models import ServerConfig
from proc_utils import hidden_gui_window_kwargs

_log = applog.get_logger(__name__)

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
    # Kept from the launcher this server used before ConanOps: a map/URL
    # argument must come straight after the exe, switches go last.
    own = ("-port=", "-queryport=", "-maxplayers=", "-multihome=")
    extra = [str(a) for a in (getattr(server, "extra_launch_args", None) or []) if str(a).strip()]
    extra = [a for a in extra if a.lower() != "-log" and not a.lower().startswith(own)]
    args[1:1] = [a for a in extra if not a.startswith("-")]
    args += [a for a in extra if a.startswith("-")]
    return args


# exe path -> (pid, start time) of the server process found last. Checked
# first, so the regular "is it running?" checks don't list every process on
# the PC; the start time guards against Windows reusing the pid.
_pid_cache: dict = {}


def _cached_pid(target_exe: str) -> Optional[int]:
    hit = _pid_cache.get(target_exe)
    if hit is None:
        return None
    pid, started = hit
    try:
        proc = psutil.Process(pid)
        if proc.create_time() == started and proc.name() == PROCESS_NAME and proc.is_running():
            return pid
    except (psutil.Error, OSError):
        pass
    _pid_cache.pop(target_exe, None)
    return None


def find_running_pid(install_dir: str) -> Optional[int]:
    """Matches name AND exe path, so two servers on one machine aren't confused.
    Uses its own Process objects rather than psutil.process_iter()'s shared
    cache (see takeover._processes)."""
    target_exe = os.path.normcase(os.path.abspath(server_exe_path(install_dir)))
    pid = _cached_pid(target_exe)
    if pid is not None:
        return pid
    for pid in psutil.pids():
        try:
            proc = psutil.Process(pid)
            with proc.oneshot():  # on Windows name() reads the exe path; read it once
                if proc.name() != PROCESS_NAME:
                    continue
                exe = proc.exe()
                if exe and os.path.normcase(os.path.abspath(exe)) == target_exe:
                    _pid_cache[target_exe] = (pid, proc.create_time())
                    return pid
        except (psutil.Error, OSError):
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


def engine_ini_path(install_dir: str) -> str:
    return os.path.join(install_dir, "ConanSandbox", "Saved", "Config", "WindowsServer", "Engine.ini")


def _sync_keys(path: str, wanted: dict) -> bool:
    import ini_utils  # local: keeps this module's import graph minimal
    try:
        current = ini_utils.read_known_keys(path, {k: sec for k, (sec, _v) in wanted.items()}) \
            if os.path.exists(path) else {}
        if all(str(current.get(k, "")).strip() == v for k, (_sec, v) in wanted.items()):
            return False
        ini_utils.apply_known_keys(path, wanted)
        return True
    except OSError:
        return False  # never block a launch over this


def server_settings_path(install_dir: str) -> str:
    return os.path.join(os.path.dirname(game_ini_path(install_dir)), "ServerSettings.ini")


def sync_server_ini(server: ServerConfig) -> bool:
    """Makes the name, password, ports and player limit in Engine.ini/Game.ini
    match ConanOps' before launch. The server reads its name only from
    Engine.ini, so without this a new server announced itself as "Conan Exiles
    Server" until a setting was saved. Writes only what differs."""
    engine = {
        "ServerName": ("OnlineSubsystemSteam", server.name),
        "ServerPassword": ("OnlineSubsystemSteam", server.password or ""),
        "Port": ("URL", str(server.game_port)),
        "GameServerQueryPort": ("OnlineSubsystemSteam", str(server.query_port)),
    }
    if server.bind_ip:
        engine["MultiHome"] = ("URL", server.bind_ip)
    wrote = _sync_keys(engine_ini_path(server.install_dir), engine)
    # Some setups also have the name and password under [OnlineSubsystem]
    # (Funcom's launcher and older guides write them there). Keep those the
    # same too, so an old value there can't win; never add the section.
    try:
        import ini_utils
        path = engine_ini_path(server.install_dir)
        there = ini_utils.read_known_keys(path, {"ServerName": "OnlineSubsystem", "ServerPassword": "OnlineSubsystem"}) \
            if os.path.exists(path) else {}
    except OSError:
        there = {}
    also = {k: ("OnlineSubsystem", v) for k, v in (("ServerName", server.name), ("ServerPassword", server.password or ""))
            if k in there}
    if also:
        wrote = _sync_keys(engine_ini_path(server.install_dir), also) or wrote
    game = {"MaxPlayers": ("/script/engine.gamesession", str(server.max_players))}
    wrote = _sync_keys(game_ini_path(server.install_dir), game) or wrote
    # A taken-over server's admin password may have come from its old launch
    # options or another file; keep it in the one file ConanOps edits.
    admin = str((getattr(server, "gameplay", None) or {}).get("AdminPassword") or "")
    if getattr(server, "adopted", False) and admin:
        import ini_utils
        settings = server_settings_path(server.install_dir)
        try:
            # Only when the file has none: one set there (by hand, or from the
            # game's own server settings) is newer and must not be overwritten.
            has_one = os.path.exists(settings) and ini_utils.read_known_keys(
                settings, {"AdminPassword": "ServerSettings"}).get("AdminPassword", "").strip()
        except OSError:
            has_one = True
        if not has_one:
            wrote = _sync_keys(settings, {"AdminPassword": ("ServerSettings", admin)}) or wrote
    return wrote


class StillRunningError(RuntimeError):
    """The server couldn't be stopped. Usually it was started with admin
    rights and ConanOps now runs without them, so Windows won't let it end
    the server."""


STILL_RUNNING_MESSAGE = ("The server is still running: Windows didn't let ConanOps stop it. This usually means it "
                         "was started with admin rights and ConanOps now runs without them. Stop it from Task "
                         "Manager, or turn on \"Run with admin rights\" in App Settings.")


def launch(server: ServerConfig) -> Optional[subprocess.Popen]:
    """Starts the server. Does nothing (returns None) if it's already running:
    a second copy on the same folder would fight the first over its world."""
    exe = server_exe_path(server.install_dir)
    if not os.path.exists(exe):
        raise FileNotFoundError(f"Server executable not found: {exe}")
    if find_running_pid(server.install_dir) is not None:
        _log.info(f"Not starting {server.name}: it's already running.")
        return None
    sync_server_ini(server)
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


# What a server says back when it doesn't have the command it was sent.
_SAVE_REFUSED = ("unknown command", "command not found", "couldn't find", "could not find", "not recognized",
                 "invalid command", "no such command")


def request_world_save(server: ServerConfig) -> bool:
    """Asks a running server to save its world over RCON. False if RCON is
    off or unreachable, or if the server's reply says it doesn't know the
    command: a stop or backup that follows then has only the last autosave,
    and the log says so instead of the save being assumed."""
    if not server.rcon_enabled:
        return False
    import rcon  # local import: process_manager stays usable without RCON present
    try:
        reply = rcon.send_command("127.0.0.1", server.rcon_port, server.rcon_password, "saveworld")
    except rcon.RconError as e:
        _log.warning(f"Couldn't ask {server.name} to save its world: {e}")
        return False
    text = str(reply or "").strip()
    if any(marker in text.lower() for marker in _SAVE_REFUSED):
        _log.warning(f"{server.name} didn't accept the save command (it answered: {text[:200]!r}), so the world "
                     "wasn't saved on request. Anything since its last autosave isn't in this stop or backup.")
        return False
    _log.info(f"{server.name} was asked to save its world (it answered: {text[:200]!r}).")
    return True


def graceful_stop(server: ServerConfig, timeout: float = 15.0) -> bool:
    """If RCON is enabled, `saveworld` and wait for the save to finish
    writing before stop(), since terminate() is a hard TerminateProcess
    on Windows. `saveworld` does not make the server exit."""
    pid = find_running_pid(server.install_dir)
    if pid is None:
        return True

    if request_world_save(server):  # best-effort -- the hard stop follows either way
        _wait_for_save_to_settle(server.install_dir, pid)

    return stop(server.install_dir, timeout=timeout)


def stop(install_dir: str, timeout: float = 10.0) -> bool:
    """True once the server has really exited. False if it's still running:
    Windows refused (it runs with admin rights and ConanOps doesn't) or it
    didn't exit even when killed. Callers must not touch its files then."""
    pid = find_running_pid(install_dir)
    if pid is None:
        return True
    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    for end, wait in ((proc.terminate, timeout), (proc.kill, 5.0)):
        try:
            end()
            proc.wait(timeout=wait)  # real exit, so files and ports are free
            return True
        except psutil.NoSuchProcess:
            return True
        except psutil.AccessDenied:
            _log.error(f"Windows didn't let ConanOps stop the server in {install_dir} (it's probably running "
                       "with admin rights).")
            break
        except psutil.TimeoutExpired:
            continue
    try:
        gone = not proc.is_running()
    except psutil.Error:
        gone = True
    if not gone:
        _log.error(f"The server in {install_dir} is still running after ConanOps tried to stop it.")
    return gone


def restart(server: ServerConfig) -> Optional[subprocess.Popen]:
    if graceful_stop(server) is False:
        raise StillRunningError(STILL_RUNNING_MESSAGE)
    return launch(server)
