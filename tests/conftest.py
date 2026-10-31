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
def no_router_traffic(monkeypatch):
    """Tests never talk to a real router: no NAT-PMP/PCP packets, no probing
    the gateway's web ports, no remembered router from another test."""
    import network_setup
    import port_mapping
    monkeypatch.setattr(port_mapping, "_exchange", lambda *a, **k: None)
    monkeypatch.setattr(network_setup, "_probe_common_locations", lambda *a, **k: [])
    monkeypatch.setattr(network_setup, "_gateway_for", lambda local_ip=None: None)
    for d in (network_setup._last_location, network_setup._last_device, network_setup._last_candidates,
              network_setup._pmp_mapped, network_setup._pmp_renewed, network_setup._pmp_gateway,
              network_setup._no_router_at):
        d.clear()


@pytest.fixture(autouse=True)
def no_real_pc_scan(monkeypatch):
    """MainWindow looks for servers and other launchers on the PC a few
    seconds after it opens (and on "Add server"). Never let a test walk the
    real drives, process list or scheduled tasks that way: on a CI runner
    dozens of those overlapping scans starve the tests. Tests of the scan
    itself call server_finder / takeover / finder.run_scan directly."""
    try:
        from ui.window import finder
    except ImportError:  # PySide6 missing -- nothing to patch
        yield
        return
    import server_finder
    server_finder.cancel_event.clear()  # an earlier test's window closing sets it
    real = finder.run_scan
    monkeypatch.setattr(finder, "run_scan", lambda *a, **k: finder.ScanResult())
    finder.run_scan_real = real  # for tests that want the real thing on stubbed sources
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


@pytest.fixture(autouse=True)
def free_windows_after_each_test():
    """Tests create a MainWindow each; Qt signal connections hold its Python
    methods, so it's never garbage-collected until its C++ side is deleted.
    Delete every leftover top-level window after each test so the suite's
    memory stays flat (it otherwise grew ~30 MB per window)."""
    yield
    try:
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        if getattr(w, "_closed", None) is not None or type(w).__name__ == "MainWindow":
            w._really_quit = True
        try:
            w.hide()
            w.deleteLater()
        except RuntimeError:
            continue
    for _ in range(2):
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
    import gc
    gc.collect()
