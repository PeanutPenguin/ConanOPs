from __future__ import annotations

import sys

import pytest

pyside6 = pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication, QMessageBox

import mod_manager
import models
from auto_bisect_runner import AutoBisectWorker, BisectOutcome, BisectProgress
from ui.auto_bisect_dialog import AutoBisectDialog


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


def _server():
    return models.ServerConfig(id="s1", name="Chudville", install_dir="/tmp/fake")


def _mods():
    return [{"id": "1", "name": "A", "enabled": True}, {"id": "2", "name": "B", "enabled": True}]


def _make_dialog(monkeypatch, was_running=False, find_all=False):
    monkeypatch.setattr(AutoBisectWorker, "start", lambda self: None)
    return AutoBisectDialog(_server(), _mods(), was_running=was_running, find_all=find_all)


def test_progress_updates_status_label(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    dlg._on_progress(BisectProgress(round_number=2, disabled_this_round=["1"], remaining_candidates=1, phase="restarting"))
    assert "Round 2" in dlg.status_label.text()
    assert "1" in dlg.status_label.text()


def test_progress_sanity_check_phase_shown_distinctly(monkeypatch):
    dlg = _make_dialog(monkeypatch, find_all=True)
    dlg._on_progress(BisectProgress(round_number=5, remaining_candidates=3, phase="sanity_check", found_so_far=["1"]))
    assert "checking" in dlg.status_label.text().lower()
    assert "1" in dlg.status_label.text()


def test_finished_with_one_culprit_shows_result_buttons(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=[{"id": "1", "name": "A", "enabled": False}, {"id": "2", "name": "B", "enabled": True}], found_culprits=["1"])

    dlg._on_finished(outcome)

    assert dlg.delete_btn.isHidden() is False
    assert dlg.keep_disabled_btn.isHidden() is False
    assert dlg.reenable_btn.isHidden() is False
    assert "1" in dlg.status_label.text()


def test_finished_with_multiple_culprits_shows_all_of_them(monkeypatch):
    dlg = _make_dialog(monkeypatch, find_all=True)
    outcome = BisectOutcome(mods=_mods(), found_culprits=["1", "2"])

    dlg._on_finished(outcome)

    assert dlg.delete_btn.isHidden() is False
    assert "1" in dlg.status_label.text()
    assert "2" in dlg.status_label.text()
    assert "2 mods" in dlg.status_label.text()


def test_finished_cancelled_hides_result_buttons(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods(), cancelled=True)

    dlg._on_finished(outcome)

    assert dlg.delete_btn.isHidden() is True
    assert "Cancelled" in dlg.status_label.text()


def test_finished_cancelled_mentions_anything_already_found(monkeypatch):
    dlg = _make_dialog(monkeypatch, find_all=True)
    outcome = BisectOutcome(mods=_mods(), cancelled=True, found_culprits=["1"])

    dlg._on_finished(outcome)

    assert "1" in dlg.status_label.text()


def test_finished_no_culprit_hides_result_buttons(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods())

    dlg._on_finished(outcome)

    assert dlg.delete_btn.isHidden() is True
    assert "ruled out" in dlg.status_label.text()


def test_finished_unresolved_suspects_shown_distinctly(monkeypatch):
    dlg = _make_dialog(monkeypatch, find_all=True)
    outcome = BisectOutcome(mods=_mods(), unresolved_suspects=["1", "2"])

    dlg._on_finished(outcome)

    assert dlg.delete_btn.isHidden() is True
    assert "1" in dlg.status_label.text()
    assert "2" in dlg.status_label.text()
    assert "couldn't pin down" in dlg.status_label.text().lower()


def test_finished_with_error_hides_result_buttons(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods(), error="disk full")

    dlg._on_finished(outcome)

    assert dlg.delete_btn.isHidden() is True
    assert "disk full" in dlg.status_label.text()


def test_handle_delete_removes_mod_after_confirmation(monkeypatch):
    server = _server()
    monkeypatch.setattr(AutoBisectWorker, "start", lambda self: None)
    dlg = AutoBisectDialog(server, _mods(), was_running=False)
    final_mods = [{"id": "1", "name": "A", "enabled": False}, {"id": "2", "name": "B", "enabled": True}]
    dlg._on_finished(BisectOutcome(mods=final_mods, found_culprits=["1"]))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    changed = []
    dlg.on_changed = lambda srv: changed.append(list(srv.mods))

    dlg._handle_delete()

    assert all(m["id"] != "1" for m in server.mods)
    assert changed  # persisted at least once


def test_handle_delete_removes_every_found_culprit(monkeypatch):
    server = _server()
    server.mods = [{"id": "3", "name": "C", "enabled": True}]  # will be overwritten by _apply_final_mods
    monkeypatch.setattr(AutoBisectWorker, "start", lambda self: None)
    dlg = AutoBisectDialog(server, _mods(), was_running=False, find_all=True)
    final_mods = [{"id": "1", "name": "A", "enabled": False}, {"id": "2", "name": "B", "enabled": False}]
    dlg._on_finished(BisectOutcome(mods=final_mods, found_culprits=["1", "2"]))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))

    dlg._handle_delete()

    assert server.mods == []


def test_handle_delete_declined_leaves_mod_alone(monkeypatch):
    server = _server()
    monkeypatch.setattr(AutoBisectWorker, "start", lambda self: None)
    dlg = AutoBisectDialog(server, _mods(), was_running=False)
    final_mods = [{"id": "1", "name": "A", "enabled": False}, {"id": "2", "name": "B", "enabled": True}]
    dlg._on_finished(BisectOutcome(mods=final_mods, found_culprits=["1"]))
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Cancel))

    dlg._handle_delete()

    # Declining the delete keeps the mod in the list -- but the list
    # itself is already the worker's final one (culprit disabled),
    # matching what's actually in modlist.txt. This test used to assert
    # server.mods stayed completely untouched, which was the bug: the
    # config and modlist.txt disagreed until someone clicked a button.
    assert server.mods == final_mods


def test_handle_keep_disabled_applies_final_mods(monkeypatch):
    server = _server()
    monkeypatch.setattr(AutoBisectWorker, "start", lambda self: None)
    dlg = AutoBisectDialog(server, _mods(), was_running=False)
    final_mods = [{"id": "1", "name": "A", "enabled": False}, {"id": "2", "name": "B", "enabled": True}]
    dlg._on_finished(BisectOutcome(mods=final_mods, found_culprits=["1"]))

    dlg._handle_keep_disabled()

    by_id = {m["id"]: m["enabled"] for m in server.mods}
    assert by_id["1"] is False
    assert by_id["2"] is True


def test_handle_reenable_turns_all_found_culprits_back_on(monkeypatch):
    server = _server()
    monkeypatch.setattr(AutoBisectWorker, "start", lambda self: None)
    dlg = AutoBisectDialog(server, _mods(), was_running=False, find_all=True)
    final_mods = [{"id": "1", "name": "A", "enabled": False}, {"id": "2", "name": "B", "enabled": False}]
    dlg._on_finished(BisectOutcome(mods=final_mods, found_culprits=["1", "2"]))

    dlg._handle_reenable()

    by_id = {m["id"]: m["enabled"] for m in server.mods}
    assert by_id["1"] is True
    assert by_id["2"] is True


def test_cancel_button_calls_worker_cancel(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    cancelled = []
    monkeypatch.setattr(dlg._worker, "cancel", lambda: cancelled.append(1))

    dlg._handle_cancel()

    assert cancelled == [1]
    assert dlg.cancel_btn.isEnabled() is False


def test_close_event_cancels_a_running_worker(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    monkeypatch.setattr(dlg._worker, "isRunning", lambda: True)
    monkeypatch.setattr(dlg._worker, "wait", lambda ms: True)
    cancelled = []
    monkeypatch.setattr(dlg._worker, "cancel", lambda: cancelled.append(1))

    from PySide6.QtGui import QCloseEvent
    dlg.closeEvent(QCloseEvent())

    assert cancelled == [1]


def test_find_all_constructs_worker_with_find_all_true(monkeypatch):
    captured = {}
    real_init = AutoBisectWorker.__init__

    def capturing_init(self, server, mods, was_running, find_all=False, is_online=None, parent=None):
        captured["find_all"] = find_all
        real_init(self, server, mods, was_running, find_all=find_all, parent=parent)

    monkeypatch.setattr(AutoBisectWorker, "__init__", capturing_init)
    monkeypatch.setattr(AutoBisectWorker, "start", lambda self: None)

    AutoBisectDialog(_server(), _mods(), was_running=False, find_all=True)

    assert captured["find_all"] is True


def test_plain_mode_constructs_worker_with_find_all_false(monkeypatch):
    captured = {}
    real_init = AutoBisectWorker.__init__

    def capturing_init(self, server, mods, was_running, find_all=False, is_online=None, parent=None):
        captured["find_all"] = find_all
        real_init(self, server, mods, was_running, find_all=find_all, parent=parent)

    monkeypatch.setattr(AutoBisectWorker, "__init__", capturing_init)
    monkeypatch.setattr(AutoBisectWorker, "start", lambda self: None)

    AutoBisectDialog(_server(), _mods(), was_running=False)

    assert captured["find_all"] is False


def test_progress_baseline_check_phase_shown_distinctly(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    dlg._on_progress(BisectProgress(round_number=1, remaining_candidates=0, phase="baseline_check"))
    assert "mod problem" in dlg.status_label.text().lower()


def test_finished_not_mod_related_hides_result_buttons_and_says_so(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods(), not_mod_related=True)

    dlg._on_finished(outcome)

    assert dlg.delete_btn.isHidden() is True
    assert dlg.keep_disabled_btn.isHidden() is True
    assert dlg.reenable_btn.isHidden() is True
    assert "doesn't appear to be a mod problem" in dlg.status_label.text().lower()


def test_finished_not_mod_related_takes_priority_over_empty_found_culprits(monkeypatch):
    """not_mod_related and "ruled out every candidate" are different
    messages -- must not collapse into the generic one."""
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods(), not_mod_related=True, found_culprits=[])

    dlg._on_finished(outcome)

    assert "ruled out" not in dlg.status_label.text().lower()
    assert "doesn't appear to be a mod problem" in dlg.status_label.text().lower()


def test_progress_confirming_phase_shown_distinctly(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    dlg._on_progress(BisectProgress(round_number=3, phase="confirming"))
    assert "stays up" in dlg.status_label.text().lower()


def test_finished_mentions_skipped_not_downloaded_mods(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods(), found_culprits=["1"], skipped_not_downloaded=["9"])

    dlg._on_finished(outcome)

    assert "9" in dlg.status_label.text()
    assert "not downloaded" in dlg.status_label.text().lower()


def test_finished_no_skipped_note_when_nothing_was_skipped(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods(), found_culprits=["1"])

    dlg._on_finished(outcome)

    assert "not downloaded" not in dlg.status_label.text().lower()


def test_finished_not_mod_related_also_mentions_skipped_mods(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods(), not_mod_related=True, skipped_not_downloaded=["9"])

    dlg._on_finished(outcome)

    assert "9" in dlg.status_label.text()


def test_finished_preflight_problems_shown_with_no_result_buttons(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods(), preflight_problems=["Install folder not found"])

    dlg._on_finished(outcome)

    assert dlg.delete_btn.isHidden() is True
    assert "Install folder not found" in dlg.status_label.text()


def test_finished_no_steamcmd_dir_shown_distinctly(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods(), no_steamcmd_dir=True)

    dlg._on_finished(outcome)

    assert dlg.delete_btn.isHidden() is True
    assert "SteamCMD" in dlg.status_label.text()


def test_finished_could_not_reproduce_shown_distinctly(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods(), could_not_reproduce=True)

    dlg._on_finished(outcome)

    assert dlg.delete_btn.isHidden() is True
    assert "started fine" in dlg.status_label.text().lower()


def test_finished_could_not_reproduce_takes_priority_over_not_mod_related_wording(monkeypatch):
    """Distinct messages for distinct meanings -- must not collapse."""
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods(), could_not_reproduce=True)

    dlg._on_finished(outcome)

    assert "doesn't appear to be a mod problem" not in dlg.status_label.text()


def test_finished_player_joined_shown_distinctly(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    outcome = BisectOutcome(mods=_mods(), player_joined=True, found_culprits=["1"])

    dlg._on_finished(outcome)

    assert dlg.delete_btn.isHidden() is True
    assert "joined" in dlg.status_label.text().lower()
    assert "1" in dlg.status_label.text()


def test_progress_reproduce_check_phase_shown_distinctly(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    dlg._on_progress(BisectProgress(round_number=1, phase="reproduce_check"))
    assert "confirming" in dlg.status_label.text().lower()


def test_progress_settling_phase_shown_distinctly(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    dlg._on_progress(BisectProgress(round_number=5, phase="settling"))
    assert "finishing up" in dlg.status_label.text().lower()


def test_reject_cancels_a_running_worker_same_as_close(monkeypatch):
    """The actual Escape-key bug: QDialog's default reject() just
    hides the dialog with no cancellation at all."""
    dlg = _make_dialog(monkeypatch)
    monkeypatch.setattr(dlg._worker, "isRunning", lambda: True)
    monkeypatch.setattr(dlg._worker, "wait", lambda ms: True)
    cancelled = []
    monkeypatch.setattr(dlg._worker, "cancel", lambda: cancelled.append(1))

    dlg.reject()

    assert cancelled == [1]


def test_reject_uses_the_longer_wait_timeout(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    monkeypatch.setattr(dlg._worker, "isRunning", lambda: True)
    monkeypatch.setattr(dlg._worker, "cancel", lambda: None)
    waited = []
    monkeypatch.setattr(dlg._worker, "wait", lambda ms: waited.append(ms))

    dlg.reject()

    assert waited == [60_000]


def test_close_event_uses_the_longer_wait_timeout(monkeypatch):
    dlg = _make_dialog(monkeypatch)
    monkeypatch.setattr(dlg._worker, "isRunning", lambda: True)
    monkeypatch.setattr(dlg._worker, "cancel", lambda: None)
    waited = []
    monkeypatch.setattr(dlg._worker, "wait", lambda ms: waited.append(ms))

    from PySide6.QtGui import QCloseEvent
    dlg.closeEvent(QCloseEvent())

    assert waited == [60_000]


def test_handle_reenable_triggers_restart_when_callback_given(monkeypatch):
    server = _server()
    monkeypatch.setattr(AutoBisectWorker, "start", lambda self: None)
    restarted = []
    dlg = AutoBisectDialog(server, _mods(), was_running=True, find_all=True, restart_server=lambda s: restarted.append(s))
    final_mods = [{"id": "1", "name": "A", "enabled": False}, {"id": "2", "name": "B", "enabled": False}]
    dlg._on_finished(BisectOutcome(mods=final_mods, found_culprits=["1", "2"]))

    dlg._handle_reenable()

    assert restarted == [server]


def test_handle_reenable_notes_restart_needed_with_no_callback(monkeypatch):
    server = _server()
    monkeypatch.setattr(AutoBisectWorker, "start", lambda self: None)
    dlg = AutoBisectDialog(server, _mods(), was_running=True, find_all=True, restart_server=None)
    final_mods = [{"id": "1", "name": "A", "enabled": False}]
    dlg._on_finished(BisectOutcome(mods=final_mods, found_culprits=["1"]))

    dlg._handle_reenable()  # must not raise with no callback

    assert "restart" in dlg.status_label.text().lower()


def test_handle_reenable_does_not_start_a_server_that_was_stopped(monkeypatch):
    """Re-enabling a mod is no reason to start a server the person had
    deliberately stopped before the run -- the worker left it stopped."""
    server = _server()
    monkeypatch.setattr(AutoBisectWorker, "start", lambda self: None)
    restarted = []
    dlg = AutoBisectDialog(server, _mods(), was_running=False, find_all=True, restart_server=lambda s: restarted.append(s))
    final_mods = [{"id": "1", "name": "A", "enabled": False}, {"id": "2", "name": "B", "enabled": False}]
    dlg._on_finished(BisectOutcome(mods=final_mods, found_culprits=["1", "2"]))

    dlg._handle_reenable()

    assert restarted == []
    assert all(m["enabled"] for m in server.mods)
    assert "next time you start" in dlg.status_label.text()
