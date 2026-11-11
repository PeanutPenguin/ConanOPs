"""Small real SQLite world files for tests: backups now check every world
database with SQLite, so a text file standing in for game.db isn't enough."""
import os
import sqlite3
import tempfile


def write_world(path, text="world data") -> None:
    path = str(path)
    for leftover in (path, path + "-wal", path + "-shm", path + "-journal"):
        if os.path.exists(leftover):
            os.remove(leftover)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE marker (v TEXT)")
    con.execute("INSERT INTO marker VALUES (?)", (text,))
    con.commit()
    con.close()


def read_world(path) -> str:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute("SELECT v FROM marker").fetchone()[0]
    finally:
        con.close()


def world_bytes(text="world data") -> bytes:
    d = tempfile.mkdtemp()
    p = os.path.join(d, "w.db")
    write_world(p, text)
    with open(p, "rb") as f:
        return f.read()
