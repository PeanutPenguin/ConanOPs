from __future__ import annotations

import sys

import pytest
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication

import steamcmd
import update_runner


@pytest.fixture(scope="module", autouse=True)
def qapp():
    # AccessPage below is a QWidget; QThread/Signal machinery works
    # without one, but constructing any QWidget requires an app
    # instance to exist first.
    yield QApplication.instance() or QApplication(sys.argv)


# --------------------------------------------------------------------- #
# update_runner workers: an unhandled exception inside run() must still
# emit its result signal, or every caller keyed off "is this worker
# still in flight" (update-busy guards, disabled buttons) gets stuck
# for the rest of the session. These call run() directly (synchronous,
# no real thread) -- exercising exactly the code path that used to
# propagate straight out of run() unhandled.
# --------------------------------------------------------------------- #

def test_check_worker_emits_on_unexpected_exception(monkeypatch):
    monkeypatch.setattr(update_runner.steamcmd, "get_latest_buildid", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
    worker = update_runner.CheckWorker("/steamcmd")
    results = []
    worker.finished_check.connect(lambda latest, info: results.append((latest, info)))

    worker.run()  # would previously raise straight out, never emitting

    assert len(results) == 1
    latest, info = results[0]
    assert latest is None
    assert info.fetched_ok is False


def test_update_worker_emits_failure_on_unexpected_exception(monkeypatch):
    monkeypatch.setattr(
        update_runner.steamcmd, "update_server",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")),
    )
    worker = update_runner.UpdateWorker("/steamcmd", "/install")
    results = []
    worker.finished_update.connect(results.append)

    worker.run()

    assert len(results) == 1
    assert isinstance(results[0], steamcmd.UpdateResult)
    assert results[0].success is False


def test_mod_download_worker_emits_failure_on_unexpected_exception(monkeypatch):
    monkeypatch.setattr(
        update_runner.steamcmd, "download_workshop_items",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down")),
    )
    worker = update_runner.ModDownloadWorker("/steamcmd", ["12345"])
    results = []
    worker.finished_download.connect(results.append)

    worker.run()

    assert len(results) == 1
    assert results[0].success is False


def test_check_worker_still_emits_normally_on_success(monkeypatch):
    """Sanity check the happy path wasn't broken by the try/except."""
    monkeypatch.setattr(update_runner.steamcmd, "get_latest_buildid", lambda *_: "999")
    monkeypatch.setattr(update_runner.changelog_mod, "fetch_latest_news", lambda: update_runner.changelog_mod.ChangelogInfo(fetched_ok=True))
    worker = update_runner.CheckWorker("/steamcmd")
    results = []
    worker.finished_check.connect(lambda latest, info: results.append((latest, info)))

    worker.run()

    assert results == [("999", results[0][1])]
    assert results[0][1].fetched_ok is True


# --------------------------------------------------------------------- #
# QThread retire-on-finished pattern (AccessPage, ConsolePage,
# DashboardPage, UpdatesPage, MainWindow all use the identical shape --
# exercised once here via AccessPage). A worker moved into
# _retiring_workers must stay referenced until QThread.finished fires,
# not get dropped the moment the page's own result-signal handler runs.
# --------------------------------------------------------------------- #

class _StubWorker(QThread):
    """A do-nothing QThread standing in for a real worker -- this test
    is about the retire bookkeeping around a worker, not about a real
    background thread run."""
    done = Signal()


def test_retire_worker_holds_reference_until_thread_finished():
    import ui.access_page as access_page

    page = access_page.AccessPage()
    worker = _StubWorker()

    page._retire_worker(worker)
    assert worker in page._retiring_workers

    worker.finished.emit()  # simulate QThread's own completion signal

    assert worker not in page._retiring_workers


def test_retire_worker_ignores_none():
    import ui.access_page as access_page

    page = access_page.AccessPage()
    page._retire_worker(None)  # must not raise
    assert page._retiring_workers == []
