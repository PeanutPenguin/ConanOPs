"""The web version's API against a real MainWindow: every read endpoint
answers, and changes go through the app's own handlers (and show up in
the app)."""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest
from PySide6.QtWidgets import QApplication

import mod_manager
import models
import web_control
from ui.main_window import MainWindow
from webui import auth

PASSWORD = "correct horse 9"


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def no_disk_writes(monkeypatch):
    monkeypatch.setattr(mod_manager, "write_modlist", lambda *a, **k: None)
    monkeypatch.setattr(models.AppConfig, "save", lambda self, *a, **k: None)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def web(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    server = models.ServerConfig(id="s1", name="Exiled Lands", install_dir=str(install))
    server.mods = [{"id": "111", "name": "Pippi", "enabled": True}, {"id": "222", "name": "Emberlight", "enabled": True}]
    other = models.ServerConfig(id="s2", name="Siptah", install_dir=str(tmp_path / "other"))
    cfg = models.AppConfig(servers=[server, other], active_server_id="s1")
    cfg.web_password_hash = auth.hash_password(PASSWORD)
    win = MainWindow(config=cfg)
    srv = win.web_control
    srv.stop()
    assert srv.start(preferred_port=_free_port())[0]
    client = _Client(srv.actual_port)
    client.login()
    yield win, client
    srv.stop()
    win.close()


class _Client:
    """Makes requests on a worker thread while the test thread keeps the
    Qt event loop turning (the API runs actions on the GUI thread)."""

    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        self.cookie = None

    def call(self, method, path, body=None, timeout=20):
        out = {}

        def run():
            data = json.dumps(body or {}).encode() if method == "POST" else None
            req = urllib.request.Request(self.base + path, data=data, method=method)
            if method == "POST":
                req.add_header("X-ConanOps", "1")
            if self.cookie:
                req.add_header("Cookie", self.cookie)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    out["status"], out["headers"], out["body"] = resp.status, resp.headers, resp.read()
            except urllib.error.HTTPError as e:
                out["status"], out["headers"], out["body"] = e.code, e.headers, e.read()
            except Exception as e:  # noqa: BLE001
                out["error"] = e

        t = threading.Thread(target=run)
        t.start()
        deadline = time.monotonic() + timeout
        while t.is_alive() and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)
        t.join(1)
        if "error" in out:
            raise out["error"]
        data = json.loads(out["body"] or b"{}")
        return out["status"], data, out.get("headers")

    def login(self):
        status, _, headers = self.call("POST", "/api/login", {"password": PASSWORD})
        assert status == 200
        self.cookie = headers["Set-Cookie"].split(";")[0]

    def get(self, path):
        status, data, _ = self.call("GET", path)
        assert status == 200, (path, data)
        return data

    def post(self, path, body=None):
        status, data, _ = self.call("POST", path, body)
        assert status == 200, (path, data)
        return data


def test_overview_lists_servers(web):
    win, c = web
    data = c.get("/api/overview")
    assert [s["name"] for s in data["servers"]] == ["Exiled Lands", "Siptah"]
    assert data["active_id"] == "s1"
    assert data["version"]
    assert data["theme"]["bg"].startswith("#")


@pytest.mark.parametrize("path", ["", "/players", "/access", "/backups", "/updates", "/mods", "/settings"])
def test_every_server_page_reads(web, path):
    win, c = web
    data = c.get(f"/api/servers/s1{path}")
    assert isinstance(data, dict) and data


def test_app_options_read(web):
    _, c = web
    data = c.get("/api/app")
    assert "keep_alive" in json.dumps(data)


def test_unknown_server_is_a_clear_error(web):
    _, c = web
    status, data, _ = c.call("GET", "/api/servers/nope/players")
    assert status == 400 and "doesn't exist" in data["error"]


def test_selecting_a_server_switches_the_app(web):
    win, c = web
    c.post("/api/servers/s2/select")
    assert win.config.active_server_id == "s2"


def test_ban_and_unban_go_through_the_app(web):
    win, c = web
    c.post("/api/servers/s1/access/ban", {"steam_id": "76561198000000001"})
    assert "76561198000000001" in win.config.servers[0].banned_ids
    c.post("/api/servers/s1/access/unban", {"steam_id": "76561198000000001"})
    assert "76561198000000001" not in win.config.servers[0].banned_ids


def test_ban_needs_a_real_steam_id(web):
    _, c = web
    status, data, _ = c.call("POST", "/api/servers/s1/access/ban", {"steam_id": "robert'); drop"})
    assert status == 400


def test_whitelist_edit(web):
    win, c = web
    c.post("/api/servers/s1/access/whitelist", {"steam_id": "76561198000000002", "add": True})
    assert "76561198000000002" in win.config.servers[0].whitelist_ids


def test_mod_toggle_and_move(web):
    win, c = web
    c.post("/api/servers/s1/mods/toggle", {"id": "222", "enabled": False})
    assert [m["enabled"] for m in win.config.servers[0].mods] == [True, False]
    c.post("/api/servers/s1/mods/move", {"id": "222", "direction": -1})
    assert [m["id"] for m in win.config.servers[0].mods] == ["222", "111"]
    mods = c.get("/api/servers/s1/mods")
    assert [m["id"] for m in mods["mods"]] == ["222", "111"]


def test_settings_save_round_trip(web):
    win, c = web
    pages = c.get("/api/servers/s1/settings")["pages"]
    assert pages
    page = next(p for p in pages if any(f["type"] in ("int", "text", "bool") and not f.get("secret")
                                        for f in p["fields"]))
    field = next(f for f in page["fields"] if f["type"] in ("int", "text", "bool") and not f.get("secret"))
    if field["type"] == "bool":
        new = not field["value"]
    elif field["type"] == "int":
        new = min(field.get("max", 10 ** 6), (field["value"] or 0) + 1)
    else:
        new = "web edit"
    result = c.post(f"/api/servers/s1/settings/{page['key']}", {"values": {field["key"]: new}})
    assert result.get("ok") is not False, result
    after = c.get("/api/servers/s1/settings")["pages"]
    again = next(f for p in after if p["key"] == page["key"] for f in p["fields"] if f["key"] == field["key"])
    assert again["value"] == new


def test_console_without_rcon_explains(web):
    _, c = web
    status, data, _ = c.call("POST", "/api/servers/s1/console", {"command": "listplayers"})
    assert status in (200, 400)
    assert data.get("ok") is False or "error" in data


def test_web_cannot_delete_or_add_servers(web):
    win, c = web
    for path in ("/api/servers/s1/delete", "/api/servers/add", "/api/app/delete_everything", "/api/app/web_password"):
        status, _, _ = c.call("POST", path, {"value": True})
        assert status in (400, 404)
    assert len(win.config.servers) == 2


def test_app_dialogs_are_captured_not_shown(web, monkeypatch):
    win, c = web
    from PySide6.QtWidgets import QMessageBox

    def act():
        QMessageBox.warning(None, "Heads up", "Something the PC would have shown")
        return 1
    result, msgs = win.web_control.api.bridge.call(act)
    assert result == 1 and msgs == ["Heads up: Something the PC would have shown"]
