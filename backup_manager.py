"""
Backup management: zip up the world save + config, list what's on disk,
restore one, and prune old backups per the server's retention settings.

A backup captures:
  ConanSandbox/Saved/<map>.db (+ -wal / -shm if present)
  ConanSandbox/Saved/Config/

Filenames encode the trigger so the Backups tab can show it without a
separate index file: e.g. 20260919-110243_scheduled.zip
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
    """The server's live world-save folder -- ConanSandbox/Saved under
    its install_dir. This is what create_backup() reads from (and what
    a missing/empty entry means when a backup silently returns None --
    see diagnostics.py's "World save" check, which is what actually
    surfaces this to a person instead of a bare "backup failed")."""
    return os.path.join(install_dir, "ConanSandbox", "Saved")


# Old name, kept as an alias since it's still used within this module --
# no reason to touch every call site just to rename a private helper.
_saved_dir = saved_dir


def default_backup_destination(install_dir: str) -> str:
    """A sensible default backup folder for a server, given its
    install_dir: a "backups" folder as a SIBLING of install_dir,
    inside that server's own ConanOps folder (install_dir is
    typically .../ConanOps/<server id>/server, so this lands on
    .../ConanOps/<server id>/backups) -- not a subfolder of install_dir
    itself, since re-downloading/validating the server there shouldn't
    ever risk touching backup files.

    Used to auto-populate backup_destination when it's never been set
    (see MainWindow._load_active_server() and SetupWizard.apply_to_server()):
    scheduled and pre-update backups silently never ran at all while
    backup_destination stayed "" ("" is falsy, so every caller's
    `if server.backup_destination and ...` guard skipped them),
    requiring a person to notice the Backups settings page and set a
    folder by hand before backups did anything. Returns "" if
    install_dir itself is empty (server not set up yet -- nothing
    sensible to default to)."""
    if not install_dir:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(install_dir)), "backups")


class BackupSpaceError(OSError):
    """Not enough free space at the backup destination to write the zip."""


# Headroom on top of the (uncompressed) size of what's being backed up --
# compression usually makes the zip much smaller, but a world database
# can compress poorly, and filling the drive to zero is how backups AND
# the live world save both break.
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

    # Verify the zip is actually intact before calling this a success --
    # testzip() reads every entry's CRC without extracting it anywhere,
    # so this is cheap regardless of backup size. Without this, a
    # truncated write (disk filled up mid-copy, a crash during the zip)
    # could report success and only be discovered corrupt the one time
    # someone actually needed to restore it -- exactly the wrong moment
    # to find out a backup never worked.
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
    """Same as create_backup(), but first asks the running server to
    checkpoint via RCON (if RCON is enabled) so the .db/-wal/-shm files
    being copied reflect a save that just happened, rather than
    whatever they happened to look like mid-write. This is still not a
    perfect atomic snapshot (the copy itself isn't a single transaction
    against a live, changing database -- SQLite's own WAL mode is what
    keeps a mid-copy read consistent enough to be restorable, not this
    function), but it substantially narrows the window compared to
    copying files off a server that's never been told to save at all.
    Best-effort: if RCON isn't enabled, or the save command fails, this
    falls back to the plain copy exactly as create_backup() does."""
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
    """Maps one entry from a backup zip to the file it should land at
    under `saved` (ConanSandbox/Saved), regardless of how that zip is
    internally laid out.

    restore_backup() used to just extractall() the zip relative to the
    install dir's ConanSandbox folder, which only works when every
    entry is already rooted at "Saved/...". But validate_backup_zip()
    (the check a person's import has to pass) accepts several other
    shapes too -- a full "ConanSandbox/Saved/..." path from a whole-
    server backup, or even a bare "game.db" with no folder structure at
    all -- so an accepted-but-differently-shaped zip would previously
    extract to the wrong place (or the install root) while restore
    still reported success. This normalizes any of those shapes down
    to the one restore_backup() actually needs.

    Returns None for an entry that isn't part of the save at all (a
    directory entry, or something unrelated the zip happened to
    contain), so it's skipped rather than dumped somewhere arbitrary.
    """
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
    """Extracts a backup zip back into Saved/. Caller is responsible for
    stopping the server first and restarting it after -- this function
    only touches files. A safety copy of the current state is taken
    first so a bad restore isn't unrecoverable."""
    saved = _saved_dir(install_dir)
    os.makedirs(saved, exist_ok=True)

    # Safety copy of current state before overwriting anything.
    safety_dest = os.path.dirname(backup.path)
    create_backup(install_dir, safety_dest, trigger="pre-restore-safety")

    # Delete any existing -wal/-shm sidecar files before extracting.
    # SQLite replays a WAL onto whatever .db file is sitting next to
    # it on next open -- if the backup's .db is restored but a leftover
    # -wal from the CURRENT (pre-restore) database is left in place,
    # SQLite will apply those old, unrelated writes on top of the
    # restored database and corrupt it. The backup zip carries its own
    # -wal/-shm (if the source had any) which get written back out by
    # extractall() right after this, so this only removes ones that
    # aren't about to be replaced.
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
    """Simple retention: keep the newest `daily_keep` backups outright,
    then from what's older, keep one per week for `weekly_keep` weeks,
    deleting the rest. Returns the list of deleted paths.

    Two safety rules on top of that, both fixes for ways this used to
    be able to wipe out every backup a server had:

    1. Only SCHEDULED backups are ever pruned. Manual backups,
       imports, pre-update/pre-restore safety copies, and anything
       else with a non-"scheduled" trigger are never touched here --
       someone who explicitly backed up (or imported an external save)
       didn't ask for it to be silently deleted just because the daily/
       weekly retention counters ran out.
    2. At least one backup (of ANY trigger) is always kept, even if
       `daily_keep` and `weekly_keep` are both 0 -- otherwise setting
       both to 0 would prune away the backup that was just taken in
       the same run, leaving nothing to restore from at all.
    """
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
    """Raised when a file the person picked doesn't look like a Conan
    Exiles save at all -- e.g. they selected an unrelated zip, or a
    plain (non-zip) file."""


def validate_backup_zip(source_path: str) -> None:
    """Best-effort sanity check that a zip actually looks like a Conan
    Exiles save before we let anyone restore from it: does it contain at
    least one thing that looks like a world database or server config?
    This is not a guarantee the save is valid or from a compatible game
    version -- just a check against the obvious mistake of importing the
    wrong file entirely. Raises ImportValidationError with a clear
    message if the check fails; does nothing if it passes."""
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
    """Validates and copies an outside backup zip into this server's own
    backup folder, renamed to ConanOps' own naming convention so it
    shows up in the normal Backups list and can be restored the same way
    as any backup ConanOps made itself. Raises ImportValidationError if
    the file doesn't pass validate_backup_zip(); raises OSError if the
    copy itself fails (disk full, permissions, etc.) -- callers should
    catch both and show the person a clear message rather than letting
    either propagate as a generic crash.

    Note this only imports the world/config data. It does NOT copy over
    the source server's ConanOps *settings* (rates, mods, schedules,
    etc.) -- those live in that other server's own config entirely
    separately, and nothing about a world-save zip carries them. If the
    two servers were meant to match, that's a separate manual step on
    the Settings pages.
    """
    validate_backup_zip(source_path)

    os.makedirs(destination, exist_ok=True)
    ts = datetime.now()
    suffix = f"_{label}" if label else ""
    fname = f"{ts.strftime('%Y%m%d-%H%M%S')}_{TRIGGER_IMPORTED}{suffix}.zip"
    out_path = os.path.join(destination, fname)
    shutil.copy2(source_path, out_path)

    size = os.path.getsize(out_path)
    return BackupEntry(path=out_path, when=ts, trigger=TRIGGER_IMPORTED, size_bytes=size)
