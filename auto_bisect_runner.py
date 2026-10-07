"""
Automatic mod diagnosis for a server that won't start or hangs on startup.

- Quick Mod Check (find_all=False): tests each enabled mod alone. Finds mods
  that fail on their own, but not mods that only fail together (e.g. a mod
  and the library it needs).
- Find All Bad Mods (find_all=True): repeated delta-debugging
  (mod_manager.ddmin), which also isolates failing combinations.

Before hunting, every run checks preflight, that the full mod list really
fails right now (otherwise every mod gets blamed), and that the server comes
up with all candidates disabled (otherwise it isn't a mod problem). Mods whose
.pak isn't downloaded are skipped, since a missing file fails regardless.

Safety rules each round follows:
- The world save (database files only, never Config/) is snapshotted once the
  server is fully stopped and restored before every restart and at the end;
  Conan can delete a missing mod's buildings on load.
- The run stops if a player joins (restarts would kick them and the reset
  would erase their work).
- The old process must have exited before the next launch (port binding), and
  a new one must keep answering queries for a while to count as "up".
- Hitting the round cap reports the in-progress test as unresolved, not found.

Runs on a QThread. Never mutates the ServerConfig; works on its own copy of
the mods list and the caller applies the final result.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from PySide6.QtCore import QThread, Signal

import applog
import mod_manager
import network_utils
import preflight
import process_manager
from models import ServerConfig

_log = applog.get_logger(__name__)

# Conan can take minutes to load even when healthy.
STARTUP_TIMEOUT_SECONDS = 240
POLL_INTERVAL_SECONDS = 5

# A process that answers once and then crashes must not count as healthy.
STAY_UP_CONFIRM_SECONDS = 20

# A timed-out (not crashed) round gets one longer retry. See _restart_and_wait.
RETRY_TIMEOUT_FACTOR = 1.5

# Backstop wait for the old process to exit before launching the next one.
PROCESS_EXIT_TIMEOUT_SECONDS = 10

# Sentinel for "round cap hit mid-test", distinct from a real result.
_ROUND_CAP_HIT = object()


@dataclass
class BisectProgress:
    round_number: int
    disabled_this_round: List[str] = field(default_factory=list)
    remaining_candidates: int = 0
    phase: str = "restarting"  # "stopping"|"restarting"|"waiting"|"confirming"|"retrying"|"sanity_check"|"baseline_check"|"reproduce_check"|"settling"
    found_so_far: List[str] = field(default_factory=list)  # culprits already confirmed this run


@dataclass
class BisectOutcome:
    mods: List[dict]           # the working mod list as this worker left it -- see below for what that means
    cancelled: bool = False
    error: str = ""
    # Preflight failed; nothing was tested.
    preflight_problems: List[str] = field(default_factory=list)
    # Server came up fine with the full mod list; nothing to reproduce.
    could_not_reproduce: bool = False
    # Still failed with every candidate disabled: not a mod problem.
    not_mod_related: bool = False
    # No SteamCMD folder, so download status couldn't be checked.
    no_steamcmd_dir: bool = False
    # Culprits confirmed by a sanity restart, in order found. May include a
    # group that only fails together (find_all). Already disabled in `mods`.
    found_culprits: List[str] = field(default_factory=list)
    # Couldn't be cleared or convicted: round cap hit mid-test, or (Quick Mod
    # Check) each mod passed alone but the full set still fails.
    unresolved_suspects: List[str] = field(default_factory=list)
    # .pak not on disk; never tested.
    skipped_not_downloaded: List[str] = field(default_factory=list)
    # Stopped early because a player joined; earlier findings still reported.
    player_joined: bool = False


class AutoBisectWorker(QThread):
    progress = Signal(object)          # BisectProgress
    finished_bisect = Signal(object)   # BisectOutcome

    # Caps total restarts across the run for pathological cases.
    MAX_ROUNDS = 40

    def __init__(self, server: ServerConfig, mods_snapshot: List[dict], was_running: bool, find_all: bool = False,
                 is_online: Optional[Callable[[], bool]] = None, parent=None):
        super().__init__(parent)
        self.server = server
        self._original_mods = [dict(m) for m in mods_snapshot]
        self.mods = [dict(m) for m in mods_snapshot]  # working copy this run actually mutates
        self.was_running = was_running
        self.find_all = find_all
        # Checked before every round. None means "never online".
        self.is_online = is_online if is_online is not None else (lambda: False)
        self._cancel_requested = False
        self._round_number = 0
        self._saved_snapshot_dir: Optional[str] = None  # set by _snapshot_saved_dir(), used by _restore_saved_dir()
        # Live progress, so a run stopped by _blocked() still reports findings.
        self._found_culprits: List[str] = []
        self._missing: List[str] = []

    def cancel(self) -> None:
        """Sets a flag polled between steps; a round mid-wait finishes first."""
        self._cancel_requested = True

    def run(self) -> None:
        try:
            candidates = [m["id"] for m in self.mods if m.get("enabled", True)]
            if not candidates:
                self.finished_bisect.emit(BisectOutcome(mods=self.mods))
                return

            result = preflight.run_preflight(self.server)
            if not result.ok:
                self.finished_bisect.emit(BisectOutcome(mods=self.mods, preflight_problems=list(result.problems)))
                return

            if not self.server.steamcmd_dir:
                self.finished_bisect.emit(BisectOutcome(mods=self.mods, no_steamcmd_dir=True))
                return

            missing = [c for c in candidates if not self._mod_file_exists(c)]
            self._missing = missing
            candidates = [c for c in candidates if c not in missing]
            if not candidates:
                self.finished_bisect.emit(BisectOutcome(mods=self.mods, skipped_not_downloaded=missing))
                return
            if missing:
                # A missing .pak breaks every test, so force these off for
                # the run; _settle() restores their original state.
                self.mods = self._apply_baseline([], missing)

            if self._blocked():
                return

            # Stop before snapshotting: copying a live database can capture
            # a torn copy that then gets restored every round.
            if process_manager.is_running(self.server.install_dir):
                self.progress.emit(BisectProgress(self._round_number, [], 0, "stopping"))
                process_manager.graceful_stop(self.server)
                self._wait_for_process_exit()
                if process_manager.is_running(self.server.install_dir):
                    self._settle(BisectOutcome(
                        mods=self._original_mods,
                        error="Couldn't stop the server to take a safe copy of the world save first, so nothing was tested.",
                    ))
                    return
                if self._blocked():
                    return

            try:
                self._saved_snapshot_dir = self._snapshot_saved_dir()
            except mod_manager.WorldSaveError as e:
                # Never test without a way to put the world back.
                self._settle(BisectOutcome(
                    mods=self._original_mods,
                    error=f"{e}. Nothing was tested, so the world save was never put at risk.",
                    skipped_not_downloaded=missing,
                ))
                return

            # ------------------------------------------ reproduce check --
            # Confirm the full set really fails now; otherwise every mod gets blamed.
            self._round_number += 1
            self.mods = self._apply_baseline(candidates, [])
            self.progress.emit(BisectProgress(self._round_number, [], len(candidates), "reproduce_check"))
            came_up = self._restart_and_wait()
            if self._blocked():
                return
            if came_up:
                self._settle(BisectOutcome(mods=self._original_mods, could_not_reproduce=True, skipped_not_downloaded=missing))
                return

            # -------------------------------------------- precondition --
            self._round_number += 1
            self.mods = self._apply_baseline([], candidates)  # everything disabled
            self.progress.emit(BisectProgress(
                self._round_number, sorted(candidates), 0, "baseline_check",
            ))
            came_up = self._restart_and_wait()
            if self._blocked():
                return
            if not came_up:
                # Still broken with all candidates off: not a mod problem.
                self._settle(BisectOutcome(mods=self._original_mods, not_mod_related=True, skipped_not_downloaded=missing))
                return

            if self.find_all:
                self._run_find_all(candidates, missing)
            else:
                self._run_one_by_one(candidates, missing)
        except Exception as e:  # noqa: BLE001 - always emit so the dialog doesn't hang forever
            _log.error(f"Auto-bisect failed unexpectedly: {e}")
            try:
                self._settle(BisectOutcome(mods=self._original_mods, error=str(e)))
            except Exception as restore_err:  # noqa: BLE001 - report the original error either way
                _log.error(f"Restoring the original mod list after that failure ALSO failed: {restore_err}")
                # Keep the snapshot: it may be the only good copy of the world.
                if self._saved_snapshot_dir:
                    e = f"{e}\n\nA copy of the world save from before testing was kept here:\n\n{self._saved_snapshot_dir}"
                # Report _original_mods since the caller applies outcome.mods.
                self.finished_bisect.emit(BisectOutcome(mods=self._original_mods, error=str(e)))

    def _blocked(self) -> bool:
        """If cancelled or a player joined, settle (reporting confirmed culprits
        and skipped mods) and return True."""
        if self._cancel_requested:
            self._settle(BisectOutcome(
                mods=self._original_mods, cancelled=True,
                found_culprits=list(self._found_culprits), skipped_not_downloaded=list(self._missing),
            ))
            return True
        if self.is_online():
            self._settle(BisectOutcome(
                mods=self._original_mods, player_joined=True,
                found_culprits=list(self._found_culprits), skipped_not_downloaded=list(self._missing),
            ))
            return True
        return False

    # ---------------------------------------------------- quick mod check --
    def _run_one_by_one(self, candidates: List[str], missing: List[str]) -> None:
        """Tests each candidate alone, one restart per mod. A mod that needs
        another mod present will also fail here."""
        found_culprits: List[str] = []
        self._found_culprits = found_culprits  # same list object -- see _blocked()
        for mod_id in candidates:
            if self._round_number >= self.MAX_ROUNDS:
                untested = candidates[candidates.index(mod_id):]
                self._settle(BisectOutcome(
                    mods=self._original_mods, found_culprits=found_culprits, unresolved_suspects=untested,
                    skipped_not_downloaded=missing,
                ), round_cap_hit=True)
                return

            self._round_number += 1
            self.mods = self._apply_ddmin_test([mod_id], candidates, found_culprits)
            self.progress.emit(BisectProgress(
                self._round_number, sorted(c for c in candidates if c != mod_id), 1,
                "restarting", list(found_culprits),
            ))
            came_up = self._restart_and_wait()
            if self._blocked():
                return
            if not came_up:
                found_culprits.append(mod_id)

        if not found_culprits:
            # Each passed alone but a mod is to blame: a combination this
            # mode can't isolate.
            self._settle(BisectOutcome(mods=self._original_mods, unresolved_suspects=candidates, skipped_not_downloaded=missing))
            return

        # Sanity check: confirm it comes up with only the culprits removed.
        remaining = [c for c in candidates if c not in found_culprits]
        if self._round_number >= self.MAX_ROUNDS:
            self._settle(BisectOutcome(
                mods=self._original_mods, found_culprits=found_culprits, unresolved_suspects=remaining,
                skipped_not_downloaded=missing,
            ), round_cap_hit=True)
            return
        self._round_number += 1
        self.mods = self._apply_baseline(remaining, found_culprits)
        self.progress.emit(BisectProgress(
            self._round_number, sorted(found_culprits), len(remaining), "sanity_check", list(found_culprits),
        ))
        came_up = self._restart_and_wait()
        if self._blocked():
            return
        if came_up:
            self._settle(BisectOutcome(
                mods=self._final_mods(found_culprits), found_culprits=found_culprits, skipped_not_downloaded=missing,
            ))
        else:
            # Still broken: likely a combination among what's left.
            self._settle(BisectOutcome(
                mods=self._original_mods, found_culprits=found_culprits, unresolved_suspects=remaining,
                skipped_not_downloaded=missing,
            ))

    # ----------------------------------------------------- find every one --
    def _run_find_all(self, candidates: List[str], missing: List[str]) -> None:
        found_culprits: List[str] = []
        self._found_culprits = found_culprits  # same list object -- see _blocked()

        while candidates:
            if self._round_number >= self.MAX_ROUNDS:
                self._settle(BisectOutcome(
                    mods=self._original_mods, found_culprits=found_culprits, unresolved_suspects=candidates,
                    skipped_not_downloaded=missing,
                ), round_cap_hit=True)
                return

            minimal_set, hit_cap, stopped = self._drive_ddmin(candidates, found_culprits)
            if stopped:
                return  # _drive_ddmin already settled via _blocked()
            if hit_cap:
                # The round cap interrupted an in-progress ddmin pass --
                # Mid-pass when the cap hit: unresolved, not found.
                self._settle(BisectOutcome(
                    mods=self._original_mods, found_culprits=found_culprits, unresolved_suspects=candidates,
                    skipped_not_downloaded=missing,
                ), round_cap_hit=True)
                return

            found_culprits.extend(minimal_set)
            candidates = [c for c in candidates if c not in minimal_set]

            if not candidates:
                self._settle(BisectOutcome(
                    mods=self._final_mods(found_culprits), found_culprits=found_culprits, skipped_not_downloaded=missing,
                ))
                return

            # Sanity round: is there another bad mod among what's left?
            if self._round_number >= self.MAX_ROUNDS:
                self._settle(BisectOutcome(
                    mods=self._original_mods, found_culprits=found_culprits, unresolved_suspects=candidates,
                    skipped_not_downloaded=missing,
                ), round_cap_hit=True)
                return
            self._round_number += 1
            self.mods = self._apply_baseline(candidates, found_culprits)
            self.progress.emit(BisectProgress(
                self._round_number, sorted(found_culprits), len(candidates), "sanity_check", list(found_culprits),
            ))
            came_up = self._restart_and_wait()
            if self._blocked():
                return
            if came_up:
                self._settle(BisectOutcome(
                    mods=self._final_mods(found_culprits), found_culprits=found_culprits, skipped_not_downloaded=missing,
                ))
                return
            # Still broken: ddmin the remaining pool again.

        self._settle(BisectOutcome(
            mods=self._final_mods(found_culprits), found_culprits=found_culprits, skipped_not_downloaded=missing,
        ))

    def _drive_ddmin(self, candidates: List[str], found_culprits: List[str]):
        """Runs one ddmin pass, restarting the server for each test. Returns
        (minimal_failing_set, hit_cap, stopped). On hit_cap the set is
        meaningless; on stopped, _blocked() has already settled, so just return."""
        gen = mod_manager.ddmin(candidates)
        try:
            to_test = next(gen)
            while True:
                if self._round_number >= self.MAX_ROUNDS:
                    return None, True, False
                if self._blocked():
                    return None, False, True
                self._round_number += 1
                self.mods = self._apply_ddmin_test(to_test, candidates, found_culprits)
                self.progress.emit(BisectProgress(
                    self._round_number, sorted(set(candidates) - set(to_test)), len(to_test),
                    "restarting", list(found_culprits),
                ))
                came_up = self._restart_and_wait()
                if self._blocked():
                    return None, False, True
                to_test = gen.send(not came_up)  # still_fails = not came_up
        except StopIteration as stop:
            return list(stop.value), False, False

    def _apply_ddmin_test(self, enabled_subset: List[str], all_candidates: List[str], found_culprits: List[str]) -> List[dict]:
        """Mods list with exactly `enabled_subset` on among the candidates, and
        all earlier culprits off. Non-candidates are left as they were."""
        want_enabled = set(enabled_subset)
        want_disabled = (set(all_candidates) - want_enabled) | set(found_culprits)
        result = []
        for m in self.mods:
            if m["id"] in want_enabled:
                result.append(dict(m, enabled=True))
            elif m["id"] in want_disabled:
                result.append(dict(m, enabled=False))
            else:
                result.append(dict(m))
        return result

    def _apply_baseline(self, enabled: List[str], disabled: List[str]) -> List[dict]:
        """Mods list with `enabled` on and `disabled` off; others unchanged."""
        want_enabled = set(enabled)
        want_disabled = set(disabled)
        result = []
        for m in self.mods:
            if m["id"] in want_enabled:
                result.append(dict(m, enabled=True))
            elif m["id"] in want_disabled:
                result.append(dict(m, enabled=False))
            else:
                result.append(dict(m))
        return result

    def _final_mods(self, found_culprits: List[str]) -> List[dict]:
        """Mods list for a confirmed finish: original enabled state minus the
        culprits. Built from _original_mods so skipped mods return to their
        starting state."""
        culprit_set = set(found_culprits)
        return [dict(m, enabled=(m.get("enabled", True) and m["id"] not in culprit_set)) for m in self._original_mods]

    def _mod_file_exists(self, mod_id: str) -> bool:
        return mod_manager.find_workshop_pak(self.server.steamcmd_dir, mod_id) is not None

    def _settle(self, outcome: BisectOutcome, round_cap_hit: bool = False) -> None:
        """The single exit path: stop the server, restore the world snapshot,
        write outcome.mods, and relaunch only if it was running before.
        Restoring even on success applies the fix to the untouched world."""
        if round_cap_hit:
            _log.warning(f"Auto-bisect hit its {self.MAX_ROUNDS}-round cap without converging; stopping.")
        self.progress.emit(BisectProgress(self._round_number, [], 0, "settling"))
        if process_manager.is_running(self.server.install_dir):
            process_manager.graceful_stop(self.server)
            self._wait_for_process_exit()
        restored = self._restore_saved_dir()
        if not restored:
            # The snapshot is now the only good copy: keep it, don't
            # relaunch, and fall back to the original mod list.
            note = (
                "The world save couldn't be put back to how it was before testing (the server may "
                "not have fully stopped). The original copy has been kept here so nothing is lost:\n\n"
                f"{self._saved_snapshot_dir}\n\nStop the server and copy those files back into "
                "ConanSandbox\\Saved, then start it again. The server was left stopped."
            )
            outcome.error = f"{outcome.error}\n\n{note}" if outcome.error else note
            outcome.mods = self._original_mods
        mod_manager.write_modlist(self.server.install_dir, self.server.steamcmd_dir, outcome.mods)
        self.mods = [dict(m) for m in outcome.mods]
        if self.was_running and restored:
            try:
                process_manager.launch(self.server)
            except (FileNotFoundError, OSError) as e:
                _log.error(f"Couldn't relaunch the server after auto-bisect finished: {e}")
        if restored:
            self._cleanup_snapshot()
        self.finished_bisect.emit(outcome)

    # --------------------------------------------------- world save safety --
    def _snapshot_saved_dir(self) -> Optional[str]:
        return mod_manager.snapshot_world_save(self.server.install_dir)

    def _restore_saved_dir(self) -> bool:
        """False if the restore failed; True if restored or nothing to restore."""
        return mod_manager.restore_world_save(self.server.install_dir, self._saved_snapshot_dir)

    @staticmethod
    def _world_save_files(saved_dir: str) -> List[str]:
        return mod_manager.world_save_files(saved_dir)

    def _cleanup_snapshot(self) -> None:
        mod_manager.cleanup_world_save_snapshot(self._saved_snapshot_dir)
        self._saved_snapshot_dir = None

    # ------------------------------------------------------------- rounds --
    def _restart_and_wait(self) -> bool:
        """Stop, reset the world save, write modlist.txt for self.mods,
        relaunch, and wait. True only if it answered and stayed up."""
        if process_manager.is_running(self.server.install_dir):
            process_manager.graceful_stop(self.server)
            self._wait_for_process_exit()

        if not self._restore_saved_dir():
            # Never test on an unreset world; run()'s handler settles.
            raise mod_manager.WorldSaveError(
                "Couldn't reset the world save between tests (the previous server process may not have exited)"
            )
        mod_manager.write_modlist(self.server.install_dir, self.server.steamcmd_dir, self.mods)

        result = self._launch_and_wait(STARTUP_TIMEOUT_SECONDS)
        if result == "timeout" and not self._cancel_requested:
            # Slow loads would otherwise permanently convict an innocent mod
            # in ddmin. Crashes aren't retried.
            self.progress.emit(BisectProgress(self._round_number, [], 0, "retrying"))
            if process_manager.is_running(self.server.install_dir):
                process_manager.graceful_stop(self.server)
                self._wait_for_process_exit()
            if not self._restore_saved_dir():
                raise mod_manager.WorldSaveError(
                    "Couldn't reset the world save between tests (the previous server process may not have exited)"
                )
            result = self._launch_and_wait(STARTUP_TIMEOUT_SECONDS * RETRY_TIMEOUT_FACTOR)
        return result == "up"

    def _launch_and_wait(self, timeout: float) -> str:
        """Launch and wait up to `timeout`. Returns "up", "crashed", "timeout",
        "cancelled", or "launch_failed"."""
        try:
            process_manager.launch(self.server)
        except (FileNotFoundError, OSError) as e:
            _log.warning(f"Auto-bisect round {self._round_number}: launch failed: {e}")
            return "launch_failed"

        self.progress.emit(BisectProgress(self._round_number, [], 0, "waiting"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._cancel_requested:
                return "cancelled"
            if not process_manager.is_running(self.server.install_dir):
                return "crashed"
            info = network_utils.query_a2s_info(self.server.bind_ip or "127.0.0.1", self.server.query_port, timeout=2.0)
            if info is not None:
                return "up" if self._confirm_stays_up() else "crashed"
            time.sleep(POLL_INTERVAL_SECONDS)
        return "timeout"  # hung, or still loading

    def _confirm_stays_up(self) -> bool:
        """After the first reply, keep re-querying for STAY_UP_CONFIRM_SECONDS;
        a hung-but-alive process must not pass."""
        self.progress.emit(BisectProgress(self._round_number, [], 0, "confirming"))
        deadline = time.monotonic() + STAY_UP_CONFIRM_SECONDS
        while time.monotonic() < deadline:
            if self._cancel_requested:
                return False
            if not process_manager.is_running(self.server.install_dir):
                return False
            info = network_utils.query_a2s_info(self.server.bind_ip or "127.0.0.1", self.server.query_port, timeout=2.0)
            if info is None:
                return False  # running but hung
            time.sleep(POLL_INTERVAL_SECONDS)
        return True

    def _wait_for_process_exit(self) -> None:
        """Backstop: a new server launched before the old one releases its
        ports can make a good mod look broken (or vice versa)."""
        deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not process_manager.is_running(self.server.install_dir):
                return
            time.sleep(0.5)
        _log.warning("Auto-bisect: the previous server process hadn't exited in time; proceeding anyway.")
