"""
SteamCMD integration: install it, update the Conan Exiles dedicated server
(app 443030) and Workshop mods, and read installed/latest build ids.
Long-running calls must run on a worker thread, not the GUI thread.
"""
from __future__ import annotations

import proc_utils
import os
import queue
import re
import subprocess
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from typing import Callable, Optional

import mod_manager

from models import APP_ID
from proc_utils import hidden_console_kwargs
from mod_manager import WORKSHOP_APP_ID
import applog

_log = applog.get_logger(__name__)

# Without this, app_update can fail with "Failed to install app '443030'
# (Missing configuration)".
_FORCE_WINDOWS_PLATFORM_ARGS = ["+@sSteamCmdForcePlatformType", "windows"]

# e.g. "Update state (0x61) downloading, progress: 45.32 (390911489 / 862992488)"
_PROGRESS_RE = re.compile(r"progress:\s*([\d.]+)")


def _sleep_cancelable(seconds: float, should_cancel: Optional[Callable[[], bool]] = None) -> None:
    """time.sleep() that returns early once should_cancel() is True."""
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if should_cancel and should_cancel():
            return
        time.sleep(min(0.25, max(0.0, end - time.monotonic())))


def _remove_if_empty_dir(path: str) -> None:
    """Removes `path` only if empty: SteamCMD's +force_install_dir works
    reliably only when it creates the folder itself."""
    try:
        if os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)
    except OSError:
        pass


def parse_progress_percent(line: str) -> Optional[float]:
    """Percentage from a SteamCMD progress line, or None."""
    m = _PROGRESS_RE.search(line)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None

STEAMCMD_ZIP_URL = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"


@dataclass
class UpdateResult:
    success: bool
    output: str
    installed_buildid: Optional[str] = None
    # "disk" or "backup" (nothing touched), or "steamcmd" (ran but didn't finish).
    reason: str = ""


# Required free space is the larger of this and the install's size, since
# SteamCMD stages changed files before swapping them in.
MIN_FREE_BYTES_FOR_UPDATE = 15 * 1024 ** 3


def folder_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def check_update_disk_space(install_dir: str) -> tuple:
    """(ok, free_bytes, needed_bytes) for the drive install_dir is on."""
    import shutil
    probe = install_dir
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        free = shutil.disk_usage(probe or ".").free
    except OSError:
        return True, 0, 0  # can't tell -- don't block the update on it
    needed = max(MIN_FREE_BYTES_FOR_UPDATE, folder_size(install_dir) if os.path.isdir(install_dir) else 0)
    return free >= needed, free, needed


def format_gb(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB"


def download_workshop_items(steamcmd_dir: str, workshop_ids: list, max_attempts: int = 2) -> "UpdateResult":
    """Downloads/updates Workshop items into steamapps/workshop/content/440900/<id>/,
    where mod_manager.workshop_pak_path() expects them."""
    exe = steamcmd_exe_path(steamcmd_dir)
    if not os.path.exists(exe):
        return UpdateResult(False, "SteamCMD isn't installed at the configured location.")
    if not workshop_ids:
        return UpdateResult(True, "No mods to download.")

    args = [exe, *_FORCE_WINDOWS_PLATFORM_ARGS, "+force_install_dir", steamcmd_dir, "+login", "anonymous"]
    for wid in workshop_ids:
        args += ["+workshop_download_item", str(WORKSHOP_APP_ID), str(wid)]
    args += ["+quit"]

    combined_output = []
    for attempt in range(1, max_attempts + 1):
        try:
            proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800, **hidden_console_kwargs())
        except subprocess.TimeoutExpired:
            combined_output.append(f"Attempt {attempt}: SteamCMD timed out downloading workshop items.")
            continue
        combined_output.append(proc.stdout)
        combined_output.append(proc.stderr)
        if proc.returncode == 0:
            # SteamCMD can create the folder without a .pak in it, so check for the .pak.
            missing = [wid for wid in workshop_ids if mod_manager.find_workshop_pak(steamcmd_dir, str(wid)) is None]
            if not missing:
                return UpdateResult(True, "\n".join(combined_output))
            combined_output.append(f"Attempt {attempt}: still missing after download: {', '.join(missing)}")
    return UpdateResult(False, "\n".join(combined_output))


def steamcmd_exe_path(steamcmd_dir: str) -> str:
    return os.path.join(steamcmd_dir, "steamcmd.exe")


def is_steamcmd_installed(steamcmd_dir: str) -> bool:
    return os.path.exists(steamcmd_exe_path(steamcmd_dir))


def _run_cancelable(args: list, timeout: float, should_cancel: Optional[Callable[[], bool]] = None,
                     poll_interval: float = 0.25, on_line: Optional[Callable[[str], None]] = None):
    """Like subprocess.run(), but can be cancelled via should_cancel and
    calls on_line with each output line as it arrives (a reader thread feeds
    a queue, since Windows pipes don't support select()). stdout and stderr
    are merged, so .stderr is always "". Raises TimeoutExpired on timeout."""
    proc = subprocess.Popen(
        args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1, env=proc_utils.child_env(), **hidden_console_kwargs(),
    )
    lines: list = []
    line_queue: "queue.Queue[Optional[str]]" = queue.Queue()

    def _pump() -> None:
        try:
            for raw_line in proc.stdout:
                line_queue.put(raw_line)
        finally:
            line_queue.put(None)  # sentinel: stdout closed, process has exited

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()

    def _drain_remaining() -> None:
        """Non-blocking read of lines already queued (cancel path)."""
        while True:
            try:
                raw_line = line_queue.get_nowait()
            except queue.Empty:
                return
            if raw_line is None:
                return
            lines.append(raw_line)
            if on_line:
                on_line(raw_line.rstrip("\n"))

    waited = 0.0
    while True:
        try:
            raw_line = line_queue.get(timeout=poll_interval)
        except queue.Empty:
            raw_line = ""
        else:
            if raw_line is None:
                returncode = proc.wait()
                return subprocess.CompletedProcess(args, returncode, "".join(lines), "")
            lines.append(raw_line)
            if on_line:
                on_line(raw_line.rstrip("\n"))

        if should_cancel and should_cancel():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            _drain_remaining()
            return subprocess.CompletedProcess(args, proc.returncode if proc.returncode is not None else -1, "".join(lines), "")

        waited += poll_interval
        if waited >= timeout:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise subprocess.TimeoutExpired(args, timeout)


def install_steamcmd(steamcmd_dir: str, progress: Optional[Callable[[str], None]] = None,
                      should_cancel: Optional[Callable[[], bool]] = None) -> bool:
    """Downloads, extracts and bootstraps steamcmd.exe if missing."""
    def log(msg: str) -> None:
        if progress:
            progress(msg)

    if is_steamcmd_installed(steamcmd_dir):
        log("SteamCMD already installed.")
        return True

    os.makedirs(steamcmd_dir, exist_ok=True)
    zip_path = os.path.join(steamcmd_dir, "steamcmd.zip")

    log("Downloading SteamCMD...")
    urllib.request.urlretrieve(STEAMCMD_ZIP_URL, zip_path)

    log("Extracting SteamCMD...")
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(steamcmd_dir)
    os.remove(zip_path)

    log("Running first-time SteamCMD bootstrap...")
    try:
        _run_cancelable(
            [steamcmd_exe_path(steamcmd_dir), *_FORCE_WINDOWS_PLATFORM_ARGS, "+quit"],
            timeout=180,
            should_cancel=should_cancel,
            on_line=log,
        )
    except subprocess.TimeoutExpired:
        log("SteamCMD bootstrap timed out.")

    # Right after its self-update, SteamCMD fails with "Missing configuration";
    # a second plain start lets it settle first.
    if not (should_cancel and should_cancel()):
        log("Letting SteamCMD finish settling after its update...")
        _sleep_cancelable(3, should_cancel)
        try:
            _run_cancelable(
                [steamcmd_exe_path(steamcmd_dir), *_FORCE_WINDOWS_PLATFORM_ARGS, "+quit"],
                timeout=180,
                should_cancel=should_cancel,
                on_line=log,
            )
        except subprocess.TimeoutExpired:
            log("SteamCMD settle pass timed out -- continuing anyway.")
    ok = is_steamcmd_installed(steamcmd_dir)
    log("SteamCMD ready." if ok else "SteamCMD install did not complete.")
    return ok



def update_server(
    steamcmd_dir: str,
    install_dir: str,
    progress: Optional[Callable[[str], None]] = None,
    max_attempts: int = 3,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_progress_percent: Optional[Callable[[float], None]] = None,
) -> UpdateResult:
    """Runs +app_update ... validate, with retries. on_progress_percent gets
    SteamCMD's download percentage (0-100) from the live output."""
    def log(msg: str) -> None:
        if progress:
            progress(msg)

    last_shown_percent = [None]

    def on_line(line: str) -> None:
        pct = parse_progress_percent(line)
        if pct is None:
            log(line)
            return
        if on_progress_percent:
            on_progress_percent(pct)
        # Log only when the whole percent changes, to avoid flooding the log.
        shown = int(pct)
        if shown != last_shown_percent[0]:
            last_shown_percent[0] = shown
            log(f"Downloading... {shown}%")

    exe = steamcmd_exe_path(steamcmd_dir)
    if not os.path.exists(exe):
        return UpdateResult(False, "SteamCMD not found at expected path.")

    # Deliberately NOT pre-creating install_dir -- see _remove_if_empty_dir().
    parent = os.path.dirname(os.path.abspath(install_dir))
    if parent:
        os.makedirs(parent, exist_ok=True)
    combined_output = []

    for attempt in range(1, max_attempts + 1):
        if should_cancel and should_cancel():
            combined_output.append("Cancelled before this attempt started.")
            return UpdateResult(False, "\n".join(combined_output))
        if attempt > 1:
            # Failures seen so far were timing-sensitive.
            _sleep_cancelable(5, should_cancel)
        _remove_if_empty_dir(install_dir)
        log(f"SteamCMD update attempt {attempt} of {max_attempts}...")
        last_shown_percent[0] = None  # reset so attempt 2's first line always logs
        args = [
            exe,
            *_FORCE_WINDOWS_PLATFORM_ARGS,
            "+force_install_dir", install_dir,
            "+login", "anonymous",
            "+app_update", str(APP_ID), "validate",
            "+quit",
        ]
        try:
            proc = _run_cancelable(args, timeout=3600, should_cancel=should_cancel, on_line=on_line)
        except subprocess.TimeoutExpired:
            log(f"Attempt {attempt} timed out after an hour.")
            combined_output.append(f"Attempt {attempt}: SteamCMD timed out.")
            continue
        if should_cancel and should_cancel():
            combined_output.append(f"Attempt {attempt}: cancelled.")
            return UpdateResult(False, "\n".join(combined_output))
        combined_output.append(proc.stdout)
        combined_output.append(proc.stderr)

        state = get_install_state(install_dir)
        # Need both: a failed no-op run leaves an old StateFlags 4 (fully installed) in place.
        if proc.returncode == 0 and state == 4:
            build_id = get_installed_buildid(install_dir)
            log("Update succeeded.")
            return UpdateResult(True, "\n".join(combined_output), build_id)
        log(f"Attempt {attempt} did not complete cleanly (returncode={proc.returncode}, state={state}).")

    return UpdateResult(False, "\n".join(combined_output), reason="steamcmd")


def get_install_state(install_dir: str) -> Optional[int]:
    manifest = os.path.join(install_dir, "steamapps", f"appmanifest_{APP_ID}.acf")
    if not os.path.exists(manifest):
        return None
    with open(manifest, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    m = re.search(r'"StateFlags"\s*"(\d+)"', content)
    return int(m.group(1)) if m else None


def get_installed_buildid(install_dir: str) -> Optional[str]:
    manifest = os.path.join(install_dir, "steamapps", f"appmanifest_{APP_ID}.acf")
    if not os.path.exists(manifest):
        return None
    with open(manifest, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    m = re.search(r'"buildid"\s*"(\d+)"', content)
    return m.group(1) if m else None


def get_latest_buildid(steamcmd_dir: str) -> Optional[str]:
    """Current public-branch build id via anonymous app_info_print, or None."""
    exe = steamcmd_exe_path(steamcmd_dir)
    if not os.path.exists(exe):
        return None
    args = [exe, *_FORCE_WINDOWS_PLATFORM_ARGS, "+login", "anonymous", "+app_info_update", "1",
            "+app_info_print", str(APP_ID), "+quit"]
    try:
        proc = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, **hidden_console_kwargs())
    except subprocess.TimeoutExpired:
        _log.warning("SteamCMD app_info_print timed out while checking the latest build id.")
        return None
    return _extract_public_buildid(proc.stdout)


def _extract_public_buildid(vdf_text: str) -> Optional[str]:
    """buildid from the "public" branch block of an app_info_print dump,
    so beta branches or other buildid fields aren't picked up by mistake."""
    m = re.search(r'"public"\s*\{', vdf_text)
    if not m:
        # Unexpected format: fall back to the first buildid anywhere.
        matches = re.findall(r'"buildid"\s*"(\d+)"', vdf_text)
        return matches[0] if matches else None

    depth = 1
    i = m.end()
    block_start = i
    while i < len(vdf_text) and depth > 0:
        if vdf_text[i] == "{":
            depth += 1
        elif vdf_text[i] == "}":
            depth -= 1
        i += 1
    public_block = vdf_text[block_start:i]

    bm = re.search(r'"buildid"\s*"(\d+)"', public_block)
    if bm:
        return bm.group(1)

    matches = re.findall(r'"buildid"\s*"(\d+)"', vdf_text)
    return matches[0] if matches else None
