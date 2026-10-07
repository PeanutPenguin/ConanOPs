"""The web version's API against a real MainWindow: every read endpoint
answers, and changes go through the app's own handlers (and show up in
the app)."""
from __future__ import annotations

import json
import os
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
    import steam_workshop_api as swa
    monkeypatch.setattr(mod_manager, "write_modlist", lambda *a, **k: None)
    monkeypatch.setattr(models.AppConfig, "save", lambda self, *a, **k: None)
    monkeypatch.setattr(swa, "get_details", lambda *a, **k: swa.DetailsResult(ok=False, error="offline (tests)"))


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
    other = models.ServerConfig(id="s2", name="Siptah", install_dir=str(tmp_path / "other"),
                                game_port=7787, query_port=27025, rcon_port=25585)
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

    def raw(self, path, data, headers, timeout=20):
        """A POST with a raw body, on a worker thread (see call)."""
        out = {}

        def run():
            req = urllib.request.Request(self.base + path, data=data, method="POST", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    out["status"], out["body"] = resp.status, resp.read()
            except urllib.error.HTTPError as e:
                out["status"], out["body"] = e.code, e.read()
        t = threading.Thread(target=run)
        t.start()
        deadline = time.monotonic() + timeout
        while t.is_alive() and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.005)
        t.join(1)
        return out["status"], json.loads(out["body"] or b"{}")

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
    assert data["version"] and data["theme"]["bg"].startswith("#")


@pytest.mark.parametrize("path", ["", "/players", "/access", "/backups", "/updates", "/mods", "/settings"])
def test_every_server_page_reads_for_any_server(web, path):
    win, c = web
    for sid in ("s1", "s2"):
        assert c.get(f"/api/servers/{sid}{path}")
    assert win.config.active_server_id == "s1"  # reading never switches the PC's server


def test_unknown_server_is_a_clear_error(web):
    _, c = web
    status, data, _ = c.call("GET", "/api/servers/nope/players")
    assert status == 400 and "doesn't exist" in data["error"]


def test_no_select_endpoint_and_pc_keeps_its_server(web):
    win, c = web
    assert c.call("POST", "/api/servers/s2/select")[0] == 404
    assert win.config.active_server_id == "s1"


# ---------------------------------------------------------------- settings --

def _field(pages, page_key, key):
    page = next(p for p in pages if p["key"] == page_key)
    return next(f for f in page["fields"] if f["key"] == key)


def test_settings_save_for_a_server_not_open_in_the_app(web):
    win, c = web
    c.post("/api/servers/s2/settings/network", {"values": {"max_players": 33}})
    assert win.config.servers[1].max_players == 33
    assert win.config.active_server_id == "s1"
    assert win.settings_network_page._committed["max_players"] == win.config.servers[0].max_players
    pages = c.get("/api/servers/s2/settings")["pages"]
    assert _field(pages, "network", "max_players")["value"] == 33


def test_web_save_never_commits_a_half_typed_desktop_edit(web):
    win, c = web
    s1 = win.config.servers[0]
    page = win.settings_network_page
    page.max_players_spin.setValue(77) if hasattr(page, "max_players_spin") else page._setters["max_players"](page._fields["max_players"], 77)
    c.post("/api/servers/s1/settings/network", {"values": {"name": "Renamed From Web"}})
    assert s1.name == "Renamed From Web"
    assert s1.max_players != 77                                  # the desktop draft wasn't saved
    assert page._getters["max_players"](page._fields["max_players"]) == 77  # ...and is still there to finish
    assert page._getters["name"](page._fields["name"]) == "Renamed From Web"  # untouched field shows the new value
    assert page.dirty_count() == 1


def test_web_shows_saved_values_not_desktop_drafts(web):
    win, c = web
    page = win.settings_restart_page
    page._setters["restart_start"](page._fields["restart_start"], "01:00")
    pages = c.get("/api/servers/s1/settings")["pages"]
    assert _field(pages, "restart", "restart_start")["value"] == win.config.servers[0].restart_start


def test_rejected_save_keeps_desktop_edits_and_explains(web):
    win, c = web
    page = win.settings_network_page
    page._setters["max_players"](page._fields["max_players"], 55)
    clash = win.config.servers[1].game_port
    status, data, _ = c.call("POST", "/api/servers/s1/settings/network", {"values": {"game_port": clash}})
    assert status == 400 and data["error"]
    assert page._getters["max_players"](page._fields["max_players"]) == 55


def test_saving_alerts_keeps_discord_live_status(web):
    win, c = web
    s1 = win.config.servers[0]
    s1.discord_status_enabled = True
    win._load_active_server()
    assert win.settings_alerts_page.dirty_count() == 0
    c.post("/api/servers/s1/settings/alerts", {"values": {"rcon_port": 25600}})
    assert s1.rcon_port == 25600 and s1.discord_status_enabled is True


def test_secrets_are_never_sent_to_the_browser(web):
    win, c = web
    s1 = win.config.servers[0]
    s1.rcon_password = "hunter2-secret"
    s1.webhook_discord_url = "https://discord.com/api/webhooks/1/secret"
    raw = json.dumps(c.get("/api/servers/s1/settings"))
    assert "hunter2-secret" not in raw and "webhooks/1/secret" not in raw
    f = _field(c.get("/api/servers/s1/settings")["pages"], "alerts", "rcon_password")
    assert f["type"] == "secret" and f["has_value"] is True
    c.post("/api/servers/s1/settings/alerts", {"values": {"rcon_port": 25601}})
    assert s1.rcon_password == "hunter2-secret"  # left alone when not typed


def test_fields_unlocked_by_a_switch_say_so(web):
    _, c = web
    pages = c.get("/api/servers/s1/settings")["pages"]
    restart = next(p for p in pages if p["key"] == "restart")
    times = [f for f in restart["fields"] if f["key"] in ("restart_start", "restart_end")]
    for f in times:
        assert f["enabled"] or f.get("enabled_when") == {"restart_enabled": True}


def test_network_labels_are_real(web):
    _, c = web
    pages = c.get("/api/servers/s1/settings")["pages"]
    assert _field(pages, "network", "password")["label"] == "Server Password"
    assert _field(pages, "network", "bind_ip")["label"].startswith("Bind Address")
    assert _field(pages, "network", "game_port")["help"]


def test_settings_action_fills_without_saving(web):
    win, c = web
    s1 = win.config.servers[0]
    before = s1.game_port
    clash = win.config.servers[1].game_port
    r = c.post("/api/servers/s1/settings/network/action", {"action": "suggest_port", "values": {"game_port": clash}})
    port = next(f for f in r["page"]["fields"] if f["key"] == "game_port")["value"]
    assert port != clash and s1.game_port == before


def test_generic_page_round_trip(web):
    win, c = web
    pages = c.get("/api/servers/s1/settings")["pages"]
    page = next(p for p in pages if p["key"] not in ("identity", "network", "backups", "restart", "alerts")
                and any(f["type"] == "bool" for f in p["fields"]))
    f = next(f for f in page["fields"] if f["type"] == "bool")
    c.post(f"/api/servers/s1/settings/{page['key']}", {"values": {f["key"]: not f["value"]}})
    again = _field(c.get("/api/servers/s1/settings")["pages"], page["key"], f["key"])
    assert again["value"] == (not f["value"])


# ------------------------------------------------------------------ access --

def test_ban_and_unban_go_through_the_app(web):
    win, c = web
    c.post("/api/servers/s1/access/ban", {"steam_id": "76561198000000001"})
    assert "76561198000000001" in win.config.servers[0].banned_ids
    assert "76561198000000001" in [win.access_page.ban_list.item(i).text() for i in range(win.access_page.ban_list.count())]
    c.post("/api/servers/s1/access/unban", {"steam_id": "76561198000000001"})
    assert "76561198000000001" not in win.config.servers[0].banned_ids


def test_ban_needs_a_real_steam_id(web):
    _, c = web
    status, _, _ = c.call("POST", "/api/servers/s1/access/ban", {"steam_id": "robert'); drop"})
    assert status == 400


def test_access_changes_refused_for_a_server_not_set_up(web, tmp_path):
    win, c = web
    win.config.servers[1].install_dir = ""
    status, data, _ = c.call("POST", "/api/servers/s2/access/ban", {"steam_id": "76561198000000001"})
    assert data["ok"] is False and "set up" in data["message"]


def test_whitelist_edit(web):
    win, c = web
    c.post("/api/servers/s1/access/whitelist", {"steam_id": "76561198000000002"})
    assert "76561198000000002" in win.config.servers[0].whitelist_ids


def test_kick_with_rcon_off_says_so_and_sends_no_alert(web, monkeypatch):
    win, c = web
    sent = []
    monkeypatch.setattr(win, "_notify", lambda *a, **k: sent.append(a))
    win.config.servers[0].rcon_enabled = False
    r = c.post("/api/servers/s1/players/kick", {"name": "Bob"})
    assert r["ok"] is False and "RCON" in r["message"] and sent == []


# -------------------------------------------------------------------- mods --

def test_mod_toggle_and_move_show_in_the_app(web):
    win, c = web
    c.post("/api/servers/s1/mods/toggle", {"id": "222", "enabled": False})
    assert [m["enabled"] for m in win.config.servers[0].mods] == [True, False]
    c.post("/api/servers/s1/mods/move", {"id": "222", "direction": -1})
    assert [m["id"] for m in win.config.servers[0].mods] == ["222", "111"]
    assert "Emberlight" in win.mods_page.list_widget.item(0).text()


def test_mod_move_rejects_garbage(web):
    _, c = web
    assert c.call("POST", "/api/servers/s1/mods/move", {"id": "222", "direction": "up"})[0] == 400


def test_adding_a_mod_twice_says_so(web):
    _, c = web
    r = c.post("/api/servers/s1/mods/add", {"id": "111"})
    assert r["ok"] is False and "already" in r["message"]


def test_mods_wait_while_the_app_is_testing_them(web):
    win, c = web
    win._automation_locked.add("s1")
    status, data, _ = c.call("POST", "/api/servers/s1/mods/toggle", {"id": "111", "enabled": False})
    assert status == 400 and "Wait" in data["error"]
    assert c.post("/api/servers/s1/start")["ok"] is False


def test_download_mods_is_a_real_download_not_the_daily_check(web, monkeypatch):
    win, c = web
    s1 = win.config.servers[0]
    s1.steamcmd_dir = "/tmp/fake-steamcmd"
    started = []
    import update_runner

    class FakeWorker:
        def __init__(self, steamcmd_dir, ids):
            from PySide6.QtCore import QObject, Signal

            class S(QObject):
                sig = Signal(object)
                fin = Signal()
            self._s = S()
            self.finished_download = self._s.sig
            self.finished = self._s.fin
            started.append(ids)

        def start(self):
            pass
    monkeypatch.setattr(update_runner, "ModDownloadWorker", FakeWorker)
    before = s1.last_mod_check_at
    r = c.post("/api/servers/s1/mods/download")
    assert r["ok"] and started == [["111", "222"]]
    assert s1.id in win._mod_refresh_workers and s1.last_mod_check_at == before
    assert win.mods_page.download_btn.text() == "Downloading…"
    assert c.post("/api/servers/s1/mods/download")["ok"] is False  # one at a time


def test_find_broken_mod_works_in_just_tell_me_mode(web, monkeypatch):
    win, c = web
    s1 = win.config.servers[0]
    s1.steamcmd_dir = "/tmp/fake-steamcmd"
    win.config.mod_recovery_mode = "alert"
    started = []
    monkeypatch.setattr(win, "_start_mod_recovery", lambda server, what, manual=False: started.append(manual) or True)
    assert c.post("/api/servers/s1/mods/find-broken")["ok"]
    assert started == [True]


def test_find_broken_mod_refuses_with_players_online(web):
    win, c = web
    win.config.servers[0].steamcmd_dir = "/tmp/fake-steamcmd"
    win._online_by_server["s1"] = {"Conan"}
    r = c.post("/api/servers/s1/mods/find-broken")
    assert r["ok"] is False and "online" in r["message"]


# ------------------------------------------------------------------- power --

def test_start_and_stop_report_real_outcomes(web, monkeypatch):
    import process_manager
    win, c = web
    monkeypatch.setattr(process_manager, "is_running", lambda d: True)
    r = c.post("/api/servers/s1/start")
    assert r["ok"] is False and "already running" in r["message"]
    monkeypatch.setattr(process_manager, "is_running", lambda d: False)
    r = c.post("/api/servers/s1/stop")
    assert r["ok"] is False and "isn't running" in r["message"]


def test_start_failure_is_explained_not_a_dialog(web, monkeypatch):
    import preflight
    import process_manager
    win, c = web
    monkeypatch.setattr(process_manager, "is_running", lambda d: False)
    monkeypatch.setattr(preflight, "run_preflight",
                        lambda s: type("R", (), {"ok": False, "problems": ["Port 7777 is taken"], "repairs": []})())
    r = c.post("/api/servers/s1/start")
    assert r["ok"] is False and "Port 7777 is taken" in r["message"]


def test_stop_clears_online_players_in_the_app(web, monkeypatch):
    import process_manager
    win, c = web
    win._online_by_server["s1"] = {"Conan"}
    win.players_page.set_online_players({"Conan"})
    monkeypatch.setattr(process_manager, "is_running", lambda d: True)
    monkeypatch.setattr(process_manager, "graceful_stop", lambda s: None)
    assert c.post("/api/servers/s1/stop")["ok"]
    assert win._online_by_server["s1"] == set()


def test_enable_rcon_for_a_server_not_open_in_the_app(web):
    win, c = web
    s2 = win.config.servers[1]
    s2.rcon_enabled = False
    win.config.servers[1].install_dir = ""
    assert c.post("/api/servers/s2/enable-rcon")["ok"]
    assert s2.rcon_enabled and s2.rcon_password and win.config.active_server_id == "s1"


# ----------------------------------------------------------------- updates --

def test_update_settings_keep_a_pending_update_and_respect_limits(web):
    win, c = web
    page = win.updates_page
    s1 = win.config.servers[0]
    st = page.state_for(s1)
    st.update(pending="Update: Build 999 available", latest="999", changelog="notes")
    page.set_server(s1)
    c.post("/api/servers/s1/updates/settings", {"interval_hours": 100})
    assert s1.auto_update_check_interval_hours == page.interval_spin.maximum()
    assert page.interval_spin.value() == page.interval_spin.maximum()
    assert not page.pending_frame.isHidden()
    data = c.get("/api/servers/s1/updates")
    assert data["pending"] == "Update: Build 999 available" and data["interval_max"] == page.interval_spin.maximum()


def test_update_state_is_per_server(web):
    win, c = web
    page = win.updates_page
    s2 = win.config.servers[1]
    page.state_for(s2).update(pending="Update: Build 5 available", latest="5", changelog="x")
    assert c.get("/api/servers/s2/updates")["pending"] == "Update: Build 5 available"
    assert c.get("/api/servers/s1/updates")["pending"] == ""
    page.record_result(s2, True, automatic=True)
    hist = c.get("/api/servers/s2/updates")["history"]
    assert hist and hist[0]["result"] == "succeeded"
    assert c.get("/api/servers/s1/updates")["history"] == []


# ----------------------------------------------------------------- backups --

def test_backup_now_refreshes_the_apps_list(web, monkeypatch):
    import backup_manager
    win, c = web
    s1 = win.config.servers[0]
    dest = os.path.join(os.path.dirname(s1.install_dir), "bk")
    os.makedirs(dest)
    s1.backup_destination = dest
    win._load_active_server()

    def fake_backup(server, destination, trigger):
        path = os.path.join(destination, "20260101-120000_manual.zip")
        with open(path, "wb") as f:
            f.write(b"x" * 10)
        return backup_manager.BackupEntry(path=path, when=__import__("datetime").datetime(2026, 1, 1), trigger="manual",
                                          size_bytes=10)
    monkeypatch.setattr(backup_manager, "create_backup_for_server", fake_backup)
    assert c.post("/api/servers/s1/backups/create")["ok"]
    assert win.settings_backups_page.backups_widget.table.rowCount() == 1
    assert len(c.get("/api/servers/s1/backups")["backups"]) == 1


def test_backup_failure_is_explained(web, monkeypatch):
    import backup_manager
    win, c = web
    s1 = win.config.servers[0]
    s1.backup_destination = os.path.dirname(s1.install_dir)
    monkeypatch.setattr(backup_manager, "create_backup_for_server", lambda *a: None)
    r = c.post("/api/servers/s1/backups/create")
    assert r["ok"] is False and "world save" in r["message"]


def test_app_backups_list_notices_new_files_by_itself(web):
    win, c = web
    s1 = win.config.servers[0]
    dest = os.path.join(os.path.dirname(s1.install_dir), "bk2")
    os.makedirs(dest)
    s1.backup_destination = dest
    w = win.settings_backups_page.backups_widget
    w.set_server(s1)
    assert w.table.rowCount() == 0
    with open(os.path.join(dest, "20260102-120000_scheduled.zip"), "wb") as f:
        f.write(b"x")
    w._refresh_if_changed()
    assert w.table.rowCount() == 1


def test_backup_import_upload(web, monkeypatch):
    import backup_manager
    win, c = web
    s1 = win.config.servers[0]
    dest = os.path.join(os.path.dirname(s1.install_dir), "bk3")
    os.makedirs(dest)
    s1.backup_destination = dest
    got = {}

    def fake_import(path, destination, label=""):
        got["data"] = open(path, "rb").read()
        got["label"] = label
        return backup_manager.BackupEntry(path=os.path.join(destination, "x.zip"),
                                          when=__import__("datetime").datetime(2026, 1, 1), trigger="imported",
                                          size_bytes=3)
    monkeypatch.setattr(backup_manager, "import_external_backup", fake_import)
    status, data = c.raw("/api/servers/s1/backups/import", b"PK\x03",
                         {"X-ConanOps": "1", "Cookie": c.cookie, "X-Filename": "my%20world.zip"})
    assert status == 200 and data["ok"]
    assert got == {"data": b"PK\x03", "label": "my-world"}


def test_backup_import_needs_sign_in(web):
    _, c = web
    status, _ = c.raw("/api/servers/s1/backups/import", b"PK", {"X-ConanOps": "1"})
    assert status == 401


# ------------------------------------------------------------- app options --

def test_app_text_options(web):
    win, c = web
    c.post("/api/app/steam_api_key", {"value": "ABC123"})
    c.post("/api/app/duckdns_domain", {"value": "myserver"})
    assert win.config.steam_api_key == "ABC123" and win.app_settings_page.steam_api_key_edit.text() == "ABC123"
    assert win.config.duckdns_domain == "myserver"
    data = c.get("/api/app")
    assert data["steam_api_key_set"] is True and "ABC123" not in json.dumps(data)
    assert c.call("POST", "/api/app/workshop_update_cutoff", {"value": "yesterday"})[0] == 400
    c.post("/api/app/workshop_update_cutoff", {"value": "2026-09-01"})
    assert win.config.workshop_update_cutoff == "2026-09-01"


def test_app_toggles_dont_pop_dialogs(web, monkeypatch):
    import keep_alive
    win, c = web
    monkeypatch.setattr(keep_alive, "enable", lambda: False)
    r = c.post("/api/app/keep_alive", {"value": True})
    assert r["ok"] is False and win.config.keep_alive_enabled is False
    assert not win.app_settings_page.keep_alive_checkbox.isChecked()
    c.post("/api/app/keep_awake", {"value": False})
    assert win.config.keep_pc_awake is False


def test_update_window_without_restart_handling_just_saves(web):
    win, c = web
    win.config.handle_update_restarts = False
    c.post("/api/app/update_window", {"value": ["03:00", "05:00"]})
    assert (win.config.update_restart_start, win.config.update_restart_end) == ("03:00", "05:00")
    assert c.call("POST", "/api/app/update_window", {"value": ["3am", "5"]})[0] == 400


def test_unknown_app_option(web):
    _, c = web
    assert c.call("POST", "/api/app/delete_everything", {"value": True})[0] == 400


def test_web_cannot_delete_or_add_servers(web):
    win, c = web
    for path in ("/api/servers/s1/delete", "/api/servers/add", "/api/app/web_password", "/api/app/admin_mode"):
        status, _, _ = c.call("POST", path, {"value": True})
        assert status in (400, 404)
    assert len(win.config.servers) == 2


# ------------------------------------------------------------ diagnostics --

def test_port_guide(web, monkeypatch):
    import network_setup
    import network_utils
    _, c = web
    monkeypatch.setattr(network_utils, "get_default_gateway", lambda ip=None: "192.168.1.1")
    monkeypatch.setattr(network_setup, "get_public_ip", lambda timeout=3.0: "203.0.113.9")
    g = c.get("/api/servers/s1/port-guide")
    assert len(g["steps"]) == 8 and "192.168.1.1" in g["steps"][0]["body"]


def test_diagnostics_checks_ports_against_other_servers(web, monkeypatch):
    import diagnostics
    _, c = web
    seen = {}
    monkeypatch.setattr(diagnostics, "run_diagnostics", lambda s, reserved_ports=None: seen.setdefault("p", reserved_ports) and [])
    c.post("/api/servers/s1/diagnostics")
    assert seen["p"]


def test_app_dialogs_are_captured_not_shown(web):
    win, c = web
    from PySide6.QtWidgets import QMessageBox

    def act():
        QMessageBox.warning(None, "Heads up", "Something the PC would have shown")
        return 1
    result, msgs = win.web_control.api.bridge.call(act)
    assert result == 1 and msgs == ["Heads up: Something the PC would have shown"]


# ------------------------------------------------------------------ alerts --

def test_alerts_are_app_settings_with_guides(web):
    win, c = web
    pages = c.get("/api/servers/s1/settings")["pages"]
    rcon = next(p for p in pages if p["key"] == "alerts")
    assert rcon["title"] == "RCON" and not any("webhook" in f["key"] for f in rcon["fields"])
    d = c.get("/api/app")
    assert d["alert_discord_set"] is False and len(d["alert_guides"]["discord"]["steps"]) >= 5
    c.post("/api/app/alert_discord_url", {"value": "https://discord.com/api/webhooks/1/abc"})
    c.post("/api/app/discord_status_enabled", {"value": True})
    assert win.config.alert_discord_url == "https://discord.com/api/webhooks/1/abc"
    assert win.config.discord_status_enabled is True
    assert win.app_settings_page.alert_discord_edit.text() == win.config.alert_discord_url
    d = c.get("/api/app")
    assert d["alert_discord_set"] is True and "webhooks/1/abc" not in json.dumps(d)
    status, data, _ = c.call("POST", "/api/app/alert_discord_url", {"value": "https://evil.example/x"})
    assert status == 400 and "Discord webhook" in data["error"]


def test_send_test_uses_typed_link_else_saved(web, monkeypatch):
    import webhooks
    win, c = web
    seen = []
    monkeypatch.setattr(webhooks, "test_discord", lambda url, name: seen.append(url) or (True, "Sent"))
    win.config.alert_discord_url = "https://discord.com/api/webhooks/1/saved"
    assert c.post("/api/alerts/test", {"kind": "discord"})["ok"]
    c.post("/api/alerts/test", {"kind": "discord", "url": "https://discord.com/api/webhooks/2/typed"})
    assert seen == ["https://discord.com/api/webhooks/1/saved", "https://discord.com/api/webhooks/2/typed"]
    assert c.call("POST", "/api/alerts/test", {"kind": "email"})[0] == 400


def test_app_options_describe_the_web_version(web):
    _, c = web
    d = c.get("/api/app")
    assert d["web_lan_url"].startswith("http://") and d["web_sessions"] >= 1
    assert d["web_remote_on"] is False


def test_a_newly_added_mod_isnt_called_missing(web, monkeypatch):
    import steam_workshop_api as swa
    win, c = web
    item = swa.WorkshopItem(id="111", title="Pippi", description="", author_steam_id="", subscriptions=1,
                            status=swa.STATUS_UPDATED)
    win.mods_page.store_status("s1", {"111", "222"}, {"111": item})
    win.config.servers[0].mods.append({"id": "333", "name": "New", "enabled": True})
    mods = {m["id"]: m for m in c.get("/api/servers/s1/mods")["mods"]}
    assert mods["222"]["status"] == "missing"     # was checked: really not on the Workshop
    assert mods["333"]["status"] == "unchecked"   # added since (and the lookup is offline here)


def test_web_settings_save_keeps_the_pc_console(web):
    win, c = web
    win.console_page.output.appendPlainText("> listplayers") if hasattr(win.console_page.output, "appendPlainText") \
        else win.console_page.output.append("> listplayers")
    c.post("/api/servers/s1/settings/alerts", {"values": {"rcon_port": 25611}})
    assert "listplayers" in win.console_page.output.toPlainText()


def test_chunked_request_bodies_work(web):
    import socket as sk
    win, c = web
    body = json.dumps({"steam_id": "76561198000000009"}).encode()
    req = (f"POST /api/servers/s1/access/whitelist HTTP/1.1\r\nHost: 127.0.0.1\r\nX-ConanOps: 1\r\n"
           f"Cookie: {c.cookie}\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
           f"Connection: close\r\n\r\n").encode()
    chunks = b"".join(f"{len(part):x}\r\n".encode() + part + b"\r\n" for part in (body[:10], body[10:])) + b"0\r\n\r\n"
    out = {}

    def run():
        s = sk.create_connection(("127.0.0.1", int(c.base.rsplit(":", 1)[1])), timeout=10)
        s.sendall(req + chunks)
        data = b""
        while True:
            part = s.recv(65536)
            if not part:
                break
            data += part
        out["resp"] = data
    t = threading.Thread(target=run)
    t.start()
    deadline = time.monotonic() + 15
    while t.is_alive() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.005)
    assert b" 200 " in out["resp"].split(b"\r\n")[0]
    assert "76561198000000009" in win.config.servers[0].whitelist_ids
