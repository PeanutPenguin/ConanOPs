"""
Installs a ConanOps update zip already on disk (picked by the person or
downloaded by app_updates).

Steps: validate the zip, back up the files it will replace, overlay it
(locked files are renamed aside, since Windows can't overwrite a running
.exe), and restore the backup if copying fails. On success the backup is
kept until the new version confirms a clean start; if it crashes first,
the next launch rolls back (check_and_recover_pending_update). The caller restarts the app.
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

# Paths here are hardcoded (not from conanops_paths) so a broken update to
# another module can't break rollback. Must match conanops_paths.APP_DATA_DIRNAME.
# The data folder holds server installs and world saves, so every app-file
# operation below must exclude it.
_APP_DATA_DIRNAME = "data"

# Files proving a zip is a ConanOps package (source installs only).
MARKER_FILES = ("main.py", "models.py")

# Suffix for locked files renamed aside during an update; swept on later startups.
_OLD_SUFFIX = ".conanops-old"

_PENDING_MARKER_NAME = "pending_self_update.json"

# Files the last update installed, so the next one can remove files we
# no longer ship without touching anything the person added.
_MANIFEST_NAME = ".conanops-manifest.json"


def _pending_marker_path() -> str:
    return os.path.join(os.path.expanduser("~"), "ConanOps", _PENDING_MARKER_NAME)


def _expected_marker_files() -> tuple:
    """Source install: main.py/models.py. Frozen build: the running exe's name,
    since nothing else is guaranteed to be on disk."""
    if getattr(sys, "frozen", False):
        return (os.path.basename(sys.executable),)
    return MARKER_FILES


def _exes_at_root(names: List[str], prefix: str) -> List[str]:
    """.exe files directly at `prefix` inside the zip (not nested)."""
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
    """Not a valid update package; str(e) is safe to show the person."""


@dataclass
class UpdateResult:
    success: bool
    message: str


def _find_source_root(names: List[str]) -> str:
    """Prefix inside the zip where the marker files live ("" or "folder/").
    Frozen builds also accept a single .exe at a candidate root, in case the
    download was renamed (e.g. "ConanOps (1).exe"). Raises UpdateValidationError."""
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
    """The zip's exe name at source_root (may differ from ours); None for source installs."""
    if not getattr(sys, "frozen", False):
        return None
    exes = _exes_at_root(names, source_root)
    return exes[0] if len(exes) == 1 else os.path.basename(sys.executable)


def validate_update_zip(zip_path: str) -> None:
    """Raises UpdateValidationError if the zip isn't a ConanOps package."""
    if not zipfile.is_zipfile(zip_path):
        raise UpdateValidationError(f"{os.path.basename(zip_path)} isn't a zip file.")
    with zipfile.ZipFile(zip_path, "r") as zf:
        _find_source_root(zf.namelist())  # raises if the shape doesn't match


def _backup_app_files(install_dir: str, backup_dir: str, rel_paths: set) -> int:
    """Copies each existing install_dir/<rel> to backup_dir (skipping unsafe
    paths); returns the count copied."""
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
    """copytree() ignore callback excluding `name` only directly inside root_dir."""
    root_norm = os.path.normcase(os.path.abspath(root_dir))

    def _ignore(dirpath, names):
        if os.path.normcase(os.path.abspath(dirpath)) == root_norm and name in names:
            return {name}
        return set()

    return _ignore


def _copy_file_with_swap(src_path: str, dest_path: str) -> None:
    """Copies over dest_path; if it's locked (a running .exe/.dll), renames it
    aside first, since Windows allows renaming an open file but not overwriting it."""
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
    """Copies every file into dest_dir, overwriting; never deletes anything."""
    for root, _dirs, files in os.walk(source_dir):
        rel = os.path.relpath(root, source_dir)
        dest_root = dest_dir if rel == "." else os.path.join(dest_dir, rel)
        os.makedirs(dest_root, exist_ok=True)
        for fn in files:
            _copy_file_with_swap(os.path.join(root, fn), os.path.join(dest_root, fn))


def cleanup_leftover_update_files(install_dir: str) -> None:
    """Run on every startup: a file still locked during the marker-driven
    sweep would otherwise never be retried."""
    _cleanup_old_files(install_dir)


def _cleanup_old_files(install_dir: str) -> None:
    """Best-effort removal of *.conanops-old files; skips walking the
    (possibly huge) data folder."""
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
    """Absolute path for a manifest entry, or None if outside the install
    folder or in the data folder, so a bad manifest can't delete anything else."""
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
    """Deletes files the old manifest lists but the new version doesn't
    ship, then writes the new list. No manifest yet = delete nothing. Never raises."""
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
    """Startup was cancelled on purpose (PIN prompt closed): reset a pending
    update to "not yet tried" so it isn't rolled back."""
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
    """Call once the main window is shown: deletes a pending update's backup
    and leftover files, marking it successful."""
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
    """Call at the very start of the app. Returns a message if it rolled
    back an update, else None.

    The first launch after an update marks it attempted; if a launch finds
    it already attempted, that run crashed before confirming, so restore the backup."""
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
        # Remove files only the failed version shipped.
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
    """Validates, backs up and installs the update. Raises only for validation;
    later failures come back as an UpdateResult. On success a pending marker is written."""
    validate_update_zip(zip_path)  # raises before anything is touched

    # Don't stack updates: it would orphan the first backup and roll back to the wrong version.
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
                # A renamed exe would land beside ours instead of replacing it.
                running_exe_name = os.path.basename(sys.executable)
                zip_exe_name = _zip_exe_name(zip_names, source_root)
                if zip_exe_name and zip_exe_name != running_exe_name:
                    _log.info(f"Renaming update's {zip_exe_name!r} to {running_exe_name!r} to replace the running exe.")
                    os.rename(os.path.join(source_dir, zip_exe_name), os.path.join(source_dir, running_exe_name))

            try:
                _log.info(f"Backing up current install ({install_dir}) to {backup_dir} before update.")
                os.makedirs(os.path.dirname(backup_dir), exist_ok=True)
                # Only files this update touches: the exe often shares a folder
                # with huge unrelated data (e.g. a server install).
                _backup_app_files(install_dir, backup_dir,
                                  _files_in(source_dir) | (_read_manifest(install_dir) or set()) | {_MANIFEST_NAME})
            except Exception as e:  # noqa: BLE001 - nothing installed yet; report and stop cleanly either way
                _log.error(f"Update aborted: couldn't back up the current install: {e}")
                return UpdateResult(False, f"Couldn't back up the current version before updating, so nothing was changed: {e}")

            try:
                _log.info(f"Copying update files from {source_dir} into {install_dir}.")
                _overlay_copy(source_dir, install_dir)
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

    # Keep the backup until the new version confirms a clean start.
    _write_pending_marker(backup_dir, install_dir, attempted=False)
    _log.info("Update files installed; pending confirmation on next successful launch.")
    return UpdateResult(True, "Update installed.")
