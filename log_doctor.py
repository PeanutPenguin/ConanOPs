"""
Reads a Conan Exiles server's own records -- its latest log, its crash
reports and its modlist.txt -- and explains in plain language why it won't
start or keeps crashing ("a mod's file is missing", "port already in use",
"ran out of memory").

Read-only, and no Qt: Diagnostics and the crash alerts call it off the GUI
thread. Matching is by pattern, so it explains the common causes; anything
unrecognised is reported with the server's own error text.
"""
from __future__ import annotations

import glob
import os
import re
import time
from dataclasses import dataclass
from typing import List, Optional

import applog

_log = applog.get_logger(__name__)

STATUS_WARNING = "warning"
STATUS_ERROR = "error"

# Fix actions the Diagnostics page offers (see ui/diagnostics_page.py).
FIX_FREE_PORT = "free_port"
FIX_DOWNLOAD_MODS = "download_mods"
FIX_FIND_BAD_MOD = "fix_mods"
FIX_RESTORE_BACKUP = "restore_backup"
FIX_REPAIR_FILES = "repair_files"
FIX_INSTALL_VC = "install_vc"
FIX_DISABLE_BATTLEYE = "disable_battleye"

LOG_TAIL_BYTES = 4 * 1024 * 1024  # the end of a log is where a crash is
CRASH_WINDOW_SECONDS = 24 * 3600
_CONTEXT_LINES = 40  # lines after a fatal error searched for the mod it happened in


@dataclass
class Finding:
    key: str
    status: str
    title: str
    message: str
    detail: str = ""
    fix: str = ""


@dataclass
class _Rule:
    key: str
    status: str
    title: str
    pattern: "re.Pattern"
    message: str
    fix: str = ""


# Most specific first: one line can match several rules, and the first wins.
_RULES = [
    _Rule("disk_full", STATUS_ERROR, "Disk full",
          re.compile(r"not enough space on the disk|disk (is )?full|ERROR_DISK_FULL|no space left on device", re.I),
          "The server ran out of disk space while it was writing. Free up space on the drive it's installed on, "
          "then start it again."),
    _Rule("world_corrupt", STATUS_ERROR, "World save damaged",
          re.compile(r"database disk image is malformed|SQLITE_CORRUPT|file is not a database|SQLITE_NOTADB", re.I),
          "The world save file is damaged, so the server can't load it. Restore the latest backup to get the world "
          "back as it was when that backup was made.", FIX_RESTORE_BACKUP),
    _Rule("world_locked", STATUS_ERROR, "World save in use",
          re.compile(r"database is locked|SQLITE_BUSY", re.I),
          "Another program had the world save open, so the server couldn't use it. This usually means a second "
          "copy of the server (or a backup tool) was running from the same folder. Make sure only one copy runs, "
          "then start it again."),
    _Rule("out_of_memory", STATUS_ERROR, "Ran out of memory",
          re.compile(r"ran out of memory|\bout of memory\b|OutOfMemory", re.I),
          "The server ran out of memory and stopped. Close other programs on this PC, run fewer servers or mods at "
          "once, or add more RAM. Large or heavily modded servers can need 12-16 GB."),
    _Rule("port_in_use", STATUS_ERROR, "Port already in use",
          re.compile(r"address already in use|WSAEADDRINUSE|SE_EADDRINUSE|(could ?n[o']t|unable to|failed to) bind"
                     r"|bind(ing)? (to )?(port|socket)\b.*fail|Failed to (init|initiali[sz]e) net driver"
                     r"|Failed to listen", re.I),
          "Another program was already using one of the server's ports, so it couldn't open them. Use a free port, "
          "or close whatever else is using it (often a second copy of the server).", FIX_FREE_PORT),
    _Rule("steam_init", STATUS_ERROR, "Couldn't connect to Steam",
          re.compile(r"SteamAPI_Init\(\) failed|SteamGameServer_Init\w* failed|Steam API (failed|could ?n[o']t)"
                     r"|failed to init(iali[sz]e)? steam|steamclient(64)?\.dll.*(not found|missing|failed)", re.I),
          "The server couldn't start its Steam connection. This is usually a missing or damaged Steam file in the "
          "server folder. Having Steam check the server's files fixes it in most cases.", FIX_REPAIR_FILES),
    _Rule("missing_dll", STATUS_ERROR, "Missing system file",
          re.compile(r"(vcruntime140(_1)?|msvcp140)\.dll\b.*(not found|missing|could ?n[o']t)"
                     r"|(not found|missing|could ?n[o']t (find|load)).*(vcruntime140|msvcp140)", re.I),
          "A Microsoft Visual C++ runtime file the server needs is missing. Installing the Visual C++ runtime "
          "fixes it.", FIX_INSTALL_VC),
    _Rule("mod_missing", STATUS_ERROR, "Mod file missing",
          re.compile(r"Failed to mount pak|Could ?n[o']t (find|open|mount) (the )?pak|pak file .*(not found|does not exist)"
                     r"|LogModController.*(missing|not found|failed)", re.I),
          "The server couldn't load one of its mods because the mod's file is missing. Downloading the mods again "
          "puts the files back.", FIX_DOWNLOAD_MODS),
    _Rule("battleye", STATUS_WARNING, "BattlEye problem",
          re.compile(r"BattlEye\b.*(fail|error|could ?n[o']t|unable)", re.I),
          "BattlEye (the anti-cheat) reported a problem starting. If the server keeps failing because of it, turn "
          "BattlEye off for this server.", FIX_DISABLE_BATTLEYE),
]
_FATAL = re.compile(r"Fatal error!?|Critical error|Assertion failed|Unhandled Exception|EXCEPTION_ACCESS_VIOLATION"
                    r"|appError called", re.I)
_MOD_IN_STACK = re.compile(r"/Game/Mods/([^/\s'\".:]+)", re.I)
_CLEAN_EXIT = re.compile(r"LogExit: Exiting\.|Log file closed", re.I)
_LOG_PREFIX = re.compile(r"^\[[^\]]*\]\[\s*\d+\]")  # "[2026.10.30-18.02.11:123][  0]"


def logs_dir(install_dir: str) -> str:
    return os.path.join(install_dir, "ConanSandbox", "Saved", "Logs")


def crashes_dir(install_dir: str) -> str:
    return os.path.join(install_dir, "ConanSandbox", "Saved", "Crashes")


def latest_log(install_dir: str) -> Optional[str]:
    paths = glob.glob(os.path.join(glob.escape(logs_dir(install_dir)), "*.log"))
    return max(paths, key=os.path.getmtime) if paths else None


def _tail_lines(path: str, max_bytes: int = LOG_TAIL_BYTES) -> List[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # the first line is probably cut
    return lines


def _clean(line: str) -> str:
    return _LOG_PREFIX.sub("", line).strip()[:300]


def _mod_name(mods: List[dict], folder: str) -> str:
    """A friendlier name for a mod folder, using the server's mod list."""
    low = folder.lower()
    for m in mods or []:
        name = str(m.get("name") or "")
        pak = os.path.splitext(os.path.basename(str(m.get("pak") or "")))[0]
        if low in (name.lower(), pak.lower()):
            return name or folder
    return folder


def _match(lines: List[str], mods: List[dict], source: str) -> List[Finding]:
    found: List[Finding] = []
    seen = set()
    for i, line in enumerate(lines):
        rule = next((r for r in _RULES if r.pattern.search(line)), None)
        if rule is not None:
            # A line with a known cause is explained by it, even if it also says "Fatal error".
            if rule.key not in seen:
                seen.add(rule.key)
                found.append(Finding(rule.key, rule.status, rule.title, rule.message,
                                     detail=f"{source}: {_clean(line)}", fix=rule.fix))
            continue
        if "fatal" not in seen and _FATAL.search(line):
            seen.add("fatal")
            window = lines[i:i + _CONTEXT_LINES]
            mod = next((m.group(1) for ln in window for m in [_MOD_IN_STACK.search(ln)] if m), "")
            if mod:
                name = _mod_name(mods, mod)
                found.append(Finding(
                    "mod_crash", STATUS_ERROR, "Crash inside a mod",
                    f"The server crashed inside the mod \"{name}\". It's most likely broken or out of date for "
                    f"this version of the game. Fix Mod Problems on the Mods page can confirm it and start the server without it.",
                    detail=f"{source}: {_clean(line)} … /Game/Mods/{mod}", fix=FIX_FIND_BAD_MOD))
            else:
                found.append(Finding(
                    "fatal", STATUS_ERROR, "Server crashed",
                    f"The server stopped with an error: \"{_clean(line)}\". If it has mods, a broken mod is the "
                    f"most common cause after a game update.",
                    detail=f"{source}: {_clean(line)}", fix=FIX_FIND_BAD_MOD if mods else ""))
    return found


def _missing_mod_files(install_dir: str, mods: List[dict]) -> List[Finding]:
    """modlist.txt lines pointing at files that aren't there: a certain cause."""
    path = os.path.join(install_dir, "ConanSandbox", "Mods", "modlist.txt")
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.strip().startswith(("#", ";"))]
    except OSError:
        return []
    missing = []
    for line in lines:
        pak = line.lstrip("*").strip().strip('"')
        full = pak if os.path.isabs(pak) or re.match(r"^[A-Za-z]:[\\/]", pak) else os.path.join(os.path.dirname(path), pak)
        if not os.path.isfile(full):
            m = re.search(r"440900[\\/](\d+)[\\/]", pak)
            by_id = next((x for x in mods or [] if m and x.get("id") == m.group(1)), None)
            missing.append((by_id or {}).get("name") or os.path.splitext(os.path.basename(pak))[0])
    if not missing:
        return []
    names = ", ".join(f"\"{n}\"" for n in missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
    return [Finding("mod_missing", STATUS_ERROR, "Mod file missing",
                    f"The server's mod list includes {names}, but the mod file{'s are' if len(missing) > 1 else ' is'} "
                    f"not on this PC, so the server can't start with {'them' if len(missing) > 1 else 'it'}. "
                    f"Downloading the mods puts the files back.",
                    detail=f"modlist.txt lines whose file doesn't exist: {', '.join(missing)}", fix=FIX_DOWNLOAD_MODS)]


def _crash_reports(install_dir: str, since: float) -> List[str]:
    """Crash report folders, newest first, changed after `since`."""
    folders = [d for d in glob.glob(os.path.join(glob.escape(crashes_dir(install_dir)), "*")) if os.path.isdir(d)]
    out = []
    for d in folders:
        try:
            if os.path.getmtime(d) >= since:
                out.append(d)
        except OSError:
            continue
    return sorted(out, key=os.path.getmtime, reverse=True)


def _crash_text(folder: str) -> List[str]:
    lines: List[str] = []
    for xml in glob.glob(os.path.join(glob.escape(folder), "*.runtime-xml")):
        try:
            with open(xml, encoding="utf-8", errors="replace") as f:
                body = f.read(2_000_000)
        except OSError:
            continue
        for tag in ("ErrorMessage", "CallStack"):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", body, re.S)
            if m:
                text = (m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
                        .replace("&quot;", '"').replace("&apos;", "'"))
                if tag == "ErrorMessage" and not _FATAL.search(text):
                    text = "Fatal error! " + text.strip()
                lines += text.splitlines()
    for lg in glob.glob(os.path.join(glob.escape(folder), "*.log")):
        lines += _tail_lines(lg, 512 * 1024)
    return lines


def examine(install_dir: str, mods: Optional[List[dict]] = None, running: bool = False,
            started_at: float = 0.0, now: Optional[float] = None) -> List[Finding]:
    """Why the server won't start or keeps crashing, most certain first.
    `running`/`started_at`: a running server's crash reports from before it
    started are history, not its current problem."""
    mods = list(mods or [])
    now = time.time() if now is None else now
    findings = _missing_mod_files(install_dir, mods)
    have = {f.key for f in findings}

    def add(new: List[Finding]) -> None:
        for f in new:
            if f.key not in have and not (f.key == "fatal" and "mod_crash" in have):
                have.add(f.key)
                findings.append(f)

    log = latest_log(install_dir)
    clean_exit = False
    if log:
        lines = _tail_lines(log)
        clean_exit = any(_CLEAN_EXIT.search(ln) for ln in lines[-50:])
        add(_match(lines, mods, f"Server log {os.path.basename(log)}"))

    recent = _crash_reports(install_dir, now - CRASH_WINDOW_SECONDS)
    current = [d for d in recent if not running or os.path.getmtime(d) >= started_at]
    if current:
        add(_match(_crash_text(current[0]), mods, f"Crash report {os.path.basename(current[0])}"))
    if len(recent) >= 2:
        findings.append(Finding(
            "crash_count", STATUS_ERROR if len(recent) >= 3 else STATUS_WARNING, "Repeated crashes",
            f"The server has crashed {len(recent)} times in the last 24 hours."
            + (" The most likely cause is listed with it." if len(findings) else
               " Its logs don't show a known cause; if it has mods, Fix Mod Problems on the Mods page checks them one by one."),
            detail=f"{len(recent)} crash report folders in {crashes_dir(install_dir)} from the last 24 hours.",
            fix="" if len(findings) else (FIX_FIND_BAD_MOD if mods else "")))
    if clean_exit and not running:
        # Stopped on purpose (or by ConanOps): only the certain causes matter.
        findings = [f for f in findings if f.key in ("mod_missing", "crash_count")]
    return findings


def explain(install_dir: str, mods: Optional[List[dict]] = None) -> str:
    """One or two sentences for a crash alert: the most likely cause, or ''."""
    try:
        findings = [f for f in examine(install_dir, mods) if f.key != "crash_count"]
    except Exception as e:  # noqa: BLE001 - an alert must never fail over this
        _log.warning(f"Couldn't read the server's logs: {e}")
        return ""
    if not findings:
        return ""
    f = findings[0]
    return f"Likely cause: {f.title.lower()}. {f.message}"
