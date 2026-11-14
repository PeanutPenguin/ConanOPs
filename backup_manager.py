"""Create, list, restore, import and prune world-save backups.

A backup zips ConanSandbox/Saved/*.db (+ -wal/-shm) and Saved/Config/.
The filename encodes time and trigger, e.g. 20260919-110243_scheduled.zip.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional

import applog

_log = applog.get_logger(__name__)

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"
TRIGGER_PRE_UPDATE = "pre-update"
TRIGGER_SAFETY = "pre-restore-safety"
AUTOMATIC_SAFETY_TRIGGERS = (TRIGGER_PRE_UPDATE, TRIGGER_SAFETY)
MANIFEST_NAME = "conanops-backup.json"
_SNAPSHOT_PREFIX = ".conanops-snapshot-"


@dataclass
class BackupEntry:
    path: str
    when: datetime
    trigger: str
    size_bytes: int
    verified: bool = True  # every world database in it passed SQLite's check

    @property
    def size_label(self) -> str:
        mb = self.size_bytes / (1024 * 1024)
        return f"{mb:.0f} MB" if mb >= 1 else f"{self.size_bytes} B"


def saved_dir(install_dir: str) -> str:
    """The server's live world-save folder (ConanSandbox/Saved)."""
    return os.path.join(install_dir, "ConanSandbox", "Saved")


_saved_dir = saved_dir


def default_backup_destination(install_dir: str, taken=()) -> str:
    """Default backup folder: a "backups" sibling of install_dir (not inside it,
    so server validation never touches backups). "" if install_dir is empty.
    taken: other servers' backup folders. Two servers side by side would
    otherwise share one folder, and so list, thin out and restore each
    other's backups; the second gets "backups-<its folder's name>"."""
    if not install_dir:
        return ""
    install = os.path.abspath(install_dir)
    used = {os.path.normcase(os.path.abspath(t)) for t in taken if t}
    candidate = os.path.join(os.path.dirname(install), "backups")
    name = os.path.basename(install.rstrip("\\/")) or "server"
    n = 1
    while os.path.normcase(candidate) in used:
        candidate = os.path.join(os.path.dirname(install), f"backups-{name}" + ("" if n == 1 else f"-{n}"))
        n += 1
    return candidate


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


_PARTIAL_SUFFIX = ".partial"


def _unique_path(destination: str, stem: str) -> str:
    """destination/stem.zip, or stem_2.zip... if two backups land in the same second."""
    path = os.path.join(destination, stem + ".zip")
    n = 2
    while os.path.exists(path) or os.path.exists(path + _PARTIAL_SUFFIX):
        path = os.path.join(destination, f"{stem}_{n}.zip")
        n += 1
    return path


def cleanup_partial_backups(destination: str) -> None:
    """Removes unfinished backups left by a crash (best-effort)."""
    for path in glob.glob(os.path.join(destination, "*.zip" + _PARTIAL_SUFFIX)):
        try:
            os.remove(path)
        except OSError:
            pass
    for path in glob.glob(os.path.join(destination, _SNAPSHOT_PREFIX + "*")):
        shutil.rmtree(path, ignore_errors=True)


def _snapshot_dir_name(destination: str) -> str:
    return os.path.join(destination, f"{_SNAPSHOT_PREFIX}{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}")


def _sqlite_uri(path: str) -> str:
    from urllib.request import pathname2url
    return f"file:{pathname2url(os.path.abspath(path))}?mode=ro"


def _quick_check(path: str) -> Optional[bool]:
    """SQLite's own check that a database file is whole: True, False (damaged,
    or not a database at all), or None when it couldn't be checked (the file
    couldn't be opened), which says nothing about the file itself."""
    try:
        con = sqlite3.connect(_sqlite_uri(path), uri=True, timeout=30)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
        finally:
            con.close()
        return bool(row) and str(row[0]).lower() == "ok"
    except sqlite3.OperationalError as e:
        if "unable to open" in str(e).lower():
            _log.warning(f"Couldn't open {path} to check it: {e}")
            return None
        _log.warning(f"{path} failed SQLite's check: {e}")
        return False
    except sqlite3.Error as e:
        _log.warning(f"{path} failed SQLite's check: {e}")
        return False


def _journal_mode(path: str) -> str:
    try:
        con = sqlite3.connect(_sqlite_uri(path), uri=True, timeout=30)
        try:
            return str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        finally:
            con.close()
    except sqlite3.Error:
        return ""


def _sqlite_copy(src: str, dst: str) -> bool:
    """A consistent copy of the database as of one moment, made by SQLite
    itself, so a server writing to it can't leave a torn copy."""
    try:
        source = sqlite3.connect(_sqlite_uri(src), uri=True, timeout=60)
        try:
            target = sqlite3.connect(dst)
            try:
                source.backup(target)  # one step: one consistent read
                if str(target.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                    target.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # everything into the .db file
            finally:
                target.close()
        finally:
            source.close()
        return True
    except sqlite3.Error as e:
        _log.warning(f"SQLite couldn't copy {src} ({e}); copying the file instead.")
        try:
            os.remove(dst)
        except OSError:
            pass
        return False


def _file_copy(src: str, dst: str) -> None:
    """The database file plus its -wal, which holds changes not yet merged in."""
    shutil.copyfile(src, dst)
    if os.path.exists(src + "-wal"):
        shutil.copyfile(src + "-wal", dst + "-wal")


def _snapshot_worlds(saved: str, snap_dir: str, live: bool, settle=None) -> tuple:
    """Copies every world database into snap_dir. Returns (paths, verified).
    A database in WAL mode, or one no server is using, is copied by SQLite in
    one consistent read. A live database in the older journal mode is copied
    as a file once the server has finished saving (a long SQLite read could
    make its next save wait). Every copy is then checked by SQLite; a copy that
    fails is made again once."""
    os.makedirs(snap_dir, exist_ok=True)
    out, verified = [], True
    for src in sorted(glob.glob(os.path.join(saved, "*.db"))):
        dst = os.path.join(snap_dir, os.path.basename(src))
        ok = False
        for attempt in range(2):
            for leftover in (dst, dst + "-wal"):
                if os.path.exists(leftover):
                    os.remove(leftover)
            use_sqlite = not live or _journal_mode(src) == "wal"
            via_sqlite = use_sqlite and _sqlite_copy(src, dst)
            if not via_sqlite:
                if live and settle is not None:
                    settle()
                _file_copy(src, dst)
            ok = _quick_check(dst) is not False
            # SQLite's own copy is complete in the .db file; checking it can
            # leave empty -wal/-shm files beside it, which don't belong in a backup.
            for extra in (dst + "-shm", dst + "-wal"):
                if os.path.exists(extra) and (extra.endswith("-shm") or via_sqlite or os.path.getsize(extra) == 0):
                    os.remove(extra)
            if ok:
                break
            _log.warning(f"The copy of {os.path.basename(src)} didn't pass SQLite's check"
                         f"{'; trying once more' if attempt == 0 else ''}.")
            if settle is not None:
                settle()
        if not ok:
            verified = False
        out.append(dst)
        if os.path.exists(dst + "-wal"):
            out.append(dst + "-wal")
    return out, verified


def _world_bytes(saved: str) -> int:
    total = 0
    for f in glob.glob(os.path.join(saved, "*.db")) + glob.glob(os.path.join(saved, "*.db-wal")):
        try:
            total += os.path.getsize(f)
        except OSError:
            pass
    return total


def create_backup(install_dir: str, destination: str, trigger: str, label: str = "",
                  live: Optional[bool] = None, settle=None, info: Optional[dict] = None) -> Optional[BackupEntry]:
    """Zips a consistent, SQLite-checked copy of the world plus Saved/Config.
    live: whether a server is using the world now (None: find out). settle:
    called to wait until the server has finished saving. None if there's
    nothing to back up; raises BackupSpaceError or OSError on failure."""
    if not destination:
        return None
    saved = _saved_dir(install_dir)
    if not os.path.isdir(saved):
        return None
    os.makedirs(destination, exist_ok=True)
    if live is None:
        import process_manager
        live = process_manager.find_running_pid(install_dir) is not None

    ts = datetime.now()
    suffix = f"_{label}" if label else ""
    out_path = _unique_path(destination, f"{ts.strftime('%Y%m%d-%H%M%S')}_{trigger}{suffix}")
    config_dir = os.path.join(saved, "Config")
    # Room for the copy of the world made first, and the zip made from it.
    _ensure_space_for_backup(glob.glob(os.path.join(saved, "*.db")) * 2, config_dir, destination)

    snap_dir = _snapshot_dir_name(destination)
    partial = out_path + _PARTIAL_SUFFIX
    try:
        worlds, verified = _snapshot_worlds(saved, snap_dir, live, settle)
        manifest = dict(info or {}, format=1, made=ts.isoformat(timespec="seconds"), trigger=trigger,
                        verified=verified, worlds=[os.path.basename(w) for w in worlds],
                        world_bytes=_world_bytes(saved), server_running=bool(live))
        # Written under a temporary name and renamed only once verified, so a
        # crash or full disk part-way through never leaves a broken .zip that
        # looks like a good backup.
        with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for f in worlds:
                zf.write(f, arcname=os.path.join("Saved", os.path.basename(f)))
            if os.path.isdir(config_dir):
                for root, _dirs, files in os.walk(config_dir):
                    for fn in files:
                        full = os.path.join(root, fn)
                        zf.write(full, arcname=os.path.join("Saved", os.path.relpath(full, saved)))
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=1))
        try:
            with zipfile.ZipFile(partial, "r") as zf:
                bad_entry = zf.testzip()
        except zipfile.BadZipFile:
            bad_entry = "(the file itself)"
        if bad_entry is not None:
            _log.error(f"Backup {out_path} failed its zip check (bad entry: {bad_entry}) -- deleting it.")
            return None
        os.replace(partial, out_path)
    finally:
        shutil.rmtree(snap_dir, ignore_errors=True)
        if os.path.exists(partial):
            try:
                os.remove(partial)
            except OSError as e:
                _log.error(f"Couldn't delete the unfinished backup {partial}: {e}")

    entry = BackupEntry(path=out_path, when=ts, trigger=trigger, size_bytes=os.path.getsize(out_path))
    entry.verified = verified
    return entry


def create_backup_for_server(server, destination: str, trigger: str, label: str = "") -> Optional[BackupEntry]:
    """create_backup() of a server: asks it to save first (RCON) and waits for
    the save to finish, so the backup has the latest world."""
    import process_manager
    pid = process_manager.find_running_pid(server.install_dir) if server.install_dir else None
    live = pid is not None

    def settle():
        if pid is not None:
            process_manager._wait_for_save_to_settle(server.install_dir, pid, max_wait=90.0)

    if live and process_manager.request_world_save(server):
        settle()
    info = {"server": getattr(server, "name", ""), "build": getattr(server, "installed_buildid", "")}
    return create_backup(server.install_dir, destination, trigger, label, live=live, settle=settle, info=info)


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
            try:
                when = datetime.fromtimestamp(os.path.getmtime(path))
            except OSError:
                continue
            trigger = "unknown"
        try:
            size = os.path.getsize(path)
        except OSError:
            continue  # deleted while listing
        entries.append(BackupEntry(path=path, when=when, trigger=trigger, size_bytes=size))
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

    parts = rel.split("/")
    # A crafted zip ("Saved/../../Windows/x", "C:/...") must not write outside
    # the server's Saved folder; imported backups can come from anywhere.
    if any(p in ("", ".", "..") or ":" in p for p in parts):
        return None
    root = os.path.abspath(os.path.dirname(saved))
    target = os.path.abspath(os.path.join(root, *parts))
    inside = os.path.normcase(os.path.join(root, "Saved") + os.sep)
    if not os.path.normcase(target).startswith(inside):
        return None
    return target


class ServerRunningError(RuntimeError):
    pass


def restore_backup(install_dir: str, backup: BackupEntry) -> None:
    """Puts a backup's world and settings back into Saved/. The server must
    be stopped. A safety backup of the current state is made first; the
    backup's files are unpacked and checked beside Saved/ before any current
    file is replaced, so a damaged backup changes nothing."""
    import process_manager
    if process_manager.find_running_pid(install_dir) is not None:
        raise ServerRunningError("The server is still running, so nothing was restored. Stop it first.")
    saved = _saved_dir(install_dir)
    os.makedirs(saved, exist_ok=True)

    has_world = bool(glob.glob(os.path.join(saved, "*.db")))
    safety = create_backup(install_dir, os.path.dirname(backup.path), trigger=TRIGGER_SAFETY, live=False)
    if has_world and safety is None:
        raise OSError("Couldn't make a safety copy of the current world first, so nothing was restored.")

    stage = os.path.join(os.path.dirname(saved), f"{_SNAPSHOT_PREFIX}restore-{datetime.now():%Y%m%d-%H%M%S-%f}")
    try:
        placed = []
        with zipfile.ZipFile(backup.path, "r") as zf:
            for member in zf.infolist():
                target = _restore_target_path(member.filename, saved)
                if target is None:
                    continue
                staged = os.path.join(stage, os.path.relpath(target, os.path.dirname(saved)))
                os.makedirs(os.path.dirname(staged), exist_ok=True)
                with zf.open(member) as src, open(staged, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                placed.append((staged, target))
        for staged, _target in placed:
            if staged.endswith(".db") and _quick_check(staged) is False:
                raise OSError(f"{os.path.basename(staged)} in this backup is damaged (it failed SQLite's check), "
                              "so nothing was restored. Try an older backup.")
        # A leftover -wal from the current DB would be replayed onto the
        # restored .db and corrupt it; the backup brings its own if it had one.
        for stale in glob.glob(os.path.join(saved, "*.db-wal")) + glob.glob(os.path.join(saved, "*.db-shm")):
            os.remove(stale)
        for staged, target in placed:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(staged, target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


# Safety copies made automatically (before an update or a restore): the newest
# few of each kind, and any from the last two weeks, are kept.
SAFETY_KEEP = 3
SAFETY_KEEP_DAYS = 14


def prune_backups(destination: str, daily_keep: int, weekly_keep: int,
                  now: Optional[datetime] = None) -> List[str]:
    """Scheduled backups: every one from the last 24 hours, then the newest of
    each day for `daily_keep` days, then the newest of each week for
    `weekly_keep` weeks before that. Pre-update and pre-restore copies: the
    newest SAFETY_KEEP of each, plus any from the last SAFETY_KEEP_DAYS.
    Manual and imported backups are never deleted, and at least one backup
    always remains. Returns the deleted paths."""
    cleanup_partial_backups(destination)
    entries = list_backups(destination)
    if not entries:
        return []
    now = now or datetime.now()
    keep = set()

    scheduled = [e for e in entries if e.trigger == TRIGGER_SCHEDULED]  # newest first
    keep |= {e.path for e in scheduled if now - e.when <= timedelta(hours=24)}
    days = []
    for e in scheduled:
        if not days or days[-1][0] != e.when.date():
            days.append((e.when.date(), e))
    for _day, e in days[:max(daily_keep, 0)]:
        keep.add(e.path)
    oldest_daily = days[min(max(daily_keep, 0), len(days)) - 1][0] if daily_keep > 0 and days else None
    weeks = []
    for e in scheduled:
        if oldest_daily is not None and e.when.date() >= oldest_daily:
            continue
        week = e.when.isocalendar()[:2]
        if not weeks or weeks[-1][0] != week:
            weeks.append((week, e))
    for _week, e in weeks[:max(weekly_keep, 0)]:
        keep.add(e.path)

    for trigger in AUTOMATIC_SAFETY_TRIGGERS:
        safety = [e for e in entries if e.trigger == trigger]
        keep |= {e.path for e in safety[:SAFETY_KEEP]}
        keep |= {e.path for e in safety if now - e.when <= timedelta(days=SAFETY_KEEP_DAYS)}

    pruned_kinds = {TRIGGER_SCHEDULED, *AUTOMATIC_SAFETY_TRIGGERS}
    keep |= {e.path for e in entries if e.trigger not in pruned_kinds}
    if not keep:
        keep.add(entries[0].path)  # newest-first: never end up with zero backups

    deleted = []
    for e in entries:
        if e.path not in keep:
            try:
                os.remove(e.path)
                deleted.append(e.path)
            except OSError as exc:
                _log.warning(f"Couldn't delete old backup {e.path} during pruning: {exc}")
    return deleted


def _same_folder(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def sync_copies(destination: str, copy_destination: str, daily_keep: int, weekly_keep: int) -> tuple:
    """Copies every backup in `destination` that `copy_destination` doesn't
    have yet (so one missed while that drive was unplugged catches up), then
    applies the same retention there. Never deletes anything in the main
    folder. Returns (copied paths, error message or "")."""
    if not copy_destination or not destination or _same_folder(destination, copy_destination):
        return [], ""
    try:
        os.makedirs(copy_destination, exist_ok=True)
    except OSError as e:
        return [], f"Couldn't open the second backup folder {copy_destination}: {e}"
    have = {os.path.basename(e.path) for e in list_backups(copy_destination)}
    copied, error = [], ""
    for entry in reversed(list_backups(destination)):  # oldest first, so the newest lands last
        name = os.path.basename(entry.path)
        if name in have:
            continue
        target = os.path.join(copy_destination, name)
        partial = target + _PARTIAL_SUFFIX
        try:
            try:
                free = shutil.disk_usage(copy_destination).free
            except OSError:
                free = None
            if free is not None and free < entry.size_bytes + _BACKUP_HEADROOM_BYTES:
                error = (f"Not enough free space in the second backup folder {copy_destination} for "
                         f"{name} ({entry.size_label}).")
                break
            shutil.copyfile(entry.path, partial)
            with zipfile.ZipFile(partial, "r") as zf:
                if zf.testzip() is not None:
                    raise OSError("the copy didn't match the original")
            os.replace(partial, target)
            copied.append(target)
        except (OSError, zipfile.BadZipFile) as e:
            error = f"Couldn't copy {name} to the second backup folder {copy_destination}: {e}"
            break
        finally:
            if os.path.exists(partial):
                try:
                    os.remove(partial)
                except OSError:
                    pass
    prune_backups(copy_destination, daily_keep, weekly_keep)
    return copied, error


@dataclass
class BackupOutcome:
    entry: Optional[BackupEntry] = None
    error: str = ""           # why there's no backup ("" when made)
    copy_error: str = ""      # the backup was made, but the second copy wasn't
    copied: int = 0
    pruned: int = 0


def run_backup_job(server, trigger: str, label: str = "") -> BackupOutcome:
    """One complete backup: make it, check it, copy it to the second folder,
    then thin out old ones in both. Never raises."""
    out = BackupOutcome()
    try:
        out.entry = create_backup_for_server(server, server.backup_destination, trigger, label)
    except BackupSpaceError as e:
        out.error = str(e)
        return out
    except Exception as e:  # noqa: BLE001 - reported, never raised from a worker
        _log.error(f"Backup of {getattr(server, 'name', '?')} failed: {e}")
        out.error = f"The backup couldn't be written: {e}"
        return out
    if out.entry is None:
        saved = saved_dir(server.install_dir)
        out.error = ("The backup file didn't pass its own check after being written, so it was deleted. This "
                     "usually means a disk problem on the backup folder's drive." if os.path.isdir(saved) else
                     "There's no world save to back up yet: Conan Exiles creates it the first time the server "
                     "starts.")
        return out
    try:
        out.pruned = len(prune_backups(server.backup_destination, server.backup_daily_keep,
                                       server.backup_weekly_keep))
    except OSError as e:
        _log.warning(f"Couldn't thin out old backups: {e}")
    copy_to = getattr(server, "backup_copy_destination", "")
    if copy_to:
        try:
            copied, out.copy_error = sync_copies(server.backup_destination, copy_to,
                                                 server.backup_daily_keep, server.backup_weekly_keep)
            out.copied = len(copied)
        except Exception as e:  # noqa: BLE001
            out.copy_error = f"Couldn't copy the backup to the second folder: {e}"
    return out


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
    out_path = _unique_path(destination, f"{ts.strftime('%Y%m%d-%H%M%S')}_{TRIGGER_IMPORTED}{suffix}")
    partial = out_path + _PARTIAL_SUFFIX
    try:
        shutil.copy2(source_path, partial)
        os.replace(partial, out_path)
    finally:
        if os.path.exists(partial):
            os.remove(partial)

    size = os.path.getsize(out_path)
    return BackupEntry(path=out_path, when=ts, trigger=TRIGGER_IMPORTED, size_bytes=size)
