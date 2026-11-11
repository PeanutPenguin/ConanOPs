"""2.0: the world report, read from a backup and tolerant of the unknown layout."""
from __future__ import annotations

import os
import sqlite3
import zipfile

import world_report

NOW = 1_800_000_000.0
DAY = 86400


def _conan_world(path, *, with_instances=True, with_last=True):
    con = sqlite3.connect(str(path))
    last = ", lastTimeOnline INTEGER" if with_last else ""
    con.execute(f"CREATE TABLE characters (id INTEGER, playerId TEXT, char_name TEXT, level INTEGER, guild INTEGER{last})")
    con.execute("CREATE TABLE guilds (guildId INTEGER, name TEXT, owner INTEGER)")
    con.execute("CREATE TABLE buildings (object_id INTEGER, owner_id INTEGER)")
    if with_instances:
        con.execute("CREATE TABLE building_instances (object_id INTEGER, instance_id INTEGER, class TEXT)")
    con.executemany("INSERT INTO guilds VALUES (?, ?, ?)", [(100, "Wolves", 1), (200, "Ghosts", 3)])
    chars = [(1, "p1", "Ana", 60, 100, NOW - 2 * DAY), (2, "p2", "Bo", 40, 100, NOW - 40 * DAY),
             (3, "p3", "Cy", 30, 200, NOW - 90 * DAY), (4, "p4", "Di", 10, None, NOW - 31 * DAY)]
    if with_last:
        con.executemany("INSERT INTO characters VALUES (?, ?, ?, ?, ?, ?)", chars)
    else:
        con.executemany("INSERT INTO characters VALUES (?, ?, ?, ?, ?)", [c[:5] for c in chars])
    # Wolves: two buildings of 3 + 2 pieces; Ghosts one of 4; Di (solo) one of 1.
    con.executemany("INSERT INTO buildings VALUES (?, ?)", [(10, 100), (11, 100), (12, 200), (13, 4)])
    if with_instances:
        rows = [(10, i, "Wall") for i in range(3)] + [(11, i, "Wall") for i in range(2)] \
            + [(12, i, "Wall") for i in range(4)] + [(13, 0, "Wall")]
        con.executemany("INSERT INTO building_instances VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()


def test_reads_clans_pieces_and_inactive_players(tmp_path):
    db = tmp_path / "game.db"
    _conan_world(db)
    rep = world_report.read_world(str(db), now=NOW)
    assert not rep.error and rep.characters == 4 and rep.clan_count == 2 and rep.pieces_total == 10
    assert [(c.name, c.members, c.pieces) for c in rep.clans] == [("Wolves", 2, 5), ("Ghosts", 1, 4)]
    assert rep.solo_pieces == 1
    assert [p.name for p in rep.inactive] == ["Cy", "Bo", "Di"]  # longest gone first
    assert rep.inactive[0].clan == "Ghosts" and rep.inactive[2].pieces == 1
    # Ghosts' only member is gone (4) + Di's own piece (1); Wolves still has Ana.
    assert rep.inactive_pieces == 5


def test_missing_tables_and_columns_are_left_out_not_fatal(tmp_path):
    db = tmp_path / "game.db"
    _conan_world(db, with_instances=False, with_last=False)
    rep = world_report.read_world(str(db), now=NOW)
    assert not rep.error and rep.pieces_total == 4  # buildings counted instead of pieces
    assert rep.inactive_count is None and rep.inactive == []
    assert any("last on" in n for n in rep.notes) and any("buildings are counted" in n for n in rep.notes)

    empty = tmp_path / "empty.db"
    sqlite3.connect(str(empty)).execute("CREATE TABLE something_else (x)").connection.close()
    rep = world_report.read_world(str(empty), now=NOW)
    assert not rep.error and rep.characters is None and rep.clan_count is None and rep.pieces_total is None
    assert len(rep.notes) == 3


def test_not_a_database_is_an_error_not_a_crash(tmp_path):
    bad = tmp_path / "game.db"
    bad.write_text("not sqlite")
    assert world_report.read_world(str(bad), now=NOW).error


def _backup(dest, stamp, db_path, verified_name=True):
    os.makedirs(dest, exist_ok=True)
    path = os.path.join(dest, f"{stamp}_scheduled.zip")
    with zipfile.ZipFile(path, "w") as zf:
        zf.write(db_path, "Saved/game.db")
        zf.writestr("Saved/Config/WindowsServer/ServerSettings.ini", "[ServerSettings]\n")
    return path


def test_build_report_uses_the_newest_backup_and_shows_growth(tmp_path):
    small, big = tmp_path / "small.db", tmp_path / "big.db"
    _conan_world(small)
    _conan_world(big)
    con = sqlite3.connect(str(big))
    con.execute("CREATE TABLE padding (x BLOB)")
    con.execute("INSERT INTO padding VALUES (?)", (b"x" * 200_000,))
    con.execute("UPDATE guilds SET name = 'Wolves II' WHERE guildId = 100")
    con.commit()
    con.close()
    dest = str(tmp_path / "backups")
    _backup(dest, "20261101-030000", str(small))
    _backup(dest, "20261108-030000", str(big))
    live = tmp_path / "live.db"
    _conan_world(live)
    before = os.path.getmtime(live)
    rep = world_report.build_report(dest, now=NOW)
    assert not rep.error and rep.backup_name.startswith("20261108")
    assert rep.clans[0].name == "Wolves II"
    assert [g.world_bytes for g in rep.growth] == [os.path.getsize(small), os.path.getsize(big)]
    assert os.path.getmtime(live) == before  # the live world is never opened
    assert rep.as_json()["clans"][0]["name"] == "Wolves II"


def test_build_report_without_backups_says_what_to_do(tmp_path):
    assert "make one first" in world_report.build_report(str(tmp_path / "none")).error
    os.makedirs(tmp_path / "b")
    assert "no checked backup" in world_report.build_report(str(tmp_path / "b")).error
