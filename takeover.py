"""
Finds the other things on this PC that start, restart or update a Conan
server -- scheduled tasks, startup items, Windows services (e.g. NSSM),
restart-loop .bat files, monitor scripts like conan_monitor.py, other
server managers -- so ConanOps can switch them off when it takes a server
over. Two managers on one server fight: each restarts or updates it while
the other is using it.

Nothing is deleted: tasks and services are disabled, startup files are
moved into ConanOps' data folder, Run-key values are recorded before
removal, and every change is listed in a record restore() can undo.
No Qt here; call off the GUI thread.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import applog
import conanops_paths
import powershell
import server_finder

_log = applog.get_logger(__name__)

KIND_PROCESS = "process"
KIND_TASK = "task"
KIND_SERVICE = "service"
KIND_RUN = "run"
KIND_STARTUP = "startup"

_SCRIPT_HOSTS = {"cmd.exe", "powershell.exe", "pwsh.exe", "python.exe", "pythonw.exe", "py.exe", "pyw.exe",
                 "wscript.exe", "cscript.exe", "node.exe"}
# Other server managers, matched on process name (casefolded, no extension).
_MANAGER_NAMES = ("windowsgsm", "conanexilesserverlauncher", "dedicatedserverlauncher", "conanservermanager",
                  "conanexilesmanager", "servermanager", "tcadmin", "gameservermanager")
# ConanOps' own entries.
_OWN_TASKS = {"conanops keep-alive", "conanops (admin)"}
_OWN_RUN_VALUES = {"conanops"}
_MARKERS = ("conansandboxserver", "443030", "conan exiles dedicated server", "conan_monitor",
            "conan_auto_update", "conansandbox")
_SCRIPT_RE = re.compile(r'([A-Za-z]:\\[^"\r\n<>|*?]*?\.(?:bat|cmd|ps1|pyw?|vbs)|(?<![\w.])/[^"\s<>|*?:]*?\.(?:bat|cmd|ps1|pyw?|vbs))'
                        r'|([^\s"\\/<>|*?]+\.(?:bat|cmd|ps1|pyw?|vbs))', re.IGNORECASE)
_FORCE_DIR_RE = re.compile(r'\+force_install_dir\s+(?:"([^"]+)"|(\S+))', re.IGNORECASE)
_MAX_SCRIPT_BYTES = 1_000_000


@dataclass
class Rival:
    kind: str
    name: str                 # what the person sees
    detail: str               # the command or path behind it
    text: str = ""            # everything matched against (command plus script contents)
    ident: Dict[str, object] = field(default_factory=dict)  # what disable()/restore() need
    install_dirs: List[str] = field(default_factory=list)   # normalized installs it names; [] = any Conan server
    stops_server: bool = False  # turning it off stops the server too (a service that hosts it)

    @property
    def key(self) -> str:
        """Stable id for "don't ask again"."""
        if self.kind == KIND_PROCESS:
            return f"{self.kind}:{self.ident.get('cmd', '')}".casefold()
        if self.kind == KIND_TASK:
            return f"{self.kind}:{self.ident.get('path', '')}{self.ident.get('name', '')}".casefold()
        if self.kind == KIND_SERVICE:
            return f"{self.kind}:{self.ident.get('name', '')}".casefold()
        if self.kind == KIND_RUN:
            return f"{self.kind}:{self.ident.get('key', '')}\\{self.ident.get('name', '')}".casefold()
        return f"{self.kind}:{self.ident.get('file', '')}".casefold()

    def describe(self) -> str:
        label = {KIND_PROCESS: "Running program", KIND_TASK: "Scheduled task", KIND_SERVICE: "Windows service",
                 KIND_RUN: "Starts with Windows", KIND_STARTUP: "Startup folder"}[self.kind]
        return f"{label}: {self.name}"

    def applies_to(self, install_dir: str) -> bool:
        return not self.install_dirs or server_finder.path_key(install_dir) in self.install_dirs


@dataclass
class TakeoverResult:
    done: List[Rival] = field(default_factory=list)
    failed: List[Tuple[Rival, str]] = field(default_factory=list)
    records: List[dict] = field(default_factory=list)
    stopped_servers: List[str] = field(default_factory=list)  # normalized installs a service stop took down
    declined: bool = False


# ------------------------------------------------------------ collecting --

def _read_text(path: str) -> str:
    try:
        if os.path.getsize(path) > _MAX_SCRIPT_BYTES:
            return ""
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return ""
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = data.decode(enc)
            if enc == "utf-16" and "\x00" in text:
                continue
            return text
        except (UnicodeDecodeError, UnicodeError):
            continue
    return data.decode("latin-1", errors="replace")


def expand_text(text: str, cwd: str = "", depth: int = 2) -> str:
    """The command plus the contents of the scripts it runs (and the ones
    those run), so a task that runs start.bat that runs the server matches."""
    out = [text]
    seen = set()
    frontier = [(text, cwd)]
    for _ in range(depth):
        nxt = []
        for t, wd in frontier:
            for m in _SCRIPT_RE.finditer(t):
                path = m.group(1) or (os.path.join(wd, m.group(2)) if wd else "")
                path = os.path.expandvars(path) if path else ""
                if not path:
                    continue
                n = os.path.normcase(os.path.abspath(path))
                if n in seen or not os.path.isfile(path):
                    continue
                seen.add(n)
                body = _read_text(path)
                if body:
                    out.append(body)
                    nxt.append((body, os.path.dirname(path)))
        frontier = nxt
        if not frontier:
            break
    return "\n".join(out)


def _paths_named(text: str) -> List[str]:
    """Server install folders a command or script names: the server exe's
    path, or a SteamCMD +force_install_dir for the server."""
    found = []
    for m in server_finder._EXE_IN_TEXT.finditer(text):
        root = server_finder._install_from_exe(m.group(1))
        if root:
            found.append(root)
    if "443030" in text:
        for m in _FORCE_DIR_RE.finditer(text):
            found.append((m.group(1) or m.group(2)).strip().rstrip("\\"))
    return found


def _relevance(text: str, known_installs: Iterable[str]) -> Tuple[bool, List[str]]:
    """(is it about a Conan server, which known installs it names)."""
    low = text.casefold()
    named = {server_finder.path_key(p) for p in _paths_named(text)}
    for inst in known_installs:
        n = server_finder.path_key(inst)
        if n.casefold() in low or inst.casefold() in low:
            named.add(n)
    about = bool(named) or any(m in low for m in _MARKERS)
    return about, sorted(named)


def _is_ours(text: str) -> bool:
    low = text.casefold()
    if "conanops" in low:
        return True
    try:
        own = conanops_paths.app_install_dir().casefold()
        return bool(own) and own in low
    except Exception:  # noqa: BLE001
        return False


def _ps_inventory() -> dict:
    """Tasks, services, Run-key values and startup items, read unelevated."""
    if os.name != "nt":
        return {}
    script = r"""
$out = @{ tasks = @(); services = @(); run = @(); startup = @() }
try {
  Get-ScheduledTask -ErrorAction Stop | Where-Object { $_.TaskPath -notlike '\Microsoft\*' } | ForEach-Object {
    $acts = @($_.Actions | Where-Object { $_.Execute } | ForEach-Object {
      [pscustomobject]@{ exe = [string]$_.Execute; args = [string]$_.Arguments; wd = [string]$_.WorkingDirectory } })
    $out.tasks += [pscustomobject]@{ path = [string]$_.TaskPath; name = [string]$_.TaskName; state = [string]$_.State; actions = $acts }
  }
} catch {}
try {
  Get-CimInstance Win32_Service -ErrorAction Stop |
    Where-Object { $_.PathName -and $_.PathName -notmatch '^"?[A-Za-z]:\\Windows\\' } | ForEach-Object {
      $extra = ''
      try {
        $i = Get-ItemProperty -Path ('HKLM:\SYSTEM\CurrentControlSet\Services\' + $_.Name + '\Parameters') -ErrorAction Stop
        $extra = [string]$i.Application + ' ' + [string]$i.AppParameters + ' ' + [string]$i.AppDirectory
      } catch {}
      $out.services += [pscustomobject]@{ name = [string]$_.Name; display = [string]$_.DisplayName;
        path = ([string]$_.PathName + ' ' + $extra); state = [string]$_.State; start = [string]$_.StartMode;
        pid = [int]$_.ProcessId }
  }
} catch {}
$skip = @('PSPath', 'PSParentPath', 'PSChildName', 'PSDrive', 'PSProvider')
foreach ($k in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
                 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run',
                 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run')) {
  try {
    $props = Get-ItemProperty -Path $k -ErrorAction Stop
    foreach ($p in $props.PSObject.Properties) {
      if ($skip -notcontains $p.Name) { $out.run += [pscustomobject]@{ key = $k; name = [string]$p.Name; value = [string]$p.Value } }
    }
  } catch {}
}
$sh = $null
try { $sh = New-Object -ComObject WScript.Shell } catch {}
$common = [Environment]::GetFolderPath('CommonStartup')
foreach ($f in @([Environment]::GetFolderPath('Startup'), $common)) {
  if ($f -and (Test-Path -LiteralPath $f)) {
    Get-ChildItem -LiteralPath $f -File -ErrorAction SilentlyContinue | ForEach-Object {
      $t = ''
      if ($_.Extension -eq '.lnk' -and $sh) {
        try { $l = $sh.CreateShortcut($_.FullName); $t = [string]$l.TargetPath + ' ' + [string]$l.Arguments + ' ' + [string]$l.WorkingDirectory } catch {}
      }
      $out.startup += [pscustomobject]@{ file = $_.FullName; common = ($f -eq $common); target = $t }
    }
  }
}
$out | ConvertTo-Json -Depth 6 -Compress
"""
    proc = powershell.run_readonly(script, timeout=60)
    if proc is None or proc.returncode != 0:
        return {}
    try:
        return json.loads((proc.stdout or "").strip() or "{}")
    except ValueError:
        _log.warning("Couldn't read the startup/task inventory.")
        return {}


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _wanted_name(name: str) -> bool:
    low = (name or "").lower()
    stem = os.path.splitext(low)[0].replace(" ", "").replace("-", "").replace("_", "")
    return low in _SCRIPT_HOSTS or low == server_finder.EXE_NAME or "conan" in stem or any(
        m in stem for m in _MANAGER_NAMES)


def _fill_details(info: dict, proc) -> None:
    import psutil
    for key, getter in (("cmdline", proc.cmdline), ("cwd", proc.cwd)):
        try:
            info[key] = getter()
        except (psutil.Error, OSError):
            info[key] = [] if key == "cmdline" else ""


def _processes() -> List[dict]:
    """[{pid, name, cmdline, cwd, ppid, create_time}] for every process.
    Uses its own Process objects rather than psutil.process_iter()'s shared
    cache, so this scan can never hold a lock the GUI's process checks wait
    on, and reads command lines only for processes that could matter."""
    import psutil
    out, by_pid = [], {}
    for pid in psutil.pids():
        try:
            proc = psutil.Process(pid)
            with proc.oneshot():
                info = {"pid": pid, "name": proc.name(), "ppid": proc.ppid(), "create_time": proc.create_time(),
                        "cmdline": [], "cwd": ""}
            info["_proc"] = proc
        except (psutil.Error, OSError):
            continue
        out.append(info)
        by_pid[pid] = info
    for info in out:
        if _wanted_name(info["name"]):
            _fill_details(info, info["_proc"])
    # Whatever started a running server matters too, whatever it's called.
    for info in out:
        if (info["name"] or "").lower() == server_finder.EXE_NAME:
            parent = by_pid.get(info["ppid"])
            if parent and not parent["cmdline"]:
                _fill_details(parent, parent["_proc"])
    for info in out:
        info.pop("_proc", None)
    return out


def _parent_chain(pid: int, procs: Dict[int, dict]) -> List[dict]:
    chain, seen = [], set()
    cur = procs.get(pid)
    while cur and cur.get("ppid") and cur["ppid"] not in seen:
        seen.add(cur["ppid"])
        parent = procs.get(cur["ppid"])
        if not parent:
            break
        chain.append(parent)
        cur = parent
    return chain


def scan(known_installs: Iterable[str] = (), inventory: Optional[dict] = None,
         processes: Optional[List[dict]] = None) -> List[Rival]:
    """Everything on this PC that looks like it starts, restarts or updates
    a Conan server (other than ConanOps)."""
    known = [k for k in known_installs if k]
    inventory = _ps_inventory() if inventory is None else inventory
    procs = _processes() if processes is None else processes
    rivals: List[Rival] = []
    own_pids = {os.getpid()}
    by_pid = {p["pid"]: p for p in procs}
    for p in _parent_chain(os.getpid(), by_pid):
        own_pids.add(p["pid"])
    server_exe = server_finder.EXE_NAME

    def add(kind, name, detail, text, ident, stops=False):
        if _is_ours(text) or _is_ours(name):
            return
        about, named = _relevance(text, known)
        if not about:
            return
        rivals.append(Rival(kind=kind, name=name, detail=detail.strip(), text=text, ident=ident,
                            install_dirs=named, stops_server=stops))

    # Running programs: script hosts and other managers that mention a Conan server,
    # plus whatever launched a running server.
    server_parents = set()
    for p in procs:
        if (p.get("name") or "").lower() == server_exe:
            for parent in _parent_chain(p["pid"], by_pid)[:1]:
                pname = (parent.get("name") or "").lower()
                if pname not in ("explorer.exe", "services.exe", "svchost.exe", "steam.exe", "nssm.exe",
                                 "wininit.exe", "userinit.exe") and parent["pid"] not in own_pids:
                    server_parents.add(parent["pid"])
    for p in procs:
        pid, pname = p["pid"], (p.get("name") or "")
        low = pname.lower()
        if pid in own_pids or low == server_exe or low == "steamcmd.exe":
            continue
        stem = os.path.splitext(low)[0].replace(" ", "").replace("-", "").replace("_", "")
        if stem.startswith(("conansandbox", "conanops", "funcom")):
            continue  # the game itself (a player's client), its launcher, or ConanOps
        is_manager = any(m in stem for m in _MANAGER_NAMES) or "conan" in stem
        if low not in _SCRIPT_HOSTS and not is_manager and pid not in server_parents:
            continue
        cmd = " ".join(p.get("cmdline") or []) or pname
        text = expand_text(cmd, p.get("cwd") or "")
        if (pid in server_parents or (is_manager and "conan" in stem)) and not _relevance(text, known)[0]:
            text += "\n" + "conansandboxserver"  # it runs a server even if its command doesn't say so
        label = pname
        script = next((a for a in (p.get("cmdline") or [])[1:] if re.search(r"\.(bat|cmd|ps1|pyw?|vbs)$", a, re.I)), "")
        if script:
            label = f"{os.path.basename(script)} ({pname})"
        add(KIND_PROCESS, label, cmd, text, {"pid": pid, "cmd": cmd, "create": p.get("create_time")})

    for t in _as_list(inventory.get("tasks")):
        name = str(t.get("name") or "")
        if name.casefold() in _OWN_TASKS:
            continue
        if str(t.get("state") or "").lower() == "disabled":
            continue
        acts = _as_list(t.get("actions"))
        detail = " && ".join(f"{a.get('exe', '')} {a.get('args', '')}".strip() for a in acts)
        text = "\n".join(expand_text(f"{a.get('exe', '')} {a.get('args', '')}", a.get("wd") or "") +
                         " " + str(a.get("wd") or "") for a in acts)
        add(KIND_TASK, name, detail, text, {"path": str(t.get("path") or "\\"), "name": name})

    for s in _as_list(inventory.get("services")):
        if str(s.get("start") or "").lower() == "disabled" and str(s.get("state") or "").lower() != "running":
            continue
        path = str(s.get("path") or "")
        text = expand_text(path)
        add(KIND_SERVICE, str(s.get("display") or s.get("name")), path, text,
            {"name": str(s.get("name")), "start": str(s.get("start") or "Manual")},
            stops=str(s.get("state") or "").lower() == "running")

    for r in _as_list(inventory.get("run")):
        name = str(r.get("name") or "")
        if name.casefold() in _OWN_RUN_VALUES:
            continue
        value = str(r.get("value") or "")
        add(KIND_RUN, name, value, expand_text(value), {"key": str(r.get("key")), "name": name, "value": value})

    for s in _as_list(inventory.get("startup")):
        file = str(s.get("file") or "")
        target = str(s.get("target") or "")
        text = expand_text(f'"{file}" {target}', os.path.dirname(file))
        add(KIND_STARTUP, os.path.basename(file), target or file, text,
            {"file": file, "common": bool(s.get("common"))})

    # A process started by a task we're also turning off is still listed: both must go.
    return rivals


def install_hints(rivals: Iterable[Rival]) -> List[str]:
    """Install folders the rivals name (for finding servers in odd places)."""
    out = []
    for r in rivals:
        for p in _paths_named(r.text):
            root = server_finder.install_root_for(p) if not server_finder.is_install(p) else p
            if root:
                out.append(root)
    return out


def launch_lines(rivals: Iterable[Rival], installs: Iterable[str]) -> Dict[str, List[str]]:
    """normalized install -> the server command line a rival launches it with."""
    out: Dict[str, List[str]] = {}
    for inst in installs:
        for r in rivals:
            line = server_finder.launch_line_in(r.text, inst)
            if line:
                out[server_finder.path_key(inst)] = line
                break
    return out


def for_install(rivals: Iterable[Rival], install_dir: str) -> List[Rival]:
    return [r for r in rivals if r.applies_to(install_dir)]


# ------------------------------------------------------------- switching --

def backup_dir() -> str:
    return os.path.join(conanops_paths.app_data_dir(), "turned-off-by-conanops")


def _elevated_script(rivals: List[Rival], backup: str) -> str:
    """Elevated script for what needs admin. Each part is independent."""
    q = powershell.ps_str
    lines = ["$ErrorActionPreference = 'Continue'"]
    for r in rivals:
        i = r.ident
        if r.kind == KIND_TASK:
            lines.append(f"try {{ Disable-ScheduledTask -TaskPath {q(i['path'])} -TaskName {q(i['name'])} "
                         f"-ErrorAction Stop | Out-Null }} catch {{}}")
        elif r.kind == KIND_SERVICE:
            lines.append(f"try {{ Set-Service -Name {q(i['name'])} -StartupType Disabled -ErrorAction Stop }} catch {{}}")
            lines.append(f"try {{ Stop-Service -Name {q(i['name'])} -Force -ErrorAction Stop }} catch {{}}")
        elif r.kind == KIND_RUN:
            lines.append(f"try {{ Remove-ItemProperty -Path {q(i['key'])} -Name {q(i['name'])} -ErrorAction Stop }} catch {{}}")
        elif r.kind == KIND_STARTUP:
            dest = os.path.join(backup, os.path.basename(str(i["file"])))
            lines.append(f"try {{ New-Item -ItemType Directory -Force -Path {q(backup)} | Out-Null; "
                         f"Move-Item -LiteralPath {q(i['file'])} -Destination {q(dest)} -Force -ErrorAction Stop }} catch {{}}")
        elif r.kind == KIND_PROCESS:
            lines.append(f"try {{ Stop-Process -Id {int(i['pid'])} -Force -ErrorAction Stop }} catch {{}}")
    return "\n".join(lines) + "\n"


def _task_state(path: str, name: str) -> str:
    proc = powershell.run_readonly(
        f"(Get-ScheduledTask -TaskPath {powershell.ps_str(path)} -TaskName {powershell.ps_str(name)} "
        f"-ErrorAction SilentlyContinue).State", timeout=30)
    return (proc.stdout or "").strip().lower() if proc else ""


def _service_off(name: str) -> bool:
    try:
        import psutil
        svc = psutil.win_service_get(name).as_dict()
        return svc.get("start_type") == "disabled" and svc.get("status") != "running"
    except Exception:  # noqa: BLE001 - not Windows, or the service is gone
        return True


def _run_value_gone(key: str, name: str) -> bool:
    try:
        import winreg
    except ImportError:
        return True
    hive = winreg.HKEY_CURRENT_USER if key.upper().startswith("HKCU") else winreg.HKEY_LOCAL_MACHINE
    sub = key.split(":\\", 1)[-1]
    try:
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, name)
        return False
    except OSError:
        return True


def _process_gone(ident: dict) -> bool:
    try:
        import psutil
        p = psutil.Process(int(ident["pid"]))
        return ident.get("create") is not None and p.create_time() != ident["create"]
    except Exception:  # noqa: BLE001 - NoSuchProcess and friends: it's gone
        return True


def is_off(r: Rival) -> bool:
    i = r.ident
    if r.kind == KIND_TASK:
        return _task_state(i["path"], i["name"]) in ("disabled", "")
    if r.kind == KIND_SERVICE:
        return _service_off(i["name"])
    if r.kind == KIND_RUN:
        return _run_value_gone(i["key"], i["name"])
    if r.kind == KIND_STARTUP:
        return not os.path.exists(str(i["file"]))
    return _process_gone(i)


def _try_unelevated(r: Rival, backup: str) -> None:
    """Best effort without a permission prompt; is_off() decides what's left."""
    i = r.ident
    try:
        if r.kind == KIND_PROCESS:
            import psutil
            proc = psutil.Process(int(i["pid"]))
            if i.get("create") is not None and proc.create_time() != i["create"]:
                return  # a different program reused the id
            # Never the server itself, and never a SteamCMD mid-update (killing it can
            # leave a half-written install); those finish on their own.
            kids = [c for c in proc.children(recursive=True)
                    if (c.name() or "").lower() not in (server_finder.EXE_NAME, "steamcmd.exe")]
            proc.kill()  # first, so a restart loop can't relaunch anything
            for c in kids:
                try:
                    c.kill()
                except psutil.Error:
                    pass
        elif r.kind == KIND_TASK:
            powershell.run_readonly(f"Disable-ScheduledTask -TaskPath {powershell.ps_str(i['path'])} "
                                    f"-TaskName {powershell.ps_str(i['name'])} -ErrorAction SilentlyContinue | Out-Null",
                                    timeout=30)
        elif r.kind == KIND_RUN and str(i["key"]).upper().startswith("HKCU"):
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, str(i["key"]).split(":\\", 1)[-1], 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, str(i["name"]))
        elif r.kind == KIND_STARTUP and not i.get("common"):
            os.makedirs(backup, exist_ok=True)
            shutil.move(str(i["file"]), os.path.join(backup, os.path.basename(str(i["file"]))))
    except Exception as e:  # noqa: BLE001 - the elevated pass gets another go
        _log.info(f"Couldn't turn off {r.describe()} without admin rights: {e}")


def _record(r: Rival, backup: str) -> dict:
    i = r.ident
    rec = {"kind": r.kind, "name": r.name, "install_dirs": list(r.install_dirs),
           "at": time.strftime("%Y-%m-%d %H:%M")}
    if r.kind == KIND_TASK:
        rec.update(path=i["path"], task=i["name"])
    elif r.kind == KIND_SERVICE:
        rec.update(service=i["name"], start=i.get("start", "Manual"))
    elif r.kind == KIND_RUN:
        rec.update(key=i["key"], value_name=i["name"], value=i["value"])
    elif r.kind == KIND_STARTUP:
        rec.update(file=i["file"], moved_to=os.path.join(backup, os.path.basename(str(i["file"]))),
                   common=bool(i.get("common")))
    return rec


def turn_off(rivals: List[Rival], server_running=None) -> TakeoverResult:
    """Switches every rival off: what works without admin rights first, then
    one permission prompt for the rest. `server_running(install)` lets the
    caller see which servers a service stop took down."""
    result = TakeoverResult()
    if not rivals:
        return result
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(backup_dir(), stamp)
    # Processes first, so a restart loop is gone before its task or service changes.
    ordered = sorted(rivals, key=lambda r: {KIND_PROCESS: 0, KIND_TASK: 1, KIND_RUN: 2, KIND_STARTUP: 3,
                                            KIND_SERVICE: 4}[r.kind])
    hosted = [inst for r in ordered if r.kind == KIND_SERVICE and r.stops_server for inst in r.install_dirs]
    was_up = {inst: bool(server_running and server_running(inst)) for inst in hosted}
    for r in ordered:
        if r.kind != KIND_SERVICE:
            _try_unelevated(r, backup)
    left = [r for r in ordered if not is_off(r)]
    if left:
        status = powershell.run_privileged(_elevated_script(left, backup), timeout=180)
        if status in (powershell.RUN_DECLINED, powershell.RUN_NEEDS_PC):
            result.declined = True
    for r in ordered:
        if is_off(r):
            result.done.append(r)
            if r.kind != KIND_PROCESS:
                result.records.append(_record(r, backup))
        else:
            why = ("the Windows permission prompt was declined" if result.declined
                   else "Windows wouldn't allow it")
            result.failed.append((r, why))
    for inst, up in was_up.items():
        if up and server_running and not server_running(inst):
            result.stopped_servers.append(inst)
    for r in result.done:
        _log.info(f"Turned off {r.describe()} ({r.detail[:200]})")
    for r, why in result.failed:
        _log.warning(f"Couldn't turn off {r.describe()}: {why}")
    return result


def restore(records: List[dict]) -> List[str]:
    """Turns back on what turn_off() switched off. Returns problems."""
    if not records:
        return []
    q = powershell.ps_str
    problems: List[str] = []
    admin_lines = ["$ErrorActionPreference = 'Continue'"]
    start_map = {"auto": "Automatic", "automatic": "Automatic", "manual": "Manual", "disabled": "Disabled",
                 "boot": "Automatic", "system": "Automatic"}
    for rec in records:
        kind = rec.get("kind")
        try:
            if kind == KIND_STARTUP and not rec.get("common") and os.path.exists(rec.get("moved_to", "")):
                os.makedirs(os.path.dirname(rec["file"]), exist_ok=True)
                shutil.move(rec["moved_to"], rec["file"])
                continue
            if kind == KIND_RUN and str(rec.get("key", "")).upper().startswith("HKCU"):
                import winreg
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rec["key"].split(":\\", 1)[-1]) as k:
                    winreg.SetValueEx(k, rec["value_name"], 0, winreg.REG_SZ, rec["value"])
                continue
        except Exception as e:  # noqa: BLE001 - try the elevated way
            _log.info(f"Unelevated restore failed for {rec.get('name')}: {e}")
        if kind == KIND_TASK:
            admin_lines.append(f"try {{ Enable-ScheduledTask -TaskPath {q(rec['path'])} -TaskName {q(rec['task'])} "
                               f"-ErrorAction Stop | Out-Null }} catch {{}}")
        elif kind == KIND_SERVICE:
            mode = start_map.get(str(rec.get("start", "")).lower(), "Manual")
            admin_lines.append(f"try {{ Set-Service -Name {q(rec['service'])} -StartupType {mode} -ErrorAction Stop }} catch {{}}")
        elif kind == KIND_RUN:
            admin_lines.append(f"try {{ New-ItemProperty -Path {q(rec['key'])} -Name {q(rec['value_name'])} "
                               f"-Value {q(rec['value'])} -PropertyType String -Force -ErrorAction Stop | Out-Null }} catch {{}}")
        elif kind == KIND_STARTUP:
            admin_lines.append(f"try {{ Move-Item -LiteralPath {q(rec['moved_to'])} -Destination {q(rec['file'])} "
                               f"-Force -ErrorAction Stop }} catch {{}}")
    if len(admin_lines) > 1:
        status = powershell.run_privileged("\n".join(admin_lines) + "\n", timeout=180)
        if status != powershell.RUN_OK:
            problems.append("Some old launchers weren't turned back on (the Windows permission prompt was "
                            "declined or failed).")
    return problems
