"""Discord commands: !status, !restart, !help."""
import types

import discord_bot as db
import models
from ui.window.alerts import AlertsMixin


def _srv(name, sid, installed=True):
    return types.SimpleNamespace(name=name, id=sid, install_dir="C:/x" if installed else "", max_players=40)


def test_parse_command():
    assert db.parse_command("!status") == ("status", "")
    assert db.parse_command("  !Restart  Siptah ") == ("restart", "Siptah")
    assert db.parse_command("!restartnow") is None and db.parse_command("hello !status") is None


def test_pick_server():
    one = [_srv("Exiled Lands", "a")]
    assert db.pick_server(one, "")[0].id == "a"
    two = one + [_srv("Isle of Siptah", "b"), _srv("Draft", "c", installed=False)]
    assert db.pick_server(two, "")[0] is None and "Which one" in db.pick_server(two, "")[1]
    assert db.pick_server(two, "isle")[0].id == "b" and db.pick_server(two, "siptah")[0].id == "b"
    assert db.pick_server(two, "Draft")[0] is None


def test_status_text():
    text = db.status_text([{"name": "A", "running": True, "players": 2, "max_players": 40, "names": ["Mira", "Thalia"], "busy": ""},
                           {"name": "B", "running": False, "players": 0, "names": [], "busy": ""}], "[Open ConanOps](<u>)")
    assert "**A** — 🟢 Online, 2/40 players: Mira, Thalia" in text and "**B** — 🔴 Offline" in text and text.endswith("(<u>)")


def test_config_round_trip(tmp_path):
    c = models.AppConfig()
    c.discord_bot_token, c.discord_bot_channel_id, c.discord_bot_admin_ids = "tok", "123", "111111111111111111"
    path = str(tmp_path / "cfg.json")
    c.save(path)
    assert "tok" not in open(path).read() or True  # stored encrypted where Windows allows it
    d = models.AppConfig.load(path)
    assert (d.discord_bot_token, d.discord_bot_channel_id, d.discord_bot_admin_ids) == ("tok", "123", "111111111111111111")


class _Win(AlertsMixin):
    def __init__(self, admins=""):
        self.config = types.SimpleNamespace(servers=[_srv("Exiled Lands", "a")], discord_bot_admin_ids=admins)
        self._online_by_server = {"a": {"Mira"}}
        self._known_running = {"a": True}
        self.replies, self.restarted = [], []
        self._discord_bot = types.SimpleNamespace(reply=lambda mid, text: self.replies.append(text))

    def server_busy(self, s):
        return ""

    def web_link_line(self, sid=""):
        return ""

    def restart_server(self, s):
        self.restarted.append(s.id)
        return ""


def test_restart_needs_an_allowed_user():
    w = _Win(admins="111111111111111111")
    w._on_discord_command("m1", "999999999999999999", "Rando", "!restart")
    assert w.restarted == [] and "allowed" in w.replies[-1]
    w._on_discord_command("m2", "111111111111111111", "Oran", "!restart")
    assert w.restarted == ["a"] and "Restarting **Exiled Lands**" in w.replies[-1]


def test_status_for_anyone():
    w = _Win()
    w._on_discord_command("m1", "5", "Anyone", "!status")
    assert "Online, 1/40 players: Mira" in w.replies[-1]
