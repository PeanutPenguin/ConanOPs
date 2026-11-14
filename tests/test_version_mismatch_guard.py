"""Safeguards against players being turned away with "Outdated Client"."""
from __future__ import annotations

import os

import ini_utils
import log_monitor
import models
import process_manager
import steamcmd
from tests.test_audit_fixes import _cfg, _quiet
from tests.test_server_finder import _window

UE_LINE = ("[2026.11.14-19.55.01:123][  0]LogNet: NotifyControlMessage: Client connecting with invalid version. "
           "LocalNetworkVersion: 3711402981, RemoteNetworkVersion: 1290553477")


def test_log_line_for_a_turned_away_player_is_recognised():
    m = log_monitor.VERSION_MISMATCH_RE.search(UE_LINE)
    assert m and (m.group("server"), m.group("client")) == ("3711402981", "1290553477")
    assert not log_monitor.VERSION_MISMATCH_RE.search("LogNet: Join succeeded: Oran")


def test_monitor_reports_a_turned_away_player(tmp_path):
    log = tmp_path / "ConanSandbox.log"
    log.write_text(UE_LINE + "\nLogNet: Join succeeded: Oran\n", encoding="utf-8")
    mon = log_monitor.LogMonitor(str(tmp_path))
    seen, joined = [], []
    mon.version_mismatch.connect(lambda ours, theirs: seen.append((ours, theirs)))
    mon.player_joined.connect(joined.append)
    mon._current_log, mon._position = str(log), 0
    mon._read_new_lines()
    assert seen == [("3711402981", "1290553477")] and joined == ["Oran"]


def test_pinned_build_id_is_removed_from_engine_ini(tmp_path):
    s = models.ServerConfig(name="x", install_dir=str(tmp_path))
    path = process_manager.engine_ini_path(s.install_dir)
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write("[OnlineSubsystem]\nbUseBuildIdOverride=True\nBuildIdOverride=123\nServerName=Chud\n")
    removed = process_manager.clear_build_id_override(s)
    text = open(path, encoding="utf-8").read()
    assert len(removed) == 2 and "BuildIdOverride" not in text and "ServerName=Chud" in text
    assert any(f.endswith(".bak") for f in os.listdir(os.path.dirname(path)))
    assert process_manager.clear_build_id_override(s) == []  # nothing left, file not rewritten


def test_ini_without_the_lines_is_left_alone(tmp_path):
    path = tmp_path / "Engine.ini"
    path.write_text("[URL]\nPort=7777\n", encoding="utf-8")
    assert ini_utils.remove_keys_anywhere(str(path), ("BuildIdOverride",)) == []
    assert os.listdir(tmp_path) == ["Engine.ini"]
    assert ini_utils.remove_keys_anywhere(str(tmp_path / "missing.ini"), ("BuildIdOverride",)) == []


def test_build_check_clears_steamcmds_cached_answer(tmp_path, monkeypatch):
    cache = tmp_path / "appcache"
    cache.mkdir()
    (cache / "appinfo.vdf").write_text("old")
    (tmp_path / "steamcmd.exe").write_text("")
    monkeypatch.setattr(steamcmd, "steamcmd_exe_path", lambda d: str(tmp_path / "steamcmd.exe"))

    class Done:
        stdout = '"branches" { "public" { "buildid" "42" } }'
    seen = []
    monkeypatch.setattr(steamcmd.subprocess, "run",
                        lambda *a, **k: seen.append(os.path.exists(cache / "appinfo.vdf")) or Done())
    assert steamcmd.get_latest_buildid(str(tmp_path)) == "42"
    assert seen == [False]


def _mismatch_window(tmp_path, monkeypatch):
    import ui.window.updates as updates
    cfg = _cfg(tmp_path)
    win = _window(monkeypatch, cfg)
    sent = _quiet(win, monkeypatch)
    started = []

    class FakeCheck:
        def __init__(self, steamcmd_dir):
            from types import SimpleNamespace
            self.finished_check = SimpleNamespace(connect=lambda fn: None)

        def start(self):
            started.append(1)
    monkeypatch.setattr(updates, "CheckWorker", FakeCheck)
    monkeypatch.setattr(win, "_retire_worker", lambda w: None)
    return cfg, win, sent, started


def test_turned_away_player_triggers_one_check_not_one_per_attempt(tmp_path, monkeypatch):
    cfg, win, _sent, started = _mismatch_window(tmp_path, monkeypatch)
    s = cfg.servers[0]
    win._on_version_mismatch(s.id, "2", "1")
    win._update_check_workers.pop(s.id)
    win._on_version_mismatch(s.id, "2", "1")
    assert started == [1]


def test_newer_build_found_after_a_turned_away_player_is_installed(tmp_path, monkeypatch):
    cfg, win, sent, _started = _mismatch_window(tmp_path, monkeypatch)
    s = cfg.servers[0]
    applied = []
    monkeypatch.setattr(win, "_on_periodic_update_check", lambda srv, latest: applied.append(latest))
    win._on_version_mismatch_check(s, "2", "10", "11")
    assert applied == ["2"] and sent[0][0] == "Player Turned Away: Game Version"


def test_server_already_newest_says_so_and_changes_nothing(tmp_path, monkeypatch):
    cfg, win, sent, _started = _mismatch_window(tmp_path, monkeypatch)
    s = cfg.servers[0]
    applied = []
    monkeypatch.setattr(win, "_on_periodic_update_check", lambda srv, latest: applied.append(latest))
    win._on_version_mismatch_check(s, s.installed_buildid, "10", "11")
    assert applied == [] and "newest build Steam offers" in sent[0][1]


def test_unreachable_steam_is_checked_again_on_the_next_attempt(tmp_path, monkeypatch):
    cfg, win, sent, started = _mismatch_window(tmp_path, monkeypatch)
    s = cfg.servers[0]
    win._on_version_mismatch(s.id, "10", "11")
    win._on_version_mismatch_check(s, None, "10", "11")
    win._on_version_mismatch(s.id, "10", "11")
    assert started == [1, 1] and "couldn't reach Steam" in sent[0][1]
