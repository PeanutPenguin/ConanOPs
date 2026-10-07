from __future__ import annotations

import os

import mod_manager


def test_snapshot_none_when_no_saved_dir(tmp_path):
    assert mod_manager.snapshot_world_save(str(tmp_path / "install")) is None


def test_snapshot_none_when_no_world_save_files(tmp_path):
    saved_dir = tmp_path / "install" / "ConanSandbox" / "Saved" / "Config"
    saved_dir.mkdir(parents=True)
    assert mod_manager.snapshot_world_save(str(tmp_path / "install")) is None


def test_snapshot_and_restore_round_trip(tmp_path):
    install_dir = tmp_path / "install"
    saved_dir = install_dir / "ConanSandbox" / "Saved"
    saved_dir.mkdir(parents=True)
    (saved_dir / "game.db").write_text("original")

    snapshot = mod_manager.snapshot_world_save(str(install_dir))
    assert snapshot is not None

    (saved_dir / "game.db").write_text("mutated")
    mod_manager.restore_world_save(str(install_dir), snapshot)

    assert (saved_dir / "game.db").read_text() == "original"


def test_restore_never_touches_config(tmp_path):
    install_dir = tmp_path / "install"
    saved_dir = install_dir / "ConanSandbox" / "Saved"
    config_dir = saved_dir / "Config" / "WindowsServer"
    config_dir.mkdir(parents=True)
    (saved_dir / "game.db").write_text("original")
    (config_dir / "Engine.ini").write_text("bind_ip=1.2.3.4")

    snapshot = mod_manager.snapshot_world_save(str(install_dir))
    (saved_dir / "game.db").write_text("mutated")
    (config_dir / "Engine.ini").write_text("bind_ip=9.9.9.9")

    mod_manager.restore_world_save(str(install_dir), snapshot)

    assert (saved_dir / "game.db").read_text() == "original"
    assert (config_dir / "Engine.ini").read_text() == "bind_ip=9.9.9.9"


def test_restore_is_a_noop_with_no_snapshot(tmp_path):
    install_dir = tmp_path / "install"
    saved_dir = install_dir / "ConanSandbox" / "Saved"
    saved_dir.mkdir(parents=True)
    (saved_dir / "game.db").write_text("untouched")

    mod_manager.restore_world_save(str(install_dir), None)  # must not raise

    assert (saved_dir / "game.db").read_text() == "untouched"


def test_cleanup_removes_the_temp_dir(tmp_path):
    install_dir = tmp_path / "install"
    saved_dir = install_dir / "ConanSandbox" / "Saved"
    saved_dir.mkdir(parents=True)
    (saved_dir / "game.db").write_text("x")

    snapshot = mod_manager.snapshot_world_save(str(install_dir))
    assert os.path.isdir(snapshot)

    mod_manager.cleanup_world_save_snapshot(snapshot)

    assert not os.path.exists(snapshot)


def test_cleanup_is_a_noop_with_none():
    mod_manager.cleanup_world_save_snapshot(None)  # must not raise


def test_world_save_files_matches_db_wal_shm_only(tmp_path):
    saved_dir = tmp_path / "Saved"
    saved_dir.mkdir()
    (saved_dir / "game.db").write_text("x")
    (saved_dir / "game.db-wal").write_text("x")
    (saved_dir / "game.db-shm").write_text("x")
    (saved_dir / "readme.txt").write_text("x")

    files = mod_manager.world_save_files(str(saved_dir))

    names = {os.path.basename(f) for f in files}
    assert names == {"game.db", "game.db-wal", "game.db-shm"}
