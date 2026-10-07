"""The web version's HTTP layer: static files, login, sessions, CSRF
protection, lockout and port handling (with a stand-in API)."""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request

import pytest

import web_control
from webui import auth


@pytest.fixture
def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeApi:
    def __init__(self):
        self.calls = []

    def match(self, method, path):
        if path == "/api/overview" and method == "GET":
            return (lambda req: self.calls.append(req) or {"servers": []}), {}
        if path == "/api/servers/x/start" and method == "POST":
            return (lambda req: self.calls.append(req) or {"ok": True}), {"sid": "x"}
        return None, {}


@pytest.fixture
def server(free_port, tmp_path):
    state = {"hash": auth.hash_password("correct horse 9")}
    srv = web_control.WebControlServer(get_active_server=lambda: None, get_password_hash=lambda: state["hash"],
                                       sessions_path=str(tmp_path / "sessions.json"))
    srv.api = _FakeApi()
    ok, _ = srv.start(preferred_port=free_port)
    assert ok
    srv.state = state
    yield srv
    srv.stop()


def _req(srv, path, method="GET", body=None, cookie=None, headers=None):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(f"http://127.0.0.1:{srv.actual_port}{path}", data=data, method=method)
    if method == "POST":
        req.add_header("X-ConanOps", "1")
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    for k, v in (headers or {}).items():
        if v is None:
            req.remove_header(k.capitalize())
        else:
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _login(srv, password="correct horse 9"):
    status, headers, _ = _req(srv, "/api/login", "POST", {"password": password, "remember": True})
    assert status == 200
    return headers["Set-Cookie"].split(";")[0]


def test_lifecycle_and_already_running(free_port):
    srv = web_control.WebControlServer(get_active_server=lambda: None)
    assert srv.start(preferred_port=free_port)[0]
    assert srv.actual_port == free_port
    ok, msg = srv.start(preferred_port=free_port)
    assert ok and "already" in msg.lower()
    srv.stop()
    assert not srv.is_running and srv.actual_port is None


def test_port_fallback_when_preferred_port_is_taken(free_port):
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("0.0.0.0", free_port))
    blocker.listen(1)
    try:
        srv = web_control.WebControlServer(get_active_server=lambda: None)
        assert srv.start(preferred_port=free_port)[0]
        assert srv.actual_port in range(free_port + 1, free_port + web_control.PORT_FALLBACK_ATTEMPTS)
        srv.stop()
    finally:
        blocker.close()


def test_static_app_files_are_served_with_security_headers(server):
    for path, kind in (("/", "text/html"), ("/app.js", "javascript"), ("/app.css", "text/css"),
                       ("/manifest.webmanifest", "manifest"), ("/icon.svg", "svg")):
        status, headers, body = _req(server, path)
        assert status == 200, path
        assert kind in headers["Content-Type"], path
        assert body
    _, headers, body = _req(server, "/")
    assert b'src="/app.js"' in body
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"


def test_unknown_path_is_404(server):
    assert _req(server, "/etc/passwd")[0] == 404
    assert _req(server, "/../web_control.py")[0] == 404


def test_api_needs_login(server):
    status, _, body = _req(server, "/api/overview")
    assert status == 401 and json.loads(body)["needs_login"]
    status, _, body = _req(server, "/api/session")
    data = json.loads(body)
    assert status == 200 and data["authed"] is False and data["password_set"] is True


def test_no_password_set_blocks_everything(server):
    server.state["hash"] = ""
    status, _, body = _req(server, "/api/overview")
    assert status == 403 and json.loads(body)["needs_password"]
    assert _req(server, "/api/login", "POST", {"password": "anything at all"})[0] == 403


def test_login_sets_httponly_strict_cookie_and_unlocks_api(server):
    status, headers, _ = _req(server, "/api/login", "POST", {"password": "correct horse 9"})
    assert status == 200
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" not in cookie
    token = cookie.split(";")[0]
    status, _, body = _req(server, "/api/overview", cookie=token)
    assert status == 200 and json.loads(body) == {"servers": []}
    assert json.loads(_req(server, "/api/session", cookie=token)[2])["authed"] is True


def test_cookie_is_secure_through_the_https_tunnel(server):
    _, headers, _ = _req(server, "/api/login", "POST", {"password": "correct horse 9"},
                         headers={"X-Forwarded-Proto": "https", "Cf-Connecting-Ip": "203.0.113.5"})
    assert "Secure" in headers["Set-Cookie"]
    assert "Strict-Transport-Security" in headers


def test_wrong_password_rejected(server):
    status, headers, _ = _req(server, "/api/login", "POST", {"password": "nope nope"})
    assert status == 401 and "Set-Cookie" not in headers


def test_repeated_wrong_passwords_lock_out_that_address(server):
    for _ in range(5):
        _req(server, "/api/login", "POST", {"password": "wrong guess"})
    status, _, body = _req(server, "/api/login", "POST", {"password": "correct horse 9"})
    assert status == 429 and "Try again" in json.loads(body)["error"]


def test_tunnel_visitors_are_limited_by_their_own_address(server):
    for _ in range(5):
        _req(server, "/api/login", "POST", {"password": "wrong guess"}, headers={"Cf-Connecting-Ip": "198.51.100.7"})
    assert _req(server, "/api/login", "POST", {"password": "correct horse 9"},
                headers={"Cf-Connecting-Ip": "198.51.100.7"})[0] == 429
    assert _req(server, "/api/login", "POST", {"password": "correct horse 9"},
                headers={"Cf-Connecting-Ip": "198.51.100.8"})[0] == 200


def test_post_without_custom_header_is_refused(server):
    token = _login(server)
    status, _, _ = _req(server, "/api/servers/x/start", "POST", {}, cookie=token, headers={"X-ConanOps": None})
    assert status == 403
    assert server.api.calls == []


def test_post_from_another_origin_is_refused(server):
    token = _login(server)
    status, _, _ = _req(server, "/api/servers/x/start", "POST", {}, cookie=token,
                        headers={"Origin": "https://evil.example"})
    assert status == 403
    status, _, _ = _req(server, "/api/servers/x/start", "POST", {}, cookie=token,
                        headers={"Origin": f"http://127.0.0.1:{server.actual_port}"})
    assert status == 200
    assert server.api.calls[-1].params == {"sid": "x"}


def test_logout_and_password_change_end_sessions(server):
    token = _login(server)
    _req(server, "/api/logout", "POST", {}, cookie=token)
    assert _req(server, "/api/overview", cookie=token)[0] == 401
    token = _login(server)
    server.sessions.revoke_all()
    assert _req(server, "/api/overview", cookie=token)[0] == 401


def test_sessions_survive_a_restart(tmp_path):
    store = auth.SessionStore(str(tmp_path / "s.json"))
    token, max_age = store.create(remember=True)
    assert max_age >= 7 * 86400
    again = auth.SessionStore(str(tmp_path / "s.json"))
    assert again.valid(token)
    assert not again.valid("made-up")
    assert "token" not in (tmp_path / "s.json").read_text() or token not in (tmp_path / "s.json").read_text()


def test_password_hashing():
    stored = auth.hash_password("correct horse 9")
    assert "correct horse 9" not in stored
    assert auth.verify_password("correct horse 9", stored)
    assert not auth.verify_password("correct horse 8", stored)
    assert not auth.verify_password("x", "garbage")
    assert auth.password_problem("short")
    assert auth.password_problem("password123")
    assert auth.password_problem("a much longer passphrase") == ""


def test_bad_json_body_is_400(server):
    token = _login(server)
    req = urllib.request.Request(f"http://127.0.0.1:{server.actual_port}/api/servers/x/start", data=b"{not json",
                                 method="POST", headers={"X-ConanOps": "1", "Cookie": token})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 400
