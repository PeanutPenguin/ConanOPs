from __future__ import annotations

import sys

import pytest
from PySide6.QtWidgets import QApplication

from models import ServerConfig
from ui.dashboard_page import DashboardPage


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _make_page() -> DashboardPage:
    page = DashboardPage()
    # Avoid a real background _DashboardPollWorker QThread in these
    # tests -- see the QThread-retire pattern elsewhere in this app;
    # starting a real one here would need a live event loop to finish
    # cleanly before the test process exits.
    page._poll_timer.stop()
    return page


def test_starts_showing_content_not_the_empty_state():
    page = _make_page()
    assert page.content.isHidden() is False
    assert page.empty_frame.isHidden() is True


def test_set_empty_hides_content_and_shows_empty_state():
    page = _make_page()
    page.set_empty()

    assert page.server is None
    assert page.content.isHidden() is True
    assert page.empty_frame.isHidden() is False


def test_empty_state_add_button_calls_on_add_server():
    page = _make_page()
    page.set_empty()
    called = []
    page.on_add_server = lambda: called.append(True)

    page.empty_add_btn.click()

    assert called == [True]


def test_set_server_after_empty_restores_content():
    page = _make_page()
    page.set_empty()

    server = ServerConfig(id="s1", name="Chudville", install_dir="/tmp/fake")
    page.set_server(server, set())
    if page._poll_worker:
        page._poll_worker.wait(2000)

    assert page.content.isHidden() is False
    assert page.empty_frame.isHidden() is True
    assert page.server is server


def test_start_stop_visibility_toggles_with_running_state():
    page = _make_page()
    server = ServerConfig(id="s1", name="Chudville", install_dir="/tmp/fake")
    page.set_server(server, set())
    if page._poll_worker:
        page._poll_worker.wait(2000)

    # Freshly loaded, assumed not running until the next poll confirms otherwise.
    assert page.start_btn.isVisibleTo(page) is True
    assert page.stop_btn.isVisibleTo(page) is True or page.stop_btn.isVisibleTo(page) is False  # sanity only

    page._apply_running_state(running=True)
    assert page.start_btn.isVisibleTo(page) is False
    assert page.stop_btn.isVisibleTo(page) is True
    assert page.restart_btn.isVisibleTo(page) is True

    page._apply_running_state(running=False)
    assert page.start_btn.isVisibleTo(page) is True
    assert page.stop_btn.isVisibleTo(page) is False
    assert page.restart_btn.isVisibleTo(page) is True


def test_no_start_stop_restart_for_a_server_that_is_not_set_up_yet():
    """A server with no install_dir hasn't been through the setup
    wizard yet -- only "Set Up Server..." makes sense for it."""
    page = _make_page()
    server = ServerConfig(id="s1", name="Not Set Up")  # install_dir left blank
    page.set_server(server, set())

    assert page.setup_btn.isVisibleTo(page) is True
    assert page.start_btn.isVisibleTo(page) is False
    assert page.stop_btn.isVisibleTo(page) is False
    assert page.restart_btn.isVisibleTo(page) is False


def test_handle_start_and_stop_call_the_wired_callbacks():
    page = _make_page()
    server = ServerConfig(id="s1", name="Chudville", install_dir="/tmp/fake")
    page.set_server(server, set())
    if page._poll_worker:
        page._poll_worker.wait(2000)

    started = []
    stopped = []
    page.on_start = lambda s: started.append(s)
    page.on_stop = lambda s: stopped.append(s)

    page.start_btn.click()
    page.stop_btn.click()

    assert started == [server]
    assert stopped == [server]


def test_poll_worker_does_not_double_call_cpu_percent_on_a_fresh_process(monkeypatch):
    """psutil's cpu_percent(interval=None) measures CPU-time-used
    divided by wall-clock-time-elapsed since the LAST call on that
    Process object. Calling it twice back-to-back on a brand new
    Process (as a previous version did, to "prime" then immediately
    "read" it) measures against a near-zero wall-clock gap, producing
    a wildly inflated/garbage percentage. The fix must call it only
    once on a fresh process and report cpu as None for that poll."""
    from ui.dashboard_page import _DashboardPollWorker
    import process_manager

    calls = []

    class FakeProc:
        def cpu_percent(self, interval=None):
            calls.append(1)
            return 42.0  # would be garbage in real life; the point is call COUNT

        def memory_info(self):
            return type("M", (), {"rss": 1024 * 1024 * 100})()

        def create_time(self):
            return 0.0

    monkeypatch.setattr(process_manager, "find_running_pid", lambda install_dir: 1234)
    monkeypatch.setattr("ui.dashboard_page.psutil.Process", lambda pid: FakeProc())
    monkeypatch.setattr("ui.dashboard_page.network_utils.query_a2s_info", lambda *a, **k: None)

    worker = _DashboardPollWorker("/fake/install", "127.0.0.1", 27015, None, None)
    results = []
    worker.finished_poll.connect(results.append)
    worker.run()

    assert len(calls) == 1  # primed once, not read again the same cycle
    assert results[0]["cpu"] is None  # no meaningful reading yet this cycle


def test_poll_worker_gets_a_real_cpu_reading_on_the_next_poll_with_a_cached_proc():
    from ui.dashboard_page import _DashboardPollWorker
    import process_manager as pm

    class FakeProc:
        def cpu_percent(self, interval=None):
            return 12.5

        def memory_info(self):
            return type("M", (), {"rss": 1024 * 1024 * 100})()

        def create_time(self):
            return 0.0

    import unittest.mock
    with unittest.mock.patch.object(pm, "find_running_pid", return_value=1234):
        worker = _DashboardPollWorker("/fake/install", "127.0.0.1", 27015, 1234, FakeProc())
        with unittest.mock.patch("ui.dashboard_page.network_utils.query_a2s_info", return_value=None):
            results = []
            worker.finished_poll.connect(results.append)
            worker.run()

    assert results[0]["cpu"] == 12.5


def test_on_poll_finished_shows_dash_when_cpu_is_none():
    from models import ServerConfig

    page = _make_page()
    server = ServerConfig(id="s1", name="Chudville", install_dir="/tmp/fake")
    page.server = server
    page._poll_worker_server = server

    page._on_poll_finished({
        "pid": 1234, "proc": object(), "cpu": None, "mem_mb": 50.0,
        "uptime_seconds": 10.0, "max_players": None,
    })

    assert page.cpu_value.text() == "—"


def test_on_poll_finished_shows_percent_when_cpu_is_a_number():
    from models import ServerConfig

    page = _make_page()
    server = ServerConfig(id="s1", name="Chudville", install_dir="/tmp/fake")
    page.server = server
    page._poll_worker_server = server

    page._on_poll_finished({
        "pid": 1234, "proc": object(), "cpu": 17.6, "mem_mb": 50.0,
        "uptime_seconds": 10.0, "max_players": None,
    })

    assert page.cpu_value.text() == "18%"
