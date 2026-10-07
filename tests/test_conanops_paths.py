from __future__ import annotations

import os

import conanops_paths

# Captured at import time (module collection), before the conftest.py
# autouse fixture replaces conanops_paths.app_install_dir with a
# per-test fake for every OTHER test's isolation -- the two tests
# below are specifically testing THIS function's real logic, so they
# call this captured reference instead of the (by then patched)
# conanops_paths.app_install_dir name.
_real_app_install_dir = conanops_paths.app_install_dir


def test_no_space_root_never_contains_a_space():
    assert " " not in conanops_paths.no_space_root()


def test_app_data_dir_is_a_subfolder_of_app_install_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(conanops_paths, "app_install_dir", lambda: str(tmp_path))
    assert conanops_paths.app_data_dir() == str(tmp_path / conanops_paths.APP_DATA_DIRNAME)


def test_no_space_root_prefers_app_install_dir_when_it_has_no_space(monkeypatch, tmp_path):
    no_space_dir = tmp_path / "NoSpaceHere"
    monkeypatch.setattr(conanops_paths, "app_install_dir", lambda: str(no_space_dir))

    assert conanops_paths.no_space_root() == str(no_space_dir / conanops_paths.APP_DATA_DIRNAME)


def test_no_space_root_falls_back_to_public_conanops_when_app_dir_has_a_space(monkeypatch, tmp_path):
    """SteamCMD's app_update fails with a space in its install path --
    confirmed against a real install where the app's own folder (a
    user's Downloads, say) had one. Falling back to the guaranteed-
    no-space Public\\ConanOps here is what avoids reproducing that."""
    spacey_dir = tmp_path / "My ConanOps Folder"
    monkeypatch.setattr(conanops_paths, "app_install_dir", lambda: str(spacey_dir))

    result = conanops_paths.no_space_root()

    assert " " not in result
    assert result.endswith("ConanOps")
    assert "Public" in result
    assert result != str(spacey_dir)


def test_app_install_dir_is_dirname_of_executable_when_frozen(monkeypatch, tmp_path):
    fake_exe = tmp_path / "dist" / "ConanOps.exe"
    monkeypatch.setattr(conanops_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(conanops_paths.sys, "executable", str(fake_exe), raising=False)

    assert _real_app_install_dir() == str(tmp_path / "dist")


def test_app_install_dir_is_repo_root_when_not_frozen(monkeypatch):
    """Source install (no PyInstaller): this module's own file lives at
    the repo root, so that's the app's install dir -- same folder
    main.py lives in."""
    monkeypatch.setattr(conanops_paths.sys, "frozen", False, raising=False)

    result = _real_app_install_dir()

    assert os.path.isfile(os.path.join(result, "conanops_paths.py"))
