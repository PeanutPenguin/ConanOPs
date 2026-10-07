"""
Automatic mod bisect dialog -- runs auto_bisect_runner.AutoBisectWorker
and shows its progress, then offers Delete/Keep Disabled/Re-enable for
whatever it found (applied to ALL found mods at once, in find_all
mode, since the dialog doesn't ask for one-at-a-time decisions on
what's fundamentally one search result). See that module's docstring
for how the actual restart-wait-check loop and, for find_all, the
delta-debugging pass work; this file is purely the UI around it.
"""
from __future__ import annotations

from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QMessageBox,
)

import mod_manager
from ui import assets
from auto_bisect_runner import AutoBisectWorker
from models import ServerConfig

# How long to wait, when the dialog is closed (X button or Escape)
# while a worker is still running, for its cancel-and-restore sequence
# to actually finish before letting the dialog go away. Generous:
# worst case that sequence is graceful_stop's own wait (up to ~20s),
# plus this module's own post-stop confirmation (10s), plus writing
# modlist.txt and possibly relaunching -- comfortably under a minute,
# but not comfortably under the 30s this used to allow.
_CLOSE_WAIT_MS = 60_000


class AutoBisectDialog(QDialog):
    def __init__(self, server: ServerConfig, mods_snapshot: List[dict], was_running: bool,
                 find_all: bool = False, on_changed=None, is_online: Optional[Callable[[], bool]] = None,
                 restart_server: Optional[Callable[[ServerConfig], None]] = None, parent=None):
        super().__init__(parent)
        self.server = server
        self.find_all = find_all
        self.was_running = was_running
        self.on_changed = on_changed
        # callable(server) -> None -- set by ModsPage, wired to
        # MainWindow's normal restart handler. Used only by "Re-enable
        # Anyway": that's the one choice here that changes what SHOULD
        # be running versus what actually is (Delete/Keep Disabled
        # both match what the worker already left running -- see
        # AutoBisectWorker._settle).
        self.restart_server = restart_server
        self._worker: Optional[AutoBisectWorker] = None
        self._outcome = None

        self.setWindowTitle("Find Every Bad Mod" if find_all else "Quick Mod Check")
        self.setModal(True)
        self.resize(520, 360)

        root = QVBoxLayout(self)

        if find_all:
            intro = QLabel(
                "ConanOps will restart the server repeatedly to isolate every mod that's causing "
                "a problem -- including a pair that only breaks things when both are enabled "
                "together, not either one alone. This takes meaningfully longer than a quick check: "
                "each isolation can mean several restarts, and a combination requires more restarts "
                "than a single culprit does."
            )
        else:
            intro = QLabel(
                "ConanOps will restart the server once per mod, testing each one completely alone, "
                "to find every mod that's independently causing a problem. This won't catch a mod "
                "that needs another mod present to work, or two mods that only break things when "
                "both are enabled together -- use Find All Bad Mods for that."
            )
        intro.setWordWrap(True)
        root.addWidget(intro)

        status_row = QHBoxLayout()
        status_row.setSpacing(12)
        self.spinner = assets.LoadingSpinner(32)
        status_row.addWidget(self.spinner, 0, Qt.AlignTop)
        self.status_label = QLabel("Starting…")
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        root.addLayout(status_row)

        self.limits_note = QLabel(
            "Note: this only catches a server that won't start or hangs on startup -- it can't "
            "detect a mod that causes problems later, or in a way that doesn't stop the server "
            "from running. The server is treated as off-limits to players for the whole run; it "
            "stops immediately (with everything restored) if anyone joins."
        )
        self.limits_note.setObjectName("Dim")
        self.limits_note.setWordWrap(True)
        root.addWidget(self.limits_note)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._handle_cancel)
        root.addWidget(self.cancel_btn)

        self.result_row = QHBoxLayout()
        self.delete_btn = QPushButton("Delete These Mods")
        self.delete_btn.clicked.connect(self._handle_delete)
        self.keep_disabled_btn = QPushButton("Keep Them Disabled")
        self.keep_disabled_btn.clicked.connect(self._handle_keep_disabled)
        self.reenable_btn = QPushButton("Re-enable Anyway")
        self.reenable_btn.clicked.connect(self._handle_reenable)
        for b in (self.delete_btn, self.keep_disabled_btn, self.reenable_btn):
            b.setVisible(False)
            self.result_row.addWidget(b)
        root.addLayout(self.result_row)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        self.close_btn = QPushButton("Close")
        self.close_btn.setObjectName("PrimaryButton")
        self.close_btn.setVisible(False)
        self.close_btn.clicked.connect(self.accept)
        close_row.addWidget(self.close_btn)
        root.addLayout(close_row)

        self._worker = AutoBisectWorker(
            server, mods_snapshot, was_running, find_all=find_all, is_online=is_online, parent=self,
        )
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_bisect.connect(self._on_finished)
        self._worker.start()

    def _cancel_and_wait_if_running(self) -> None:
        """Shared by closeEvent (X button) and reject() (Escape, or
        any other path that dismisses the dialog without going through
        accept()) -- both must cancel a still-running worker and wait
        for its cancel-and-restore sequence to actually finish before
        letting the dialog go away. Escape used to skip this entirely
        (QDialog's default reject() just hides the dialog), leaving
        the worker to keep restarting the server in the background
        with no one watching it, the automation lock never released,
        and this dialog liable to be destroyed while its QThread was
        still running."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(_CLOSE_WAIT_MS)

    def closeEvent(self, event) -> None:  # noqa: N802 -- Qt's naming
        self._cancel_and_wait_if_running()
        super().closeEvent(event)

    def reject(self) -> None:
        self._cancel_and_wait_if_running()
        super().reject()

    def _handle_cancel(self) -> None:
        if self._worker is None:
            return
        self.status_label.setText("Cancelling -- restoring the original mod list…")
        self.cancel_btn.setEnabled(False)
        self._worker.cancel()

    def _on_progress(self, progress) -> None:
        found_note = f" (already found: {', '.join(progress.found_so_far)})" if progress.found_so_far else ""
        if progress.phase == "reproduce_check":
            self.status_label.setText(
                "First, confirming the problem is actually happening right now: testing with the "
                "current, full mod list…"
            )
        elif progress.phase == "baseline_check":
            self.status_label.setText(
                "Confirmed it's currently broken. Now checking whether this is even a mod problem: "
                "testing with every candidate mod disabled…"
            )
        elif progress.phase == "sanity_check":
            self.status_label.setText(
                f"Round {progress.round_number}: checking whether anything else is still wrong, "
                f"with everything found so far disabled{found_note}…"
            )
        elif progress.phase == "restarting":
            disabled = ", ".join(progress.disabled_this_round) or "(none -- testing the single remaining candidate)"
            self.status_label.setText(
                f"Round {progress.round_number}: restarting with disabled: {disabled}\n"
                f"Candidates remaining: {progress.remaining_candidates}{found_note}"
            )
        elif progress.phase == "confirming":
            self.status_label.setText(
                f"Round {progress.round_number}: it answered -- making sure it stays up and keeps "
                f"answering before trusting that…{found_note}"
            )
        elif progress.phase == "stopping":
            self.status_label.setText(
                "Stopping the server first, so the world save can be copied safely before any testing…"
            )
        elif progress.phase == "retrying":
            self.status_label.setText(
                f"Round {progress.round_number}: it didn't answer in time -- giving it one more, "
                f"longer try in case it was just loading slowly…{found_note}"
            )
        elif progress.phase == "settling":
            self.status_label.setText("Finishing up -- putting the world save and mod list back in order…")
        else:
            self.status_label.setText(
                f"Round {progress.round_number}: waiting to see if the server comes up cleanly…{found_note}"
            )

    def _on_finished(self, outcome) -> None:
        self._outcome = outcome
        self._worker = None
        self.spinner.hide()
        self.cancel_btn.setVisible(False)
        self.close_btn.setVisible(True)
        # The worker has already written outcome.mods to modlist.txt
        # (AutoBisectWorker._settle) -- commit the SAME list to the
        # server's config right now, whatever the outcome, rather than
        # only if the person later clicks Delete/Keep Disabled/Re-enable.
        # Before this, just closing the dialog after a result left the
        # Mods tab showing culprits as enabled while modlist.txt had
        # them off, and the next unrelated mod edit silently re-enabled
        # them.
        self._apply_final_mods()

        skipped_note = ""
        if outcome.skipped_not_downloaded:
            skipped_note = (
                f"\n\n(Not tested -- not downloaded yet: {self._names(outcome.skipped_not_downloaded)}. "
                f"Go to Download Mods on the Mods tab, then run this again to actually test them.)"
            )

        if outcome.preflight_problems:
            message = (
                "Couldn't even start checking -- there's a problem with the server setup itself, "
                "unrelated to any mod:\n" + "\n".join(outcome.preflight_problems) +
                "\n\nFix that first (see the Diagnostics settings tab), then run this again."
            )
            self.status_label.setText(message)
            return
        if outcome.no_steamcmd_dir:
            self.status_label.setText(
                "No SteamCMD folder is configured for this server, so nothing could be checked for "
                "download status -- none of your mods were actually tested. Set up SteamCMD (the "
                "setup wizard, or App Settings) and run this again."
            )
            return
        if outcome.player_joined:
            message = (
                "Stopped -- someone joined while this was running, and it isn't safe to keep "
                "restarting the server with players connected. The original mod list has been "
                "restored."
                + (f" Already found before stopping: {self._names(outcome.found_culprits)}." if outcome.found_culprits else "")
            )
            self.status_label.setText(message + skipped_note)
            return
        if outcome.cancelled:
            message = (
                "Cancelled -- the original mod list has been restored."
                + (f" (Already found before cancelling: {self._names(outcome.found_culprits)}.)" if outcome.found_culprits else "")
            )
            self.status_label.setText(message + skipped_note)
            return
        if outcome.error:
            message = f"Auto-bisect stopped unexpectedly: {outcome.error}\n\nThe original mod list has been restored."
            self.status_label.setText(message + skipped_note)
            return
        if outcome.could_not_reproduce:
            self.status_label.setText(
                "The server actually started fine with its current, full mod list -- there was "
                "nothing to reproduce, so nothing was tested or changed. If you're still seeing a "
                "problem, it may not be one that stops the server from starting (see the note "
                "above), or it may have been a one-off."
            )
            return
        if outcome.not_mod_related:
            message = (
                "This doesn't appear to be a mod problem: the server still didn't come up even with "
                "every mod disabled. The original mod list has been restored -- nothing here was "
                "changed. Check the Diagnostics settings tab for other likely causes (ports, "
                "install files, disk space)."
            )
            self.status_label.setText(message + skipped_note)
            return

        if not outcome.found_culprits:
            if outcome.unresolved_suspects:
                message = (
                    "Couldn't pin down a specific cause -- the original mod list has been restored. "
                    "Suspects that couldn't be cleared: " + self._names(outcome.unresolved_suspects) + ". "
                    "This can happen if the problem isn't actually one of these mods, if it needs two "
                    "or more of them enabled together (try Find All Bad Mods), or if more ran out of "
                    "time to isolate than this pass could handle."
                )
            else:
                message = (
                    "Couldn't pin down a cause -- every candidate was ruled out, and the original "
                    "mod list has been restored. The problem may not actually be one of these mods."
                )
            self.status_label.setText(message + skipped_note)
            return

        names = self._names(outcome.found_culprits)
        if outcome.unresolved_suspects:
            # A PARTIAL result: something failed on its own, but the run
            # couldn't confirm that removing it fixes things (the final
            # sanity check still failed, or the round cap stopped it).
            # The worker restored the ORIGINAL mod list in this case --
            # nothing is disabled -- so offering "Keep Disabled" here
            # would be a lie, and "Delete" would act on an unconfirmed
            # guess. Report it and leave the decision to the person.
            self.status_label.setText(
                f"Partial result -- the original mod list has been restored and nothing was disabled.\n\n"
                f"Failed when tested: {names}. But the server still wasn't confirmed healthy with "
                f"{'that' if len(outcome.found_culprits) == 1 else 'those'} removed, so something else is "
                f"likely involved too. Not cleared yet: {self._names(outcome.unresolved_suspects)}.\n\n"
                f"You can disable {'it' if len(outcome.found_culprits) == 1 else 'them'} yourself on the Mods "
                f"tab, or try Find All Bad Mods."
                + skipped_note
            )
            return
        running_note = " The server is running without them." if self.was_running else ""
        if len(outcome.found_culprits) == 1:
            message = f"Found it: mod {names} appears to be the cause. It's currently disabled.{running_note.replace('them', 'it')}"
        else:
            message = f"Found {len(outcome.found_culprits)} mods implicated: {names}. All are currently disabled.{running_note}"
        self.status_label.setText(message + skipped_note)
        for b in (self.delete_btn, self.keep_disabled_btn, self.reenable_btn):
            b.setVisible(True)

    def _names(self, mod_ids) -> str:
        """Display names instead of raw Workshop ids where known --
        "Pippi (880454836)" reads a lot better than a bare number."""
        by_id = {m["id"]: m for m in self.server.mods}
        out = []
        for mid in mod_ids:
            name = (by_id.get(mid) or {}).get("name")
            out.append(f"{name} ({mid})" if name and name != mid else mid)
        return ", ".join(out)

    def _apply_final_mods(self) -> None:
        """Commits the worker's final mod list (whatever state it left
        things in -- see AutoBisectWorker's docstring) to the actual
        server, the same "reassign server.mods, then tell the caller"
        pattern every other mod_manager-backed action in this app
        already uses."""
        self.server.mods = [dict(m) for m in self._outcome.mods]
        if self.on_changed:
            self.on_changed(self.server)

    def _handle_delete(self) -> None:
        culprits = self._outcome.found_culprits
        names = self._names(culprits)
        plural = "s" if len(culprits) != 1 else ""
        reply = QMessageBox.question(
            self, "Delete Mod" + plural,
            f"Remove mod{plural} {names} from this server's mod list entirely? (Downloaded files "
            f"aren't deleted -- only the entr{'ies' if len(culprits) != 1 else 'y'} here.) "
            f"{'They are' if len(culprits) != 1 else 'It is'} already disabled, so this doesn't "
            f"need another restart.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return
        self._apply_final_mods()
        mods = self.server.mods
        for culprit in culprits:
            mods = mod_manager.remove_mod(mods, culprit)
        self.server.mods = mods
        if self.on_changed:
            self.on_changed(self.server)
        self._finish_result_choice(f"Mod{plural} {names} removed.")

    def _handle_keep_disabled(self) -> None:
        self._apply_final_mods()  # already left with just the found culprits disabled -- nothing more to change
        names = self._names(self._outcome.found_culprits)
        self._finish_result_choice(f"Mod(s) {names} kept disabled. No restart needed -- they're already off.")

    def _handle_reenable(self) -> None:
        self._apply_final_mods()
        mods = self.server.mods
        for culprit in self._outcome.found_culprits:
            mods = mod_manager.set_enabled(mods, culprit, True)
        self.server.mods = mods
        if self.on_changed:
            self.on_changed(self.server)
        names = self._names(self._outcome.found_culprits)
        if self.restart_server and self.was_running:
            self._finish_result_choice(
                f"Mod(s) {names} re-enabled -- restarting the server now to load them. If they really "
                f"were the cause, the problem will likely come back."
            )
            self.restart_server(self.server)
        elif not self.was_running:
            # The server was stopped before this run and the worker
            # left it stopped -- re-enabling a mod is no reason to
            # start a server the person had deliberately turned off.
            self._finish_result_choice(
                f"Mod(s) {names} re-enabled -- they'll load the next time you start the server. If "
                f"they really were the cause, the problem will likely come back."
            )
        else:
            self._finish_result_choice(
                f"Mod(s) {names} re-enabled -- if they really were the cause, the problem will likely "
                f"come back. The running server won't load them until it's restarted."
            )

    def _finish_result_choice(self, message: str) -> None:
        self.status_label.setText(message)
        for b in (self.delete_btn, self.keep_disabled_btn, self.reenable_btn):
            b.setVisible(False)
