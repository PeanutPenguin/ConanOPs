from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

import self_update


def _make_install_dir(tmp_path, name="install", version="1.0.0"):
    install_dir = tmp_path / name
    install_dir.mkdir()
    (install_dir / "main.py").write_text("print('hello')\n")
    (install_dir / "models.py").write_text("APP_ID = 443030\n")
    (install_dir / "version.py").write_text(f'VERSION = "{version}"\n')
    (install_dir / "unrelated_file.txt").write_text("leave me alone\n")
    return install_dir


def _make_update_zip(tmp_path, name="update.zip", wrapped=True, version="2.0.0", extra_files=None):
    zip_path = tmp_path / name
    prefix = "conanops/" if wrapped else ""
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{prefix}main.py", "print('hello v2')\n")
        zf.writestr(f"{prefix}models.py", "APP_ID = 443030\nNEW_THING = True\n")
        zf.writestr(f"{prefix}version.py", f'VERSION = "{version}"\n')
        for name, content in (extra_files or {}).items():
            zf.writestr(f"{prefix}{name}", content)
    return str(zip_path)


def _make_bad_zip(tmp_path, name="not-conanops.zip"):
    zip_path = tmp_path / name
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("readme.txt", "just some other zip")
    return str(zip_path)


# --------------------------------------------------------------- validation --

def test_validate_accepts_wrapped_zip(tmp_path):
    zip_path = _make_update_zip(tmp_path, wrapped=True)
    self_update.validate_update_zip(zip_path)  # should not raise


def test_validate_accepts_unwrapped_zip(tmp_path):
    zip_path = _make_update_zip(tmp_path, wrapped=False)
    self_update.validate_update_zip(zip_path)  # should not raise


def test_validate_rejects_non_zip(tmp_path):
    not_a_zip = tmp_path / "notes.txt"
    not_a_zip.write_text("hello")
    with pytest.raises(self_update.UpdateValidationError):
        self_update.validate_update_zip(str(not_a_zip))


def test_validate_rejects_unrelated_zip(tmp_path):
    zip_path = _make_bad_zip(tmp_path)
    with pytest.raises(self_update.UpdateValidationError):
        self_update.validate_update_zip(zip_path)


# ----------------------------------------------------------- happy path --

def test_apply_update_replaces_files_and_bumps_version(tmp_path):
    install_dir = _make_install_dir(tmp_path, version="1.0.0")
    zip_path = _make_update_zip(tmp_path, version="2.0.0")

    result = self_update.apply_update(zip_path, str(install_dir))

    assert result.success is True
    with open(install_dir / "version.py") as f:
        assert '2.0.0' in f.read()
    with open(install_dir / "main.py") as f:
        assert "hello v2" in f.read()


def test_apply_update_never_deletes_files_not_in_the_zip(tmp_path):
    """The overlay is non-destructive -- a local file the update
    package doesn't mention (e.g. something the person added, or a
    future version dropping a module) must survive."""
    install_dir = _make_install_dir(tmp_path)
    zip_path = _make_update_zip(tmp_path)

    self_update.apply_update(zip_path, str(install_dir))

    assert (install_dir / "unrelated_file.txt").exists()
    with open(install_dir / "unrelated_file.txt") as f:
        assert f.read() == "leave me alone\n"


def test_apply_update_adds_new_files_from_the_package(tmp_path):
    install_dir = _make_install_dir(tmp_path)
    zip_path = _make_update_zip(tmp_path, extra_files={"brand_new_module.py": "X = 1\n"})

    self_update.apply_update(zip_path, str(install_dir))

    assert (install_dir / "brand_new_module.py").exists()


def test_apply_update_handles_subfolders(tmp_path):
    install_dir = _make_install_dir(tmp_path)
    zip_path = _make_update_zip(tmp_path, extra_files={"ui/new_page.py": "class NewPage: pass\n"})

    self_update.apply_update(zip_path, str(install_dir))

    assert (install_dir / "ui" / "new_page.py").exists()


def test_successful_update_keeps_backup_until_confirmed(tmp_path):
    """The backup is a safety net until the NEW version actually starts
    up successfully -- apply_update() alone must not delete it, only
    confirm_update_success() (called after a clean startup) does."""
    install_dir = _make_install_dir(tmp_path)
    zip_path = _make_update_zip(tmp_path)

    result = self_update.apply_update(zip_path, str(install_dir))

    assert result.success is True
    marker = self_update._read_pending_marker()
    assert marker is not None
    assert marker["attempted"] is False
    backup_dir = marker["backup_dir"]
    assert os.path.isdir(backup_dir), "backup should still exist right after a successful apply"


def test_confirm_update_success_clears_backup_and_marker(tmp_path):
    install_dir = _make_install_dir(tmp_path)
    zip_path = _make_update_zip(tmp_path)

    self_update.apply_update(zip_path, str(install_dir))
    marker = self_update._read_pending_marker()
    backup_dir = marker["backup_dir"]
    assert os.path.isdir(backup_dir)

    self_update.confirm_update_success()

    assert not os.path.isdir(backup_dir)
    assert self_update._read_pending_marker() is None


def test_recover_pending_update_first_launch_marks_attempted_and_does_nothing_else(tmp_path):
    install_dir = _make_install_dir(tmp_path)
    zip_path = _make_update_zip(tmp_path)
    self_update.apply_update(zip_path, str(install_dir))

    msg = self_update.check_and_recover_pending_update()

    assert msg is None
    marker = self_update._read_pending_marker()
    assert marker is not None and marker["attempted"] is True
    # main.py's job is to run apply_update() ->
    # check_and_recover_pending_update() -> ... -> confirm_update_success();
    # nothing should have been rolled back on this first pass.
    with open(os.path.join(install_dir, "version.py")) as f:
        assert "2.0.0" in f.read()


def test_recover_pending_update_rolls_back_after_failed_first_launch(tmp_path):
    """Simulates: apply_update() succeeded, the first post-update launch
    started (marking attempted=True) but crashed before confirming --
    the *next* launch's recovery check should restore the old version."""
    install_dir = _make_install_dir(tmp_path, version="1.0.0")
    zip_path = _make_update_zip(tmp_path, version="2.0.0")
    self_update.apply_update(zip_path, str(install_dir))
    self_update.check_and_recover_pending_update()  # first launch: marks attempted

    with open(os.path.join(install_dir, "version.py")) as f:
        assert "2.0.0" in f.read()  # sanity: still on the new version pre-rollback

    msg = self_update.check_and_recover_pending_update()  # second launch: rolls back

    assert msg is not None and "rolled back" in msg.lower()
    with open(os.path.join(install_dir, "version.py")) as f:
        assert "1.0.0" in f.read()
    assert self_update._read_pending_marker() is None


def test_recover_pending_update_no_marker_is_a_noop(tmp_path):
    assert self_update.check_and_recover_pending_update() is None


# -------------------------------------------------------------- failure path --

def test_apply_update_rolls_back_on_copy_failure(tmp_path, monkeypatch):
    install_dir = _make_install_dir(tmp_path, version="1.0.0")
    zip_path = _make_update_zip(tmp_path, version="2.0.0")

    real_copy2 = self_update.shutil.copy2

    def flaky_copy2(src, dst, *a, **kw):
        # shutil.copytree's default copy_function is bound at Python's
        # *definition* time, so patching shutil.copy2 doesn't affect the
        # backup step (which uses copytree) -- only the explicit
        # shutil.copy2 calls in _overlay_copy, i.e. the forward update
        # step and (if it runs) the rollback step. Only fail the
        # forward copy (src is the extracted zip, not the backup dir),
        # so rollback -- which copies the same filename back from the
        # backup -- can still succeed and actually be exercised.
        if "version.py" in str(dst) and str(install_dir) in str(dst) and "app_update_backup" not in str(src):
            raise OSError("simulated permission error")
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(self_update.shutil, "copy2", flaky_copy2)

    result = self_update.apply_update(zip_path, str(install_dir))

    assert result.success is False
    # The install dir must still be in a working state (original
    # version.py content, since the simulated failure happened on the
    # forward copy of that specific file and rollback should restore it).
    with open(install_dir / "version.py") as f:
        assert "1.0.0" in f.read()


def test_apply_update_raises_before_touching_disk_on_invalid_zip(tmp_path):
    install_dir = _make_install_dir(tmp_path)
    bad_zip = _make_bad_zip(tmp_path)
    original_files = set(os.listdir(install_dir))

    with pytest.raises(self_update.UpdateValidationError):
        self_update.apply_update(bad_zip, str(install_dir))

    assert set(os.listdir(install_dir)) == original_files


# -------------------------------------------------------- frozen (PyInstaller) build --

def test_frozen_build_validates_against_the_executable_name(tmp_path, monkeypatch):
    """Under a frozen build there's no main.py/models.py at all -- the
    marker has to be the exe itself, by name. A previous version of
    this module always required main.py/models.py regardless, so a
    real update package for the shipped app could never validate."""
    monkeypatch.setattr(self_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(self_update.sys, "executable", "/opt/ConanOps/ConanOps.exe", raising=False)

    zip_path = tmp_path / "update.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("ConanOps.exe", "not a real exe, just a marker for the test")
        zf.writestr("some_dll.dll", "fake")

    self_update.validate_update_zip(str(zip_path))  # should not raise


def test_frozen_build_rejects_source_only_zip(tmp_path, monkeypatch):
    """The inverse: a source-shaped zip (main.py/models.py) doesn't
    satisfy a frozen build's marker requirement either -- the two
    marker schemes are mutually exclusive, not a union."""
    monkeypatch.setattr(self_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(self_update.sys, "executable", "/opt/ConanOps/ConanOps.exe", raising=False)

    zip_path = _make_update_zip(tmp_path)
    with pytest.raises(self_update.UpdateValidationError):
        self_update.validate_update_zip(zip_path)


def test_frozen_build_accepts_a_renamed_single_exe(tmp_path, monkeypatch):
    """A browser often saves a re-downloaded file as "ConanOps (1).exe"
    -- there's still exactly one .exe in the package, so it should be
    accepted rather than rejected as "not a real update"."""
    monkeypatch.setattr(self_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(self_update.sys, "executable", "/opt/ConanOps/ConanOps.exe", raising=False)

    zip_path = tmp_path / "update.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("ConanOps (1).exe", "not a real exe, just a marker for the test")
        zf.writestr("some_dll.dll", "fake")

    self_update.validate_update_zip(str(zip_path))  # should not raise


def test_frozen_build_renamed_exe_still_replaces_the_running_one(tmp_path, monkeypatch):
    """The renamed-exe fallback has to actually land the file at the
    RUNNING exe's path, not just validate -- otherwise the update
    "succeeds" while the real exe silently never gets replaced."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "ConanOps.exe").write_text("old build\n")
    monkeypatch.setattr(self_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(self_update.sys, "executable", str(install_dir / "ConanOps.exe"), raising=False)

    zip_path = tmp_path / "update.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("ConanOps (1).exe", "new build\n")

    result = self_update.apply_update(str(zip_path), str(install_dir))

    assert result.success is True
    assert (install_dir / "ConanOps.exe").read_text() == "new build\n"
    assert not (install_dir / "ConanOps (1).exe").exists()


def test_locked_file_is_renamed_aside_instead_of_overwritten(tmp_path):
    """Simulates Windows refusing to overwrite the running exe in
    place: shutil.copy2 raises PermissionError for that one file, and
    _copy_file_with_swap should rename it aside and copy the new file
    into its place rather than letting the whole update fail."""
    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()
    (src_dir / "ConanOps.exe").write_text("new build\n")
    locked_path = dst_dir / "ConanOps.exe"
    locked_path.write_text("old build (locked)\n")

    real_copy2 = self_update.shutil.copy2
    calls = {"n": 0}

    def flaky_copy2(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("file in use")
        return real_copy2(src, dst)

    self_update.shutil.copy2 = flaky_copy2
    try:
        self_update._copy_file_with_swap(str(src_dir / "ConanOps.exe"), str(locked_path))
    finally:
        self_update.shutil.copy2 = real_copy2

    assert locked_path.read_text() == "new build\n"
    assert (dst_dir / "ConanOps.exe.conanops-old").read_text() == "old build (locked)\n"


def test_cleanup_old_files_sweeps_renamed_leftovers(tmp_path):
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "ConanOps.exe.conanops-old").write_text("stale\n")
    (install_dir / "keep_me.py").write_text("x = 1\n")

    self_update._cleanup_old_files(str(install_dir))

    assert not (install_dir / "ConanOps.exe.conanops-old").exists()
    assert (install_dir / "keep_me.py").exists()


# -------------------------------------------------------- concurrent updates --

def test_apply_update_refuses_while_a_prior_update_is_unconfirmed(tmp_path):
    install_dir = _make_install_dir(tmp_path)
    zip_path = _make_update_zip(tmp_path)
    self_update.apply_update(zip_path, str(install_dir))  # leaves a pending marker

    second_zip = _make_update_zip(tmp_path, name="second.zip", version="3.0.0")
    result = self_update.apply_update(second_zip, str(install_dir))

    assert result.success is False
    assert "restart" in result.message.lower()
    # the first update's version should still be the one on disk
    with open(install_dir / "version.py") as f:
        assert "2.0.0" in f.read()


def test_apply_update_succeeds_again_after_confirming_the_prior_one(tmp_path):
    install_dir = _make_install_dir(tmp_path)
    zip_path = _make_update_zip(tmp_path)
    self_update.apply_update(zip_path, str(install_dir))
    self_update.confirm_update_success()

    second_zip = _make_update_zip(tmp_path, name="second.zip", version="3.0.0")
    result = self_update.apply_update(second_zip, str(install_dir))

    assert result.success is True
    with open(install_dir / "version.py") as f:
        assert "3.0.0" in f.read()


# -------------------------------------------------------------- data dir --

def test_backup_excludes_the_app_data_dir(tmp_path):
    """The app's own update backup must never drag server installs,
    backups, or config along with it -- both for size (many GB per
    server) and because a running server has its own files open."""
    install_dir = _make_install_dir(tmp_path)
    data_dir = install_dir / self_update._APP_DATA_DIRNAME
    data_dir.mkdir()
    (data_dir / "config.json").write_text('{"servers": []}')
    (data_dir / "some-server-id").mkdir()
    (data_dir / "some-server-id" / "world.db").write_text("pretend world save data")
    zip_path = _make_update_zip(tmp_path)

    result = self_update.apply_update(zip_path, str(install_dir))

    assert result.success is True
    marker = self_update._read_pending_marker()
    backup_dir = marker["backup_dir"]
    assert not os.path.exists(os.path.join(backup_dir, self_update._APP_DATA_DIRNAME))
    # and the real data dir is obviously untouched by any of this
    assert (data_dir / "config.json").exists()
    assert (data_dir / "some-server-id" / "world.db").exists()


def test_backup_only_copies_files_the_update_replaces(tmp_path):
    """The backup holds exactly what the update overwrites -- never
    unrelated files that happen to share the folder. Copying the whole
    folder is what left updates stuck on "Updating..." for hours when
    ConanOps.exe sat next to something big (a server install,
    Downloads, a drive root)."""
    install_dir = _make_install_dir(tmp_path)
    unrelated = install_dir / "some_module" / "data"
    unrelated.mkdir(parents=True)
    (unrelated / "fixture.json").write_text("{}")
    (install_dir / "huge_unrelated_file.bin").write_bytes(b"x" * 1024)
    zip_path = _make_update_zip(tmp_path)

    result = self_update.apply_update(zip_path, str(install_dir))

    assert result.success is True
    backup_dir = Path(self_update._read_pending_marker()["backup_dir"])
    assert (backup_dir / "main.py").exists()
    assert not (backup_dir / "huge_unrelated_file.bin").exists()
    assert not (backup_dir / "some_module").exists()


def test_cleanup_old_files_never_walks_into_the_app_data_dir(tmp_path):
    """Same reasoning as the backup exclusion, for the leftover-file
    sweep -- server folders can be large and are never where a
    .conanops-old file would legitimately be."""
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    (install_dir / "ConanOps.exe.conanops-old").write_text("stale app file")
    data_dir = install_dir / self_update._APP_DATA_DIRNAME
    data_dir.mkdir()
    # Named to LOOK like a leftover file, but it's inside the data
    # dir -- must survive, since nothing in there is ever a swapped-
    # aside APP file.
    (data_dir / "world.db.conanops-old").write_text("actually just a save file, not app leftovers")

    self_update._cleanup_old_files(str(install_dir))

    assert not (install_dir / "ConanOps.exe.conanops-old").exists()
    assert (data_dir / "world.db.conanops-old").exists()
