"""Alerts actually get delivered: what ConanOps sends to Discord and ntfy,
checked against a local stand-in server."""
from __future__ import annotations

import json
import threading
from email.header import decode_header, make_header
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import webhooks


@pytest.fixture
def endpoint():
    got = []
    state = {"status": 204, "body": b""}

    class H(BaseHTTPRequestHandler):
        def _handle(self):
            n = int(self.headers.get("Content-Length", 0))
            got.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                        "body": self.rfile.read(n)})
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(state["body"])
        do_POST = do_PATCH = _handle

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", got, state
    srv.shutdown()


def test_discord_requests_identify_themselves(endpoint):
    """Discord's Cloudflare refuses Python's default User-Agent."""
    base, got, _ = endpoint
    assert webhooks.send_discord(base + "/api/webhooks/1/abc", "hello")
    assert got[-1]["headers"]["User-Agent"].startswith("ConanOps/")
    assert "Python-urllib" not in got[-1]["headers"]["User-Agent"]


def test_discord_never_pings_and_stays_under_the_length_limit(endpoint):
    base, got, _ = endpoint
    webhooks.send_discord(base + "/api/webhooks/1/abc", "@everyone " + "x" * 5000)
    body = json.loads(got[-1]["body"])
    assert body["allowed_mentions"] == {"parse": []}
    assert len(body["content"]) <= webhooks.DISCORD_MAX_CHARS


def test_ntfy_title_with_special_characters_is_delivered(endpoint):
    """Every ConanOps alert title has a "—" in it ("Server Crashed — name")."""
    base, got, _ = endpoint
    assert webhooks.send_ntfy(base + "/topic", "it crashed", title="Server Crashed — Exiled Lands")
    raw = got[-1]["headers"]["Title"]
    assert str(make_header(decode_header(raw))) == "Server Crashed — Exiled Lands"
    assert got[-1]["body"] == b"it crashed"


def test_live_status_keeps_thread_links_working(endpoint):
    base, got, state = endpoint
    state.update(status=200, body=b'{"id": "77"}')
    url = base + "/api/webhooks/1/abc?thread_id=9"
    assert webhooks.update_discord_status(url, "", "Online") == "77"
    assert got[-1]["path"] == "/api/webhooks/1/abc?thread_id=9&wait=true"
    assert webhooks.update_discord_status(url, "77", "Offline") == "77"
    assert got[-1]["method"] == "PATCH" and got[-1]["path"] == "/api/webhooks/1/abc/messages/77?thread_id=9"


def test_test_messages_explain_problems(endpoint, monkeypatch):
    base, got, state = endpoint
    ok, text = webhooks.test_discord("https://example.com/hook", "S")
    assert not ok and "isn't a Discord webhook link" in text
    ok, text = webhooks.test_ntfy("ntfy.sh/topic", "S")
    assert not ok and "isn't an ntfy topic link" in text
    # A deleted webhook answers 404.
    monkeypatch.setattr(webhooks, "discord_url_problem", lambda url: "")
    state["status"] = 404
    ok, text = webhooks.test_discord(base + "/api/webhooks/1/abc", "S")
    assert not ok and "doesn't work anymore" in text
    state["status"] = 204
    ok, text = webhooks.test_discord(base + "/api/webhooks/1/abc", "Exiled Lands")
    assert ok and "Exiled Lands" in json.loads(got[-1]["body"])["content"]


def test_discord_url_check():
    good = ["https://discord.com/api/webhooks/123/abc-DEF_9", "https://discordapp.com/api/webhooks/1/x",
            "https://ptb.discord.com/api/v10/webhooks/1/x", "https://discord.com/api/webhooks/1/x?thread_id=5"]
    for url in good:
        assert webhooks.discord_url_problem(url) == "", url
    for url in ("http://discord.com/api/webhooks/1/x", "https://discord.com/channels/1/2", "https://evil.com/api/webhooks/1/x"):
        assert webhooks.discord_url_problem(url), url


def test_rate_limit_is_retried_once(endpoint, monkeypatch):
    base, got, state = endpoint
    calls = {"n": 0}
    import urllib.request
    real = urllib.request.urlopen

    def flaky(req, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            import email.message
            import urllib.error
            hdrs = email.message.Message()
            hdrs["Retry-After"] = "0.1"
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many", hdrs, None)
        return real(req, timeout=timeout)
    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    monkeypatch.setattr(webhooks.time, "sleep", lambda s: None)
    assert webhooks.send_discord(base + "/api/webhooks/1/abc", "hi")
    assert calls["n"] == 2


def test_changing_the_webhook_starts_new_status_messages(monkeypatch):
    import sys
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import models
    from ui.main_window import MainWindow
    monkeypatch.setattr(models.AppConfig, "save", lambda self, *a, **k: None)
    s = models.ServerConfig(id="s1", name="X", discord_status_message_id="55")
    cfg = models.AppConfig(servers=[s], active_server_id="s1", alert_discord_url="https://discord.com/api/webhooks/1/a")
    win = MainWindow(config=cfg)
    try:
        page = win.app_settings_page
        page.set_alert_links("https://discord.com/api/webhooks/1/a", "", False)
        assert s.discord_status_message_id == "55"
        page.set_alert_links("https://discord.com/api/webhooks/2/b", "", False)
        assert s.discord_status_message_id == ""
    finally:
        win.close()


def test_alerts_go_to_the_app_wide_links(monkeypatch):
    import sys
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import models
    from ui import main_window
    monkeypatch.setattr(models.AppConfig, "save", lambda self, *a, **k: None)
    made = []

    class FakeWorker:
        def __init__(self, discord, ntfy, message, title, name, get_link_line=None):
            from PySide6.QtCore import QObject, Signal

            class S(QObject):
                sig = Signal(object)
                fin = Signal()
            self._s = S()
            self.finished_notify = self._s.sig
            self.finished = self._s.fin
            made.append((discord, ntfy, title))

        def start(self):
            pass

        def isRunning(self):
            return False

        def wait(self, *a):
            return True
    import ui.window.alerts as alerts_part  # _notify lives there
    monkeypatch.setattr(alerts_part, "_NotifyWorker", FakeWorker)
    s = models.ServerConfig(id="s1", name="Exiled")
    cfg = models.AppConfig(servers=[s], active_server_id="s1", alert_discord_url="https://discord.com/api/webhooks/1/a",
                           alert_ntfy_url="https://ntfy.sh/t")
    win = main_window.MainWindow(config=cfg)
    try:
        win._notify(s, "It crashed", title="Server Crashed")
        assert made == [("https://discord.com/api/webhooks/1/a", "https://ntfy.sh/t", "Server Crashed — Exiled")]
    finally:
        win.close()


def test_guides_have_steps():
    import alert_guides
    for title, intro, steps in alert_guides.ALL.values():
        assert title.endswith("?") and intro and len(steps) >= 4
    assert "Webhooks" in alert_guides.DISCORD[2][1][0]


def test_long_ntfy_titles_stay_on_one_line(endpoint):
    base, got, _ = endpoint
    title = "Server Crashed — " + "A Very Long Server Name " * 6
    assert webhooks.send_ntfy(base + "/topic", "x", title=title)
    raw = got[-1]["headers"]["Title"]
    assert "\n" not in raw and str(make_header(decode_header(raw))) == title
    assert got[-1]["headers"]["Content-Type"].startswith("text/plain")


def test_from_anywhere_link_goes_to_ntfy_not_discord(monkeypatch):
    import sys
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    import models
    from ui.main_window import MainWindow
    monkeypatch.setattr(models.AppConfig, "save", lambda self, *a, **k: None)
    sent = []
    monkeypatch.setattr(webhooks, "send_discord", lambda *a, **k: sent.append(("discord", a)) or True)
    monkeypatch.setattr(webhooks, "send_ntfy", lambda url, text, title="": sent.append(("ntfy", url, text)) or True)
    import threading
    monkeypatch.setattr(threading, "Thread", lambda target=None, args=(), daemon=None, **k:
                        type("T", (), {"start": lambda self: target(*args)})())
    s = models.ServerConfig(id="s1", name="X")
    win = MainWindow(config=models.AppConfig(servers=[s], active_server_id="s1",
                                             alert_discord_url="https://discord.com/api/webhooks/1/a",
                                             alert_ntfy_url="https://ntfy.sh/private"))
    try:
        win._on_web_tunnel_changed("https://abc.trycloudflare.com", "Connected")
        assert [x[0] for x in sent] == ["ntfy"] and "abc.trycloudflare.com" in sent[0][2]
    finally:
        win.close()
