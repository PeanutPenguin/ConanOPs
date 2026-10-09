"""Missing game files get repaired by Steam; a missing world save is caught
before the server quietly starts a fresh one."""
import os
import types

import backup_manager
import models
import preflight
import process_manager


def _install(tmp_path, exe=True, world=True):
    root = tmp_path / "srv"
    saved = root / "ConanSandbox" / "Saved"
    saved.mkdir(parents=True)
    if world:
        (saved / "game.db").write_bytes(b"x")
    s = models.ServerConfig(name="t", install_dir=str(root), steamcmd_dir="", bind_ip="")
    s.backup_destination = str(tmp_path / "backups")
    os.makedirs(s.backup_destination)
    exe_path = process_manager.server_exe_path(str(root))
    if exe:
        os.makedirs(os.path.dirname(exe_path), exist_ok=True)
        open(exe_path, "w").close()
    return s


def test_missing_exe_is_flagged_for_repair(tmp_path):
    r = preflight.run_preflight(_install(tmp_path, exe=False))
    assert r.files_missing and not r.ok


def test_missing_world_with_backups_blocks_start(tmp_path):
    s = _install(tmp_path, world=False)
    assert not preflight.run_preflight(s).world_missing  # no backups yet: a first start makes the world
    open(os.path.join(s.backup_destination, "20261001-120000_scheduled.zip"), "w").close()
    r = preflight.run_preflight(s)
    assert r.world_missing and not r.ok and any("brand-new" in p for p in r.problems)
    s._allow_new_world = True
    assert not preflight.run_preflight(s).world_missing


def test_world_present_is_fine(tmp_path):
    s = _install(tmp_path)
    open(os.path.join(s.backup_destination, "20261001-120000_scheduled.zip"), "w").close()
    assert not preflight.run_preflight(s).world_missing


def test_watchdog_repairs_files_on_the_third_try(monkeypatch):
    from ui.window.power import PowerMixin
    calls = []
    w = types.SimpleNamespace(
        _watchdog_last_attempt={}, _watchdog_attempts={"a": 2}, _files_repaired=set(),
        _WATCHDOG_STREAK_RESET_SECONDS=3600, _WATCHDOG_MAX_ATTEMPTS=5, _WATCHDOG_BACKOFF_SECONDS=[0] * 5,
        _expected_stop=set(), _notify=lambda *a, **k: None,
        _repair_game_files=lambda srv, **k: calls.append(k) or True,
        _run_op=lambda *a, **k: calls.append("plain restart"))
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True))
    server = types.SimpleNamespace(id="a", mods=[])
    PowerMixin._attempt_watchdog_restart(w, server, reason="it crashed", need_stop_first=False)
    assert calls and calls[0]["then_start"] is True
    # Once per crash streak: the next try is a plain restart again.
    w._files_repaired.add("a")
    w._watchdog_last_attempt = {}
    PowerMixin._attempt_watchdog_restart(w, server, reason="it crashed", need_stop_first=False)
    assert calls[-1] == "plain restart"
