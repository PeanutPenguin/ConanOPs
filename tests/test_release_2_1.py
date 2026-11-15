"""2.1: mod verdicts and load order, Discord commands, heartbeat, small fixes."""
from __future__ import annotations

from datetime import time as dtime
from types import SimpleNamespace

import discord_bot
import heartbeat
import mod_compat
import models
import scheduler
import steam_workshop_api as swa
import world_report
from tests.test_audit_fixes import _cfg, _quiet
from tests.test_server_finder import _window


def _item(status, updated=1000, children=()):
    return SimpleNamespace(status=status, time_updated=updated, children=list(children), title="T")


# ------------------------------------------------------------- verdicts --

def test_old_mod_that_ran_on_this_build_counts_as_working():
    proof = mod_compat.proof_for("77", 1000, True)
    assert mod_compat.verdict(_item(swa.STATUS_STALE), True, proof, "77")[0] == mod_compat.WORKING
    assert mod_compat.verdict(_item(swa.STATUS_STALE), True, None, "77")[0] == mod_compat.UNVERIFIED


def test_proof_stops_counting_after_a_game_update_or_a_new_mod_version():
    proof = mod_compat.proof_for("77", 1000, True)
    assert mod_compat.verdict(_item(swa.STATUS_STALE), True, proof, "78")[0] == mod_compat.UNVERIFIED
    assert mod_compat.verdict(_item(swa.STATUS_STALE, updated=5000), True, proof, "77")[0] == mod_compat.UNVERIFIED


def test_failed_legacy_removed_and_current():
    bad = mod_compat.proof_for("77", 1000, False)
    assert mod_compat.verdict(_item(swa.STATUS_UPDATED), True, bad, "77")[0] == mod_compat.FAILED
    assert mod_compat.verdict(_item(swa.STATUS_LEGACY), True, None, "77")[0] == mod_compat.LEGACY
    assert mod_compat.verdict(None, True)[0] == mod_compat.REMOVED
    assert mod_compat.verdict(None, False) == ("", "")
    assert mod_compat.verdict(_item(swa.STATUS_UPDATED), True, None, "77")[0] == mod_compat.CURRENT


# ----------------------------------------------------------- load order --

def test_required_mod_is_moved_above_the_mod_that_needs_it():
    mods = [{"id": "a"}, {"id": "b"}, {"id": "lib"}, {"id": "off", "enabled": False}]
    items = {"a": _item("x", children=["lib"]), "b": _item("x"), "lib": _item("x"),
             "off": _item("x", children=["zzz"])}
    assert mod_compat.order_problems(mods, items) == [("a", "lib")]
    fixed = mod_compat.fixed_order(mods, items)
    assert [m["id"] for m in fixed] == ["lib", "a", "b", "off"]
    assert mod_compat.order_problems(fixed, items) == []


def test_mods_that_need_each_other_dont_loop_forever():
    mods = [{"id": "a"}, {"id": "b"}]
    items = {"a": _item("x", children=["b"]), "b": _item("x", children=["a"])}
    assert len(mod_compat.fixed_order(mods, items)) == 2


# ------------------------------------------------------------ patch date --

def test_cutoff_is_the_later_of_the_set_and_published_dates():
    doc = {"breaking_patches": [{"date": "2026-09-01"}, {"date": "2027-01-20"}, {"date": "nonsense"}, "x"]}
    assert mod_compat.latest_patch_date(doc) == "2027-01-20"
    assert mod_compat.latest_patch_date([]) == ""
    assert mod_compat.effective_cutoff("2026-09-01", "2027-01-20") == "2027-01-20"
    assert mod_compat.effective_cutoff("2027-03-01", "2027-01-20") == "2027-03-01"
    assert mod_compat.effective_cutoff("typo", "") == swa.DEFAULT_CUTOFF_DATE
    cfg = models.AppConfig()
    cfg.workshop_patch_date = "2027-01-20"
    assert cfg.mod_cutoff() == "2027-01-20"


def test_shipped_patch_list_is_readable():
    import json
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mod_patches.json")
    with open(path, encoding="utf-8") as f:
        assert mod_compat.latest_patch_date(json.load(f))


# ---------------------------------------------------- mod watch behaviour --

def _mod_window(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    win = _window(monkeypatch, cfg)
    sent = _quiet(win, monkeypatch)
    s = cfg.servers[0]
    s.mods = [{"id": "111", "enabled": True, "name": "Pippi"}, {"id": "222", "enabled": True, "name": "Gone"}]
    return cfg, win, sent, s


def test_mod_removed_from_the_workshop_is_reported_once(tmp_path, monkeypatch):
    import mod_versions
    _cfg_, win, sent, s = _mod_window(tmp_path, monkeypatch)
    monkeypatch.setattr(mod_versions, "newer_on_workshop", lambda *a: [])
    result = SimpleNamespace(ok=True, items={"111": _item(swa.STATUS_UPDATED)})
    win._on_mod_watch_result(s, result)
    win._on_mod_watch_result(s, result)
    assert [t for t, _m in sent] == ["Mod Removed From Workshop"] and "Gone" in sent[0][1]


def test_newer_mod_is_announced_not_installed_when_automatic_updates_are_off(tmp_path, monkeypatch):
    import mod_versions
    _cfg_, win, sent, s = _mod_window(tmp_path, monkeypatch)
    s.mod_auto_update = False
    monkeypatch.setattr(mod_versions, "newer_on_workshop", lambda *a: [("111", 5000)])
    monkeypatch.setattr(win, "_with_warning", lambda *a, **k: (_ for _ in ()).throw(AssertionError("restarted")))
    result = SimpleNamespace(ok=True, items={"111": _item("x"), "222": _item("x")})
    win._on_mod_watch_result(s, result)
    win._on_mod_watch_result(s, result)
    assert [t for t, _m in sent] == ["Mod Update Waiting"]


def test_started_server_records_which_mod_versions_it_ran(tmp_path, monkeypatch):
    import mod_versions
    cfg, win, _sent, s = _mod_window(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg, "save", lambda *a, **k: None)
    monkeypatch.setattr(mod_versions, "installed_times", lambda d, ids: {"111": 1000, "222": None})
    s.mod_proof = {"999": mod_compat.proof_for("1", 5, True)}  # a mod no longer listed
    win._record_mod_proof(s)
    assert s.mod_proof == {"111": {"build": "1", "time": 1000, "ok": True}}


def test_failure_right_after_a_mod_update_names_that_mod(tmp_path, monkeypatch):
    import time
    cfg, win, sent, s = _mod_window(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg, "save", lambda *a, **k: None)
    s.mod_versions = {"111": 1000}
    win._recent_mod_change[s.id] = (time.monotonic(), ["111"])
    assert win._blame_recent_mod_change(s, "crashed") == "Pippi"
    assert s.mod_proof["111"]["ok"] is False and sent[0][0] == "Mod Update Broke the Server"
    assert win._blame_recent_mod_change(s, "crashed") == ""  # nothing changed recently any more


# -------------------------------------------------------------- Discord --

def test_new_discord_commands_are_recognised():
    assert discord_bot.parse_command("!link") == ("link", "")
    assert discord_bot.parse_command("!backup Siptah") == ("backup", "Siptah")
    assert discord_bot.parse_command("!update") == ("update", "")
    assert discord_bot.parse_command("!players") == ("players", "")
    assert discord_bot.parse_command("!linker") is None
    rows = [{"name": "A", "running": True, "names": ["Oran", "Bo"]}, {"name": "B", "running": False, "names": []},
            {"name": "C", "running": True, "names": []}]
    text = discord_bot.players_text(rows)
    assert "2 online: Oran, Bo" in text and "offline" in text and "nobody online" in text


def _bot_window(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    win = _window(monkeypatch, cfg)
    _quiet(win, monkeypatch)
    replies = []
    win._discord_bot = SimpleNamespace(reply=lambda mid, text: replies.append(text))
    cfg.discord_bot_admin_ids = "123456789012345678"
    return cfg, win, replies


def test_link_and_backup_are_for_allowed_people_only(tmp_path, monkeypatch):
    cfg, win, replies = _bot_window(tmp_path, monkeypatch)
    started = []
    monkeypatch.setattr(win, "start_backup", lambda srv, trigger, label="": started.append(trigger) or "")
    monkeypatch.setattr(win, "web_link_line", lambda sid="": "[Open ConanOps](https://x.example)")
    win._on_discord_command("1", "999999999999999999", "stranger", "!link")
    win._on_discord_command("2", "999999999999999999", "stranger", "!backup")
    assert all("allowed list" in r for r in replies) and started == []
    win._on_discord_command("3", "123456789012345678", "oran", "!link")
    win._on_discord_command("4", "123456789012345678", "oran", "!backup")
    assert "https://x.example" in replies[2] and started == ["manual"] and "Backing up" in replies[3]


def test_link_says_so_when_there_is_none(tmp_path, monkeypatch):
    cfg, win, replies = _bot_window(tmp_path, monkeypatch)
    monkeypatch.setattr(win, "web_link_line", lambda sid="": "")
    win._on_discord_command("1", "123456789012345678", "oran", "!link")
    assert "no link right now" in replies[0]


# ----------------------------------------------------- heartbeat & fixes --

def test_heartbeat_link_is_checked_and_a_dead_one_doesnt_raise():
    import pytest
    assert heartbeat.clean_url("  https://hc-ping.com/abc ") == "https://hc-ping.com/abc"
    assert heartbeat.clean_url("") == ""
    with pytest.raises(ValueError):
        heartbeat.clean_url("hc-ping.com/abc")
    assert heartbeat.ping("") is False
    assert heartbeat.ping("http://127.0.0.1:9/nothing", timeout=1.0) is False


def test_heartbeat_link_survives_a_save_and_load(tmp_path):
    cfg = models.AppConfig()
    cfg.heartbeat_url = "https://hc-ping.com/abc"
    path = str(tmp_path / "config.json")
    cfg.save(path)
    assert models.AppConfig.load(path).heartbeat_url == "https://hc-ping.com/abc"


def test_world_report_reads_the_live_world_on_an_upgraded_server():
    names = [("ConanSandbox/Saved/game.db", 900), ("ConanSandbox/Saved/game_0.db", 500),
             ("ConanSandbox/Saved/game_backup_1.db", 950)]
    assert world_report._main_world(names)[0].endswith("game_0.db")
    assert world_report._main_world(names[:1] + names[2:])[0].endswith("game.db")


def test_restart_window_with_equal_times_still_happens():
    four = dtime(4, 0)
    assert scheduler.is_within_window(dtime(4, 30), four, four)
    assert not scheduler.is_within_window(dtime(5, 0), four, four)
    assert scheduler.is_within_window(dtime(23, 59), dtime(23, 30), dtime(23, 30))


def test_config_with_a_broken_server_entry_still_loads(tmp_path):
    import json
    cfg = models.AppConfig()
    cfg.servers.append(models.ServerConfig(name="Good"))
    path = str(tmp_path / "config.json")
    cfg.save(path)
    raw = json.load(open(path, encoding="utf-8"))
    raw["servers"].append("not a server")
    json.dump(raw, open(path, "w", encoding="utf-8"))
    assert [s.name for s in models.AppConfig.load(path).servers] == ["Good"]
