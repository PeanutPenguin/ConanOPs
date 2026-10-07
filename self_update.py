"""
Updates ConanOps' own application files from a zip the person picks
themselves via the App Settings page. Deliberately not automatic or
networked -- this only ever installs a file someone has already put
on disk and explicitly selected; nothing here reaches out to the
internet on its own.

Safety model:
  1. Validate the zip actually looks like a ConanOps package before
     touching anything on disk.
  2. Back up the current install directory.
  3. Overlay every file from the zip on top of the install directory
     (never deletes a file that isn't present in the zip). A file that
     can't be overwritten in place because it's locked (Windows won't
     let you overwrite a running .exe/.dll) is renamed aside instead,
     so the running process keeps working from the renamed copy while
     the new one lands in its place.
  4. If step 3 fails partway through, restore from the backup so the
     app is left in a known-working state, and keep the backup around
     since something went wrong.
  5. If the copy succeeds, the backup is NOT deleted yet -- a pending
     marker is written instead, and the backup is only cleared once
     the NEW process actually confirms it started up cleanly (see
     confirm_update_success(), called from main.py after MainWindow
     finishes constructing). If the new version crashes before that,
     the *next* launch finds the marker still "pending confirmation"
     and rolls the install back to the backup automatically -- see
     check_and_recover_pending_update().

Actually running the new code still requires the process to restart
(Python already has the old modules loaded in memory) -- that's the
caller's job, not this module's; see MainWindow._relaunch_after_update.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import applog

_log = applog.get_logger(__name__)

# Deliberately NOT conanops_paths.no_space_root(): the update backup and
# pending-update marker only ever get copied with shutil (never handed
# to SteamCMD, which is the actual source of the no-space requirement),
# and this module's whole job is recovering from a broken update, so it
# stays on the one path-resolution rule (~) that every other file in
# this module already assumes and that doesn't depend on any other
# ConanOps module resolving correctly.

# Must match conanops_paths.APP_DATA_DIRNAME exactly -- hardcoded
# rather than imported so a broken update to conanops_paths.py can't
# also break this module's ability to back up, roll back, or clean up
# after that same update (see the comment above this one for the same
# reasoning applied to the backup/marker paths). This is the subfolder
# everything ConanOps creates (server installs, backups, config) lives
# under when it defaults to living alongside the app itself -- and it
# has to be excluded from every operation below that treats
# APP_INSTALL_DIR as a single unit, or backing up/restoring/relaunching
# the APP would drag every SERVER's files (many GB, and world-save
# files a running server has open) along with it.
_APP_DATA_DIRNAME = "data"

# Files that must be present (at the same relative location) for a zip
# to be considered a real ConanOps package, not just any zip file.
# Only meaningful for a SOURCE install -- see _expected_marker_files().
MARKER_FILES = ("main.py", "models.py")

# Suffix used when a file can't be overwritten in place (it's locked --
# almost always our own currently-running .exe on Windows) and gets
# renamed aside instead. Swept up by _cleanup_old_files() once the new
# version has confirmed it started successfully and the old process
# holding the lock has had time to exit.
_OLD_SUFFIX = ".conanops-old"

_PENDING_MARKER_NAME = "pending_self_update.json"

# List of every file the last update installed, relative to the install
# folder. Lets the NEXT update remove files ConanOps itself shipped
# before but no longer ships -- without ever touching a file it didn't
# install (anything the person added themselves is never in the list).
_MANIFEST_NAME = ".conanops-manifest.json"


def _pending_marker_path() -> str:
    return os.path.join(os.path.expanduser("~"), "ConanOps", _PENDING_MARKER_NAME)


def _expected_marker_files() -> tuple:
    """What "looks like a real ConanOps package" means depends on how
    ConanOps itself is currently running:

    - Source install (this file being run with `python main.py`):
      main.py/models.py living at the same place inside the zip as they
      do in the current install, exactly as before.
    - PyInstaller-frozen build (the packaged .exe): there's no main.py
      or models.py on disk in this kind of install at all -- everything
      is bundled into the executable -- so requiring them, as a
      previous version of this module unconditionally did, meant a
      real update package for the shipped app could NEVER pass
      validation. The marker instead has to be the executable itself,
      by name, since that's the one file guaranteed to exist in both
      the current install and a legitimately-built update package for
      the same app.
    """
    if getattr(sys, "frozen", False):
        return (os.path.basename(sys.executable),)
    return MARKER_FILES


def _exes_at_root(names: List[str], prefix: str) -> List[str]:
    """.exe files that sit directly at `prefix` inside the zip (not
    nested in a further subfolder)."""
    out = []
    plen = len(prefix)
    for n in names:
        if not n.startswith(prefix):
            continue
        rest = n[plen:]
        if rest and "/" not in rest.rstrip("/") and rest.lower().endswith(".exe"):
            out.append(rest)
    return out


class UpdateValidationError(Exception):
    """Raised when the selected file doesn't look like a real ConanOps
    update package. Safe to show str(e) directly to the person."""


@dataclass
class UpdateResult:
    success: bool
    message: str


def _find_source_root(names: List[str]) -> str:
    """Returns the path prefix under which the expected marker files
    (see _expected_marker_files()) live inside the zip -- "" if they're
    at the zip's root, or e.g. "conanops/" if the zip wraps everything
    in one top-level folder (the shape every zip this app hands out
    uses). Raises UpdateValidationError if neither shape is found.

    Frozen builds get one extra fallback: if the zip's exe isn't named
    exactly like the currently-running one (someone's browser saved it
    as "ConanOps (1).exe", say) but there's still exactly one .exe file
    sitting at a candidate root, that's accepted too -- a renamed
    download shouldn't be indistinguishable from a bogus zip when
    there's really only one plausible file it could mean."""
    markers = _expected_marker_files()
    name_set = set(names)
    candidate_prefixes = [""] + sorted({n.split("/", 1)[0] + "/" for n in names if "/" in n})

    if all(f in name_set for f in markers):
        return ""
    for prefix in candidate_prefixes[1:]:
        if all(f"{prefix}{f}" in name_set for f in markers):
            return prefix

    if getattr(sys, "frozen", False):
        for prefix in candidate_prefixes:
            exes = _exes_at_root(names, prefix)
            if len(exes) == 1:
                _log.info(
                    f"Update zip's exe is named {exes[0]!r}, not {markers[0]!r} -- "
                    "accepting it anyway since it's the only .exe at that level."
                )
                return prefix

    raise UpdateValidationError(
        "This doesn't look like a ConanOps update file -- couldn't find "
        f"{' and '.join(markers)} inside it. Make sure you picked the right file."
    )


def _zip_exe_name(names: List[str], source_root: str) -> Optional[str]:
    """The .exe filename actually inside the zip at source_root, which
    may differ from the currently-running exe's name (see the renamed-
    download fallback in _find_source_root). None for a source install."""
    if not getattr(sys, "frozen", False):
        return None
    exes = _exes_at_root(names, source_root)
    return exes[0] if len(exes) == 1 else os.path.basename(sys.executable)


def validate_update_zip(zip_path: str) -> None:
    """Best-effort sanity check before touching anything on disk. Raises
    UpdateValidationError with a message safe to show the person if the
    check fails; does nothing if it passes."""
    if not zipfile.is_zipfile(zip_path):
        raise UpdateValidationError(f"{os.path.basename(zip_path)} isn't a zip file.")
    with zipfile.ZipFile(zip_path, "r") as zf:
        _find_source_root(zf.namelist())  # raises if the shape doesn't match


def _backup_app_files(install_dir: str, backup_dir: str, rel_paths: set) -> int:
    """Copies each existing install_dir/<rel> into backup_dir/<rel>.
    Paths outside the install folder or inside its data folder are
    skipped (see _safe_target). Returns how many files were copied."""
    os.makedirs(backup_dir, exist_ok=True)
    copied = 0
    for rel in sorted(rel_paths):
        src = _safe_target(install_dir, rel)
        if not src or not os.path.isfile(src):
            continue
        dest = os.path.join(backup_dir, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        copied += 1
    return copied


def _ignore_at_top_level(root_dir: str, name: str):
    """Returns a shutil.copytree() ignore callback that excludes a
    single entry, but only when it's directly inside root_dir -- not
    any folder that happens to share the same name nested deeper in
    the tree. shutil calls this once per directory it visits, passing
    that directory's own path each time, so comparing against
    root_dir (normalized once, up front) is enough to tell "top
    level" apart from everything under it."""
    root_norm = os.path.normcase(os.path.abspath(root_dir))

    def _ignore(dirpath, names):
        if os.path.normcase(os.path.abspath(dirpath)) == root_norm and name in names:
            return {name}
        return set()

    return _ignore


def _copy_file_with_swap(src_path: str, dest_path: str) -> None:
    """Copies src_path over dest_path. If dest_path can't be overwritten
    in place because something has it open -- on Windows this is almost
    always our own currently-running .exe/.dll -- it's renamed aside
    (Windows allows renaming an open file even though it disallows
    overwriting one) and the new file takes its place instead. The
    still-running old process keeps executing fine from the renamed
    copy; the renamed leftover is swept up later by _cleanup_old_files()
    once the update is confirmed."""
    try:
        shutil.copy2(src_path, dest_path)
        return
    except PermissionError:
        pass
    old_path = dest_path + _OLD_SUFFIX
    n = 1
    while os.path.exists(old_path):
        old_path = f"{dest_path}{_OLD_SUFFIX}.{n}"
        n += 1
    _log.info(f"{dest_path} is locked (in use) -- renaming it to {old_path} and copying the new file into place.")
    os.rename(dest_path, old_path)
    shutil.copy2(src_path, dest_path)


def _overlay_copy(source_dir: str, dest_dir: str) -> None:
    """Copies every file from source_dir into dest_dir, creating
    subfolders as needed and overwriting any existing files with the
    same relative path (see _copy_file_with_swap for locked files).
    Never deletes anything in dest_dir that isn't present in
    source_dir -- an intentionally non-destructive overlay, both for
    the forward update and for restoring from a backup."""
    for root, _dirs, files in os.walk(source_dir):
        rel = os.path.relpath(root, source_dir)
        dest_root = dest_dir if rel == "." else os.path.join(dest_dir, rel)
        os.makedirs(dest_root, exist_ok=True)
        for fn in files:
            _copy_file_with_swap(os.path.join(root, fn), os.path.join(dest_root, fn))


def cleanup_leftover_update_files(install_dir: str) -> None:
    """Public wrapper around _cleanup_old_files(), meant to be called
    unconditionally on every startup (see main.py), not only when a
    pending-update marker exists. confirm_update_success() and
    check_and_recover_pending_update() both already sweep as part of
    finishing their own marker-driven work, but if a file was still
    locked at that moment (best-effort: the cleanup only logs a
    warning and moves on), the marker gets cleared anyway -- and with
    it, the only trigger that would have retried the sweep. Calling
    this independently every launch means a leftover file left behind
    by a previous cleanup attempt still eventually gets swept, instead
    of sitting there permanently once nothing references it anymore."""
    _cleanup_old_files(install_dir)


def _cleanup_old_files(install_dir: str) -> None:
    """Removes leftover *.conanops-old[.N] files from a previous update
    that had to rename a locked file aside (see _copy_file_with_swap).
    Called once an update is confirmed, by which point the old process
    that was holding the file open has exited and released it. Best
    effort -- if a file's still locked for some reason, it's simply
    left for the next confirm to try again.

    Prunes _APP_DATA_DIRNAME out of the walk rather than just skipping
    files found inside it: server folders can be large, and there's
    never a reason to walk into them at all here -- .conanops-old
    files only ever come from swapping a locked APP file aside (see
    _copy_file_with_swap), never anything under the data folder."""
    top = os.path.normcase(os.path.abspath(install_dir))
    for root, dirs, files in os.walk(install_dir):
        if os.path.normcase(os.path.abspath(root)) == top:
            dirs[:] = [d for d in dirs if d != _APP_DATA_DIRNAME]
        for fn in files:
            if _OLD_SUFFIX in fn:
                path = os.path.join(root, fn)
                try:
                    os.remove(path)
                except OSError as e:
                    _log.warning(f"Couldn't remove leftover update file {path}: {e}")


def _files_in(folder: str) -> set:
    """Relative paths ('/'-separated) of every file under folder."""
    out = set()
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), folder).replace(os.sep, "/")
            out.add(rel)
    out.discard(_MANIFEST_NAME)
    return out


def _read_manifest(install_dir: str) -> Optional[set]:
    try:
        with open(os.path.join(install_dir, _MANIFEST_NAME), "r", encoding="utf-8") as f:
            data = json.load(f)
        return {str(p) for p in data.get("files", [])}
    except (OSError, ValueError, AttributeError):
        return None


def _write_manifest(install_dir: str, files: set) -> None:
    try:
        with open(os.path.join(install_dir, _MANIFEST_NAME), "w", encoding="utf-8") as f:
            json.dump({"files": sorted(files)}, f, indent=0)
    except OSError as e:
        _log.warning(f"Couldn't write the update manifest: {e}")


def _safe_target(install_dir: str, rel: str) -> Optional[str]:
    """Absolute path for a manifest entry, or None if it would land
    outside the install folder or inside its data folder -- a damaged or
    hand-edited manifest must never be able to delete anything else."""
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return None
    if rel.split("/", 1)[0] == _APP_DATA_DIRNAME:
        return None
    root = os.path.abspath(install_dir)
    target = os.path.abspath(os.path.join(root, *rel.split("/")))
    return target if target.startswith(root + os.sep) else None


def _prune_files(install_dir: str, remove: set) -> list:
    removed = []
    for rel in sorted(remove):
        target = _safe_target(install_dir, rel)
        if not target or not os.path.isfile(target):
            continue
        try:
            os.remove(target)
            removed.append(rel)
        except OSError as e:
            _log.warning(f"Couldn't remove old file {target}: {e}")
            continue
        # Tidy folders this leaves empty (never the install folder itself).
        parent = os.path.dirname(target)
        root = os.path.abspath(install_dir)
        while parent != root and parent.startswith(root + os.sep):
            try:
                os.rmdir(parent)  # only succeeds if empty
            except OSError:
                break
            parent = os.path.dirname(parent)
    return removed


def _prune_and_write_manifest(install_dir: str, new_files: set) -> None:
    """Deletes files the previous update installed that the new one
    doesn't ship, then records the new list. The very first update (no
    manifest yet) deletes nothing -- it only starts the list, since
    there's no way to tell an old ConanOps file from one the person
    added. Never raises: a leftover file is harmless."""
    try:
        old = _read_manifest(install_dir)
        if old is not None:
            removed = _prune_files(install_dir, old - new_files)
            if removed:
                _log.info(f"Removed {len(removed)} file(s) the new version no longer ships: {', '.join(removed)}")
        _write_manifest(install_dir, new_files)
    except Exception as e:  # noqa: BLE001
        _log.warning(f"Old-file cleanup skipped: {e}")


def defer_update_confirmation() -> None:
    """Startup ended on purpose before the main window came up (the
    person closed the PIN prompt). Resets a pending update to "not yet
    tried", so the next launch gets a fair first run instead of rolling
    back a perfectly good update -- a declined unlock isn't a crash."""
    marker = _read_pending_marker()
    if marker is not None and marker.get("attempted"):
        _write_pending_marker(marker.get("backup_dir", ""), marker.get("install_dir", ""), attempted=False)


def update_pending() -> bool:
    return _read_pending_marker() is not None


def _write_pending_marker(backup_dir: str, install_dir: str, attempted: bool) -> None:
    marker = {"backup_dir": backup_dir, "install_dir": install_dir, "attempted": attempted}
    os.makedirs(os.path.dirname(_pending_marker_path()), exist_ok=True)
    with open(_pending_marker_path(), "w", encoding="utf-8") as f:
        json.dump(marker, f)


def _read_pending_marker() -> Optional[dict]:
    try:
        with open(_pending_marker_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _clear_pending_marker() -> None:
    try:
        os.remove(_pending_marker_path())
    except OSError:
        pass


def confirm_update_success() -> None:
    """Call once, after the app has finished starting up normally (see
    main.py, right after MainWindow is constructed and shown). If an
    update is pending confirmation, this is what actually deletes its
    backup and sweeps up any renamed-aside locked files -- reaching
    this point is the signal that the new version came up cleanly."""
    marker = _read_pending_marker()
    if marker is None:
        return
    backup_dir = marker.get("backup_dir")
    install_dir = marker.get("install_dir")
    if install_dir:
        _cleanup_old_files(install_dir)
    if backup_dir:
        try:
            shutil.rmtree(backup_dir)
        except OSError as e:
            _log.warning(f"Update confirmed but couldn't delete the backup at {backup_dir}: {e}")
    _clear_pending_marker()
    _log.info("Update confirmed successful; backup cleared.")


def check_and_recover_pending_update() -> Optional[str]:
    """Call once, as close to the very start of the app as possible
    (see main.py). Returns a message to show the person if it rolled
    an update back, else None.

    An update is only "pending" between apply_update() writing the
    marker and confirm_update_success() clearing it. The first launch
    after an update finds the marker with attempted=False, marks it
    attempted=True, and lets that launch proceed normally -- if it's
    this same launch that calls confirm_update_success() later, the
    marker is cleared and nothing else happens. But if that launch
    crashes before reaching confirm_update_success(), the process
    dies with attempted=True still on disk, and the *next* launch
    (this function, on that next run) finds attempted=True and knows
    the update didn't survive its first run -- so it restores the
    backup right now, before the rest of the app has a chance to load
    whatever the broken update left behind."""
    marker = _read_pending_marker()
    if marker is None:
        return None

    if not marker.get("attempted", False):
        _write_pending_marker(marker.get("backup_dir", ""), marker.get("install_dir", ""), attempted=True)
        return None

    backup_dir = marker.get("backup_dir")
    install_dir = marker.get("install_dir")
    _clear_pending_marker()
    if not (backup_dir and install_dir and os.path.isdir(backup_dir)):
        _log.error("Pending update needed rollback, but its backup is missing -- nothing to restore.")
        return (
            "The last update didn't start up successfully, but ConanOps couldn't find its backup to "
            "restore automatically. You may need to reinstall."
        )
    try:
        failed_files = _read_manifest(install_dir)
        _overlay_copy(backup_dir, install_dir)
        # The backup's own manifest (now restored) lists the previous
        # version's files; anything only the failed version shipped goes.
        restored_files = _read_manifest(install_dir)
        if failed_files is not None and restored_files is not None:
            _prune_files(install_dir, failed_files - restored_files)
        _cleanup_old_files(install_dir)
        shutil.rmtree(backup_dir, ignore_errors=True)
    except OSError as e:
        _log.error(f"Rollback of failed update also failed: {e}")
        return (
            f"The last update didn't start up successfully, and restoring the previous version also failed "
            f"({e}). Your backup is at {backup_dir} -- you may need to copy it back into {install_dir} manually."
        )
    _log.info("Rolled back an update that failed to start up successfully.")
    return "The last update didn't start up successfully, so ConanOps rolled back to the previous version."


def apply_update(zip_path: str, install_dir: str) -> UpdateResult:
    """Validates, backs up, and installs the update. Returns a result
    rather than raising for anything that happens after validation, so
    the caller can show a clear message either way without needing to
    catch a grab-bag of exception types. On success the backup is kept
    and a pending marker is written -- see confirm_update_success()."""
    validate_update_zip(zip_path)  # raises before anything is touched

    # Refuse to stack a second update on top of one that hasn't been
    # confirmed yet, rather than silently overwriting its marker: that
    # would orphan the first update's backup (nothing would ever clean
    # it up) and would mean check_and_recover_pending_update() rolling
    # back to the wrong version if this second update turns out to be
    # the one that fails to start. This also closes off the narrow
    # window where two apply_update() calls landing in the same second
    # could otherwise race on the same backup_dir path.
    existing = _read_pending_marker()
    if existing is not None:
        return UpdateResult(
            False,
            "An update you installed earlier hasn't finished yet. ConanOps finishes an update -- and "
            "checks the new version really works -- the first time it starts up afterwards, and that "
            "hasn't happened since the last install (it may not have restarted, or was closed during "
            "that first start). Quit ConanOps completely (tray icon → Quit), open it again, then install "
            "this update.",
        )

    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_dir = os.path.join(os.path.expanduser("~"), "ConanOps", "app_update_backup", ts)

    try:
        with tempfile.TemporaryDirectory(prefix="conanops-update-") as extract_dir:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zip_names = zf.namelist()
                source_root = _find_source_root(zip_names)
                zf.extractall(extract_dir)
            source_dir = os.path.join(extract_dir, source_root) if source_root else extract_dir

            if getattr(sys, "frozen", False):
                # The zip's exe may be named differently than the one
                # actually running (see the renamed-download fallback
                # in _find_source_root). The overlay below copies by
                # relative path, so if we left it named e.g. "ConanOps
                # (1).exe" it would land as a NEW file alongside the
                # real one instead of replacing it. Rename it to match
                # the running exe's name before the overlay runs.
                running_exe_name = os.path.basename(sys.executable)
                zip_exe_name = _zip_exe_name(zip_names, source_root)
                if zip_exe_name and zip_exe_name != running_exe_name:
                    _log.info(f"Renaming update's {zip_exe_name!r} to {running_exe_name!r} to replace the running exe.")
                    os.rename(os.path.join(source_dir, zip_exe_name), os.path.join(source_dir, running_exe_name))

            try:
                _log.info(f"Backing up current install ({install_dir}) to {backup_dir} before update.")
                os.makedirs(os.path.dirname(backup_dir), exist_ok=True)
                # Only the files this update will overwrite or remove --
                # never the whole folder. ConanOps.exe often sits in a
                # folder with other things in it (Downloads, the Desktop,
                # a drive root, or right next to a 50+ GB server
                # install), and copying all of that is what made an
                # update sit on "Updating…" for hours. Restoring this
                # backup (see check_and_recover_pending_update) puts
                # exactly these files back.
                _backup_app_files(install_dir, backup_dir,
                                  _files_in(source_dir) | (_read_manifest(install_dir) or set()) | {_MANIFEST_NAME})
            except Exception as e:  # noqa: BLE001 - nothing installed yet; report and stop cleanly either way
                _log.error(f"Update aborted: couldn't back up the current install: {e}")
                return UpdateResult(False, f"Couldn't back up the current version before updating, so nothing was changed: {e}")

            try:
                _log.info(f"Copying update files from {source_dir} into {install_dir}.")
                _overlay_copy(source_dir, install_dir)
                # Remove files the previous version shipped that this one
                # doesn't. The backup made above still has them, so a
                # rollback brings them back.
                _prune_and_write_manifest(install_dir, _files_in(source_dir))
            except Exception as e:  # noqa: BLE001 - copy partly landed; always try to restore rather than propagate
                _log.error(f"Update failed while copying new files ({e}); restoring from backup.")
                try:
                    _overlay_copy(backup_dir, install_dir)
                    _cleanup_old_files(install_dir)
                    shutil.rmtree(backup_dir, ignore_errors=True)
                    restored_msg = "Your previous version was restored, and is still what's running."
                except Exception as restore_err:  # noqa: BLE001 - report either way, never raise past this point
                    _log.error(f"Restore from backup also failed: {restore_err}")
                    restored_msg = (
                        f"Restoring the previous version ALSO failed -- your backup is still safe at "
                        f"{backup_dir}, please copy its contents back into {install_dir} manually."
                    )
                return UpdateResult(False, f"Update failed while copying files: {e}. {restored_msg}")
    except Exception as e:  # noqa: BLE001 - e.g. a corrupt zip (BadZipFile) blowing up mid-extract
        _log.error(f"Update failed unexpectedly: {e}")
        return UpdateResult(False, f"Update failed unexpectedly, and nothing should have been changed: {e}")

    # Copy succeeded. Don't delete the backup yet -- keep it until the
    # NEW process confirms it actually starts up cleanly (see
    # confirm_update_success() / check_and_recover_pending_update()).
    _write_pending_marker(backup_dir, install_dir, attempted=False)
    _log.info("Update files installed; pending confirmation on next successful launch.")
    return UpdateResult(True, "Update installed.")
