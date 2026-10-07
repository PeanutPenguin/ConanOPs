from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import health_check_runner
import models
import network_utils
import process_manager


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _run_worker_synchronously(servers):
    """QThread.start() launches a real OS thread -- for a deterministic
    test, just call run() directly (same thing run() would do on that
    thread, minus the actual threading) and capture the signal."""
    worker = health_check_runner.HealthCheckWorker(servers)
    captured = []
    worker.finished_check.connect(lambda results: captured.append(results))
    worker.run()
    return captured[0]


def test_skips_servers_with_no_install_dir():
    server = models.ServerConfig(id="s1", install_dir="")
    results = _run_worker_synchronously([server])
    assert results == []


def test_not_running_server_reports_no_query(monkeypatch, tmp_path):
    server = models.ServerConfig(id="s1", install_dir=str(tmp_path))
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: False)
    queried = []
    monkeypatch.setattr(network_utils, "query_a2s_info", lambda *a, **k: queried.append(1))

    results = _run_worker_synchronously([server])

    assert results == [("s1", False, None)]
    assert queried == []  # never queries a server that isn't running


def test_running_server_gets_queried(monkeypatch, tmp_path):
    server = models.ServerConfig(id="s1", install_dir=str(tmp_path), bind_ip="127.0.0.1", query_port=27015)
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: True)
    info = {"name": "Chudville", "players": 3, "max_players": 40}
    monkeypatch.setattr(network_utils, "query_a2s_info", lambda ip, port, timeout=1.5: info)

    results = _run_worker_synchronously([server])

    assert results == [("s1", True, info)]


def test_running_but_unresponsive_reports_none_info(monkeypatch, tmp_path):
    server = models.ServerConfig(id="s1", install_dir=str(tmp_path))
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: True)
    monkeypatch.setattr(network_utils, "query_a2s_info", lambda *a, **k: None)

    results = _run_worker_synchronously([server])

    assert results == [("s1", True, None)]


def test_one_server_failing_does_not_drop_the_rest(monkeypatch, tmp_path):
    ok_server = models.ServerConfig(id="ok", install_dir=str(tmp_path))
    broken_server = models.ServerConfig(id="broken", install_dir=str(tmp_path))

    calls = {"n": 0}

    def is_running(install_dir):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated failure")
        return False

    monkeypatch.setattr(process_manager, "is_running", is_running)

    results = _run_worker_synchronously([broken_server, ok_server])

    assert results == [("ok", False, None)]
