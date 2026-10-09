"""
Finds Conan Exiles dedicated servers already on this PC that ConanOps
doesn't manage yet, and reads their current settings so taking them over
changes nothing about how they run.

Looks in: running server processes, Steam library folders, and common
install folders on each fixed drive. No Qt here; call off the GUI thread
(the drive scan can take a moment).
"""
from __future__ import annotations

import glob
import os
import re
import string
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import applog
import ini_field_specs
import ini_utils
import process_manager

_log = applog.get_logger(__name__)

APP_FOLDER = "Conan Exiles Dedicated Server"
EXE_NAME = os.path.basename(process_manager.EXE_RELATIVE_PATH).lower()


@dataclass
class FoundServer:
    install_dir: str
    name: str = ""
    running: bool = False
    steamcmd_dir: str = ""
    values: Dict[str, object] = field(default_factory=dict)   # ServerConfig attributes to set
    gameplay: Dict[str, object] = field(default_factory=dict)  # ServerSettings.ini values by key
    mods: List[dict] = field(default_factory=list)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def is_install(path: str) -> bool:
    return os.path.isfile(process_manager.server_exe_path(path))


def _install_from_exe(exe: str) -> Optional[str]:
    parts = os.path.normpath(exe).split(os.sep)
    if len(parts) > 4 and parts[-1].lower() == EXE_NAME:
        return os.sep.join(parts[:-4]) or None
    return None


def _running() -> Dict[str, List[str]]:
    """install dir -> command line, for every running Conan server."""
    import psutil
    found = {}
    for p in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            if (p.info.get("name") or "").lower() != EXE_NAME:
                continue
            install = _install_from_exe(p.info.get("exe") or "")
            if install:
                found[_norm(install)] = p.info.get("cmdline") or []
        except (psutil.Error, OSError):
            continue
    return found


def _steam_libraries() -> List[str]:
    roots = []
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            roots.append(winreg.QueryValueEx(k, name)[0])
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    libs = []
    for root in roots:
        libs.append(root)
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                libs += [p.replace("\\\\", "\\") for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
        except OSError:
            pass
    return libs


def _fixed_drives() -> List[str]:
    if os.name != "nt":
        return []
    drives = []
    for letter in string.ascii_uppercase[2:]:  # skip A: and B:
        root = f"{letter}:\\"
        try:
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                drives.append(root)
        except (AttributeError, OSError):
            if os.path.isdir(root):
                drives.append(root)
    return drives


def candidate_dirs(extra_roots: Optional[List[str]] = None) -> List[str]:
    """Folders that might hold a server install (not yet checked)."""
    cands = []
    for lib in _steam_libraries():
        cands.append(os.path.join(lib, "steamapps", "common", APP_FOLDER))
    for root in _fixed_drives() + list(extra_roots or []):
        # e.g. C:\ConanServer, D:\Servers\Conan, C:\steamcmd\steamapps\common\Conan Exiles Dedicated Server
        for pattern in ("*", os.path.join("*", "*"), os.path.join("*", "steamapps", "common", APP_FOLDER),
                        os.path.join("*", "*", "steamapps", "common", APP_FOLDER)):
            try:
                cands += [d for d in glob.glob(os.path.join(root, pattern)) if "conan" in d.lower() or
                          "steamcmd" in d.lower() or "server" in d.lower()]
            except OSError:
                continue
    return cands


def _ini(install: str, name: str) -> str:
    return os.path.join(install, "ConanSandbox", "Saved", "Config", "WindowsServer", name)


def _arg(cmdline: List[str], key: str) -> str:
    for a in cmdline:
        m = re.match(rf"^-?{key}=(.+)$", a, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def _coerce(spec, raw: str):
    raw = raw.strip()
    try:
        if spec.kind == "bool":
            return raw.lower() in ("true", "1", "yes")
        if spec.kind == "int" or (spec.kind == "choice" and re.fullmatch(r"-?\d+", raw)):
            return int(float(raw))
        if spec.kind == "float":
            return float(raw)
    except ValueError:
        return None
    return raw


def read_server(install: str, cmdline: Optional[List[str]] = None) -> FoundServer:
    """Everything ConanOps can learn about an existing install."""
    found = FoundServer(install_dir=install, running=cmdline is not None)
    cmdline = cmdline or []
    eng = ini_utils.read_known_keys(_ini(install, "Engine.ini"), {
        "ServerName": "OnlineSubsystem", "ServerPassword": "OnlineSubsystem",
        "Port": "URL", "GameServerQueryPort": "OnlineSubsystemSteam"})
    game = ini_utils.read_known_keys(_ini(install, "Game.ini"), {
        "RconEnabled": "RconPlugin", "RconPassword": "RconPlugin", "RconPort": "RconPlugin",
        "MaxPlayers": "/Script/Engine.GameSession"})
    v = found.values
    found.name = eng.get("ServerName", "").strip() or os.path.basename(install.rstrip("\\/")) or "Conan Server"

    def num(*vals) -> Optional[int]:
        for x in vals:
            if x and re.fullmatch(r"\s*\d+\s*", str(x)):
                return int(x)
        return None
    for attr, value in (("game_port", num(_arg(cmdline, "Port"), eng.get("Port"))),
                        ("query_port", num(_arg(cmdline, "QueryPort"), eng.get("GameServerQueryPort"))),
                        ("max_players", num(_arg(cmdline, "MaxPlayers"), game.get("MaxPlayers"))),
                        ("rcon_port", num(game.get("RconPort")))):
        if value:
            v[attr] = value
    if "ServerPassword" in eng:
        v["password"] = eng["ServerPassword"].strip()
    if "RconEnabled" in game:
        v["rcon_enabled"] = game["RconEnabled"].strip().lower() in ("true", "1")
    if game.get("RconPassword"):
        v["rcon_password"] = game["RconPassword"].strip()
    bind = _arg(cmdline, "MULTIHOME")
    if bind:
        v["bind_ip"] = bind

    wanted = {k: spec.section for k, spec in ini_field_specs.ALL_FIELDS_BY_KEY.items() if not k.startswith("__")}
    for key, raw in ini_utils.read_known_keys(_ini(install, "ServerSettings.ini"), wanted).items():
        value = _coerce(ini_field_specs.ALL_FIELDS_BY_KEY[key], raw)
        if value is not None:
            found.gameplay[key] = value

    # SteamCMD installs put the server at <steamcmd>\steamapps\common\<app>.
    parts = os.path.normpath(install).split(os.sep)
    if len(parts) > 3 and [p.lower() for p in parts[-3:-1]] == ["steamapps", "common"]:
        sc = os.sep.join(parts[:-3])
        if os.path.isfile(os.path.join(sc, "steamcmd.exe")):
            found.steamcmd_dir = sc

    # Mods: modlist.txt lines are pak paths; Workshop paks sit in .../440900/<id>/.
    try:
        with open(os.path.join(install, "ConanSandbox", "Mods", "modlist.txt"), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.search(r"440900[\\/](\d+)[\\/]", line)
                if m and not any(x["id"] == m.group(1) for x in found.mods):
                    name = os.path.splitext(re.split(r"[\\/]", line.strip())[-1])[0]
                    found.mods.append({"id": m.group(1), "name": name, "enabled": True})
    except OSError:
        pass
    return found


def find_unmanaged(managed_dirs: List[str], ignored_dirs: List[str] = (), extra_roots=None) -> List[FoundServer]:
    """Servers on this PC that aren't managed (or dismissed) yet; running ones first."""
    skip = {_norm(d) for d in list(managed_dirs) + list(ignored_dirs) if d}
    running = {}
    try:
        running = _running()
    except Exception as e:  # noqa: BLE001 - a process-list hiccup must not stop the scan
        _log.warning(f"Couldn't list running servers: {e}")
    seen, out = set(), []
    for d in list(running) + candidate_dirs(extra_roots):
        nd = _norm(d)
        if nd in seen or nd in skip:
            continue
        seen.add(nd)
        if is_install(d):
            try:
                out.append(read_server(d, running.get(nd)))
            except Exception as e:  # noqa: BLE001
                _log.warning(f"Couldn't read the server at {d}: {e}")
    out.sort(key=lambda f: not f.running)
    return out
