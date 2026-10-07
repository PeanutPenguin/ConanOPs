"""
Regression tests for the Mods-section review fixes. Each test here
targets a specific bug that the existing suite couldn't see -- most of
them fail against the pre-fix code, which is the point: they prove the
fix, not just the happy path.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox

import auto_bisect_runner
import mod_manager
import models
import network_utils
import preflight
import process_manager
import steamcmd
import steam_workshop_api as swa
from auto_bisect_runner import AutoBisectWorker, BisectOutcome
from ui.auto_bisect_dialog import AutoBisectDialog
from ui.mods_page import BisectDialog, ModsPage
from update_runner import ModDownloadWorker


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def no_real_dialogs(monkeypatch):
    """Answer every message box instead of blocking on it. Tests that
    care about the answer or the text change `prompts` themselves."""
    prompts = {"question": QMessageBox.Yes, "shown": []}

    def record(kind):
        def _f(*a, **k):
            prompts["shown"].append((kind, a[1] if len(a) > 1 else "", a[2] if len(a) > 2 else ""))
            return prompts["question"] if kind == "question" else None
        return staticmethod(_f)
    for kind in ("question", "information", "warning"):
        monkeypatch.setattr(QMessageBox, kind, record(kind))
    return prompts


def _mods(ids, enabled=True):
    return [{"id": i, "name": f"Mod {i}", "enabled": enabled} for i in ids]


def _pak(steamcmd_dir, wid, filename):
    d = os.path.join(steamcmd_dir, "steamapps", "workshop", "content", str(mod_manager.WORKSHOP_APP_ID), wid)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, filename)
    open(path, "w").close()
    return path


# ============================================================ downloads ==

def test_finished_download_rewrites_modlist_with_the_real_pak_path(tmp_path, monkeypatch):
    """Adding a not-yet-downloaded mod writes a guessed path; the
    download finishing has to replace it with the real one."""
    install, sc = str(tmp_path / "install"), str(tmp_path / "steamcmd")
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=install, steamcmd_dir=sc)
    page = ModsPage()
    page.on_changed = lambda srv: mod_manager.write_modlist(srv.install_dir, srv.steamcmd_dir, srv.mods)
    page.set_server(server)
    monkeypatch.setattr(ModDownloadWorker, "start", lambda self: None)

    page.id_edit.setText("1369802940")
    page._add_mod()
    guessed = open(mod_manager.modlist_path(install)).read()
    assert "1369802940.pak" in guessed  # the (wrong) guess, before download

    page._download_mods()
    real = _pak(sc, "1369802940", "Emberlight.pak")
    page._on_download_finished(server, steamcmd.UpdateResult(True, "ok"))

    assert open(mod_manager.modlist_path(install)).read().strip() == f"*{real}"


def test_finished_download_for_another_server_still_persists_and_reports(monkeypatch, no_real_dialogs):
    page = ModsPage()
    a = models.ServerConfig(id="a", name="Alpha", steamcmd_dir="/nonexistent-sc")
    a.mods = _mods(["1"])
    b = models.ServerConfig(id="b", name="Bravo")
    changed = []
    page.on_changed = lambda srv: changed.append(srv.id)
    monkeypatch.setattr(ModDownloadWorker, "start", lambda self: None)
    page.set_server(a)
    page._download_mods()
    page.set_server(b)

    page._on_download_finished(a, steamcmd.UpdateResult(False, "boom"))

    assert changed == ["a"]
    warnings = [p for p in no_real_dialogs["shown"] if p[0] == "warning"]
    assert warnings and "Alpha" in warnings[-1][2]  # not silently dropped
    assert page.download_btn.isEnabled() and page.download_btn.text() == "Download Mods"


# ================================================= ModsPage list actions ==

def _page_with(ids):
    page = ModsPage()
    server = models.ServerConfig(id="s1", name="Chudville")
    server.mods = _mods(ids)
    page.set_server(server)
    return page, server


def test_move_up_twice_keeps_working_because_selection_survives():
    page, server = _page_with(["1", "2", "3"])
    page.list_widget.setCurrentRow(2)

    page._move(-1)
    page._move(-1)

    assert [m["id"] for m in server.mods] == ["3", "1", "2"]
    assert page._selected_id() == "3"


def test_toggle_twice_keeps_working_and_tolerates_missing_enabled_key():
    page, server = _page_with(["1"])
    server.mods = [{"id": "1", "name": "No flag"}]  # older configs may lack the key
    page._refresh()
    page.list_widget.setCurrentRow(0)

    page._toggle_selected()
    assert server.mods[0]["enabled"] is False
    page._toggle_selected()
    assert server.mods[0]["enabled"] is True


def test_manual_bisect_refuses_when_no_mod_is_enabled(monkeypatch, no_real_dialogs):
    page, server = _page_with(["1", "2"])
    for m in server.mods:
        m["enabled"] = False
    opened = []
    monkeypatch.setattr(BisectDialog, "exec", lambda self: opened.append(1))

    page._open_bisect()

    assert opened == []
    assert any(p[0] == "information" for p in no_real_dialogs["shown"])


# ======================================================= mod_manager ==

def test_report_result_with_no_candidates_does_not_crash():
    state = mod_manager.start_bisect([])
    state = mod_manager.report_result(state, problem_still_happens=False)
    assert state.done and state.culprit is None


def test_ruled_out_bisect_leaves_every_candidate_enabled():
    mods = _mods(["a", "b", "c", "d"])
    state = mod_manager.start_bisect(["a", "b", "c", "d"])
    while not state.done:
        mods = mod_manager.apply_bisect_test(mods, state)
        state = mod_manager.report_result(state, problem_still_happens=True)

    mods = mod_manager.apply_bisect_result(mods, state)

    assert state.culprit is None
    assert all(m["enabled"] for m in mods)


def test_bisect_result_disables_only_the_culprit_and_ignores_non_candidates():
    mods = _mods(["a", "b", "c"]) + [{"id": "x", "name": "x", "enabled": False}]
    state = mod_manager.start_bisect(["a", "b", "c"])
    while not state.done:
        mods = mod_manager.apply_bisect_test(mods, state)
        off = {m["id"] for m in mods if not m["enabled"]}
        state = mod_manager.report_result(state, problem_still_happens="b" not in off)

    mods = mod_manager.apply_bisect_result(mods, state)

    assert {m["id"]: m["enabled"] for m in mods} == {"a": True, "b": False, "c": True, "x": False}


def test_manual_bisect_never_repeats_the_same_test_configuration():
    """A "fixed" answer with exactly one mod disabled already proves
    that mod -- one more round with the identical config is a wasted
    restart."""
    for culprit in "abcdefg":
        ids = list("abcdefg")
        mods = _mods(ids)
        state = mod_manager.start_bisect(ids)
        seen = []
        while not state.done:
            mods = mod_manager.apply_bisect_test(mods, state)
            config = frozenset(m["id"] for m in mods if not m["enabled"])
            assert config not in seen, f"repeated test {sorted(config)} hunting {culprit}"
            seen.append(config)
            state = mod_manager.report_result(state, problem_still_happens=culprit not in config)
        assert state.culprit == culprit


def _count_ddmin(candidates, is_failing):
    calls = []
    gen = mod_manager.ddmin(candidates)
    try:
        to_test = next(gen)
        while True:
            calls.append(frozenset(to_test))
            to_test = gen.send(is_failing(frozenset(to_test)))
    except StopIteration as stop:
        return stop.value, calls


def test_ddmin_does_not_retest_the_same_subset_at_the_same_granularity():
    """Each ddmin test is a real server restart (up to ~4 min). At
    n=2 the complements ARE the chunks; testing both was 2 wasted
    restarts per level."""
    _, calls = _count_ddmin(list("abcd"), lambda s: False)  # nothing reduces -- worst case
    for i in range(1, len(calls)):
        assert calls[i] != calls[i - 1] or len(calls[i]) == 1
    assert calls[:2] == [frozenset("ab"), frozenset("cd")]
    assert frozenset("ab") not in calls[2:4] and frozenset("cd") not in calls[2:4]


@pytest.mark.parametrize("culprit", list("abcdefgh"))
def test_ddmin_single_culprit_cost_is_logarithmic(culprit):
    result, calls = _count_ddmin(list("abcdefgh"), lambda s: culprit in s)
    assert result == [culprit]
    assert len(calls) <= 6  # 8 mods: halve 3 times, at most 2 tests per level


def test_restore_removes_a_leftover_wal_that_is_not_in_the_snapshot(tmp_path):
    """The exact case restore_world_save's comment exists for: a stale
    -wal next to the restored .db would be replayed onto it."""
    saved = tmp_path / "ConanSandbox" / "Saved"
    saved.mkdir(parents=True)
    (saved / "game.db").write_text("original")
    snap = mod_manager.snapshot_world_save(str(tmp_path))
    (saved / "game.db").write_text("mutated")
    (saved / "game.db-wal").write_text("changes to replay")

    assert mod_manager.restore_world_save(str(tmp_path), snap) is True

    assert (saved / "game.db").read_text() == "original"
    assert not (saved / "game.db-wal").exists()


def test_restore_reports_failure_instead_of_only_logging(tmp_path, monkeypatch):
    saved = tmp_path / "ConanSandbox" / "Saved"
    saved.mkdir(parents=True)
    (saved / "game.db").write_text("original")
    snap = mod_manager.snapshot_world_save(str(tmp_path))

    def locked(src, dst):
        raise PermissionError("file in use by another process")
    monkeypatch.setattr(mod_manager.os, "replace", locked)

    assert mod_manager.restore_world_save(str(tmp_path), snap) is False
    # The live .db is untouched and no staging leftovers remain.
    assert (saved / "game.db").read_text() == "original"
    assert sorted(p.name for p in saved.iterdir()) == ["game.db"]
    mod_manager.cleanup_world_save_snapshot(snap)


def test_find_workshop_pak_handles_glob_special_characters(tmp_path):
    sc = str(tmp_path / "Steam [backup]")
    real = _pak(sc, "42", "Mod.pak")
    assert mod_manager.find_workshop_pak(sc, "42") == real


def test_world_save_files_handles_glob_special_characters(tmp_path):
    saved = tmp_path / "Conan [main]" / "Saved"
    saved.mkdir(parents=True)
    (saved / "game.db").write_text("x")
    assert [os.path.basename(f) for f in mod_manager.world_save_files(str(saved))] == ["game.db"]


# ================================================ manual BisectDialog ==

def _world(tmp_path, content="original world"):
    saved = tmp_path / "ConanSandbox" / "Saved"
    saved.mkdir(parents=True)
    (saved / "game.db").write_text(content)
    return saved


def test_escape_restores_mod_list_and_world(tmp_path, monkeypatch):
    monkeypatch.setattr(process_manager, "is_running", lambda d: False)
    saved = _world(tmp_path)
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(tmp_path))
    server.mods = _mods(["a", "b", "c", "d"])
    dlg = BisectDialog(server, on_changed=None)
    assert any(not m["enabled"] for m in server.mods)  # round 1 disabled some
    (saved / "game.db").write_text("mutated")

    dlg.reject()  # what Escape does

    assert all(m["enabled"] for m in server.mods)
    assert (saved / "game.db").read_text() == "original world"
    assert dlg._saved_snapshot_dir is None


def test_x_button_restores_only_once(tmp_path, monkeypatch, no_real_dialogs):
    """Qt's closeEvent calls reject() itself -- the restore (and its
    prompt) must not run twice."""
    monkeypatch.setattr(process_manager, "is_running", lambda d: False)
    _world(tmp_path)
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(tmp_path))
    server.mods = _mods(["a", "b"])
    dlg = BisectDialog(server, on_changed=None)
    dlg.show()

    dlg.close()

    assert [p for p in no_real_dialogs["shown"] if p[0] == "question"].__len__() == 1


def test_ruled_out_finish_in_dialog_reenables_the_last_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(process_manager, "is_running", lambda d: False)
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(tmp_path))
    server.mods = _mods(["a", "b", "c"])
    written = []
    dlg = BisectDialog(server, on_changed=lambda srv: written.append([dict(m) for m in srv.mods]))

    while not dlg.state.done:
        dlg._report(still_broken=True)

    assert all(m["enabled"] for m in server.mods)
    assert all(m["enabled"] for m in written[-1])  # and that's what modlist.txt got


def test_restore_stops_a_running_server_before_touching_the_world(tmp_path, monkeypatch):
    saved = _world(tmp_path)
    running = {"v": False}
    monkeypatch.setattr(process_manager, "is_running", lambda d: running["v"])
    stops = []

    def stop(srv, timeout=15.0):
        stops.append(1)
        running["v"] = False
    monkeypatch.setattr(process_manager, "graceful_stop", stop)
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(tmp_path))
    server.mods = _mods(["a", "b"])
    dlg = BisectDialog(server, on_changed=None)
    running["v"] = True  # the person restarted it by hand for a test round
    (saved / "game.db").write_text("mutated")

    dlg._restore_and_close(already_closing=True)

    assert stops == [1]
    assert (saved / "game.db").read_text() == "original world"


def test_restore_keeps_the_snapshot_if_the_server_wont_stop(tmp_path, monkeypatch, no_real_dialogs):
    saved = _world(tmp_path)
    running = {"v": False}
    monkeypatch.setattr(process_manager, "is_running", lambda d: running["v"])
    monkeypatch.setattr(process_manager, "graceful_stop", lambda srv, timeout=15.0: False)
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(tmp_path))
    server.mods = _mods(["a", "b"])
    dlg = BisectDialog(server, on_changed=None)
    snap = dlg._saved_snapshot_dir
    running["v"] = True
    (saved / "game.db").write_text("mutated")

    dlg._restore_and_close(already_closing=True)
    dlg.reject()  # closing afterwards must not delete the only good copy either

    assert os.path.isdir(snap)
    assert any(p[0] == "warning" and snap in p[2] for p in no_real_dialogs["shown"])
    mod_manager.cleanup_world_save_snapshot(snap)


def test_declining_the_world_reset_keeps_current_world(tmp_path, monkeypatch, no_real_dialogs):
    monkeypatch.setattr(process_manager, "is_running", lambda d: False)
    saved = _world(tmp_path)
    server = models.ServerConfig(id="s1", name="Chudville", install_dir=str(tmp_path))
    server.mods = _mods(["a", "b"])
    dlg = BisectDialog(server, on_changed=None)
    (saved / "game.db").write_text("real play since the bisect started")
    no_real_dialogs["question"] = QMessageBox.No

    dlg._restore_and_close(already_closing=True)

    assert (saved / "game.db").read_text() == "real play since the bisect started"
    assert all(m["enabled"] for m in server.mods)  # mod list still restored
    assert dlg._saved_snapshot_dir is None


# ============================== auto-bisect: runner -> dialog contract ==

class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run_real_worker(monkeypatch, ids, is_broken, max_rounds=None):
    """Runs an actual AutoBisectWorker against a fake server whose
    health is decided by is_broken(enabled_ids) -- so the dialog tests
    below get REAL outcome shapes, not hand-built ones."""
    clock = _FakeClock()
    monkeypatch.setattr(auto_bisect_runner.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(auto_bisect_runner.time, "sleep", clock.sleep)
    written = []
    monkeypatch.setattr(mod_manager, "write_modlist", lambda i, s, m: written.append([dict(x) for x in m]))
    running = {"v": False}
    monkeypatch.setattr(process_manager, "is_running", lambda d: running["v"])
    monkeypatch.setattr(process_manager, "launch", lambda srv: running.__setitem__("v", True))
    monkeypatch.setattr(process_manager, "graceful_stop", lambda srv, timeout=15.0: running.__setitem__("v", False))
    monkeypatch.setattr(preflight, "run_preflight", lambda s: preflight.CheckResult(ok=True, problems=[], repairs=[]))
    monkeypatch.setattr(AutoBisectWorker, "_mod_file_exists", lambda self, mid: True)
    monkeypatch.setattr(AutoBisectWorker, "_snapshot_saved_dir", lambda self: None)
    monkeypatch.setattr(AutoBisectWorker, "_restore_saved_dir", lambda self: True)

    server = models.ServerConfig(id="s1", name="Chudville", install_dir="/x", steamcmd_dir="/y", bind_ip="127.0.0.1", query_port=27015)
    server.mods = _mods(ids)
    worker = AutoBisectWorker(server, server.mods, was_running=False)
    if max_rounds:
        worker.MAX_ROUNDS = max_rounds
    monkeypatch.setattr(
        network_utils, "query_a2s_info",
        lambda ip, port, timeout=2.0: None if is_broken({m["id"] for m in worker.mods if m["enabled"]}) else {"name": "ok"},
    )
    got = {}
    worker.finished_bisect.connect(lambda o: got.setdefault("o", o))
    worker.run()
    return server, got["o"], written


def _dialog_for(server, outcome, monkeypatch):
    monkeypatch.setattr(AutoBisectWorker, "start", lambda self: None)
    changed = []
    dlg = AutoBisectDialog(server, list(server.mods), was_running=False,
                           on_changed=lambda srv: changed.append([dict(m) for m in srv.mods]))
    dlg._on_finished(outcome)
    return dlg, changed


def test_partial_result_after_failed_sanity_check_offers_no_false_buttons(monkeypatch):
    # "a" breaks it alone; "b"+"c" also break it together.
    server, outcome, written = _run_real_worker(
        monkeypatch, ["a", "b", "c"], lambda on: "a" in on or {"b", "c"} <= on,
    )
    assert outcome.found_culprits == ["a"] and outcome.unresolved_suspects  # the partial shape

    dlg, _ = _dialog_for(server, outcome, monkeypatch)

    assert dlg.keep_disabled_btn.isHidden() and dlg.delete_btn.isHidden() and dlg.reenable_btn.isHidden()
    text = dlg.status_label.text()
    assert "Partial" in text and "currently disabled" not in text
    assert all(m["enabled"] for m in written[-1])  # truthfully: nothing was disabled


def test_round_cap_after_a_culprit_is_reported_as_partial(monkeypatch):
    ids = [f"m{i}" for i in range(10)]
    server, outcome, _ = _run_real_worker(monkeypatch, ids, lambda on: "m0" in on, max_rounds=4)
    assert outcome.found_culprits == ["m0"] and outcome.unresolved_suspects

    dlg, _ = _dialog_for(server, outcome, monkeypatch)

    assert dlg.keep_disabled_btn.isHidden()
    assert "Partial" in dlg.status_label.text()


def test_closing_without_choosing_still_matches_what_modlist_got(monkeypatch):
    server, outcome, written = _run_real_worker(monkeypatch, ["a", "b", "c"], lambda on: "b" in on)
    assert outcome.found_culprits == ["b"] and not outcome.unresolved_suspects

    dlg, changed = _dialog_for(server, outcome, monkeypatch)
    dlg.close()  # no Delete / Keep / Re-enable

    assert server.mods == written[-1]  # config == modlist.txt
    assert {m["id"]: m["enabled"] for m in server.mods} == {"a": True, "b": False, "c": True}
    assert changed  # persisted through on_changed, not just set in memory


def test_result_messages_use_mod_names_not_just_ids(monkeypatch):
    server, outcome, _ = _run_real_worker(monkeypatch, ["a", "b"], lambda on: "a" in on)
    dlg, _ = _dialog_for(server, outcome, monkeypatch)
    assert "Mod a" in dlg.status_label.text()


# ============================================================ workshop ==

def test_iris_cutoff_is_september_1_2026_utc():
    assert swa.IRIS_CUTOFF_TIMESTAMP == int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())


def test_mod_recooked_during_the_iris_beta_is_listed():
    item = swa.WorkshopItem(
        id="1", title="Early recook", description="", author_steam_id="", subscriptions=0,
        time_updated=int(datetime(2026, 9, 5, tzinfo=timezone.utc).timestamp()), tags=["Enhanced"],
    )
    assert swa._is_iris_ready(item)


def test_mod_last_updated_before_the_iris_beta_is_hidden():
    item = swa.WorkshopItem(
        id="1", title="Stale", description="", author_steam_id="", subscriptions=0,
        time_updated=int(datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc).timestamp()), tags=["Enhanced"],
    )
    assert not swa._is_iris_ready(item)
