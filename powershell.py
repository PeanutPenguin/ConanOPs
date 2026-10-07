"""
Running PowerShell scripts from ConanOps -- shared by firewall rules
(network_setup.py), the background-mode scheduled task
(background_mode.py) and Windows Update settings (windows_update.py).

How scripts are passed
----------------------
Never with -EncodedCommand. Base64-encoded command lines are a classic
malware pattern, so antivirus products and Defender's attack-surface
rules flag them -- exactly the false-positive problem an unsigned
PyInstaller app already has too much of. Instead the script is written
to a temp .ps1 file and run with a short, readable -Command:

  * read-only / unelevated: the file is read and run with
    Invoke-Expression (not -File, so a Group-Policy-enforced execution
    policy doesn't block it).
  * elevated: the file sits in a folder any unelevated process can
    write to, so the elevated command reads it ONCE, checks its SHA-256
    against the hash ConanOps computed when writing it, and only then
    runs that same in-memory text. Swapping the file between writing
    and elevating just makes the hash check fail -- nothing runs.

Values are embedded with ps_str() (a single-quoted literal with every
quote character PowerShell recognizes doubled), never by string
concatenation of raw user text.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from typing import Iterable, List, Optional

import applog
import proc_utils
from proc_utils import hidden_window_kwargs

_log = applog.get_logger(__name__)

RUN_OK = "ok"
RUN_DECLINED = "declined"
RUN_FAILED = "failed"

PREAMBLE = (
    "$ErrorActionPreference = 'Stop'\n"
    "$ProgressPreference = 'SilentlyContinue'\n"
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
)


def ps_str(value: str) -> str:
    """A PowerShell single-quoted literal. PowerShell treats the curly
    quotes U+2018-U+201B as single quotes too, so all of them get
    doubled, not just the ASCII one. Control characters are dropped."""
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
    """Writes the script (UTF-8 with BOM, so Windows PowerShell 5.1 reads
    non-ASCII correctly) and returns (folder, path, sha256-hex)."""
    folder = tempfile.mkdtemp(prefix="conanops-ps-")
    path = os.path.join(folder, "script.ps1")
    data = ("\ufeff" + script).encode("utf-8")
    with open(path, "wb") as f:
        f.write(data)
    text_hash = hashlib.sha256(script.encode("utf-8")).hexdigest().upper()
    return folder, path, text_hash


def _runner_command(path: str, expected_hash: Optional[str]) -> str:
    """The short -Command that loads the script file. With a hash, the
    text is verified before it runs (see module docstring)."""
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
    """Runs a script with this process's own rights and returns the
    completed process (stdout = whatever the script printed), or None if
    PowerShell couldn't run at all."""
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
    """Runs `script` with Administrator rights: directly if this process
    is already elevated, else through ONE Windows permission (UAC)
    prompt. Returns RUN_OK / RUN_DECLINED / RUN_FAILED."""
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
