"""
SteamCMD integration: detect/install it, run updates for the Conan Exiles
dedicated server (app id 443030), and read back the installed build id.

Note: this module shells out to steamcmd.exe and is Windows-oriented (the
whole app targets Windows, matching the original scripts). Long-running
calls (download, update) are meant to be invoked from a QThread worker in
the UI layer, not the main thread.
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

# Forces SteamCMD to fetch the Windows build of an app regardless of
# what platform SteamCMD itself is running on -- every published
# Conan Exiles server setup guide includes this, and its absence is a
# documented cause of `app_update` failing with "ERROR! Failed to
# install app '443030' (Missing configuration)" even when everything
# else about the command is correct.
_FORCE_WINDOWS_PLATFORM_ARGS = ["+@sSteamCmdForcePlatformType", "windows"]

# Matches SteamCMD's own progress line for a download/validate pass, e.g.:
#   "Update state (0x61) downloading, progress: 45.32 (390911489 / 862992488)"
# The percentage is what actually drives the UI's determinate progress bar
# (see ui/setup_wizard.py's InstallPage) instead of an indeterminate spinner
# that gives no sense of whether ~60 GB have downloaded or the process is
# stuck.
_PROGRESS_RE = re.compile(r"progress:\s*([\d.]+)")


def _sleep_cancelable(seconds: float, should_cancel: Optional[Callable[[], bool]] = None) -> None:
    """time.sleep() that wakes early if should_cancel() turns True, so a
    Cancel click never waits out a full pause."""
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if should_cancel and should_cancel():
            return
        time.sleep(min(0.25, max(0.0, end - time.monotonic())))


def _remove_if_empty_dir(path: str) -> None:
    """Removes `path` only if it exists and is completely empty, so
    SteamCMD's +force_install_dir creates the folder itself. Every
    successful manual run pointed SteamCMD at a folder that didn't
    exist yet; ConanOps pre-created it empty. Never touches a folder
    with anything in it, so an existing install is always safe."""
    try:
        if os.path.isdir(path) and not os.listdir(path):
            os.rmdir(path)
    except OSError:
        pass


def parse_progress_percent(line: str) -> Optional[float]:
    """Extracts the percentage from one line of SteamCMD's own progress
    output, or None if this line isn't a progress line."""
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
    # Why it failed, when known: "disk" (not enough free space -- nothing
    # was touched), "backup" (the pre-update backup failed -- nothing was
    # touched), "steamcmd" (SteamCMD ran and didn't finish cleanly).
    reason: str = ""


# Free space required before an update starts: whichever is larger, a
# fixed floor or the server's current size. SteamCMD downloads changed
# files into a staging area before swapping them in, so a big patch can
# briefly need close to the install's size again on top of it.
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
    """Downloads (or updates, if already present) each of the given
    Steam Workshop items for Conan Exiles into steamcmd's own
    steamapps/workshop/content/440900/<id>/ folder -- the same location
    mod_manager.workshop_pak_path() points modlist.txt entries at.

    Nothing in ConanOps used to actually call this: mods added on the
    Mods page got written into modlist.txt with a path pointing at
    where the .pak SHOULD be, but the .pak itself was never fetched, so
    the server would start with every configured mod silently missing
    unless someone happened to have placed the files there by hand."""
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
            # A directory existing isn't enough -- SteamCMD can create
            # the numbered content folder without ever actually
            # placing a .pak inside it (a failed/partial download,
            # e.g.), which used to read as "downloaded" here even
            # though nothing playable was ever fetched.
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
    """Like subprocess.run(), but polls `should_cancel` every
    `poll_interval` seconds and terminates the process early if it ever
    returns True, instead of blocking uninterruptibly for up to
    `timeout` seconds. Returns an object with .returncode/.stdout/
    .stderr (a real CompletedProcess on normal completion, or a
    minimal stand-in on cancel/timeout).

    Without this, a Cancel button wired to QThread.requestInterruption()
    was pure theater during a SteamCMD download/update: the worker
    thread was blocked inside subprocess.run() the whole time, which
    has no way to observe that flag, so the process would keep running
    to completion (or its own hour-long timeout) regardless of Cancel
    having been clicked.

    If `on_line` is given, it's called with each line of output AS IT
    ARRIVES (stripped of its trailing newline), rather than only once
    the whole process has finished. This used to be the actual cause
    of downloads looking "stalled": the previous implementation drove
    this same polling loop with proc.communicate(timeout=poll_interval)
    in a loop, and Popen.communicate() only ever RETURNS output once
    the process has fully exited -- so between "SteamCMD update attempt
    1 of 2..." and the next log line, a real multi-minute-to-multi-hour
    download produced zero visible feedback, indistinguishable from a
    hang. Reading the pipe as it's written (via a background reader
    thread feeding a queue, since Windows pipes don't support select())
    fixes that and is also what makes a real progress percentage (see
    parse_progress_percent()) possible instead of an indeterminate
    spinner.

    stdout and stderr are merged (steamcmd interleaves status and error
    text on both, and a caller reading it as one combined stream for
    progress/diagnostics doesn't need them kept separate) -- the
    returned/raised object's .stderr is always "".
    """
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
        """Pulls anything already buffered in the queue without
        blocking, for the cancel/timeout paths below where the reader
        thread may have a few more lines queued up already."""
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
            raw_line = ""  # nothing new this tick -- fall through to the cancel/timeout checks below
        else:
            if raw_line is None:
                # Reader hit EOF: the process has exited. Reap it and return.
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
    """Downloads and extracts steamcmd.exe if it isn't already present.
    steamcmd.exe self-bootstraps (updates itself) the first time it runs,
    so no separate installer is needed beyond this."""
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

    # The bootstrap above self-updates SteamCMD and relaunches it. In
    # every failed setup, the server download started immediately
    # afterward and hit "Missing configuration", while the identical
    # command succeeded later against the same, already-settled
    # SteamCMD folder. A second plain start lets that post-update
    # relaunch finish settling before anything real is asked of it.
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
    """Runs steamcmd +app_update against the dedicated server app id,
    validating files. Retries once on failure, mirroring the original
    .bat script's behavior.

    If given, on_progress_percent is called with SteamCMD's own
    download percentage (0-100, as a float) as it's parsed out of the
    live output -- see _run_cancelable()'s docstring for why this
    exists (a real progress bar instead of an indeterminate spinner,
    and feedback that isn't indistinguishable from a hang)."""
    def log(msg: str) -> None:
        if progress:
            progress(msg)

    last_shown_percent = [None]  # mutable box so the nested function below can update it

    def on_line(line: str) -> None:
        pct = parse_progress_percent(line)
        if pct is None:
            log(line)
            return
        if on_progress_percent:
            on_progress_percent(pct)
        # Only echo a progress line to the text log when the whole
        # percentage actually advanced -- SteamCMD can print several
        # of these a second, and echoing every single one verbatim
        # would flood the log without showing anything the progress
        # bar isn't already showing.
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
            # Give SteamCMD a moment between tries rather than
            # hammering it again instantly -- the failures seen so far
            # were timing-sensitive.
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
        # A non-zero SteamCMD exit code, or install state != 4 (fully
        # installed per Steam's StateFlags), both mean the attempt
        # didn't actually complete -- checking state alone isn't enough,
        # since a NO-OP failed run (network blip before SteamCMD even
        # touches the manifest) leaves state at whatever it already was
        # from a previous successful install, which would otherwise be
        # misreported as "this attempt succeeded."
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
    """Asks Steam (anonymously, no key needed) what the current public
    branch build id is for the app, via `app_info_print`. Used to detect
    whether an update is available without downloading anything."""
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
    """Finds the buildid specifically inside the "public" branch block of
    a `+app_info_print` VDF dump, e.g.:

        "branches"
        {
            "public"
            {
                "buildid"      "1234567"
                ...
            }
            "beta"
            {
                "buildid"      "7654321"
                ...
            }
        }

    Grabbing the first "buildid" anywhere in the dump (the previous
    approach) can pick up a beta/other branch's buildid instead, or any
    other unrelated "buildid" field earlier in the text, and silently
    misreport whether an update is available. This scopes the search to
    the "public" block specifically by locating its opening brace and
    matching braces to find where that block ends.
    """
    m = re.search(r'"public"\s*\{', vdf_text)
    if not m:
        # Fall back to the old best-effort behavior if the VDF doesn't
        # look like what we expect (format could have changed).
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

    # "public" block found but no buildid line in it (unexpected) --
    # fall back rather than silently returning nothing.
    matches = re.findall(r'"buildid"\s*"(\d+)"', vdf_text)
    return matches[0] if matches else None
