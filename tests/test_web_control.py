from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request

import pytest

import models
import process_manager
import preflight
import web_control


def _get(url):
    with urllib.request.urlopen(url, timeout=3) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _get_html(url):
    with urllib.request.urlopen(url, timeout=3) as resp:
        return resp.status, resp.read().decode("utf-8")


def _post(url):
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=3) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


@pytest.fixture
def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_start_and_stop_lifecycle(free_port):
    srv = web_control.WebControlServer(get_active_server=lambda: None)
    ok, msg = srv.start(preferred_port=free_port)
    assert ok is True
    assert srv.is_running is True
    assert srv.actual_port == free_port

    srv.stop()
    assert srv.is_running is False
    assert srv.actual_port is None


def test_starting_twice_is_a_noop_success(free_port):
    srv = web_control.WebControlServer(get_active_server=lambda: None)
    srv.start(preferred_port=free_port)
    ok, msg = srv.start(preferred_port=free_port)
    assert ok is True
    assert "already" in msg.lower()
    srv.stop()


def test_port_fallback_when_preferred_port_is_taken(free_port):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", free_port))
    blocker.listen(1)
    try:
        srv = web_control.WebControlServer(get_active_server=lambda: None)
        ok, msg = srv.start(preferred_port=free_port)
        assert ok is True
        assert srv.actual_port != free_port
        assert srv.actual_port in range(free_port, free_port + web_control.PORT_FALLBACK_ATTEMPTS)
        srv.stop()
    finally:
        blocker.close()


def test_index_page_serves_html(free_port):
    srv = web_control.WebControlServer(get_active_server=lambda: None)
    srv.start(preferred_port=free_port)
    try:
        status, body = _get_html(f"http://127.0.0.1:{free_port}/")
        assert status == 200
        assert "<html" in body.lower()
        assert "ConanOps" in body
    finally:
        srv.stop()


def test_status_endpoint_reports_unconfigured_when_no_server(free_port):
    srv = web_control.WebControlServer(get_active_server=lambda: None)
    srv.start(preferred_port=free_port)
    try:
        status, data = _get(f"http://127.0.0.1:{free_port}/api/status")
        assert status == 200
        assert data["configured"] is False
        assert data["running"] is False
    finally:
        srv.stop()


def test_status_endpoint_reports_offline_for_configured_but_not_running(tmp_path, free_port):
    install_dir = tmp_path / "server"
    install_dir.mkdir()
    server = models.ServerConfig(name="Test Server", install_dir=str(install_dir))

    srv = web_control.WebControlServer(get_active_server=lambda: server)
    srv.start(preferred_port=free_port)
    try:
        status, data = _get(f"http://127.0.0.1:{free_port}/api/status")
        assert data["configured"] is True
        assert data["name"] == "Test Server"
        assert data["running"] is False
        assert data["players"] is None
    finally:
        srv.stop()


def test_start_action_calls_process_manager_launch(tmp_path, free_port, monkeypatch):
    install_dir = tmp_path / "server"
    install_dir.mkdir()
    server = models.ServerConfig(name="Test Server", install_dir=str(install_dir))

    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s.name))
    monkeypatch.setattr(process_manager, "is_running", lambda d: False)
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True))

    srv = web_control.WebControlServer(get_active_server=lambda: server)
    srv.start(preferred_port=free_port)
    try:
        status, data = _post(f"http://127.0.0.1:{free_port}/api/start")
        assert data["success"] is True
        assert launched == ["Test Server"]
    finally:
        srv.stop()


def test_start_action_blocked_by_failed_preflight(tmp_path, free_port, monkeypatch):
    install_dir = tmp_path / "server"
    install_dir.mkdir()
    server = models.ServerConfig(name="Test Server", install_dir=str(install_dir))

    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s.name))
    monkeypatch.setattr(process_manager, "is_running", lambda d: False)
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=False, problems=["missing exe"]))

    srv = web_control.WebControlServer(get_active_server=lambda: server)
    srv.start(preferred_port=free_port)
    try:
        status, data = _post(f"http://127.0.0.1:{free_port}/api/start")
        assert data["success"] is False
        assert "missing exe" in data["message"]
        assert launched == []  # must not have actually launched
    finally:
        srv.stop()


def test_start_action_refuses_when_already_running(tmp_path, free_port, monkeypatch):
    install_dir = tmp_path / "server"
    install_dir.mkdir()
    server = models.ServerConfig(name="Test Server", install_dir=str(install_dir))

    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(s.name))
    monkeypatch.setattr(process_manager, "is_running", lambda d: True)

    srv = web_control.WebControlServer(get_active_server=lambda: server)
    srv.start(preferred_port=free_port)
    try:
        status, data = _post(f"http://127.0.0.1:{free_port}/api/start")
        assert data["success"] is False
        assert launched == []
    finally:
        srv.stop()


def test_stop_action_calls_process_manager_stop(tmp_path, free_port, monkeypatch):
    install_dir = tmp_path / "server"
    install_dir.mkdir()
    server = models.ServerConfig(name="Test Server", install_dir=str(install_dir))

    stopped = []
    monkeypatch.setattr(process_manager, "is_running", lambda d: True)
    monkeypatch.setattr(process_manager, "graceful_stop", lambda s, **kw: (stopped.append(s.install_dir), True)[1])

    srv = web_control.WebControlServer(get_active_server=lambda: server)
    srv.start(preferred_port=free_port)
    try:
        status, data = _post(f"http://127.0.0.1:{free_port}/api/stop")
        assert data["success"] is True
        assert stopped == [str(install_dir)]
    finally:
        srv.stop()


def test_restart_action_calls_process_manager_restart(tmp_path, free_port, monkeypatch):
    install_dir = tmp_path / "server"
    install_dir.mkdir()
    server = models.ServerConfig(name="Test Server", install_dir=str(install_dir))

    restarted = []
    monkeypatch.setattr(process_manager, "restart", lambda s: restarted.append(s.name))
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True))

    srv = web_control.WebControlServer(get_active_server=lambda: server)
    srv.start(preferred_port=free_port)
    try:
        status, data = _post(f"http://127.0.0.1:{free_port}/api/restart")
        assert data["success"] is True
        assert restarted == ["Test Server"]
    finally:
        srv.stop()


def test_unknown_path_returns_404(free_port):
    srv = web_control.WebControlServer(get_active_server=lambda: None)
    srv.start(preferred_port=free_port)
    try:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            _get(f"http://127.0.0.1:{free_port}/nonexistent")
        assert exc_info.value.code == 404
    finally:
        srv.stop()


def test_url_for_uses_actual_port(free_port, monkeypatch):
    monkeypatch.setattr("network_utils.get_local_ip", lambda: "192.168.1.50")
    srv = web_control.WebControlServer(get_active_server=lambda: None)
    srv.start(preferred_port=free_port)
    try:
        url = srv.url_for()
        assert url == f"http://192.168.1.50:{free_port}"
    finally:
        srv.stop()


def test_url_for_returns_none_when_not_running():
    srv = web_control.WebControlServer(get_active_server=lambda: None)
    assert srv.url_for() is None


def test_binds_all_interfaces_not_just_localhost(free_port):
    """Reachable from other devices on the network, per spec -- confirm
    it isn't accidentally bound to 127.0.0.1 only."""
    srv = web_control.WebControlServer(get_active_server=lambda: None)
    srv.start(preferred_port=free_port)
    try:
        sock, addr = srv._httpd.socket, srv._httpd.server_address
        assert addr[0] == "0.0.0.0"
    finally:
        srv.stop()
