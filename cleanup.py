"""
Leave-no-trace removal for "remove this server" and "Delete Everything".

Files: server_paths() (per server), owned_roots() (ConanOps data folders),
temp_leftovers(); the program folder is self_delete.py's job. Windows
settings (firewall rules, scheduled tasks, active hours, VC++ runtime) are
undone by one elevated cleanup_script(). Every delete goes through
is_safe_to_delete(), so drive roots and standard user/system folders are never removed.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from typing import Iterable, List, Optional

import applog
import conanops_paths
import powershell

_log = applog.get_logger(__name__)

_BACKUP_NAME_RE = re.compile(r"^\d{8}-\d{6}_.+\.zip$", re.IGNORECASE)
# Marks a _MEIxxxxx temp folder as ours rather than another PyInstaller app's.
_OWN_MEI_MARKER = os.path.join("assets", "conanops-icon-small.svg")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _within(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([_norm(child), _norm(parent)]) == _norm(parent)
    except ValueError:
        return False


def _protected_paths() -> set:
    home = os.path.expanduser("~")
    env = os.environ
    paths = {home, os.path.dirname(home)}
    for name in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos", "AppData",
                 "OneDrive", "Saved Games", "Favorites", "Links", "Contacts", "Searches"):
        paths.add(os.path.join(home, name))
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData",
                "PUBLIC", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP", "OneDrive"):
        if env.get(var):
            paths.add(env[var])
    paths.add(tempfile.gettempdir())
    return {_norm(p) for p in paths if p}


def is_safe_to_delete(path: str) -> bool:
    """False for drive roots, home and its standard folders, system folders,
    the temp folder, and anything containing those."""
    if not path:
        return False
    p = _norm(path)
    drive, rest = os.path.splitdrive(p)
    if rest.strip("\\/") == "" or p == _norm(os.sep):
        return False
    protected = _protected_paths()
    if p in protected:
        return False
    return not any(_within(q, p) for q in protected)


def _onerror(func, path, exc_info):
    # Read-only files (common in Steam content) make rmtree fail on Windows.
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise exc_info[1]


def _long(path: str) -> str:
    """Adds the \\\\?\\ prefix on Windows so paths over 260 chars can be deleted."""
    if sys.platform == "win32":
        p = os.path.abspath(path)
        if not p.startswith("\\\\?\\"):
            return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
        return p
    return path


def remove_path(path: str, retries: int = 3) -> Optional[str]:
    """Returns None on success or if already gone, else an error message.
    Retries because a just-stopped server can hold its files briefly."""
    if not path or not os.path.lexists(path):
        return None
    if not is_safe_to_delete(path):
        return f"Skipped {path} -- ConanOps won't delete a folder like that."
    last = None
    for attempt in range(retries):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(_long(path), onerror=_onerror)
            else:
                try:
                    os.chmod(path, stat.S_IWRITE)
                except OSError:
                    pass
                os.remove(path)
            return None
        except FileNotFoundError:
            return None
        except OSError as e:
            last = e
            time.sleep(1.0 * (attempt + 1))
    return f"Couldn't delete {path}: {last}"


def remove_backups(folder: str, owned: bool) -> List[str]:
    """Owned folder: delete it all. Otherwise delete only our backup zips,
    and the folder only if that leaves it empty."""
    if not folder or not os.path.isdir(folder):
        return []
    if owned:
        err = remove_path(folder)
        return [err] if err else []
    errors = []
    for f in os.listdir(folder):
        if _BACKUP_NAME_RE.match(f):
            err = remove_path(os.path.join(folder, f))
            if err:
                errors.append(err)
    try:
        if not os.listdir(folder) and is_safe_to_delete(folder):
            os.rmdir(folder)
    except OSError:
        pass
    return errors


def owned_roots() -> List[str]:
    """ConanOps' own data folders that exist (not the program folder)."""
    candidates = [
        conanops_paths.no_space_root(),
        os.path.join(os.path.expanduser("~"), "ConanOps"),
        conanops_paths.public_fallback_root(),
    ]
    out, seen = [], set()
    for c in candidates:
        n = _norm(c)
        if n in seen or not os.path.isdir(c) or not is_safe_to_delete(c):
            continue
        # A generically named folder only counts if it holds our files.
        if os.path.basename(c.rstrip("\\/")).lower() != "conanops" and not any(
                os.path.exists(os.path.join(c, m)) for m in ("config.json", "conanops.lock", "theme.json")):
            continue
        seen.add(n)
        out.append(c)
    return out


def is_owned_location(path: str) -> bool:
    return any(_within(path, root) for root in owned_roots())


def sessions_file(server_id: str) -> str:
    return os.path.join(os.path.expanduser("~"), "ConanOps", "sessions", f"{server_id}.json")


def server_paths(server, other_servers: Iterable) -> dict:
    """{"files": [...], "backups": (folder, owned) or None, "always": [...]},
    skipping paths other servers use. "always" goes even when files are kept."""
    others = list(other_servers)
    other_paths = [p for o in others for p in (o.install_dir, o.steamcmd_dir, o.backup_destination) if p]

    def used_by_others(path: str) -> bool:
        return any(_within(op, path) or _within(path, op) for op in other_paths)

    files = []
    if server.install_dir and not used_by_others(server.install_dir):
        files.append(server.install_dir)
    if server.steamcmd_dir and not used_by_others(server.steamcmd_dir):
        files.append(server.steamcmd_dir)
    # The per-server folder we created around them (<data>\<id>\).
    for p in (server.install_dir, server.steamcmd_dir):
        parent = os.path.dirname(os.path.abspath(p)) if p else ""
        if parent and os.path.basename(parent) == server.id and is_owned_location(parent) \
                and not used_by_others(parent) and parent not in files:
            files.append(parent)

    backups = None
    if server.backup_destination and not used_by_others(server.backup_destination):
        owned = is_owned_location(server.backup_destination) or any(
            _within(server.backup_destination, f) for f in files
        )
        backups = (server.backup_destination, owned)
    return {"files": files, "backups": backups, "always": [sessions_file(server.id)]}


def temp_leftovers() -> List[str]:
    tmp = tempfile.gettempdir()
    found = []
    for pattern in ("conanops-*", "conanops_uninstall_*.bat"):
        found.extend(glob.glob(os.path.join(tmp, pattern)))
    current = _norm(getattr(sys, "_MEIPASS", "") or "")
    for d in glob.glob(os.path.join(tmp, "_MEI*")):
        if _norm(d) != current and os.path.exists(os.path.join(d, _OWN_MEI_MARKER)):
            found.append(d)
    return found


def cleanup_script(server_ids: Iterable[str] = (), legacy_names: Iterable[str] = (),
                   program_dirs: Iterable[str] = (), remove_task: bool = False,
                   restore_active_hours: Optional[dict] = None, uninstall_vcredist: bool = False) -> str:
    """Elevated PowerShell undoing our Windows changes; each part is
    independent and best-effort."""
    import network_setup
    q = powershell.ps_str
    parts = [network_setup._remove_script(list(server_ids), list(legacy_names))]

    dirs = [os.path.abspath(d).rstrip("\\/") for d in program_dirs if d]
    if dirs:
        # Allow/block rules Windows itself made from "allow access?" popups.
        parts.append(
            f"$dirs = {powershell.ps_array(dirs)}\n"
            "try { Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | Where-Object { "
            "$p = [Environment]::ExpandEnvironmentVariables([string]$_.Program); "
            "$p -and ($dirs | Where-Object { $p.StartsWith($_ + '\\', [StringComparison]::OrdinalIgnoreCase) }) "
            "} | Get-NetFirewallRule -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue } catch {}\n"
        )
    if remove_task:
        parts.append("try {\n" + network_setup.web_rule_remove_script() + "} catch {}\n")
        import background_mode
        import keep_alive
        parts.append("try {\n" + keep_alive.unregister_script() + "} catch {}\n")
        import admin_mode
        parts.append("try {\n" + admin_mode.unregister_script() + "} catch {}\n")
        parts.append(
            f"try {{ Get-ScheduledTask -TaskName {q(background_mode.TASK_NAME)} -TaskPath {q(background_mode.TASK_PATH)} "
            "-ErrorAction SilentlyContinue | Unregister-ScheduledTask -Confirm:$false } catch {}\n"
            f"try {{ $f = (New-Object -ComObject Schedule.Service); $f.Connect(); "
            f"$f.GetFolder('\\').DeleteFolder({q(background_mode.TASK_PATH.strip(chr(92)))}, 0) }} catch {{}}\n"
        )
    if restore_active_hours:
        import windows_update
        parts.append(windows_update.restore_script(restore_active_hours))
    if uninstall_vcredist:
        import vcredist
        parts.append(vcredist.uninstall_script())
    parts.append("exit 0\n")
    return "".join(parts)
