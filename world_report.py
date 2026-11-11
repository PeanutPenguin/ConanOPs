"""
A report on a server's world: clans, how much each has built, players who
haven't been on in a while, and how the world file has grown.

It's always read from a backup, never the live world: the newest verified
backup's world is copied to a temporary file and opened read-only, so a
running server is never touched or slowed by a long read.

Funcom doesn't publish the world database's layout and it has changed
between game versions, so every table and column is looked up first and
anything missing is simply left out of the report (with a note saying so)
rather than failing.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

import applog
import backup_manager

_log = applog.get_logger(__name__)

INACTIVE_DAYS = 30
MAX_INACTIVE_LISTED = 200
MAX_CLANS_LISTED = 200


@dataclass
class Clan:
    id: str
    name: str
    members: int = 0
    pieces: int = 0          # building pieces it owns
    last_active: Optional[float] = None  # newest lastTimeOnline among its members


@dataclass
class Player:
    name: str
    clan: str
    level: Optional[int]
    last_online: Optional[float]
    pieces: int = 0          # building pieces in their own name (not their clan's)


@dataclass
class SizePoint:
    when: datetime
    world_bytes: int


@dataclass
class WorldReport:
    backup_name: str = ""
    backup_when: Optional[datetime] = None
    world_bytes: int = 0
    characters: Optional[int] = None
    clans: List[Clan] = field(default_factory=list)
    clan_count: Optional[int] = None
    pieces_total: Optional[int] = None
    solo_pieces: Optional[int] = None      # building pieces owned by players outside a clan
    inactive: List[Player] = field(default_factory=list)
    inactive_count: Optional[int] = None
    inactive_pieces: Optional[int] = None  # pieces owned by inactive players and all-inactive clans
    growth: List[SizePoint] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    error: str = ""

    def as_json(self) -> dict:
        return {
            "backup_name": self.backup_name,
            "backup_when": self.backup_when.isoformat(timespec="minutes") if self.backup_when else "",
            "world_bytes": self.world_bytes, "characters": self.characters, "clan_count": self.clan_count,
            "pieces_total": self.pieces_total, "solo_pieces": self.solo_pieces,
            "inactive_count": self.inactive_count, "inactive_pieces": self.inactive_pieces,
            "inactive_days": INACTIVE_DAYS,
            "clans": [c.__dict__ for c in self.clans],
            "inactive": [p.__dict__ for p in self.inactive],
            "growth": [{"when": g.when.isoformat(timespec="minutes"), "bytes": g.world_bytes} for g in self.growth],
            "notes": self.notes, "error": self.error,
        }


# ------------------------------------------------------------ the backup --

def _is_world(name: str) -> bool:
    n = name.replace("\\", "/").lower()
    return n.endswith(".db") and "/config/" not in n and not os.path.basename(n).startswith(".")


def _main_world(names: Sequence[Tuple[str, int]]) -> Optional[Tuple[str, int]]:
    """game.db if there is one, otherwise the biggest world database."""
    worlds = [(n, size) for n, size in names if _is_world(n)]
    for n, size in worlds:
        if os.path.basename(n.replace("\\", "/")).lower() == "game.db":
            return n, size
    return max(worlds, key=lambda w: w[1], default=None)


def world_size_history(destination: str, limit: int = 120) -> List[SizePoint]:
    """The world file's size in each backup (read from the zip's index, so
    nothing is unpacked), oldest first."""
    out = []
    for entry in sorted(backup_manager.list_backups(destination), key=lambda e: e.when)[-limit:]:
        try:
            with zipfile.ZipFile(entry.path) as zf:
                world = _main_world([(i.filename, i.file_size) for i in zf.infolist()])
        except (OSError, zipfile.BadZipFile):
            continue
        if world:
            out.append(SizePoint(entry.when, world[1]))
    return out


def newest_usable_backup(destination: str):
    for entry in backup_manager.list_backups(destination):  # newest first
        if not entry.verified:
            continue
        try:
            with zipfile.ZipFile(entry.path) as zf:
                if _main_world([(i.filename, i.file_size) for i in zf.infolist()]):
                    return entry
        except (OSError, zipfile.BadZipFile):
            continue
    return None


def _extract_world(zip_path: str, folder: str) -> Tuple[str, int]:
    with zipfile.ZipFile(zip_path) as zf:
        infos = {i.filename: i for i in zf.infolist()}
        world = _main_world([(n, i.file_size) for n, i in infos.items()])
        if world is None:
            raise FileNotFoundError("That backup has no world in it.")
        target = os.path.join(folder, "world.db")
        with zf.open(infos[world[0]]) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst, 1024 * 1024)
        wal = world[0] + "-wal"
        if wal in infos:
            with zf.open(infos[wal]) as src, open(target + "-wal", "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
        return target, world[1]


# --------------------------------------------------------- the database --

class _Schema:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.tables: Dict[str, Dict[str, str]] = {}
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            cols = {row[1].lower(): row[1] for row in con.execute(f'PRAGMA table_info("{name}")')}
            self.tables[name.lower()] = cols
        self._names = {name.lower(): name for (name,) in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    def table(self, *candidates: str) -> Optional[str]:
        for c in candidates:
            if c.lower() in self.tables:
                return self._names[c.lower()]
        return None

    def col(self, table: Optional[str], *candidates: str) -> Optional[str]:
        if not table:
            return None
        cols = self.tables.get(table.lower(), {})
        for c in candidates:
            if c.lower() in cols:
                return cols[c.lower()]
        return None


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _ts(value) -> Optional[float]:
    """lastTimeOnline is seconds since 1970 (some versions: milliseconds)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 1e11:
        v /= 1000.0
    return v


def read_world(path: str, now: Optional[float] = None, report: Optional[WorldReport] = None) -> WorldReport:
    """Reads an (unpacked) world database. Never raises for an odd layout."""
    now = time.time() if now is None else now
    rep = report or WorldReport()
    try:
        con = sqlite3.connect(f"file:{_uri_path(path)}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as e:
        rep.error = f"The world couldn't be opened: {e}"
        return rep
    try:
        _fill(rep, _Schema(con), now)
    except sqlite3.DatabaseError as e:
        rep.error = f"The world couldn't be read: {e}"
    finally:
        con.close()
    return rep


def _uri_path(path: str) -> str:
    from urllib.request import pathname2url
    return pathname2url(os.path.abspath(path))


def _fill(rep: WorldReport, db: _Schema, now: float) -> None:
    con = db.con
    chars = db.table("characters")
    c_id = db.col(chars, "id")
    c_name = db.col(chars, "char_name", "name")
    c_guild = db.col(chars, "guild", "guildId")
    c_level = db.col(chars, "level")
    c_last = db.col(chars, "lastTimeOnline", "last_time_online", "lastOnline")

    guilds = db.table("guilds")
    g_id = db.col(guilds, "guildId", "id")
    g_name = db.col(guilds, "name")

    pieces_by_owner: Dict[str, int] = {}
    buildings = db.table("buildings")
    b_obj = db.col(buildings, "object_id", "objectId")
    b_owner = db.col(buildings, "owner_id", "ownerId", "owner")
    instances = db.table("building_instances")
    i_obj = db.col(instances, "object_id", "objectId")
    if buildings and b_owner:
        if instances and i_obj and b_obj:
            # One building can be many pieces; count the pieces.
            rows = con.execute(
                f"SELECT b.{_q(b_owner)}, COUNT(i.{_q(i_obj)}) FROM {_q(buildings)} b "
                f"LEFT JOIN {_q(instances)} i ON i.{_q(i_obj)} = b.{_q(b_obj)} GROUP BY b.{_q(b_owner)}")
            for owner, n in rows:
                pieces_by_owner[str(owner)] = int(n or 0)
        else:
            rep.notes.append("This world doesn't list building pieces, so buildings are counted instead.")
            for owner, n in con.execute(f"SELECT {_q(b_owner)}, COUNT(*) FROM {_q(buildings)} "
                                        f"GROUP BY {_q(b_owner)}"):
                pieces_by_owner[str(owner)] = int(n or 0)
        rep.pieces_total = sum(pieces_by_owner.values())
    else:
        rep.notes.append("This world doesn't list buildings in a way ConanOps recognises, so building counts "
                         "are left out.")

    clans: Dict[str, Clan] = {}
    if guilds and g_id:
        rows = con.execute(f"SELECT {_q(g_id)}, {_q(g_name) if g_name else 'NULL'} FROM {_q(guilds)}")
        for gid, name in rows:
            key = str(gid)
            clans[key] = Clan(id=key, name=str(name or f"Clan {gid}"), pieces=pieces_by_owner.get(key, 0))
        rep.clan_count = len(clans)
    else:
        rep.notes.append("No clan list was found in this world.")

    players: List[Player] = []
    if chars and c_name:
        cols = [c_name, c_guild, c_level, c_last, c_id]
        sql = "SELECT " + ", ".join(_q(c) if c else "NULL" for c in cols) + f" FROM {_q(chars)}"
        for name, guild, level, last, cid in con.execute(sql):
            gkey = str(guild) if guild not in (None, "", 0, "0", -1, "-1") else ""
            clan = clans.get(gkey)
            p = Player(name=str(name or "?"), clan=clan.name if clan else "",
                       level=int(level) if isinstance(level, (int, float)) else None,
                       last_online=_ts(last), pieces=pieces_by_owner.get(str(cid), 0) if cid is not None else 0)
            players.append(p)
            if clan is not None:
                clan.members += 1
                if p.last_online and (clan.last_active is None or p.last_online > clan.last_active):
                    clan.last_active = p.last_online
        rep.characters = len(players)
        if c_last is None:
            rep.notes.append("This world doesn't record when players were last on, so inactive players can't "
                             "be listed.")
    else:
        rep.notes.append("No character list was found in this world.")

    if pieces_by_owner and players is not None:
        char_ids = set()
        if chars and c_id:
            char_ids = {str(r[0]) for r in con.execute(f"SELECT {_q(c_id)} FROM {_q(chars)}")}
        rep.solo_pieces = sum(n for owner, n in pieces_by_owner.items() if owner in char_ids)

    rep.clans = sorted(clans.values(), key=lambda c: (-c.pieces, c.name.lower()))[:MAX_CLANS_LISTED]

    if c_last is not None and players:
        cutoff = now - INACTIVE_DAYS * 86400
        inactive = [p for p in players if p.last_online is not None and p.last_online < cutoff]
        inactive.sort(key=lambda p: (p.last_online or 0))
        rep.inactive_count = len(inactive)
        rep.inactive = inactive[:MAX_INACTIVE_LISTED]
        idle_clans = [c for c in clans.values() if c.members and c.last_active is not None and c.last_active < cutoff]
        rep.inactive_pieces = sum(p.pieces for p in inactive) + sum(c.pieces for c in idle_clans)


def build_report(destination: str, now: Optional[float] = None) -> WorldReport:
    """The whole report from the newest verified backup in `destination`."""
    rep = WorldReport()
    if not destination or not os.path.isdir(destination):
        rep.error = "There are no backups yet. The report is made from a backup, so make one first."
        return rep
    rep.growth = world_size_history(destination)
    entry = newest_usable_backup(destination)
    if entry is None:
        rep.error = "There's no checked backup with a world in it yet. Make a backup, then try again."
        return rep
    rep.backup_name = os.path.basename(entry.path)
    rep.backup_when = entry.when
    try:
        with zipfile.ZipFile(entry.path) as zf:
            needed = _main_world([(i.filename, i.file_size) for i in zf.infolist()])[1]
        free = shutil.disk_usage(tempfile.gettempdir()).free
    except (OSError, zipfile.BadZipFile, TypeError) as e:
        rep.error = f"The backup couldn't be read: {e}"
        return rep
    if free < needed * 1.2 + 50 * 1024 * 1024:
        rep.error = (f"There isn't enough free space on the drive Windows uses for temporary files to unpack "
                     f"the world ({needed / 1024 ** 3:.1f} GB needed).")
        return rep
    folder = tempfile.mkdtemp(prefix="conanops-world-")
    try:
        try:
            path, size = _extract_world(entry.path, folder)
        except (OSError, zipfile.BadZipFile, KeyError) as e:
            rep.error = f"The world couldn't be taken out of the backup: {e}"
            return rep
        rep.world_bytes = size
        read_world(path, now=now, report=rep)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return rep
