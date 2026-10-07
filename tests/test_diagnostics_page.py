from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import diagnostics
from ui.diagnostics_page import DiagnosticsPage


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


class _FakeServer:
    id = "s1"


def _make_page(server=None, reserved_ports=None):
    server = server if server is not None else _FakeServer()
    return DiagnosticsPage(
        get_active_server=lambda: server,
        get_reserved_ports=lambda: reserved_ports or set(),
    )


def test_run_disables_button_and_shows_running_state(monkeypatch):
    page = _make_page()
    started = []
    monkeypatch.setattr("diagnostics_runner.DiagnosticsWorker.start", lambda self: started.append(True))

    page._run()

    assert page.run_btn.isEnabled() is False
    assert page.run_btn.text() == "Running…"
    assert started == [True]
    assert page._worker is not None


def test_run_is_a_noop_while_already_running(monkeypatch):
    page = _make_page()
    monkeypatch.setattr("diagnostics_runner.DiagnosticsWorker.start", lambda self: None)
    page._run()
    first_worker = page._worker

    page._run()  # second click while the first is still "running"

    assert page._worker is first_worker


def test_finished_diagnostics_re_enables_button_and_shows_results():
    page = _make_page()
    page._worker = object()  # pretend a run is in progress
    page.run_btn.setEnabled(False)
    page.run_btn.setText("Running…")

    results = [
        diagnostics.DiagnosticResult("Install folder", diagnostics.STATUS_OK, "Looks fine."),
        diagnostics.DiagnosticResult("Ports", diagnostics.STATUS_ERROR, "Conflict."),
    ]
    page._on_diagnostics_finished(results)

    assert page._worker is None
    assert page.run_btn.isEnabled() is True
    assert page.run_btn.text() == "Run Diagnostics"
    assert len(page._result_rows) == 2
    assert "1 problem" in page.last_run_label.text()


def test_run_does_nothing_with_no_active_server(monkeypatch):
    page = DiagnosticsPage(get_active_server=lambda: None, get_reserved_ports=lambda: set())
    started = []
    monkeypatch.setattr("diagnostics_runner.DiagnosticsWorker.start", lambda self: started.append(True))

    page._run()

    assert started == []
    assert page._worker is None


# ------------------------------------------------------------- hover detail --

from ui.diagnostics_page import _result_row


def test_result_row_shows_tooltip_when_detail_present():
    result = diagnostics.DiagnosticResult(
        "SteamCMD", diagnostics.STATUS_OK, "SteamCMD is installed.",
        detail="steamcmd.exe found under C:\\ConanOps\\data\\s1\\steamcmd.",
    )
    row = _result_row(result)
    assert row.toolTip() == "steamcmd.exe found under C:\\ConanOps\\data\\s1\\steamcmd."


def test_result_row_has_no_tooltip_when_detail_absent():
    result = diagnostics.DiagnosticResult("Ports", diagnostics.STATUS_OK, "Looks fine.")
    row = _result_row(result)
    assert row.toolTip() == ""
