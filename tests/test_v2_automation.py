"""2.0: Workshop mod auto-update, the weekly report and scheduled events."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

import pytest

import history
import models
import mod_versions
import scheduled_events as se
import weekly_report
from steam_workshop_api import DetailsResult, WorkshopItem
from tests.test_v2_reliability import _countdown_window, _server_with_world
from tests.test_server_finder import _window


def _item(wid, ts):
    return WorkshopItem(id=wid, title=wid, description="", author_steam_id="", subscriptions=0, time_updated=ts)


# ------------------------------------------------------------ mod versions --

def _acf(steamcmd, items):
    d = os.path.join(steamcmd, "steamapps", "workshop")
    os.makedirs(d, exist_ok=True)
    body = "".join(f'\t\t"{wid}"\n\t\t{{\n\t\t\t"size"\t\t"10"\n\t\t\t"timeupdated"\t\t"{ts}"\n'
                   f'\t\t\t"manifest"\t\t"1"\n\t\t}}\n' for wid, ts in items.items())
    with open(os.path.join(d, "appworkshop_440900.acf"), "w") as f:
        f.write('"AppWorkshop"\n{\n\t"appid"\t\t"440900"\n\t"WorkshopItemsInstalled"\n\t{\n' + body
                + '\t}\n\t"WorkshopItemDetails"\n\t{\n\t\t"111"\n\t\t{\n\t\t\t"timeupdated"\t\t"1"\n\t\t}\n\t}\n}\n')


def test_installed_versions_come_from_steams_manifest(tmp_path):
    _acf(str(tmp_path), {"111": 5000, "222": 7000})
    assert mod_versions.installed_times(str(tmp_path), ["111", "222", "333"]) == {"111": 5000, "222": 7000,
                                                                                  "333": None}


def test_without_a_manifest_the_download_time_stands_in(tmp_path):
    folder = tmp_path / "steamapps" / "workshop" / "content" / "440900" / "111"
    folder.mkdir(parents=True)
    (folder / "Mod.pak").write_text("x")
    os.utime(folder / "Mod.pak", (8000, 8000))
    assert mod_versions.installed_times(str(tmp_path), ["111"]) == {"111": 8000}


def test_newer_on_workshop_ignores_missing_and_unanswered_mods(tmp_path):
    _acf(str(tmp_path), {"111": 5000, "222": 7000})
    items = {"111": _item("111", 9000), "222": _item("222", 7050), "333": _item("333", 9999)}
    assert mod_versions.newer_on_workshop(str(tmp_path), ["111", "222", "333", "444"], items) == [("111", 9000)]


# ----------------------------------------------------------- mod watching --

def _mod_window(tmp_path, monkeypatch):
    import process_manager
    win, s, said, clock = _countdown_window(tmp_path, monkeypatch)
    s.steamcmd_dir = str(tmp_path / "steamcmd")
    s.mods = [{"id": "111", "name": "Better Thralls", "enabled": True}]
    _acf(s.steamcmd_dir, {"111": 5000})
    calls = []
    monkeypatch.setattr(process_manager, "is_running", lambda d: True)
    monkeypatch.setattr(process_manager, "graceful_stop", lambda srv: calls.append("stop") or True)
    monkeypatch.setattr(win, "_update_mods_then_relaunch",
                        lambda srv, was_running, watch=False: calls.append(("mods", was_running)))
    return win, s, said, clock, calls


def test_a_newer_mod_warns_players_then_restarts_with_it(tmp_path, monkeypatch):
    win, s, said, clock, calls = _mod_window(tmp_path, monkeypatch)
    try:
        win._on_mod_watch_result(s, DetailsResult(items={"111": _item("111", 9000)}, ok=True))
        assert "mod update in 15 minutes" in said[-1] and not calls
        win._online_by_server[s.id] = set()  # everyone left
        win._tick_countdowns()
        assert calls == ["stop", ("mods", True)]
        win._after_mod_download(s, True)
        assert s.mod_versions == {"111": 9000}
        assert any(e.kind == "mod_update" and "Better Thralls" in e.detail for e in history.events(s.id))
    finally:
        win.close()


def test_mod_watch_does_nothing_when_up_to_date_or_switched_off(tmp_path, monkeypatch):
    win, s, said, clock, calls = _mod_window(tmp_path, monkeypatch)
    try:
        win._on_mod_watch_result(s, DetailsResult(items={"111": _item("111", 5000)}, ok=True))
        s.mod_auto_update = False
        win._on_mod_watch_result(s, DetailsResult(items={"111": _item("111", 9000)}, ok=True))
        win._on_mod_watch_result(s, DetailsResult(ok=False, error="offline"))
        assert not said and not calls and s.id not in win._countdowns
    finally:
        win.close()


def test_a_failed_mod_update_isnt_retried_every_hour(tmp_path, monkeypatch):
    win, s, said, clock, calls = _mod_window(tmp_path, monkeypatch)
    try:
        win._online_by_server[s.id] = set()
        win._on_mod_watch_result(s, DetailsResult(items={"111": _item("111", 9000)}, ok=True))
        assert calls == ["stop", ("mods", True)]
        win._after_mod_download(s, False)
        assert s.mod_versions == {}
        win._on_mod_watch_result(s, DetailsResult(items={"111": _item("111", 9000)}, ok=True))
        assert calls == ["stop", ("mods", True)]  # backed off
    finally:
        win.close()


def test_a_server_that_wont_stop_keeps_running_on_the_old_mods(tmp_path, monkeypatch):
    import process_manager
    win, s, said, clock, calls = _mod_window(tmp_path, monkeypatch)
    monkeypatch.setattr(process_manager, "graceful_stop", lambda srv: False)
    try:
        win._online_by_server[s.id] = set()
        win._on_mod_watch_result(s, DetailsResult(items={"111": _item("111", 9000)}, ok=True))
        assert calls == [] and win._known_running[s.id] is True
        assert s.id not in win._mod_update_restarting
    finally:
        win.close()


def test_scheduler_asks_hourly_only_with_mods_and_the_switch_on(monkeypatch):
    import sys
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication(sys.argv)
    from scheduler import Scheduler
    s = models.ServerConfig(name="x", steamcmd_dir="C:/steamcmd")
    s.mods = [{"id": "111", "enabled": True}]
    sch = Scheduler(get_servers=lambda: [s], is_online=lambda srv: False, on_state_changed=lambda: None)
    fired = []
    sch.mod_watch_due.connect(fired.append)
    t = datetime(2026, 11, 10, 12, 0)
    sch._check_mod_watch(s, t)
    sch._check_mod_watch(s, t + timedelta(minutes=30))
    sch._check_mod_watch(s, t + timedelta(minutes=61))
    assert len(fired) == 2
    s.mod_auto_update = False
    sch._check_mod_watch(s, t + timedelta(hours=5))
    assert len(fired) == 2


# ---------------------------------------------------------- weekly report --

def test_weekly_report_is_due_once_a_week_after_its_slot():
    monday_9 = datetime(2026, 11, 9, 9, 0)  # a Monday
    assert not weekly_report.is_due(monday_9 - timedelta(minutes=1), True, 0, 9, "")
    assert weekly_report.is_due(monday_9, True, 0, 9, "")
    assert weekly_report.is_due(monday_9 + timedelta(days=2), True, 0, 9, "2026-W45")  # PC was off
    assert not weekly_report.is_due(monday_9 + timedelta(days=2), True, 0, 9, weekly_report.week_key(monday_9))
    assert not weekly_report.is_due(monday_9, False, 0, 9, "")


def test_weekly_report_text_covers_the_week(tmp_path):
    sid = "rep1"
    now = datetime(2026, 11, 9, 9, 0)
    t0 = (now - timedelta(days=1)).timestamp()
    for i in range(12):
        history.record_sample(sid, up=i != 5, players=3 if i < 6 else 1, cpu=20.0, mem_mb=8192,
                              now=t0 + i * 300, force=True)
    history.record_event(sid, "crash", now=t0 + 1500)
    history.record_event(sid, "scheduled_restart", now=t0 + 1600)
    summary = history.summary(sid, (now - timedelta(days=7)).timestamp(), now.timestamp())
    from session_tracker import Session
    sessions = [Session("A", (now - timedelta(hours=3)).isoformat(), (now - timedelta(hours=1)).isoformat()),
                Session("B", (now - timedelta(days=9)).isoformat(), (now - timedelta(days=8)).isoformat())]
    players = weekly_report.players_in(sessions, now - timedelta(days=7), now)
    text = weekly_report.report_text(summary, players, 4, now - timedelta(hours=2))
    assert "Uptime: 91.7%" in text and "1 crash" in text and "Restarts: 1" in text
    assert "1 different player, 2 hours played, most at once 3" in text
    assert "Memory: up to 8.0 GB, average CPU 20%" in text and "4 made this week" in text


def test_weekly_report_sends_once_for_every_server(tmp_path, monkeypatch):
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    try:
        a = cfg.add_server("A")
        a.install_dir = _server_with_world(tmp_path, "A")
        cfg.alert_ntfy_url = "https://ntfy.sh/x"
        sent = []
        monkeypatch.setattr(win, "_notify", lambda srv, text, title="": sent.append((srv.name, title, text)))
        monday = datetime(2026, 11, 9, 9, 5)
        win._reports_tick(monday)
        win._reports_tick(monday + timedelta(hours=1))
        assert [(n, t) for n, t, _x in sent] == [("A", "Weekly Report")]
        assert cfg.last_weekly_report == "2026-W46"
        assert "Uptime" in sent[0][2]
    finally:
        win.close()


def test_send_now_explains_when_there_is_nowhere_to_send(monkeypatch):
    cfg = models.AppConfig()
    win = _window(monkeypatch, cfg)
    try:
        assert "Discord or ntfy" in win.send_weekly_report_now()
    finally:
        win.close()


# -------------------------------------------------------- scheduled events --

def _weekend_xp(**extra):
    raw = {"name": "Double XP", "kind": "settings", "start_day": 4, "start": "18:00", "end_day": 6,
           "end": "23:00", "changes": {"PlayerXPRateMultiplier": "2"}}
    raw.update(extra)
    return raw


def test_event_checks():
    ev = se.clean_event(_weekend_xp())
    assert ev["changes"] == {"PlayerXPRateMultiplier": 2.0} and ev["id"]
    for bad, why in ((_weekend_xp(name=""), "name"), (_weekend_xp(start="25:00"), "time like"),
                     (_weekend_xp(changes={"AdminPassword": "x"}), "can't be changed"),
                     (_weekend_xp(changes={}), "at least one"),
                     (_weekend_xp(end_day=4, end="18:00"), "same moment"),
                     (_weekend_xp(changes={"PlayerXPRateMultiplier": "lots"}), "enter a number")):
        with pytest.raises(ValueError, match=why):
            se.clean_event(bad)
    msg = se.clean_event({"name": "Discord", "kind": "message", "days": [5, 6], "time": "12:00",
                          "repeat_minutes": 120, "text": " Join   us! "})
    assert msg["text"] == "Join us!" and se.describe_when(msg) == "Weekends, every 2 hours from 12:00"
    with pytest.raises(ValueError, match="repeats"):
        se.clean_event({"name": "x", "kind": "message", "days": [1], "time": "12:00", "repeat_minutes": 7,
                        "text": "x"})


def test_event_windows_including_ones_that_wrap():
    ev = se.clean_event(_weekend_xp())
    fri = datetime(2026, 11, 13)  # a Friday
    assert not se.window_on(ev, fri.replace(hour=17, minute=59))
    assert se.window_on(ev, fri.replace(hour=18))
    assert se.window_on(ev, fri + timedelta(days=2, hours=22, minutes=59))
    assert not se.window_on(ev, fri + timedelta(days=2, hours=23))
    wrap = se.clean_event(_weekend_xp(start_day=6, start="20:00", end_day=0, end="02:00"))
    assert se.window_on(wrap, fri + timedelta(days=3, hours=1))  # Monday 01:00
    nightly = se.clean_event(_weekend_xp(daily=True, start="22:00", end="02:00"))
    assert se.window_on(nightly, fri.replace(hour=23)) and se.window_on(nightly, fri.replace(hour=1))
    assert not se.window_on(nightly, fri.replace(hour=12))


def test_messages_come_round_once_each_time():
    ev = se.clean_event({"name": "x", "kind": "message", "days": list(range(7)), "time": "12:00",
                         "repeat_minutes": 60, "text": "hi"})
    noon = datetime(2026, 11, 10, 12, 3)
    assert se.message_due(ev, noon) == "2026-11-10 12:00"
    ev["state"]["last_sent"] = "2026-11-10 12:00"
    assert se.message_due(ev, noon) == ""
    assert se.message_due(ev, noon.replace(hour=12, minute=30)) == ""
    assert se.message_due(ev, noon.replace(hour=13, minute=1)) == "2026-11-10 13:00"
    assert se.message_due(ev, noon.replace(hour=11)) == ""


def test_ending_an_event_keeps_a_change_made_by_hand():
    ev = se.clean_event(_weekend_xp(changes={"PlayerXPRateMultiplier": 2, "HarvestAmountMultiplier": 3}))
    gameplay = {"PlayerXPRateMultiplier": 1.0, "HarvestAmountMultiplier": 1.0}
    wanted, originals = se.plan_start(ev, gameplay)
    gameplay.update(wanted)
    gameplay["HarvestAmountMultiplier"] = 5.0  # changed by hand during the event
    assert se.plan_end({"applied": wanted, "originals": originals}, gameplay) == {"PlayerXPRateMultiplier": 1.0}


def _event_window(tmp_path, monkeypatch):
    win, s, said, clock = _countdown_window(tmp_path, monkeypatch)
    restarts = []
    monkeypatch.setattr(win, "_do_restart", lambda srv, on_error=None: restarts.append(srv.id))
    import process_manager
    monkeypatch.setattr(process_manager, "is_running", lambda d: True)
    return win, s, said, clock, restarts


def _ini_value(server, key):
    path = os.path.join(server.install_dir, "ConanSandbox", "Saved", "Config", "WindowsServer",
                        "ServerSettings.ini")
    for line in open(path, encoding="utf-8"):
        if line.split("=", 1)[0].strip().lower() == key.lower():
            return line.split("=", 1)[1].strip()
    return None


def test_settings_event_changes_restarts_and_puts_back(tmp_path, monkeypatch):
    win, s, said, clock, restarts = _event_window(tmp_path, monkeypatch)
    try:
        s.gameplay["PlayerXPRateMultiplier"] = 1.0
        ev = win.save_scheduled_event(s, _weekend_xp(motd="Double XP all weekend!"))
        fri = datetime(2026, 11, 13, 18, 0)
        win._handle_events_due(s, fri)
        assert s.gameplay["PlayerXPRateMultiplier"] == 2.0 and _ini_value(s, "PlayerXPRateMultiplier") == "2.0"
        assert s.gameplay[se.MOTD_KEY] == "Double XP all weekend!"
        assert "restart for Double XP in 15 minutes" in said[-1] and not restarts
        win._online_by_server[s.id] = set()
        win._tick_countdowns()
        assert restarts == [s.id]
        win._handle_events_due(s, fri + timedelta(hours=1))
        assert restarts == [s.id]  # nothing new while it's on
        win._handle_events_due(s, fri + timedelta(days=2, hours=5))  # Sunday 23:00
        assert s.gameplay["PlayerXPRateMultiplier"] == 1.0 and s.gameplay[se.MOTD_KEY] == ""
        assert restarts == [s.id, s.id]
        assert s.scheduled_events[0]["id"] == ev["id"] and not s.scheduled_events[0]["state"]["active"]
        kinds = [e.detail for e in history.events(s.id, kinds=["event"])]
        assert any("Double XP started" in d for d in kinds) and any("Double XP ended" in d for d in kinds)
    finally:
        win.close()


def test_a_stopped_server_just_gets_the_new_settings(tmp_path, monkeypatch):
    win, s, said, clock, restarts = _event_window(tmp_path, monkeypatch)
    try:
        win._known_running[s.id] = False
        win.save_scheduled_event(s, _weekend_xp())
        win._handle_events_due(s, datetime(2026, 11, 13, 19, 0))
        assert s.gameplay["PlayerXPRateMultiplier"] == 2.0 and not restarts and not said
        assert not s.scheduled_events[0]["state"]["needs_restart"]
    finally:
        win.close()


def test_deleting_an_event_thats_on_puts_the_settings_back(tmp_path, monkeypatch):
    win, s, said, clock, restarts = _event_window(tmp_path, monkeypatch)
    try:
        win._online_by_server[s.id] = set()
        s.gameplay["PlayerXPRateMultiplier"] = 1.0
        ev = win.save_scheduled_event(s, _weekend_xp())
        win._handle_events_due(s, datetime(2026, 11, 13, 19, 0))
        assert s.gameplay["PlayerXPRateMultiplier"] == 2.0 and restarts == [s.id]
        assert win.delete_scheduled_event(s, ev["id"])
        assert s.gameplay["PlayerXPRateMultiplier"] == 1.0 and restarts == [s.id, s.id]
        assert s.scheduled_events == []
    finally:
        win.close()


def test_scheduled_message_is_posted_once(tmp_path, monkeypatch):
    win, s, said, clock, restarts = _event_window(tmp_path, monkeypatch)
    try:
        s.rcon_enabled = True
        win.save_scheduled_event(s, {"name": "Rules", "kind": "message", "days": list(range(7)),
                                     "time": "20:00", "text": "Be nice."})
        t = datetime(2026, 11, 10, 20, 1)
        win._handle_events_due(s, t)
        win._handle_events_due(s, t + timedelta(minutes=1))
        assert said.count("Be nice.") == 1
        assert [e.detail for e in history.events(s.id, kinds=["announcement"])] == ["Be nice."]
    finally:
        win.close()


def test_a_damaged_saved_event_is_dropped_unless_its_on():
    ok = se.clean_event(_weekend_xp())
    on = dict(_weekend_xp(start="nope"), id="x", state={"active": True, "applied": {}, "originals": {}})
    out = se.load_events([ok, {"name": "bad"}, on, "junk"])
    assert [e["name"] for e in out] == ["Double XP", "Double XP"] and out[1]["enabled"] is False
