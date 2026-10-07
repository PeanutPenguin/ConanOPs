from __future__ import annotations

import os
import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

import auto_bisect_runner
import mod_manager
import models
import network_utils
import preflight
import process_manager
from auto_bisect_runner import AutoBisectWorker


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _install_fake_clock(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(auto_bisect_runner.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(auto_bisect_runner.time, "sleep", clock.sleep)
    return clock


def _mods(ids):
    return [{"id": i, "name": i, "enabled": True} for i in ids]


def _run_synchronously(worker: AutoBisectWorker):
    captured = {}
    worker.finished_bisect.connect(lambda outcome: captured.setdefault("outcome", outcome))
    worker.run()
    return captured["outcome"]


def _server():
    return models.ServerConfig(
        id="s1", name="Chudville", install_dir="/tmp/fake", steamcmd_dir="/tmp/fake-steamcmd",
        bind_ip="127.0.0.1", query_port=27015,
    )


class _FakeServerProcess:
    """A server process that actually has a running/stopped state:
    launch() starts it, graceful_stop() stops it. The previous mock
    reported is_running() == True forever, which is how the "snapshot
    taken while the server is still up" bug stayed invisible -- a
    process that can never stop can't distinguish "stopped, then
    snapshotted" from "snapshotted while live"."""

    def __init__(self, running: bool = False):
        self.running = running
        self.events = []  # ordered log: "stop", "launch", "snapshot:<running>", ...

    def is_running(self, install_dir):
        return self.running

    def launch(self, srv):
        self.events.append("launch")
        self.running = True

    def graceful_stop(self, srv, timeout=15.0):
        self.events.append("stop")
        self.running = False
        return True


def _install_process(monkeypatch, running: bool = False) -> _FakeServerProcess:
    proc = _FakeServerProcess(running=running)
    monkeypatch.setattr(process_manager, "is_running", proc.is_running)
    monkeypatch.setattr(process_manager, "launch", proc.launch)
    monkeypatch.setattr(process_manager, "graceful_stop", proc.graceful_stop)
    return proc


def _install_common_mocks(monkeypatch, worker=None):
    monkeypatch.setattr(mod_manager, "write_modlist", lambda install_dir, steamcmd_dir, mods: None)
    _install_process(monkeypatch, running=bool(worker and worker.was_running))
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
    # No real .pak files exist on disk in this test environment --
    # treat everything as already downloaded by default so existing
    # tests can focus on the bisection logic itself; tests for the
    # "skip undownloaded mods" behavior specifically override this.
    monkeypatch.setattr(AutoBisectWorker, "_mod_file_exists", lambda self, mod_id: True)
    # No real world save exists either -- skip the snapshot/restore
    # machinery by default so tests don't need a real Saved folder on
    # disk; dedicated tests for that mechanism use a real tmp_path.
    monkeypatch.setattr(AutoBisectWorker, "_snapshot_saved_dir", lambda self: None)
    monkeypatch.setattr(AutoBisectWorker, "_restore_saved_dir", lambda self: True)


def _oracle_broken_if_any_enabled(bad_ids, worker):
    """"Broken" iff at least one of `bad_ids` is currently enabled --
    the natural oracle shape for "some mod(s) are bad": correctly
    reports "fixed" for the empty-set baseline and "still broken" for
    the full/reproduce check and whenever any bad mod is back on."""
    def fake_query(ip, port, timeout=1.5):
        enabled = {m["id"] for m in worker.mods if m.get("enabled", True)}
        return {"name": "ok"} if not (bad_ids & enabled) else None
    return fake_query


# --------------------------------------------------------- up-front checks --

def test_preflight_failure_stops_before_anything_is_touched(monkeypatch):
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b"]), was_running=False)
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=False, problems=["bad port"], repairs=[]))
    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(1))

    outcome = _run_synchronously(worker)

    assert outcome.preflight_problems == ["bad port"]
    assert launched == []


def test_no_steamcmd_dir_reported_distinctly_not_as_all_missing(monkeypatch):
    server = _server()
    server.steamcmd_dir = ""
    worker = AutoBisectWorker(server, _mods(["a", "b"]), was_running=False)
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda s: launched.append(1))

    outcome = _run_synchronously(worker)

    assert outcome.no_steamcmd_dir is True
    assert outcome.skipped_not_downloaded == []  # distinct from the "some downloaded, some not" case
    assert launched == []


def test_could_not_reproduce_when_full_list_already_works(monkeypatch):
    """The other real bug: a healthy server must not get bisected at
    all -- confirmed against real failures where 3, 4, 6, 8, and 12
    genuinely fine mods all got fully convicted before this existed."""
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", lambda ip, port, timeout=1.5: {"name": "ok"})  # always fine

    outcome = _run_synchronously(worker)

    assert outcome.could_not_reproduce is True
    assert outcome.found_culprits == []
    assert worker._round_number == 1  # stopped after the very first test


def test_not_mod_related_when_still_broken_with_everything_disabled(monkeypatch):
    """The original bug report this precondition check exists for: a
    server that's broken for a non-mod reason must NOT have its
    entire mod list blamed."""
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", lambda ip, port, timeout=1.5: None)  # never comes up, no matter what

    outcome = _run_synchronously(worker)

    assert outcome.not_mod_related is True
    assert outcome.could_not_reproduce is False
    assert outcome.found_culprits == []
    assert all(m["enabled"] for m in outcome.mods)  # restored, nothing blamed


def test_proceeds_past_both_checks_when_genuinely_mod_related(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", _oracle_broken_if_any_enabled({"a"}, worker))

    outcome = _run_synchronously(worker)

    assert outcome.could_not_reproduce is False
    assert outcome.not_mod_related is False
    assert outcome.found_culprits == ["a"]


# --------------------------------------------------------- quick mod check --

def test_quick_check_finds_a_single_culprit(monkeypatch):
    _install_fake_clock(monkeypatch)
    culprit = "c"
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c", "d"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", _oracle_broken_if_any_enabled({culprit}, worker))

    outcome = _run_synchronously(worker)

    assert outcome.found_culprits == [culprit]
    assert outcome.cancelled is False
    assert outcome.error == ""
    disabled_final = {m["id"] for m in outcome.mods if not m["enabled"]}
    assert disabled_final == {culprit}


def test_quick_check_finds_multiple_independent_culprits(monkeypatch):
    _install_fake_clock(monkeypatch)
    bad = {"b", "e"}
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c", "d", "e", "f"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", _oracle_broken_if_any_enabled(bad, worker))

    outcome = _run_synchronously(worker)

    assert set(outcome.found_culprits) == bad
    disabled_final = {m["id"] for m in outcome.mods if not m["enabled"]}
    assert disabled_final == bad


def test_quick_check_reports_unresolved_when_only_a_combo_exists(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)

    def fake_query(ip, port, timeout=1.5):
        enabled = {m["id"] for m in worker.mods if m.get("enabled", True)}
        return None if {"a", "b"}.issubset(enabled) else {"name": "ok"}
    monkeypatch.setattr(network_utils, "query_a2s_info", fake_query)

    outcome = _run_synchronously(worker)

    assert outcome.found_culprits == []
    assert set(outcome.unresolved_suspects) == {"a", "b", "c"}


def test_quick_check_relaunches_only_if_was_running(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", _oracle_broken_if_any_enabled({"a"}, worker))
    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda srv: launched.append(1))

    _run_synchronously(worker)

    # One launch per actual test round -- was_running=False means
    # settle() must NOT add one more launch on top of those.
    assert len(launched) == worker._round_number


def test_quick_check_relaunches_when_was_running(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b"]), was_running=True)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", _oracle_broken_if_any_enabled({"a"}, worker))
    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda srv: launched.append(1))

    _run_synchronously(worker)

    assert len(launched) >= 1


def test_quick_check_cancel_mid_run_restores_and_reports_found_so_far(monkeypatch):
    _install_fake_clock(monkeypatch)
    bad = {"b", "e"}
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c", "d", "e", "f"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)

    call_count = {"n": 0}

    def fake_query(ip, port, timeout=1.5):
        call_count["n"] += 1
        if call_count["n"] >= 4:
            worker.cancel()
        enabled = {m["id"] for m in worker.mods if m.get("enabled", True)}
        return {"name": "ok"} if not (bad & enabled) else None
    monkeypatch.setattr(network_utils, "query_a2s_info", fake_query)

    outcome = _run_synchronously(worker)

    assert outcome.cancelled is True
    assert all(m["enabled"] for m in outcome.mods)  # restored


def test_quick_check_stops_immediately_if_a_player_joins(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c"]), was_running=False, is_online=lambda: True)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", lambda ip, port, timeout=1.5: {"name": "ok"})

    outcome = _run_synchronously(worker)

    assert outcome.player_joined is True
    assert worker._round_number == 0  # never even got to the first test


def test_quick_check_launch_failure_counts_as_still_broken(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(process_manager, "launch", lambda srv: (_ for _ in ()).throw(FileNotFoundError("no exe")))

    outcome = _run_synchronously(worker)

    # Reproduce check also fails to launch -- reads as could_not_reproduce=False,
    # not_mod_related=True (removing everything didn't help either, since it can't even launch).
    assert outcome.not_mod_related is True


def test_quick_check_round_cap_never_convicts_the_interrupted_mod(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    ids = [f"m{i}" for i in range(10)]
    worker = AutoBisectWorker(server, _mods(ids), was_running=False)
    worker.MAX_ROUNDS = 4  # reproduce + baseline + a couple individual tests before the cap bites
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", _oracle_broken_if_any_enabled({"m9"}, worker))  # never isolated in time

    outcome = _run_synchronously(worker)

    assert outcome.cancelled is False
    assert outcome.error == ""
    assert all(m["enabled"] for m in outcome.mods)  # restored rather than left half-tested
    assert "m9" not in outcome.found_culprits  # never confirmed -- must not be reported as found


def test_progress_signal_emitted_for_each_round(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", lambda ip, port, timeout=1.5: {"name": "ok"})  # always fine, incl. reproduce check

    progress_events = []
    worker.progress.connect(lambda p: progress_events.append(p))

    _run_synchronously(worker)

    assert len(progress_events) >= 1
    assert progress_events[0].phase == "reproduce_check"
    assert progress_events[0].round_number == 1


# -------------------------------------------------------------- find_all --

def test_find_all_finds_two_independent_culprits(monkeypatch):
    _install_fake_clock(monkeypatch)
    bad = {"b", "e"}
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c", "d", "e", "f"]), was_running=False, find_all=True)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", _oracle_broken_if_any_enabled(bad, worker))

    outcome = _run_synchronously(worker)

    assert set(outcome.found_culprits) == bad
    assert outcome.error == ""
    assert outcome.cancelled is False
    disabled_final = {m["id"] for m in outcome.mods if not m["enabled"]}
    assert disabled_final == bad


def test_find_all_finds_a_combo_only_culprit(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c", "d"]), was_running=False, find_all=True)
    _install_common_mocks(monkeypatch, worker)

    def fake_query(ip, port, timeout=1.5):
        enabled = {m["id"] for m in worker.mods if m.get("enabled", True)}
        return None if {"a", "c"}.issubset(enabled) else {"name": "ok"}
    monkeypatch.setattr(network_utils, "query_a2s_info", fake_query)

    outcome = _run_synchronously(worker)

    assert set(outcome.found_culprits) == {"a", "c"}


def test_find_all_with_only_one_bad_mod_still_finds_just_that_one(monkeypatch):
    _install_fake_clock(monkeypatch)
    culprit = "c"
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c", "d"]), was_running=False, find_all=True)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", _oracle_broken_if_any_enabled({culprit}, worker))

    outcome = _run_synchronously(worker)

    assert outcome.found_culprits == [culprit]  # sanity round confirms nothing else is wrong


def test_find_all_with_every_mod_bad(monkeypatch):
    """Every single mod is independently bad -- exercises the "no
    candidates left" exit path rather than the sanity-round path."""
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b"]), was_running=False, find_all=True)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", _oracle_broken_if_any_enabled({"a", "b"}, worker))

    outcome = _run_synchronously(worker)

    assert set(outcome.found_culprits) == {"a", "b"}


def test_find_all_respects_cancel_mid_run(monkeypatch):
    _install_fake_clock(monkeypatch)
    bad = {"b", "e"}
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c", "d", "e", "f"]), was_running=False, find_all=True)
    _install_common_mocks(monkeypatch, worker)

    call_count = {"n": 0}

    def fake_query(ip, port, timeout=1.5):
        call_count["n"] += 1
        if call_count["n"] >= 4:
            worker.cancel()
        enabled = {m["id"] for m in worker.mods if m.get("enabled", True)}
        return {"name": "ok"} if not (bad & enabled) else None
    monkeypatch.setattr(network_utils, "query_a2s_info", fake_query)

    outcome = _run_synchronously(worker)

    assert outcome.cancelled is True


def test_find_all_stops_immediately_if_a_player_joins_mid_ddmin(monkeypatch):
    _install_fake_clock(monkeypatch)
    bad = {"b", "e"}
    server = _server()
    online = {"v": False}
    worker = AutoBisectWorker(server, _mods(["a", "b", "c", "d", "e", "f"]), was_running=False,
                               find_all=True, is_online=lambda: online["v"])
    _install_common_mocks(monkeypatch, worker)

    call_count = {"n": 0}

    def fake_query(ip, port, timeout=1.5):
        call_count["n"] += 1
        if call_count["n"] >= 4:
            online["v"] = True
        enabled = {m["id"] for m in worker.mods if m.get("enabled", True)}
        return {"name": "ok"} if not (bad & enabled) else None
    monkeypatch.setattr(network_utils, "query_a2s_info", fake_query)

    outcome = _run_synchronously(worker)

    assert outcome.player_joined is True
    assert all(m["enabled"] for m in outcome.mods)


def test_find_all_stops_at_round_cap_and_reports_unresolved(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    ids = [f"m{i}" for i in range(10)]
    worker = AutoBisectWorker(server, _mods(ids), was_running=False, find_all=True)
    worker.MAX_ROUNDS = 4
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(network_utils, "query_a2s_info", _oracle_broken_if_any_enabled({"m9"}, worker))

    outcome = _run_synchronously(worker)

    assert outcome.cancelled is False
    assert outcome.error == ""
    assert all(m["enabled"] for m in outcome.mods)
    assert outcome.found_culprits == []


# ------------------------------------------------------- skipped mods --

def test_missing_mod_files_are_skipped_not_tested(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)
    downloaded = {"a", "c"}
    monkeypatch.setattr(AutoBisectWorker, "_mod_file_exists", lambda self, mod_id: mod_id in downloaded)
    tested_mods = []

    def fake_query(ip, port, timeout=1.5):
        enabled = {m["id"] for m in worker.mods if m.get("enabled", True)}
        tested_mods.append(frozenset(enabled))
        return {"name": "ok"}  # everything fine
    monkeypatch.setattr(network_utils, "query_a2s_info", fake_query)

    outcome = _run_synchronously(worker)

    assert outcome.skipped_not_downloaded == ["b"]
    assert all("b" not in s for s in tested_mods)


def test_missing_mod_restored_to_original_state_not_stuck_disabled(monkeypatch):
    """A mod forced off for being undownloaded must end up back at
    whatever it actually started as once a culprit IS found and
    confirmed -- not permanently disabled just because this run had
    to work around it."""
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b", "c"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)
    downloaded = {"a", "b"}
    monkeypatch.setattr(AutoBisectWorker, "_mod_file_exists", lambda self, mod_id: mod_id in downloaded)
    monkeypatch.setattr(network_utils, "query_a2s_info", _oracle_broken_if_any_enabled({"a"}, worker))

    outcome = _run_synchronously(worker)

    by_id = {m["id"]: m["enabled"] for m in outcome.mods}
    assert by_id["a"] is False  # the confirmed culprit
    assert by_id["b"] is True   # innocent, back on
    assert by_id["c"] is True   # was never downloaded, but restored to its ORIGINAL (enabled) state


def test_all_missing_reports_immediately_without_any_restart(monkeypatch):
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b"]), was_running=False)
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
    monkeypatch.setattr(AutoBisectWorker, "_mod_file_exists", lambda self, mod_id: False)
    launched = []
    monkeypatch.setattr(process_manager, "launch", lambda srv: launched.append(1))

    outcome = _run_synchronously(worker)

    assert set(outcome.skipped_not_downloaded) == {"a", "b"}
    assert launched == []


# ------------------------------------------------------- world save safety --

def test_snapshot_taken_and_restored_around_a_real_run(monkeypatch, tmp_path):
    """End-to-end with a REAL temp Saved folder (not mocked away):
    confirms a file present before the run is still there, unmodified,
    after it -- the actual guarantee this whole mechanism exists for."""
    _install_fake_clock(monkeypatch)
    install_dir = tmp_path / "install"
    saved_dir = install_dir / "ConanSandbox" / "Saved"
    saved_dir.mkdir(parents=True)
    (saved_dir / "game.db").write_text("original world data")

    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(install_dir), steamcmd_dir=str(tmp_path / "sc"), bind_ip="127.0.0.1", query_port=27015)
    worker = AutoBisectWorker(server, _mods(["a"]), was_running=False)
    monkeypatch.setattr(mod_manager, "write_modlist", lambda install_dir, steamcmd_dir, mods: None)
    proc = _install_process(monkeypatch, running=False)
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
    monkeypatch.setattr(AutoBisectWorker, "_mod_file_exists", lambda self, mod_id: True)

    def fake_launch(srv):
        # Simulate the game engine deleting/changing the save the
        # moment it loads without mod "a" present.
        proc.launch(srv)
        (saved_dir / "game.db").write_text("mutated by a test run")
    monkeypatch.setattr(process_manager, "launch", fake_launch)
    monkeypatch.setattr(network_utils, "query_a2s_info", lambda ip, port, timeout=1.5: {"name": "ok"})

    outcome = _run_synchronously(worker)

    assert outcome.error == ""  # a real run, not an early bail-out that happens to leave the file alone
    assert "launch" in proc.events  # the save really was mutated at least once
    assert (saved_dir / "game.db").read_text() == "original world data"


def test_snapshot_only_taken_after_server_confirmed_stopped(monkeypatch, tmp_path):
    """Copying a live database mid-write can capture a torn copy --
    the snapshot must only be taken once the server is confirmed
    fully stopped. The previous version of this test used
    was_running=False with a server that was never running at all,
    so it couldn't fail; this one starts with the server UP."""
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b"]), was_running=True)
    _install_common_mocks(monkeypatch, worker)
    proc = _install_process(monkeypatch, running=True)

    def tracking_snapshot(self):
        proc.events.append(f"snapshot:running={proc.running}")
        return None
    monkeypatch.setattr(AutoBisectWorker, "_snapshot_saved_dir", tracking_snapshot)
    monkeypatch.setattr(network_utils, "query_a2s_info", _oracle_broken_if_any_enabled({"a"}, worker))

    _run_synchronously(worker)

    snapshot_events = [e for e in proc.events if e.startswith("snapshot:")]
    assert snapshot_events == ["snapshot:running=False"]
    first_snapshot = proc.events.index("snapshot:running=False")
    assert "stop" in proc.events[:first_snapshot]  # stopped BEFORE the copy, not after


def test_run_stops_without_testing_if_server_wont_stop_for_the_snapshot(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a", "b"]), was_running=True)
    _install_common_mocks(monkeypatch, worker)
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: True)  # refuses to die
    snapshots = []
    monkeypatch.setattr(AutoBisectWorker, "_snapshot_saved_dir", lambda self: snapshots.append(1))

    outcome = _run_synchronously(worker)

    assert snapshots == []
    assert outcome.error
    assert outcome.found_culprits == []
    assert all(m["enabled"] for m in outcome.mods)

def test_restore_saved_dir_is_a_noop_without_a_snapshot(tmp_path):
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(tmp_path))
    worker = AutoBisectWorker(server, _mods(["a"]), was_running=False)
    worker._saved_snapshot_dir = None

    worker._restore_saved_dir()  # must not raise


def test_snapshot_only_covers_world_save_files_never_config(tmp_path):
    """Config/ (bind IP, RCON, network ports) must never be touched --
    an earlier version of this DID revert Config/ every round, which
    could make the server fail to start for a reason unrelated to
    any mod."""
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(tmp_path / "install"))
    worker = AutoBisectWorker(server, _mods(["a"]), was_running=False)
    saved_dir = tmp_path / "install" / "ConanSandbox" / "Saved"
    config_dir = saved_dir / "Config" / "WindowsServer"
    config_dir.mkdir(parents=True)
    (saved_dir / "game.db").write_text("original world data")
    (config_dir / "Engine.ini").write_text("bind_ip=192.168.1.50")

    worker._saved_snapshot_dir = worker._snapshot_saved_dir()
    assert worker._saved_snapshot_dir is not None

    (saved_dir / "game.db").write_text("mutated world data")
    (config_dir / "Engine.ini").write_text("bind_ip=192.168.1.99")

    worker._restore_saved_dir()

    assert (saved_dir / "game.db").read_text() == "original world data"
    assert (config_dir / "Engine.ini").read_text() == "bind_ip=192.168.1.99"


def test_snapshot_none_when_saved_dir_has_no_world_save_files_yet(tmp_path):
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(tmp_path / "install"))
    worker = AutoBisectWorker(server, _mods(["a"]), was_running=False)
    saved_dir = tmp_path / "install" / "ConanSandbox" / "Saved" / "Config"
    saved_dir.mkdir(parents=True)

    assert worker._snapshot_saved_dir() is None


def test_cleanup_snapshot_removes_the_temp_dir(tmp_path):
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(tmp_path))
    worker = AutoBisectWorker(server, _mods(["a"]), was_running=False)
    saved_dir = tmp_path / "ConanSandbox" / "Saved"
    saved_dir.mkdir(parents=True)
    (saved_dir / "game.db").write_text("world data")
    snapshot_dir = worker._snapshot_saved_dir()
    assert snapshot_dir is not None
    worker._saved_snapshot_dir = snapshot_dir

    worker._cleanup_snapshot()

    assert not os.path.exists(snapshot_dir)
    assert worker._saved_snapshot_dir is None


# ---------------------------------------------------- stay-up confirmation --

def test_a_mod_that_crashes_shortly_after_answering_once_is_not_treated_as_healthy(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)

    call_count = {"n": 0}

    def fake_is_running(install_dir):
        call_count["n"] += 1
        return call_count["n"] < 4
    monkeypatch.setattr(process_manager, "is_running", fake_is_running)
    monkeypatch.setattr(network_utils, "query_a2s_info", lambda ip, port, timeout=1.5: {"name": "ok"})

    outcome = _run_synchronously(worker)

    assert outcome.not_mod_related is True


def test_a_mod_that_hangs_after_answering_once_is_not_treated_as_healthy(monkeypatch):
    """Same failure mode, but the process stays technically "running"
    while no longer answering queries at all -- the confirmation
    window must re-query, not just check the process object exists."""
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a"]), was_running=False)
    _install_common_mocks(monkeypatch, worker)  # the fake process never dies on its own once launched

    call_count = {"n": 0}

    def fake_query(ip, port, timeout=1.5):
        call_count["n"] += 1
        return {"name": "ok"} if call_count["n"] == 1 else None  # answers once, then hangs
    monkeypatch.setattr(network_utils, "query_a2s_info", fake_query)

    outcome = _run_synchronously(worker)

    assert outcome.not_mod_related is True  # every test (incl. baseline) hangs the same way


def test_confirm_stays_up_returns_true_when_it_genuinely_stays_up(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a"]), was_running=False)
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: True)
    monkeypatch.setattr(network_utils, "query_a2s_info", lambda ip, port, timeout=2.0: {"name": "ok"})

    assert worker._confirm_stays_up() is True


def test_confirm_stays_up_returns_false_if_cancelled(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a"]), was_running=False)
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: True)
    worker._cancel_requested = True

    assert worker._confirm_stays_up() is False


# -------------------------------------------------------- process exit wait --

def test_wait_for_process_exit_returns_once_is_running_goes_false(monkeypatch):
    _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a"]), was_running=False)
    call_count = {"n": 0}

    def fake_is_running(install_dir):
        call_count["n"] += 1
        return call_count["n"] < 3
    monkeypatch.setattr(process_manager, "is_running", fake_is_running)

    worker._wait_for_process_exit()

    assert call_count["n"] == 3


def test_wait_for_process_exit_gives_up_after_its_timeout(monkeypatch):
    clock = _install_fake_clock(monkeypatch)
    server = _server()
    worker = AutoBisectWorker(server, _mods(["a"]), was_running=False)
    monkeypatch.setattr(process_manager, "is_running", lambda install_dir: True)  # never exits

    worker._wait_for_process_exit()

    assert clock.t >= auto_bisect_runner.PROCESS_EXIT_TIMEOUT_SECONDS


# --------------------------------------------------------------- _final_mods --

def test_final_mods_disables_culprits_and_restores_everything_else_from_original():
    server = _server()
    original = [
        {"id": "a", "name": "a", "enabled": True},
        {"id": "b", "name": "b", "enabled": True},
        {"id": "c", "name": "c", "enabled": False},  # was already off before this run
    ]
    worker = AutoBisectWorker(server, original, was_running=False)

    result = worker._final_mods(found_culprits=["a"])

    by_id = {m["id"]: m["enabled"] for m in result}
    assert by_id["a"] is False
    assert by_id["b"] is True
    assert by_id["c"] is False  # untouched -- was never a candidate, stays as it originally was
