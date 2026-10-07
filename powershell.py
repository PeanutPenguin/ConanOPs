"""
Shared PowerShell runner (firewall, scheduled tasks, Windows Update).

Scripts are never passed with -EncodedCommand (antivirus flags it); they're
written to a temp .ps1 and loaded with Invoke-Expression (so execution
policy can't block them). Elevated runs verify the file's SHA-256 before
running, since an unelevated process could swap it. Embed values only via ps_str().
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from typing import Dict, Iterable, List, Optional

import applog
import proc_utils
from proc_utils import hidden_window_kwargs

_log = applog.get_logger(__name__)

RUN_OK = "ok"
RUN_DECLINED = "declined"
RUN_FAILED = "failed"
RUN_NEEDS_PC = "needs_pc"  # a Windows permission prompt was needed, but nobody is at the PC to answer it


# Web actions run for someone not at the PC, so a UAC prompt would go
# unanswered; under no_prompts() run_privileged() returns RUN_NEEDS_PC.
_gate_lock = threading.Lock()
# Per thread, so a prompt started at the PC isn't blocked by a web action.
_gates: Dict[int, List[List[str]]] = {}


@contextmanager
def no_prompts():
    """Blocks UAC prompts on this thread (and carry_gate() work). Yields a
    list collecting a note for each change that needed one."""
    hits: List[str] = []
    ident = threading.get_ident()
    with _gate_lock:
        _gates.setdefault(ident, []).append(hits)
    try:
        yield hits
    finally:
        with _gate_lock:
            stack = _gates.get(ident, [])
            if hits in stack:
                stack.remove(hits)
            if not stack:
                _gates.pop(ident, None)


def carry_gate(fn):
    """Wraps fn to carry this thread's no_prompts() state to another thread."""
    with _gate_lock:
        lists = list(_gates.get(threading.get_ident(), []))
    if not lists:
        return fn

    def run(*args, **kwargs):
        ident = threading.get_ident()
        with _gate_lock:
            _gates.setdefault(ident, []).extend(lists)
        try:
            return fn(*args, **kwargs)
        finally:
            with _gate_lock:
                stack = _gates.get(ident, [])
                for x in lists:
                    if x in stack:
                        stack.remove(x)
                if not stack:
                    _gates.pop(ident, None)
    return run


def prompts_blocked() -> bool:
    """True inside no_prompts() when not already running as admin."""
    with _gate_lock:
        active = bool(_gates.get(threading.get_ident()))
    return active and not proc_utils.is_admin()


def _note_blocked(script: str) -> None:
    first = next((ln.strip() for ln in script.splitlines() if ln.strip().startswith("#")), "")
    _log.info("Skipped a Windows permission prompt during a web action (nobody at the PC to answer it).")
    with _gate_lock:
        for hits in _gates.get(threading.get_ident(), []):
            hits.append(first.lstrip("# ") or "a change that needs Windows' permission")

PREAMBLE = (
    "$ErrorActionPreference = 'Stop'\n"
    "$ProgressPreference = 'SilentlyContinue'\n"
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
)


def ps_str(value: str) -> str:
    """Single-quoted PowerShell literal. PowerShell also treats curly quotes
    U+2018-U+201B as quotes, so those are doubled too; control chars dropped."""
    value = re.sub(r"[\x00-\x1f\x7f]", "", str(value))
    for q in ("'", "\u2018", "\u2019", "\u201a", "\u201b"):
        value = value.replace(q, q + q)
    return "'" + value + "'"


def ps_array(values: Iterable[str]) -> str:
    return "@(" + ",".join(ps_str(v) for v in values) + ")"


def powershell_exe() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    path = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if os.path.exists(path) else "powershell.exe"


_BASE_ARGS = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]


def _write_script(script: str) -> tuple:
    """Writes UTF-8 with BOM (needed by PowerShell 5.1); returns (folder, path, sha256)."""
    folder = tempfile.mkdtemp(prefix="conanops-ps-")
    path = os.path.join(folder, "script.ps1")
    data = ("\ufeff" + script).encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)
    text_hash = hashlib.sha256(script.encode("utf-8")).hexdigest().upper()
    return folder, path, text_hash


def _runner_command(path: str, expected_hash: Optional[str]) -> str:
    """-Command that loads the file; with a hash it exits 97 on mismatch."""
    load = f"$s = [IO.File]::ReadAllText({ps_str(path)}, [Text.Encoding]::UTF8).TrimStart([char]0xFEFF)"
    if expected_hash:
        check = (
            "$h = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash("
            "[Text.Encoding]::UTF8.GetBytes($s))).Replace('-', ''); "
            f"if ($h -ne {ps_str(expected_hash)}) {{ exit 97 }}; "
        )
    else:
        check = ""
    return f"{load}; {check}Invoke-Expression $s; exit 0"


def args_for(script: str) -> tuple:
    """(argument list, temp folder) to run `script` unelevated."""
    folder, path, _h = _write_script(PREAMBLE + script)
    return [*_BASE_ARGS, "-Command", _runner_command(path, None)], folder


def run_readonly(script: str, timeout: float = 30.0) -> Optional[subprocess.CompletedProcess]:
    """Runs unelevated; returns the completed process, or None if PowerShell failed to run."""
    folder = None
    try:
        args, folder = args_for(script)
        return subprocess.run(
            [powershell_exe(), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, **hidden_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        _log.warning(f"PowerShell call failed: {e}")
        return None
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)


def run_privileged(script: str, timeout: float = 90.0) -> str:
    """Runs as admin (directly if elevated, else one UAC prompt). Returns
    RUN_OK / RUN_DECLINED / RUN_FAILED / RUN_NEEDS_PC."""
    if prompts_blocked():
        _note_blocked(script)
        return RUN_NEEDS_PC
    folder, path, text_hash = _write_script(PREAMBLE + script)
    try:
        args: List[str] = [*_BASE_ARGS, "-Command", _runner_command(path, text_hash)]
        if proc_utils.is_admin():
            try:
                proc = subprocess.run(
                    [powershell_exe(), *args],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=timeout, **hidden_window_kwargs(),
                )
            except (OSError, subprocess.TimeoutExpired, ValueError) as e:
                _log.warning(f"Elevated script failed: {e}")
                return RUN_FAILED
            if proc.returncode != 0:
                _log.warning(f"Elevated script exited {proc.returncode}: {(proc.stderr or '').strip()[:500]}")
            return RUN_OK if proc.returncode == 0 else RUN_FAILED

        code = proc_utils.run_elevated_and_wait(powershell_exe(), subprocess.list2cmdline(args), timeout=timeout)
        if code == proc_utils.ELEVATION_DECLINED:
            _log.info("The Windows permission prompt was declined.")
            return RUN_DECLINED
        if code == 97:
            _log.error("Elevated script failed its integrity check and was NOT run.")
            return RUN_FAILED
        if code != 0:
            _log.warning(f"Elevated script failed (exit {code}).")
            return RUN_FAILED
        return RUN_OK
    finally:
        shutil.rmtree(folder, ignore_errors=True)
