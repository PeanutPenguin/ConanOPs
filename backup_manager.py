"""Create, list, restore, import and prune world-save backups.

A backup zips ConanSandbox/Saved/*.db (+ -wal/-shm) and Saved/Config/.
The filename encodes time and trigger, e.g. 20260919-110243_scheduled.zip.
"""
from __future__ import annotations

import glob
import os
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

import applog

_log = applog.get_logger(__name__)

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"
TRIGGER_PRE_UPDATE = "pre-update"


@dataclass
class BackupEntry:
    path: str
    when: datetime
    trigger: str
    size_bytes: int

    @property
    def size_label(self) -> str:
        mb = self.size_bytes / (1024 * 1024)
        return f"{mb:.0f} MB" if mb >= 1 else f"{self.size_bytes} B"


def saved_dir(install_dir: str) -> str:
    """The server's live world-save folder (ConanSandbox/Saved)."""
    return os.path.join(install_dir, "ConanSandbox", "Saved")


_saved_dir = saved_dir


def default_backup_destination(install_dir: str) -> str:
    """Default backup folder: a "backups" sibling of install_dir (not inside it,
    so server validation never touches backups). "" if install_dir is empty."""
    if not install_dir:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(install_dir)), "backups")


class BackupSpaceError(OSError):
    """Not enough free space at the backup destination to write the zip."""


# Extra space beyond the uncompressed size; a full drive breaks the live save too.
_BACKUP_HEADROOM_BYTES = 500 * 1024 ** 2


def _ensure_space_for_backup(db_files, config_dir: str, destination: str) -> None:
    import shutil
    total = 0
    for f in db_files:
        try:
            total += os.path.getsize(f)
        except OSError:
            pass
    if os.path.isdir(config_dir):
        for root, _dirs, files in os.walk(config_dir):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    try:
        free = shutil.disk_usage(destination).free
    except OSError:
        return  # can't tell -- don't block the backup on it
    needed = total + _BACKUP_HEADROOM_BYTES
    if free < needed:
        raise BackupSpaceError(
            f"Not enough free space for a backup in {destination}: {free / 1024 ** 3:.1f} GB free, about "
            f"{needed / 1024 ** 3:.1f} GB needed. Free up space or pick a backup folder on another drive."
        )


def create_backup(install_dir: str, destination: str, trigger: str, label: str = "") -> Optional[BackupEntry]:
    if not destination:
        return None
    saved = _saved_dir(install_dir)
    if not os.path.isdir(saved):
        return None
    os.makedirs(destination, exist_ok=True)

    ts = datetime.now()
    suffix = f"_{label}" if label else ""
    fname = f"{ts.strftime('%Y%m%d-%H%M%S')}_{trigger}{suffix}.zip"
    out_path = os.path.join(destination, fname)

    db_files = glob.glob(os.path.join(saved, "*.db")) + \
        glob.glob(os.path.join(saved, "*.db-wal")) + \
        glob.glob(os.path.join(saved, "*.db-shm"))
    config_dir = os.path.join(saved, "Config")
    _ensure_space_for_backup(db_files, config_dir, destination)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in db_files:
            zf.write(f, arcname=os.path.join("Saved", os.path.basename(f)))
        if os.path.isdir(config_dir):
            for root, _dirs, files in os.walk(config_dir):
                for fn in files:
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, saved)
                    zf.write(full, arcname=os.path.join("Saved", rel))

    size = os.path.getsize(out_path)

    # Check every entry's CRC so a truncated zip is never reported as a good backup.
    try:
        with zipfile.ZipFile(out_path, "r") as zf:
            bad_entry = zf.testzip()
    except zipfile.BadZipFile:
        bad_entry = "(the file itself)"
    if bad_entry is not None:
        _log.error(f"Backup {out_path} failed integrity verification (bad entry: {bad_entry}) -- deleting it.")
        try:
            os.remove(out_path)
        except OSError as e:
            _log.error(f"Also couldn't delete the corrupt backup {out_path}: {e}")
        return None

    return BackupEntry(path=out_path, when=ts, trigger=trigger, size_bytes=size)


def create_backup_for_server(server, destination: str, trigger: str, label: str = "") -> Optional[BackupEntry]:
    """create_backup(), after a best-effort RCON "saveworld" so the copied files are fresh.
    Not an atomic snapshot; SQLite WAL keeps a live copy restorable."""
    if server.rcon_enabled:
        import rcon
        try:
            rcon.send_command("127.0.0.1", server.rcon_port, server.rcon_password, "saveworld")
        except rcon.RconError as e:
            _log.warning(f"Couldn't request a save via RCON before backing up {server.name}: {e}")
    return create_backup(server.install_dir, destination, trigger, label)


def list_backups(destination: str) -> List[BackupEntry]:
    if not os.path.isdir(destination):
        return []
    entries = []
    for path in glob.glob(os.path.join(destination, "*.zip")):
        fname = os.path.basename(path)
        try:
            date_part, rest = fname.split("_", 1)
            trigger = rest.rsplit(".", 1)[0].split("_")[0]
            when = datetime.strptime(date_part, "%Y%m%d-%H%M%S")
        except ValueError:
            when = datetime.fromtimestamp(os.path.getmtime(path))
            trigger = "unknown"
        entries.append(BackupEntry(path=path, when=when, trigger=trigger, size_bytes=os.path.getsize(path)))
    entries.sort(key=lambda e: e.when, reverse=True)
    return entries


def _restore_target_path(name: str, saved: str) -> Optional[str]:
    """Maps a zip entry to its target under `saved`, whatever the zip layout
    ("Saved/...", "ConanSandbox/Saved/...", bare "game.db", "Config/...").
    Returns None for entries that aren't part of a save."""
    norm = name.replace("\\", "/")
    if norm.endswith("/") or not norm:
        return None  # directory entry

    lowered = norm.lower()
    marker = "/saved/"
    idx = lowered.find(marker)
    if idx != -1:
        rel = norm[idx + 1:]  # keeps the leading "Saved/"
    elif lowered.startswith("saved/"):
        rel = norm
    else:
        base = os.path.basename(norm)
        base_lower = base.lower()
        if base_lower.endswith((".db", ".db-wal", ".db-shm")):
            rel = f"Saved/{base}"
        elif "/config/" in lowered:
            cfg_idx = lowered.find("/config/")
            rel = f"Saved/Config/{norm[cfg_idx + len('/config/'):]}"
        elif lowered.startswith("config/"):
            rel = f"Saved/{norm}"
        elif base_lower.endswith((".ini",)):
            rel = f"Saved/Config/{base}"
        else:
            return None  # not something we recognize as part of a save

    return os.path.join(os.path.dirname(saved), *rel.split("/"))


def restore_backup(install_dir: str, backup: BackupEntry) -> None:
    """Extracts a backup into Saved/, after taking a safety backup of the current
    state. The caller must stop the server first and restart it after."""
    saved = _saved_dir(install_dir)
    os.makedirs(saved, exist_ok=True)

    safety_dest = os.path.dirname(backup.path)
    create_backup(install_dir, safety_dest, trigger="pre-restore-safety")

    # A leftover -wal from the current DB would be replayed onto the restored
    # .db and corrupt it; the backup brings its own -wal/-shm if it had any.
    for stale in glob.glob(os.path.join(saved, "*.db-wal")) + glob.glob(os.path.join(saved, "*.db-shm")):
        try:
            os.remove(stale)
        except OSError as exc:
            _log.warning(f"Couldn't remove stale WAL/SHM file {stale} before restore: {exc}")

    with zipfile.ZipFile(backup.path, "r") as zf:
        for member in zf.infolist():
            target = _restore_target_path(member.filename, saved)
            if target is None:
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def prune_backups(destination: str, daily_keep: int, weekly_keep: int) -> List[str]:
    """Keeps the newest `daily_keep` scheduled backups plus one per week for
    `weekly_keep` weeks; deletes the rest. Returns deleted paths.
    Only "scheduled" backups are ever pruned, and at least one backup always remains."""
    entries = list_backups(destination)
    if not entries:
        return []

    prunable = [e for e in entries if e.trigger == TRIGGER_SCHEDULED]
    protected = [e for e in entries if e.trigger != TRIGGER_SCHEDULED]

    keep_paths = {e.path for e in protected}
    keep_paths |= {e.path for e in prunable[:max(daily_keep, 0)]}
    older = prunable[max(daily_keep, 0):]

    seen_weeks = set()
    for e in older:
        week_key = e.when.isocalendar()[:2]  # (ISO year, ISO week)
        if week_key not in seen_weeks and len(seen_weeks) < weekly_keep:
            seen_weeks.add(week_key)
            keep_paths.add(e.path)

    if not keep_paths:
        keep_paths.add(entries[0].path)  # entries is newest-first; never end up with zero backups

    deleted = []
    for e in entries:
        if e.path not in keep_paths:
            try:
                os.remove(e.path)
                deleted.append(e.path)
            except OSError as exc:
                _log.warning(f"Couldn't delete old backup {e.path} during pruning: {exc}")
    return deleted


TRIGGER_IMPORTED = "imported"


class ImportValidationError(Exception):
    """The chosen file doesn't look like a Conan Exiles save."""


def validate_backup_zip(source_path: str) -> None:
    """Raises ImportValidationError unless the zip contains something that looks
    like a world DB or server config. Catches wrong files, not bad saves."""
    if not zipfile.is_zipfile(source_path):
        raise ImportValidationError(f"{os.path.basename(source_path)} isn't a zip file.")

    with zipfile.ZipFile(source_path, "r") as zf:
        names = zf.namelist()

    looks_like_save = any(
        n.endswith(".db") or n.endswith(".db-wal") or n.endswith(".db-shm")
        or "ServerSettings.ini" in n or "Engine.ini" in n or "/Saved/" in n or n.startswith("Saved/")
        for n in names
    )
    if not looks_like_save:
        raise ImportValidationError(
            "This zip doesn't contain anything that looks like a Conan Exiles save "
            "(no .db file, no Saved/ folder, no ServerSettings.ini/Engine.ini). "
            "Make sure you picked the right file."
        )


def import_external_backup(source_path: str, destination: str, label: str = "") -> BackupEntry:
    """Validates an outside zip and copies it into `destination` under ConanOps'
    naming so it lists and restores like any backup. Imports world/config data
    only, not ConanOps settings. Raises ImportValidationError or OSError."""
    validate_backup_zip(source_path)

    os.makedirs(destination, exist_ok=True)
    ts = datetime.now()
    suffix = f"_{label}" if label else ""
    fname = f"{ts.strftime('%Y%m%d-%H%M%S')}_{TRIGGER_IMPORTED}{suffix}.zip"
    out_path = os.path.join(destination, fname)
    shutil.copy2(source_path, out_path)

    size = os.path.getsize(out_path)
    return BackupEntry(path=out_path, when=ts, trigger=TRIGGER_IMPORTED, size_bytes=size)
