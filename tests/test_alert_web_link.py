"""Every Discord alert and status message links to the web version."""
import types

import webhooks
from ui.window.alerts import AlertsMixin


def test_link_line_and_limit():
    line = webhooks.web_link_line("https://x.trycloudflare.com/?server=a#/dashboard")
    assert line == "[Open ConanOps](<https://x.trycloudflare.com/?server=a#/dashboard>)"
    assert "home Wi-Fi" in webhooks.web_link_line("http://192.168.1.5:8765", home_only=True)
    assert webhooks.web_link_line("") == ""
    long = webhooks.with_link("x" * 3000, line)
    assert len(long) <= webhooks.DISCORD_MAX_CHARS and long.endswith(line)
    assert webhooks.with_link("hi", "") == "hi"


def test_notify_puts_link_on_discord_only(monkeypatch):
    sent = {}
    monkeypatch.setattr(webhooks, "send_discord", lambda url, msg: sent.setdefault("discord", msg) or True)
    monkeypatch.setattr(webhooks, "send_ntfy", lambda url, msg, title="": sent.setdefault("ntfy", msg) or True)
    webhooks.notify("d", "n", "Server crashed", title="Crash", link_line="[Open ConanOps](<u>)")
    assert sent["discord"] == "**Crash**: Server crashed\n[Open ConanOps](<u>)"
    assert sent["ntfy"] == "Server crashed"


def _win(running=True, remote="", remote_on=False, lan="http://192.168.1.5:8765"):
    w = types.SimpleNamespace()
    w.web_control = types.SimpleNamespace(is_running=running, url_for=lambda: lan)
    w.web_tunnel = types.SimpleNamespace(url=remote)
    w.config = types.SimpleNamespace(web_remote_enabled=remote_on)
    return w


def test_link_prefers_from_anywhere_and_names_the_server():
    line = AlertsMixin.web_link_line(_win(remote="https://a.trycloudflare.com", remote_on=True), "s1")
    assert "<https://a.trycloudflare.com/?server=s1#/dashboard>" in line and "Wi-Fi" not in line
    line = AlertsMixin.web_link_line(_win(), "s1")
    assert "<http://192.168.1.5:8765/?server=s1#/dashboard>" in line and "home Wi-Fi" in line


def test_no_link_when_web_version_is_off():
    assert AlertsMixin.web_link_line(_win(running=False), "s1") == ""
