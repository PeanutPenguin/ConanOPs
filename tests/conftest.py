"""
Shared pytest fixtures.

Isolates each test's HOME directory so applog's rotating log file
and any AppConfig.save()/load() default-path calls never touch the
real ~/ConanOps on the machine running the tests, and so tests don't
see each other's log/config state. Also isolates
conanops_paths.app_install_dir() -- no_space_root() now defaults to
wherever ConanOps itself is installed (see conanops_paths.py's
docstring) rather than always the user's profile folder, and left
unpatched that would resolve to the real checkout this test suite
lives in on whatever machine runs it, not a per-test temp dir.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Several project modules (webhooks, network_setup, backup_manager,
# steamcmd, secrets_store) call applog.get_logger() at *import* time,
# which resolves ~/ConanOps immediately. Set HOME to an isolated temp
# dir before collection imports any of them, so the very first log
# file created never lands in the real home directory of whatever
# machine runs the tests.
_collection_home = tempfile.mkdtemp(prefix="conanops-test-home-")
os.environ["HOME"] = _collection_home
os.environ["USERPROFILE"] = _collection_home

import pytest

import conanops_paths


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # os.path.expanduser on Windows
    # no_space_root() prefers conanops_paths.app_install_dir() over
    # the profile folder now; nothing calls it at collection time (see
    # module docstring above), so patching it here, before each test
    # body runs, is enough -- same per-test isolation guarantee this
    # fixture already gives HOME/USERPROFILE, just for the other input
    # no_space_root() reads. A dedicated subfolder of tmp_path, not
    # tmp_path itself: plenty of tests build their own paths directly
    # under tmp_path (e.g. tmp_path / "install"), and aliasing this to
    # tmp_path made those accidentally nested INSIDE data_dir()/
    # no_space_root() -- purely a test-environment artifact, but one
    # that made _handle_delete_everything's final "wipe data_dir"
    # step sweep up install folders that particular test never meant
    # to be inside it at all.
    monkeypatch.setattr(conanops_paths, "app_install_dir", lambda: str(tmp_path / "app"))
    yield tmp_path


@pytest.fixture(autouse=True)
def no_workshop_status_network(monkeypatch):
    """MainWindow wires the Mods tab to check mod status on Steam the
    first time a server with mods is shown. Never let a test reach the
    real Steam API that way -- tests that care about the status check
    call ModsPage._on_mod_status() directly."""
    try:
        import steam_workshop_api
        import workshop_search_runner
    except ImportError:  # PySide6 missing -- nothing to patch
        yield
        return

    def offline_run(self):
        self.finished_status.emit(steam_workshop_api.DetailsResult(ok=False, error="offline (tests)"))
    monkeypatch.setattr(workshop_search_runner.ModStatusWorker, "run", offline_run)
    yield


@pytest.fixture(autouse=True)
def server_ops_inline(monkeypatch):
    """Stops and restarts run on a worker thread in the real app (they
    wait for the world to save). Tests run them inline so assertions
    right after a call see the result -- tests/test_safe_updates.py
    covers the threaded path itself."""
    try:
        from ui.main_window import MainWindow
    except ImportError:
        yield
        return
    monkeypatch.setattr(MainWindow, "RUN_OPS_INLINE", True)
    yield
