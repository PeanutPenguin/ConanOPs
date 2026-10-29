"""
Finds Conan Exiles dedicated servers already on this PC that ConanOps
doesn't manage yet, and reads their current settings so taking them over
changes nothing about how they run.

Looks in: running server processes, Steam library folders (including
appmanifest_443030.acf), folders named by other launchers (scripts,
scheduled tasks, services -- see takeover.py), common install folders, and
(deep scan) a bounded walk of every fixed drive. No Qt here; call off the
GUI thread.
"""
from __future__ import annotations

import os
import re
import string
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import applog
import ini_field_specs
import ini_utils
import process_manager

_log = applog.get_logger(__name__)

APP_ID = 443030
APP_FOLDER = "Conan Exiles Dedicated Server"
EXE_NAME = os.path.basename(process_manager.EXE_RELATIVE_PATH).lower()

# Launch arguments ConanOps sets itself (build_launch_args); everything else
# an existing launcher used is kept in ServerConfig.extra_launch_args.
_OWN_ARGS = ("port", "queryport", "maxplayers", "multihome", "log")
# These move the Saved folder (world, settings, logs) somewhere ConanOps
# doesn't look, so taking such a server over could start an empty world.
_BLOCKING_ARGS = ("usedir", "userdir", "saveddir", "savedir")

# Deep scan limits: enough for D:\Games\Servers\Conan\Server\..., but never
# a multi-minute crawl of a full drive.
DEEP_MAX_DEPTH = 6
DEEP_MAX_DIRS = 80_000
DEEP_MAX_SECONDS = 25.0
_SKIP_DIR_NAMES = {
    "windows", "$recycle.bin", "system volume information", "recovery", "perflogs", "$windows.~bt",
    "$windows.~ws", "$winreagent", "windowsapps", "winsxs", "appdata", "node_modules", ".git", ".svn",
    "__pycache__", "shadercache", "downloading", "temp", "msocache", "config.msi", "intel", "amd", "nvidia",
}


@dataclass
class FoundServer:
    install_dir: str
    name: str = ""
    running: bool = False
    steamcmd_dir: str = ""
    values: Dict[str, object] = field(default_factory=dict)   # ServerConfig attributes to set
    gameplay: Dict[str, object] = field(default_factory=dict)  # ServerSettings.ini values by key
    mods: List[dict] = field(default_factory=list)            # Workshop mods, in modlist order
    local_mods: List[dict] = field(default_factory=list)      # non-Workshop modlist lines: {"line", "after"}
    extra_args: List[str] = field(default_factory=list)       # launch args ConanOps doesn't set itself
    warnings: List[str] = field(default_factory=list)          # shown before taking it over
    blocker: str = ""                                          # non-empty: can't be taken over safely
    steam_client: bool = False                                 # installed by the Steam app, not SteamCMD
    rivals: list = field(default_factory=list)                 # takeover.Rival entries that also run it


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def is_install(path: str) -> bool:
    return bool(path) and os.path.isfile(process_manager.server_exe_path(path))


def install_root_for(path: str) -> Optional[str]:
    """The install folder for something the person picked: the install
    itself, any folder inside it, the server exe, or a parent holding
    exactly one install a level or two down. None if it isn't one."""
    if not path:
        return None
    p = os.path.abspath(path.strip().strip('"'))
    if os.path.isfile(p):
        p = os.path.dirname(p)
    probe = p
    for _ in range(6):  # walk up from ...\ConanSandbox\Binaries\Win64 and the like
        if is_install(probe):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    # A folder holding the install: e.g. C:\steamcmd or ...\steamapps\common.
    hits = []
    for sub in ("*", os.path.join("*", "*"), os.path.join("steamapps", "common", "*")):
        try:
            import glob
            hits += [d for d in glob.glob(os.path.join(glob.escape(p), sub)) if is_install(d)]
        except OSError:
            continue
    unique = sorted({_norm(h): h for h in hits}.values())
    return unique[0] if len(unique) == 1 else None


_EXE_TAIL = re.compile(r"^(.+?)[\\/]+ConanSandbox[\\/]+Binaries[\\/]+Win64[\\/]+"
                       r"ConanSandboxServer-Win64-Shipping\.exe$", re.IGNORECASE)


def _install_from_exe(exe: str) -> Optional[str]:
    """The install folder for a server exe path (either slash style)."""
    m = _EXE_TAIL.match((exe or "").strip().strip('"'))
    return m.group(1) if m else None


def path_key(p: str) -> str:
    """A path as Windows compares it: one slash style, no trailing slash, any case."""
    p = (p or "").strip().strip('"')
    if not p:
        return ""
    if os.name == "nt":
        p = os.path.abspath(p)
    return re.sub(r"[\\/]+", lambda _m: "\\", p).rstrip("\\").casefold()


def same_path(a: str, b: str) -> bool:
    """Compares paths as Windows does, whichever slash or case they use."""
    return bool(a) and bool(b) and path_key(a) == path_key(b)


@dataclass
class RunningScan:
    by_install: Dict[str, List[str]] = field(default_factory=dict)  # normalized install dir -> cmdline
    unreadable: int = 0  # servers running whose folder Windows wouldn't show us (started as admin)


def scan_running() -> RunningScan:
    import psutil
    out = RunningScan()
    # Own Process objects, not process_iter()'s shared cache (see takeover._processes).
    for pid in psutil.pids():
        try:
            p = psutil.Process(pid)
            if (p.name() or "").lower() != EXE_NAME:
                continue
        except (psutil.Error, OSError):
            continue  # gone already, or not ours to read: not a server we can see
        try:
            try:
                exe = p.exe()
            except (psutil.Error, OSError):
                exe = ""
            try:
                cmdline = p.cmdline()
            except (psutil.Error, OSError):
                cmdline = []
            install = _install_from_exe(exe) or (_install_from_exe(cmdline[0]) if cmdline else None)
            if install:
                out.by_install[_norm(install)] = cmdline
            else:
                out.unreadable += 1
        except (psutil.Error, OSError):
            out.unreadable += 1
    return out


def _running() -> Dict[str, List[str]]:
    """install dir -> command line, for every running Conan server."""
    return scan_running().by_install


def _steam_roots() -> List[str]:
    roots = []
    try:
        import winreg
        for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                          (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam")):
            try:
                with winreg.OpenKey(hive, key) as k:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            roots.append(os.path.normpath(winreg.QueryValueEx(k, name)[0]))
                        except OSError:
                            pass
            except OSError:
                pass
    except ImportError:
        pass
    return list({_norm(r): r for r in roots}.values())


def _steam_libraries() -> List[str]:
    libs = []
    for root in _steam_roots():
        libs.append(root)
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, encoding="utf-8", errors="replace") as f:
                libs += [os.path.normpath(p.replace("\\\\", "\\"))
                         for p in re.findall(r'"path"\s+"([^"]+)"', f.read())]
        except OSError:
            pass
    return list({_norm(lib): lib for lib in libs}.values())


def _library_installs(lib: str) -> List[str]:
    """Conan server folders in one Steam/SteamCMD library: the folder its
    appmanifest names, plus any steamapps\\common folder that looks like one
    (Funcom has renamed the folder before)."""
    common = os.path.join(lib, "steamapps", "common")
    out = []
    manifest = os.path.join(lib, "steamapps", f"appmanifest_{APP_ID}.acf")
    try:
        with open(manifest, encoding="utf-8", errors="replace") as f:
            m = re.search(r'"installdir"\s+"([^"]+)"', f.read())
        if m:
            out.append(os.path.join(common, m.group(1)))
    except OSError:
        pass
    out.append(os.path.join(common, APP_FOLDER))
    try:
        for entry in os.scandir(common):
            low = entry.name.lower()
            if entry.is_dir() and "conan" in low and "server" in low:
                out.append(entry.path)
    except OSError:
        pass
    return out


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


def _shallow_candidates(root: str) -> List[str]:
    """Quick look one and two levels down, e.g. C:\\ConanServer, D:\\Servers\\Conan."""
    out = []
    try:
        level1 = [e for e in os.scandir(root) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return out
    for e in level1:
        if e.name.lower() in _SKIP_DIR_NAMES:
            continue
        out.append(e.path)
        out += _library_installs(e.path)  # C:\steamcmd\steamapps\common\...
        try:
            for e2 in os.scandir(e.path):
                if e2.is_dir(follow_symlinks=False) and e2.name.lower() not in _SKIP_DIR_NAMES:
                    out.append(e2.path)
                    if "steam" in e2.name.lower():
                        out += _library_installs(e2.path)
        except OSError:
            continue
    return out


def deep_walk(roots: Iterable[str], max_depth: int = DEEP_MAX_DEPTH, max_dirs: int = DEEP_MAX_DIRS,
              max_seconds: float = DEEP_MAX_SECONDS) -> List[str]:
    """Breadth-first walk for folders containing ConanSandbox\\Binaries,
    skipping system and cache folders, links, and anything past the limits."""
    found: List[str] = []
    queue = [(r, 0) for r in roots]
    seen = 0
    deadline = time.monotonic() + max_seconds
    while queue and seen < max_dirs and time.monotonic() < deadline:
        path, depth = queue.pop(0)
        seen += 1
        try:
            entries = [e for e in os.scandir(path) if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        names = {e.name.lower() for e in entries}
        if "conansandbox" in names:
            if is_install(path):
                found.append(path)
            continue  # never walk inside a server install
        if depth >= max_depth:
            continue
        for e in entries:
            low = e.name.lower()
            if low in _SKIP_DIR_NAMES or low.startswith("$"):
                continue
            if low == "workshop" and os.path.basename(path).lower() == "steamapps":
                continue  # mods, never servers
            queue.append((e.path, depth + 1))
    if queue:
        _log.info(f"Deep server scan stopped at its limit after {seen} folders.")
    return found


def candidate_dirs(extra_roots: Optional[List[str]] = None, deep: bool = False,
                   hints: Iterable[str] = ()) -> List[str]:
    """Folders that might hold a server install (not yet checked)."""
    cands = list(hints)
    for lib in _steam_libraries():
        cands += _library_installs(lib)
    roots = _fixed_drives() + list(extra_roots or [])
    home = os.path.expanduser("~")
    user_roots = [os.path.join(home, d) for d in ("Desktop", "Documents", "Downloads")]
    for root in roots + user_roots:
        cands += _shallow_candidates(root)
    if deep:
        cands += deep_walk(roots + user_roots)
    return cands


def _ini(install: str, name: str) -> str:
    return os.path.join(install, "ConanSandbox", "Saved", "Config", "WindowsServer", name)


def _arg(cmdline: List[str], key: str) -> str:
    for a in cmdline:
        m = re.match(rf"^-?{key}=(.+)$", a.strip(), re.IGNORECASE)
        if m:
            return m.group(1).strip().strip('"')
    return ""


def split_args(cmdline: List[str]) -> tuple:
    """(extra args to keep, blocker message) from a server command line,
    minus the exe and the arguments ConanOps sets itself."""
    extra, blocker = [], ""
    first_is_exe = bool(cmdline) and cmdline[0].strip().strip('"').lower().endswith(".exe")
    for a in cmdline[1:] if first_is_exe else cmdline:
        a = a.strip()
        if not a:
            continue
        key = re.match(r"^-?([A-Za-z_]+)", a)
        k = key.group(1).lower() if key else ""
        if a.startswith("-") and k in _OWN_ARGS:
            continue
        if a.startswith("-") and k in _BLOCKING_ARGS:
            blocker = (f"It's started with {a.split('=')[0]}, which keeps its world and settings in another "
                       f"folder. ConanOps can't manage that yet, so it's left alone.")
        extra.append(a)
    return extra, blocker


def windows_split(command: str) -> List[str]:
    """Splits a Windows command line the way most programs read it."""
    out, cur, quoted, any_char = [], [], False, False
    for ch in command:
        if ch == '"':
            quoted = not quoted
            any_char = True
        elif ch in " \t" and not quoted:
            if cur or any_char:
                out.append("".join(cur))
            cur, any_char = [], False
        else:
            cur.append(ch)
            any_char = True
    if cur or any_char:
        out.append("".join(cur))
    return out


# The path may hold spaces but never another "X:\", so "nssm.exe E:\Srv\..." starts at E:.
_EXE_IN_TEXT = re.compile(r'"?([A-Za-z]:\\(?:(?![A-Za-z]:\\)[^"\r\n*?<>|])*?\\ConanSandbox\\Binaries\\Win64\\'
                          r'ConanSandboxServer-Win64-Shipping\.exe)"?([^\r\n&|]*)', re.IGNORECASE)


def launch_line_in(text: str, install_dir: str) -> Optional[List[str]]:
    """The server command line a script or task uses for this install, if any."""
    for m in _EXE_IN_TEXT.finditer(text or ""):
        if same_path(_install_from_exe(m.group(1)) or "", install_dir):
            return [m.group(1)] + windows_split(m.group(2).strip())
    return None


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


def _read_mods(found: FoundServer) -> None:
    """Workshop mods become ConanOps mods (keeping the .pak they point at, so
    they still load before ConanOps downloads its own copy); anything else
    in modlist.txt is kept as a line, in its place."""
    path = os.path.join(found.install_dir, "ConanSandbox", "Mods", "modlist.txt")
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            lines = [ln.rstrip("\r\n") for ln in f]
    except OSError:
        return
    last_id, missing = "", []
    mods_dir = os.path.dirname(path)
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        pak = line.lstrip("*").strip().strip('"')
        m = re.search(r"440900[\\/](\d+)[\\/]", pak)
        if m:
            wid = m.group(1)
            if not any(x["id"] == wid for x in found.mods):
                name = os.path.splitext(re.split(r"[\\/]", pak)[-1])[0]
                found.mods.append({"id": wid, "name": name, "enabled": True, "pak": pak})
                last_id = wid
            continue
        found.local_mods.append({"line": line, "after": last_id})
        full = pak if os.path.isabs(pak) else os.path.join(mods_dir, pak)
        if not os.path.isfile(full):
            missing.append(os.path.basename(pak))
    if found.local_mods:
        names = ", ".join(os.path.basename(x["line"].lstrip("*")) for x in found.local_mods[:4])
        more = f" and {len(found.local_mods) - 4} more" if len(found.local_mods) > 4 else ""
        found.warnings.append(f"Keeps {len(found.local_mods)} mod file{'s' if len(found.local_mods) != 1 else ''} "
                              f"that aren't from the Steam Workshop ({names}{more}) in the mod list as they are.")
    if missing:
        found.warnings.append(f"modlist.txt lists files that aren't there: {', '.join(missing[:4])}.")


def read_server(install: str, cmdline: Optional[List[str]] = None, launcher_line: Optional[List[str]] = None) -> FoundServer:
    """Everything ConanOps can learn about an existing install. `cmdline` is
    the running process's; `launcher_line` is the one a script or task uses."""
    found = FoundServer(install_dir=install, running=cmdline is not None)
    args = list(cmdline) if cmdline else list(launcher_line or [])
    ini_eng = _ini(install, "Engine.ini")
    # Conan reads [OnlineSubsystemSteam]; older guides (and ConanOps 1.0.10's
    # reader) used [OnlineSubsystem], so take that as a fallback.
    keys = ("ServerName", "ServerPassword", "GameServerQueryPort")
    eng = ini_utils.read_known_keys(ini_eng, {k: "OnlineSubsystem" for k in keys})
    eng.update(ini_utils.read_known_keys(ini_eng, {k: "OnlineSubsystemSteam" for k in keys}))
    eng.update(ini_utils.read_known_keys(ini_eng, {"Port": "URL", "MultiHome": "URL"}))
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
    for attr, value in (("game_port", num(_arg(args, "Port"), eng.get("Port"))),
                        ("query_port", num(_arg(args, "QueryPort"), eng.get("GameServerQueryPort"))),
                        ("max_players", num(_arg(args, "MaxPlayers"), game.get("MaxPlayers"))),
                        ("rcon_port", num(game.get("RconPort")))):
        if value:
            v[attr] = value
    if "ServerPassword" in eng:
        v["password"] = eng["ServerPassword"].strip()
    if "RconEnabled" in game:
        v["rcon_enabled"] = game["RconEnabled"].strip().lower() in ("true", "1")
    if game.get("RconPassword"):
        v["rcon_password"] = game["RconPassword"].strip()
    bind = _arg(args, "MULTIHOME") or eng.get("MultiHome", "").strip()
    if bind:
        v["bind_ip"] = bind
    found.extra_args, found.blocker = split_args(args)

    wanted = {k: spec.section for k, spec in ini_field_specs.ALL_FIELDS_BY_KEY.items() if not k.startswith("__")}
    for key, raw in ini_utils.read_known_keys(_ini(install, "ServerSettings.ini"), wanted).items():
        value = _coerce(ini_field_specs.ALL_FIELDS_BY_KEY[key], raw)
        if value is not None:
            found.gameplay[key] = value

    # SteamCMD installs put the server at <steamcmd>\steamapps\common\<app>;
    # the Steam app puts it in one of its libraries.
    parts = os.path.normpath(install).split(os.sep)
    if len(parts) > 3 and [p.lower() for p in parts[-3:-1]] == ["steamapps", "common"]:
        lib = os.sep.join(parts[:-3])
        if os.path.isfile(os.path.join(lib, "steamcmd.exe")):
            found.steamcmd_dir = lib
        elif _norm(lib) in {_norm(x) for x in _steam_libraries()} or os.path.isfile(os.path.join(lib, "steam.exe")):
            found.steam_client = True
            found.warnings.append("It was installed with the Steam app, which may also update it. ConanOps "
                                  "updates it from now on; in Steam you can set it to update only when launched.")

    _read_mods(found)
    # Mods downloaded by another SteamCMD: use that one so they keep updating in place.
    if not found.steamcmd_dir:
        for m in found.mods:
            pm = re.match(r"^(.*?)[\\/]steamapps[\\/]workshop[\\/]", m.get("pak", ""), re.IGNORECASE)
            if pm and os.path.isfile(os.path.join(pm.group(1), "steamcmd.exe")):
                found.steamcmd_dir = pm.group(1)
                break

    saved = os.path.join(install, "ConanSandbox", "Saved")
    if not found.blocker and not os.path.isdir(saved):
        found.warnings.append("It has no world save yet, so it will start a new world.")
    return found


def find_unmanaged(managed_dirs: List[str], ignored_dirs: List[str] = (), extra_roots=None,
                   deep: bool = False, hints: Iterable[str] = (), running: Optional[RunningScan] = None,
                   launch_lines: Optional[Dict[str, List[str]]] = None) -> List[FoundServer]:
    """Servers on this PC that aren't managed (or dismissed) yet; running ones first.
    `launch_lines`: normalized install dir -> command line found in a launcher."""
    skip = {_norm(d) for d in list(managed_dirs) + list(ignored_dirs) if d}
    if running is None:
        try:
            running = RunningScan(by_install=_running())
        except Exception as e:  # noqa: BLE001 - a process-list hiccup must not stop the scan
            _log.warning(f"Couldn't list running servers: {e}")
            running = RunningScan()
    seen, out = set(), []
    for d in list(running.by_install) + candidate_dirs(extra_roots, deep=deep, hints=hints):
        nd = _norm(d)
        if nd in seen or nd in skip:
            continue
        seen.add(nd)
        if is_install(d):
            try:
                out.append(read_server(d, running.by_install.get(nd), (launch_lines or {}).get(path_key(d))))
            except Exception as e:  # noqa: BLE001
                _log.warning(f"Couldn't read the server at {d}: {e}")
    out.sort(key=lambda f: (bool(f.blocker), not f.running))
    return out
