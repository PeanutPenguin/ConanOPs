from __future__ import annotations

import os
import zipfile
from datetime import datetime, timedelta

import pytest

import backup_manager as bm


def _make_fake_server(tmp_path, name="server"):
    install_dir = tmp_path / name
    saved = install_dir / "ConanSandbox" / "Saved"
    saved.mkdir(parents=True)
    (saved / "game.db").write_text("world data")
    config = saved / "Config"
    config.mkdir()
    (config / "ServerSettings.ini").write_text("[General]\nMaxPlayers=40\n")
    return str(install_dir)


def test_create_backup_contains_db_and_config(tmp_path):
    install_dir = _make_fake_server(tmp_path)
    dest = str(tmp_path / "backups")

    entry = bm.create_backup(install_dir, dest, trigger=bm.TRIGGER_MANUAL)

    assert entry is not None
    assert os.path.exists(entry.path)
    with zipfile.ZipFile(entry.path) as zf:
        names = zf.namelist()
    assert any(n.endswith("game.db") for n in names)
    assert any("ServerSettings.ini" in n for n in names)


def test_create_backup_returns_none_when_no_saved_dir(tmp_path):
    install_dir = str(tmp_path / "no-server-here")
    dest = str(tmp_path / "backups")
    assert bm.create_backup(install_dir, dest, trigger=bm.TRIGGER_MANUAL) is None


def test_list_backups_parses_trigger_and_sorts_newest_first(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "20260101-000000_scheduled.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # empty zip
    (dest / "20260201-000000_manual.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    entries = bm.list_backups(str(dest))

    assert [e.trigger for e in entries] == ["manual", "scheduled"]  # newest first
    assert entries[0].when > entries[1].when


def test_restore_backup_extracts_into_saved_and_takes_safety_copy(tmp_path):
    install_dir = _make_fake_server(tmp_path)
    dest = str(tmp_path / "backups")
    entry = bm.create_backup(install_dir, dest, trigger=bm.TRIGGER_MANUAL)

    # Simulate the world changing, then restoring the earlier backup.
    saved = os.path.join(install_dir, "ConanSandbox", "Saved")
    with open(os.path.join(saved, "game.db"), "w") as f:
        f.write("corrupted!!")

    bm.restore_backup(install_dir, entry)

    with open(os.path.join(saved, "game.db")) as f:
        assert f.read() == "world data"

    # A pre-restore safety backup should now also exist.
    backups_after = bm.list_backups(dest)
    assert any("pre-restore-safety" in e.trigger for e in backups_after)


def test_prune_backups_keeps_daily_and_deletes_the_rest(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    now = datetime.now()
    # 5 backups, one per day, all older than "daily_keep" window.
    for i in range(5):
        ts = (now - timedelta(days=i)).strftime("%Y%m%d-%H%M%S")
        (dest / f"{ts}_scheduled.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    deleted = bm.prune_backups(str(dest), daily_keep=2, weekly_keep=0)

    remaining = bm.list_backups(str(dest))
    assert len(remaining) == 2  # only daily_keep survive since weekly_keep=0
    assert len(deleted) == 3


def test_prune_backups_noop_when_under_daily_keep(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "20260101-000000_scheduled.zip").write_bytes(b"PK\x05\x06" + b"\x00" * 18)

    deleted = bm.prune_backups(str(dest), daily_keep=5, weekly_keep=2)

    assert deleted == []
    assert len(bm.list_backups(str(dest))) == 1


def test_validate_backup_zip_accepts_a_real_save(tmp_path):
    install_dir = _make_fake_server(tmp_path)
    dest = str(tmp_path / "backups")
    entry = bm.create_backup(install_dir, dest, trigger=bm.TRIGGER_MANUAL)
    bm.validate_backup_zip(entry.path)  # should not raise


def test_validate_backup_zip_rejects_unrelated_zip(tmp_path):
    junk = tmp_path / "vacation-photos.zip"
    with zipfile.ZipFile(junk, "w") as zf:
        zf.writestr("beach.jpg", b"not a save file")

    with pytest.raises(bm.ImportValidationError):
        bm.validate_backup_zip(str(junk))


def test_validate_backup_zip_rejects_non_zip(tmp_path):
    not_a_zip = tmp_path / "notes.txt"
    not_a_zip.write_text("hello")
    with pytest.raises(bm.ImportValidationError):
        bm.validate_backup_zip(str(not_a_zip))


def test_import_external_backup_copies_and_renames(tmp_path):
    install_dir = _make_fake_server(tmp_path)
    source_dest = str(tmp_path / "other-server-backups")
    source_entry = bm.create_backup(install_dir, source_dest, trigger=bm.TRIGGER_MANUAL)

    dest = str(tmp_path / "backups")
    imported = bm.import_external_backup(source_entry.path, dest)

    assert imported.trigger == bm.TRIGGER_IMPORTED
    assert os.path.exists(imported.path)
    assert imported.path != source_entry.path


def test_restore_handles_full_server_path_shape(tmp_path):
    """A zip laid out as a whole-server backup (ConanSandbox/Saved/...)
    passes validate_backup_zip() just like a Saved/-rooted one does, so
    restore has to handle that shape too, not just the one
    create_backup() itself produces."""
    install_dir = _make_fake_server(tmp_path)
    zip_path = tmp_path / "external.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("ConanSandbox/Saved/game.db", "imported world data")
        zf.writestr("ConanSandbox/Saved/Config/ServerSettings.ini", "[General]\nMaxPlayers=10\n")
    entry = bm.BackupEntry(path=str(zip_path), when=datetime.now(), trigger=bm.TRIGGER_IMPORTED, size_bytes=0)

    bm.restore_backup(install_dir, entry)

    saved = os.path.join(install_dir, "ConanSandbox", "Saved")
    with open(os.path.join(saved, "game.db")) as f:
        assert f.read() == "imported world data"
    with open(os.path.join(saved, "Config", "ServerSettings.ini")) as f:
        assert "MaxPlayers=10" in f.read()


def test_restore_handles_bare_db_with_no_folder_structure(tmp_path):
    """A zip with just a bare game.db at its root (no Saved/ folder at
    all) also passes validation, since it plainly looks like a save --
    restore should still put it in the right place."""
    install_dir = _make_fake_server(tmp_path)
    zip_path = tmp_path / "bare.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("game.db", "bare db contents")
    entry = bm.BackupEntry(path=str(zip_path), when=datetime.now(), trigger=bm.TRIGGER_IMPORTED, size_bytes=0)

    bm.restore_backup(install_dir, entry)

    saved = os.path.join(install_dir, "ConanSandbox", "Saved")
    with open(os.path.join(saved, "game.db")) as f:
        assert f.read() == "bare db contents"


def test_default_backup_destination_is_a_sibling_of_install_dir(tmp_path):
    install_dir = str(tmp_path / "s1" / "server")
    assert bm.default_backup_destination(install_dir) == str(tmp_path / "s1" / "backups")


def test_default_backup_destination_empty_when_no_install_dir():
    assert bm.default_backup_destination("") == ""


def test_saved_dir_is_public_and_matches_internal_alias(tmp_path):
    """saved_dir() is the public name; _saved_dir is kept only as an
    alias so this module's own internal call sites didn't all need
    touching -- confirms they actually agree."""
    install_dir = str(tmp_path / "server")
    assert bm.saved_dir(install_dir) == os.path.join(install_dir, "ConanSandbox", "Saved")
    assert bm._saved_dir is bm.saved_dir


# --------------------------------------------------------- integrity check --

def test_create_backup_verifies_the_zip_and_succeeds_normally(tmp_path):
    """The happy path -- a genuinely valid zip -- must still succeed;
    the integrity check shouldn't be a false-positive trap."""
    install_dir = _make_fake_server(tmp_path)
    dest = str(tmp_path / "backups")

    entry = bm.create_backup(install_dir, dest, bm.TRIGGER_MANUAL)

    assert entry is not None
    assert os.path.exists(entry.path)


def test_create_backup_deletes_a_corrupt_zip_and_returns_none(tmp_path, monkeypatch):
    """Simulates a truncated/corrupted write (disk full mid-copy, a
    crash during zipping) -- must not report success for a backup
    that wouldn't actually restore."""
    install_dir = _make_fake_server(tmp_path)
    dest = str(tmp_path / "backups")

    real_zipfile_init = zipfile.ZipFile

    written_paths = []

    class _CorruptingZipFile:
        """Wraps the real ZipFile for writing (so the backup gets built
        normally), but reports a bad entry when opened for reading --
        simulating testzip() catching real corruption without actually
        needing to hand-craft a broken zip file byte-for-byte."""
        def __init__(self, path, mode="r", *a, **k):
            self._real = real_zipfile_init(path, mode, *a, **k)
            self._mode = mode
            if mode == "w":
                written_paths.append(path)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._real.close()
            return False

        def write(self, *a, **k):
            return self._real.write(*a, **k)

        def testzip(self):
            return "Saved/corrupted.db" if self._mode == "r" else None

    monkeypatch.setattr(bm.zipfile, "ZipFile", _CorruptingZipFile)

    entry = bm.create_backup(install_dir, dest, bm.TRIGGER_MANUAL)

    assert entry is None
    assert len(written_paths) == 1
    assert not os.path.exists(written_paths[0])  # deleted, not left behind looking valid


def test_create_backup_handles_a_zip_that_fails_to_even_open(tmp_path, monkeypatch):
    install_dir = _make_fake_server(tmp_path)
    dest = str(tmp_path / "backups")

    real_zipfile_init = zipfile.ZipFile

    class _BadOnReopenZipFile:
        def __init__(self, path, mode="r", *a, **k):
            if mode == "r":
                raise zipfile.BadZipFile("not a zip file")
            self._real = real_zipfile_init(path, mode, *a, **k)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._real.close()
            return False

        def write(self, *a, **k):
            return self._real.write(*a, **k)

    monkeypatch.setattr(bm.zipfile, "ZipFile", _BadOnReopenZipFile)

    entry = bm.create_backup(install_dir, dest, bm.TRIGGER_MANUAL)

    assert entry is None
