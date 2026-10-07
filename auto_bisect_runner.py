"""
Automatic mod diagnosis for a server that won't start or hangs on
startup. Two modes:

- Quick Mod Check (find_all=False): tests each currently-enabled mod
  ALONE, one at a time (everything else disabled) -- N restarts for N
  mods. Finds every mod that's independently sufficient to cause the
  failure by itself. Fast and simple, but can't catch two mods that
  only NEED each other -- a dependency/library mod and something that
  requires it, or two mods that only break things when enabled
  TOGETHER -- since testing either one completely alone is exactly the
  situation that breaks a mod that needs its companion present. Find
  All Bad Mods below is what catches those.

- Find All Bad Mods (find_all=True): delta-debugging (mod_manager.ddmin
  -- see its own docstring) run repeatedly -- find one minimal failing
  set (a single mod, or a group that only fails together, including a
  dependency pair), remove it, sanity-test what's left, and if still
  broken, ddmin again over the rest. Slower, but correctly handles
  combinations (dependency-related or not) too.

Every run goes through the same three checks, in order, before either
mode does any actual hunting:

1. Preflight (preflight.run_preflight) -- if the install itself has a
   problem (missing exe, bad ports, etc.), that's not a mod question
   at all, and running dozens of restarts against a server that can't
   start for an unrelated reason would just convict mods for it.
2. Does the server actually fail with its CURRENT, full mod list right
   now? Without this, a server that happens to be fine at the moment
   (a prior timeout, a one-off hiccup) still gets bisected -- and
   because ddmin (and, to a lesser extent, testing mods one at a time)
   assumes the full set really does fail, if nothing ever fails during
   the run every mod eventually gets blamed for a problem that was
   never there. Confirmed against real simulated runs: 3, 4, 6, 8, and
   12 mod lists that were actually fine all got fully convicted before
   this check existed.
3. Does the server come up with EVERY candidate mod disabled? If it
   doesn't, the problem isn't mod-related in the first place, and
   neither mode can produce a meaningful answer from there -- every
   single test from that point on would report "still broken"
   regardless of what's enabled.

Before any of that, candidates whose Workshop .pak file isn't actually
present on disk are set aside and never tested at all (see
skipped_not_downloaded on BisectOutcome) -- a missing file makes Conan
fail to load that mod entry regardless of whether the mod itself is
fine, which would otherwise get blamed on the mod exactly like a real
bug in it. And if steamcmd_dir isn't even configured, this is
reported as its own distinct, plain failure (see no_steamcmd_dir) --
without SteamCMD to check against, literally every candidate would
otherwise look "not downloaded," which reads exactly like a clean
"every candidate ruled out" pass and hides that nothing was actually
tested at all.

Runs on a background QThread since one round alone means stopping a
process, launching a new one, and waiting up to several minutes for it
to prove it's healthy or not -- doing that on the UI thread would
freeze the whole app for the entire run.

Several more things every round does that are easy to get wrong and
were, in fact, gotten wrong here before this revision:

- The WORLD SAVE (specifically its database files -- see
  _world_save_files -- never anything under Config/, which is where
  the server's actual .ini settings live) gets reset to a snapshot
  taken right when this run started, before every single restart, and
  the run refuses to even take that snapshot until the server is
  confirmed fully stopped (copying a live database mid-write can
  capture a torn, inconsistent copy, which then gets restored before
  every subsequent round). Conan Exiles can delete a mod's placed
  items and buildings from the world the moment it loads without that
  mod present -- without resetting the save between tests, test 5
  might not even be looking at the same world test 1 saw. The
  snapshot is restored one final time when the run ends, however it
  ends (see _settle), so the real world reflects the pristine
  snapshot plus whatever mod list this run is actually reporting --
  never whatever an intermediate test happened to leave behind.
- The server is treated as OFF LIMITS to players for the run's entire
  duration, not just checked once before it starts -- see is_online
  below. A test's restart would disconnect anyone who joined mid-run,
  and the next world-save reset would erase whatever they built.
- The OLD server process is confirmed to have actually exited -- not
  just asked to stop -- before the NEXT one is launched, and a NEW
  process has to answer a status query AND keep answering for a
  confirmation window afterward, not just once, before its round
  counts as "came up." Skipping either of these can make a perfectly
  fine mod look broken (the new process fails to bind a port the old
  one hadn't released yet) or a genuinely broken one look fine (a
  process that crashes moments after its first reply still counted as
  healthy) -- and the confirmation window itself checks that the
  server is still actually ANSWERING queries, not just that its
  process object still exists (a hung-but-alive process used to pass).
- Hitting the round cap mid-search no longer convicts whatever was
  being tested at that exact moment -- an unfinished test is neither a
  confirmed culprit nor a cleared one, and reporting it as "found" was
  actively misleading (found_culprits is supposed to mean "confirmed,
  safe to act on"). It's reported as unresolved instead.

Deliberately never mutates the ServerConfig object it's given, other
than READING install_dir/steamcmd_dir/network settings (on the
assumption nothing else changes those mid-run, which is reasonable for
a person watching a multi-minute automated process rather than
editing other settings at the same time). It works entirely from its
own snapshot of the mods list and writes modlist.txt directly -- the
caller applies the FINAL result to server.mods once this worker
reports done, not anything mid-run, so there's no risk of this
background thread and the UI thread racing to write the same
in-memory object.
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

# How long to wait, after (re)launching, for the server to either come
# up and answer a status query or prove it's not going to. Generous --
# Conan Exiles can genuinely take a few minutes to finish loading even
# when nothing at all is wrong, and mistaking "still loading" for
# "still broken" would throw away a correct round's result.
STARTUP_TIMEOUT_SECONDS = 240
POLL_INTERVAL_SECONDS = 5

# After the FIRST successful status reply, keep checking for this much
# longer before trusting that the server is actually healthy -- a
# process that answers once and then crashes moments later (loading a
# broken mod can do exactly this) must not count as "came up fine."
STAY_UP_CONFIRM_SECONDS = 20

# A round that times out (still running, never answered -- as opposed
# to crashing) gets one retry with this much more time before it
# counts as a failure. See _restart_and_wait.
RETRY_TIMEOUT_FACTOR = 1.5

# How long to wait, after stopping the old process, for it to actually
# finish exiting before launching the next one. graceful_stop()/stop()
# in process_manager.py already wait for the common paths, but their
# hard-kill fallback historically returned immediately after issuing
# the kill without confirming it had actually taken effect -- this is
# a backstop regardless of what that layer already did.
PROCESS_EXIT_TIMEOUT_SECONDS = 10

# A distinct marker (not a real mod id) used internally to signal "the
# round cap was hit mid-test" up through the ddmin driver without
# confusing that with a genuine result -- see _drive_ddmin.
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
    # Preflight itself failed (missing exe, bad ports, etc.) -- nothing
    # was tested at all. mods is left completely untouched.
    preflight_problems: List[str] = field(default_factory=list)
    # The server came up FINE with its current, full mod list -- there
    # was nothing to reproduce, so nothing was tested. Not the same as
    # not_mod_related below (that means "confirmed broken, but not by
    # a mod"); this means "wasn't actually broken when checked."
    could_not_reproduce: bool = False
    # True if the very first check (everything disabled) STILL didn't
    # come up -- meaning the problem isn't a mod at all, and nothing
    # else in this run was attempted. mods/found_culprits are left at
    # their ORIGINAL, fully-restored state in this case.
    not_mod_related: bool = False
    # No SteamCMD folder configured at all -- nothing could be checked
    # for download status, so nothing was tested. Distinct from
    # skipped_not_downloaded (some configured, some missing) so this
    # never reads like a clean "every candidate ruled out" pass.
    no_steamcmd_dir: bool = False
    # All culprits found this run, in the order found, EVERY one
    # confirmed via a sanity restart -- never a guess from a round
    # that got interrupted (see unresolved_suspects for that). Quick
    # Mod Check reports zero or more (one per mod that failed on its
    # own); find_all can additionally report a whole group together
    # when a mod only fails as part of a combination (ddmin's minimal
    # failing SET was size 2+) -- this list doesn't distinguish
    # "independently bad" from "only bad together," just that all of
    # them are implicated. Already disabled on `mods` above; the
    # caller (the dialog) decides what to actually do with them
    # (delete, keep disabled, re-enable).
    found_culprits: List[str] = field(default_factory=list)
    # Candidates a run couldn't clear one way or the other -- either
    # the round cap interrupted an in-progress test (the pool includes
    # whatever was mid-test, unconfirmed either way), or (Quick Mod
    # Check only) every mod tested fine alone yet the full set still
    # fails, meaning a combination is involved that this mode can't
    # isolate. Empty on a clean, fully-resolved finish.
    unresolved_suspects: List[str] = field(default_factory=list)
    # Candidates whose Workshop .pak file wasn't actually present on
    # disk -- never tested at all, since a missing file would make
    # Conan fail to load that mod entry regardless of whether the mod
    # itself is fine. Go to the Mods tab and Download Mods first, then
    # run this again to actually test them.
    skipped_not_downloaded: List[str] = field(default_factory=list)
    # True if the run had to stop early because a player joined --
    # every test restarts the server, which isn't safe to do with
    # anyone connected. Whatever was found before that point (if
    # anything was confirmed) is still reported; nothing further ran.
    player_joined: bool = False


class AutoBisectWorker(QThread):
    progress = Signal(object)          # BisectProgress
    finished_bisect = Signal(object)   # BisectOutcome

    # A safety valve, mainly for find_all (a ddmin pass over a
    # combination culprit can cost meaningfully more than one test per
    # mod) but shared by both modes: caps total ROUNDS across the
    # whole run so a mod list with many genuinely bad mods, or a
    # pathological combination case, can't turn into an unbounded
    # number of multi-minute restarts with no end in sight.
    MAX_ROUNDS = 40

    def __init__(self, server: ServerConfig, mods_snapshot: List[dict], was_running: bool, find_all: bool = False,
                 is_online: Optional[Callable[[], bool]] = None, parent=None):
        super().__init__(parent)
        self.server = server
        self._original_mods = [dict(m) for m in mods_snapshot]
        self.mods = [dict(m) for m in mods_snapshot]  # working copy this run actually mutates
        self.was_running = was_running
        self.find_all = find_all
        # callable() -> bool, whether anyone is currently connected --
        # checked before EVERY round, not just once at the start (see
        # this module's own docstring). None (no callable given) is
        # treated as "never online," matching the pre-existing
        # behavior for callers that don't wire this up.
        self.is_online = is_online if is_online is not None else (lambda: False)
        self._cancel_requested = False
        self._round_number = 0
        self._saved_snapshot_dir: Optional[str] = None  # set by _snapshot_saved_dir(), used by _restore_saved_dir()
        # Live views of the current mode's progress, so a run stopped by
        # _blocked() (cancel / player joined) still reports what it had
        # already confirmed and what it skipped, instead of dropping it.
        self._found_culprits: List[str] = []
        self._missing: List[str] = []

    def cancel(self) -> None:
        """Thread-safe-ish: flips a flag the run loop polls between
        steps. A round already mid-wait still finishes checking (or
        times out) before stopping, rather than leaving the server
        mid-restart with nobody watching what state it's in."""
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
                # Everything enabled turned out to be undownloaded --
                # nothing this worker can actually test.
                self.finished_bisect.emit(BisectOutcome(mods=self.mods, skipped_not_downloaded=missing))
                return
            if missing:
                # Force these off for the rest of this run -- a
                # missing .pak file makes Conan choke on the broken
                # reference regardless of what else is enabled, which
                # would otherwise silently contaminate EVERY test this
                # run runs, not just a test of the missing mod itself.
                # _settle() puts them back to whatever state they
                # actually started in when this run ends.
                self.mods = self._apply_baseline([], missing)

            if self._blocked():
                return

            # Stop FIRST, then snapshot -- copying the world database
            # while the server is still running (was_running) can
            # capture a torn, mid-write copy, and that copy would then
            # be restored before every round and once more at the end.
            # The reproduce check below restarts it anyway, so this
            # costs nothing extra.
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
                # A world save exists but couldn't be copied -- testing
                # now would mean mutating the real world with no way to
                # put it back. Refuse rather than carry on unprotected.
                self._settle(BisectOutcome(
                    mods=self._original_mods,
                    error=f"{e}. Nothing was tested, so the world save was never put at risk.",
                    skipped_not_downloaded=missing,
                ))
                return

            # ------------------------------------------ reproduce check --
            # Confirm the problem is actually happening RIGHT NOW with
            # the full candidate set, before assuming it and searching
            # for a cause that isn't there. Without this, a server
            # that's actually fine gets bisected anyway, and since
            # every mode here assumes the full set really does fail,
            # nothing ever "fixes" anything during the run -- which
            # means every candidate eventually gets blamed.
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
                # STILL doesn't come up even with every candidate mod
                # disabled -- removing every possible mod-related
                # cause didn't fix it, so whatever's actually wrong
                # isn't one of these mods. Nothing further to test;
                # every subsequent test would just report "still
                # broken" no matter what's enabled, and blaming mods
                # for a non-mod problem is worse than useless.
                self._settle(BisectOutcome(mods=self._original_mods, not_mod_related=True, skipped_not_downloaded=missing))
                return
            # It DOES come up with every candidate disabled -- confirms
            # the problem really is one of these mods. Proceed.

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
                # Don't delete the world-save snapshot here: whether the
                # world was actually put back is unknown at this point,
                # so the snapshot may be the only good copy left.
                if self._saved_snapshot_dir:
                    e = f"{e}\n\nA copy of the world save from before testing was kept here:\n\n{self._saved_snapshot_dir}"
                # _original_mods, not self.mods: the caller applies
                # outcome.mods to the server (and rewrites modlist.txt
                # from it), and after a failed _settle the only safe
                # thing to apply is the list this run started from --
                # never whatever partial test subset self.mods is on.
                self.finished_bisect.emit(BisectOutcome(mods=self._original_mods, error=str(e)))

    def _blocked(self) -> bool:
        """Checked after every test: has this run been cancelled, or
        has someone joined? Either way, stop immediately rather than
        run even one more restart. Returns True (and has already
        emitted the final outcome) if the run should stop here.

        The mod list goes back to the original either way (nothing
        unconfirmed gets acted on), but culprits ALREADY confirmed
        this run are still reported, along with anything skipped for
        not being downloaded."""
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
        """Tests each candidate mod completely alone -- everything
        else disabled -- one restart per mod. Finds every mod that's
        independently sufficient to cause the failure. Immune to the
        "masking" problem that broke an earlier, since-removed version
        of this (which re-enabled every OTHER candidate each round):
        every test here explicitly enables exactly one mod and nothing
        else, so a second bad mod has nowhere to hide -- though see
        this module's own docstring for the trade-off that comes with
        that: a mod that NEEDS another mod present will also fail
        alone, indistinguishable here from actually being broken."""
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
            # Every mod tested fine alone -- the precondition checks
            # already confirmed a mod IS responsible, so this means
            # it's a combination this mode can't see (two or more mods
            # that only fail together, dependency-related or not).
            # Report the whole candidate pool as unresolved rather
            # than a false "nothing found."
            self._settle(BisectOutcome(mods=self._original_mods, unresolved_suspects=candidates, skipped_not_downloaded=missing))
            return

        # Final sanity check: confirm the server actually comes up
        # with just the found culprits removed and everything else
        # back on, rather than reporting an unverified result.
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
            # Removing every independently-bad mod STILL didn't fix
            # it -- a combination culprit is likely also present among
            # what's left, which this mode can't isolate.
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
                # whatever it was mid-testing is neither confirmed
                # guilty nor cleared, so it's unresolved, not "found."
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

            # Sanity round: are the culprits found SO FAR the whole
            # story, or is there at least one more bad mod hiding
            # among what's left? Enable every remaining candidate,
            # disable every found culprit, and see if it comes up.
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
                # Clean with just the found culprits removed --
                # nothing else in here is causing a problem.
                self._settle(BisectOutcome(
                    mods=self._final_mods(found_culprits), found_culprits=found_culprits, skipped_not_downloaded=missing,
                ))
                return
            # Still broken with everything else re-enabled -- at
            # least one more bad mod (or combo) remains among
            # `candidates`. Loop back and ddmin that remaining pool.

        self._settle(BisectOutcome(
            mods=self._final_mods(found_culprits), found_culprits=found_culprits, skipped_not_downloaded=missing,
        ))

    def _drive_ddmin(self, candidates: List[str], found_culprits: List[str]):
        """Runs one full ddmin pass over `candidates`, actually
        restarting the server for each test ddmin asks for. Returns
        (minimal_failing_set, hit_cap, stopped):
        - hit_cap: MAX_ROUNDS was reached mid-pass; the first element
          is meaningless and must NOT be treated as a confirmed result.
        - stopped: cancelled or a player joined; _blocked() has
          ALREADY settled and emitted the outcome, so the caller must
          just return. Decided by the same single _blocked() call that
          settled -- not a second is_online() check that could disagree
          with the first if someone leaves in between."""
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
        """The mods list for one test: exactly `enabled_subset`
        enabled among this pass's candidates, everything else in
        `all_candidates` disabled, and every already-found culprit
        from an EARLIER pass (this run's find-all loop) also disabled.
        A mod that was never a candidate at all (already disabled
        before this run started) is left exactly as it was. Used by
        both modes -- for Quick Mod Check, `enabled_subset` is always
        a single mod id."""
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
        """The mods list for a one-off explicit target state: every id
        in `enabled` turned on, every id in `disabled` turned off. A
        mod that's in neither list (not a candidate at all) is left
        exactly as it already is."""
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
        """The mods list to report/apply on a CONFIRMED, successful
        finish: every originally-enabled mod back on except the
        confirmed culprits, which stay off. Built from
        self._original_mods (not self.mods) so a mod that was forced
        off earlier for being undownloaded (see `missing` in run())
        ends up back at whatever it actually started as, not stuck
        disabled forever just because this run had to work around it."""
        culprit_set = set(found_culprits)
        return [dict(m, enabled=(m.get("enabled", True) and m["id"] not in culprit_set)) for m in self._original_mods]

    def _mod_file_exists(self, mod_id: str) -> bool:
        return mod_manager.find_workshop_pak(self.server.steamcmd_dir, mod_id) is not None

    def _settle(self, outcome: BisectOutcome, round_cap_hit: bool = False) -> None:
        """The ONE path every terminal branch funnels through. Stops
        whatever's running (confirming it's actually gone), resets the
        world save to this run's pristine starting snapshot, writes
        `outcome.mods` to modlist.txt, and relaunches if -- and only
        if -- self.was_running, regardless of whether this run found
        anything: the goal is always to put the server back to
        whatever OPERATING state it was in before this run started,
        with just the specific fix (if any) applied, not to leave it
        running because the last test happened to leave it running.

        Restoring the world save here even on a SUCCESS applies the
        confirmed fix against the untouched original world, rather
        than whatever incidental state the last test's brief uptime
        left behind -- and, just as importantly, means a later
        "re-enable anyway" choice re-adds that mod against a world
        that never actually loaded without it after this point, so its
        placed items and buildings were never actually at risk of
        being stripped by that later choice.

        round_cap_hit logs a warning (this is the one case where the
        run stopped without the person asking it to and without a
        clean resolution)."""
        if round_cap_hit:
            _log.warning(f"Auto-bisect hit its {self.MAX_ROUNDS}-round cap without converging; stopping.")
        self.progress.emit(BisectProgress(self._round_number, [], 0, "settling"))
        if process_manager.is_running(self.server.install_dir):
            process_manager.graceful_stop(self.server)
            self._wait_for_process_exit()
        restored = self._restore_saved_dir()
        if not restored:
            # The snapshot is now the ONLY good copy of the world --
            # never delete it, and don't relaunch onto a world that may
            # still reflect a test configuration. Fall back to the
            # original mod list so nothing unconfirmed gets applied.
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
        """True if the world save is back at the snapshot (or there was
        never one to restore); False if the restore failed."""
        return mod_manager.restore_world_save(self.server.install_dir, self._saved_snapshot_dir)

    @staticmethod
    def _world_save_files(saved_dir: str) -> List[str]:
        return mod_manager.world_save_files(saved_dir)

    def _cleanup_snapshot(self) -> None:
        mod_manager.cleanup_world_save_snapshot(self._saved_snapshot_dir)
        self._saved_snapshot_dir = None

    # ------------------------------------------------------------- rounds --
    def _restart_and_wait(self) -> bool:
        """Stops the server if running (and actually confirms it's
        exited, not just that stopping it was requested -- see
        _wait_for_process_exit), resets the world save to this run's
        snapshot, writes modlist.txt for the current test (self.mods,
        already updated by the caller), relaunches, and waits up to
        STARTUP_TIMEOUT_SECONDS for it to answer a status query AND
        keep answering for a confirmation window afterward (see
        STAY_UP_CONFIRM_SECONDS). Returns True only if it stayed up
        and kept responding; False if it crashed, hung, never started,
        or answered once and then died."""
        if process_manager.is_running(self.server.install_dir):
            process_manager.graceful_stop(self.server)
            self._wait_for_process_exit()

        if not self._restore_saved_dir():
            # Testing on a world that wasn't reset would let an earlier
            # round's damage decide this round's verdict -- and would
            # keep mutating the real world. Abort; run()'s handler
            # settles (which retries the restore, keeps the snapshot if
            # that fails too, and reports it).
            raise mod_manager.WorldSaveError(
                "Couldn't reset the world save between tests (the previous server process may not have exited)"
            )
        mod_manager.write_modlist(self.server.install_dir, self.server.steamcmd_dir, self.mods)

        result = self._launch_and_wait(STARTUP_TIMEOUT_SECONDS)
        if result == "timeout" and not self._cancel_requested:
            # Never answered, but never crashed either. A heavily
            # modded server can legitimately take longer than the
            # timeout to load, and ddmin treats every "still fails" as
            # permanent -- one slow start would convict an innocent
            # mod for good. Give a timed-out round exactly one more,
            # longer attempt (from a clean stop + world reset) before
            # believing it. Crashes aren't retried: those are real.
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
        """Launches with whatever modlist.txt already says and waits up
        to `timeout`. Returns "up" (answered and stayed up), "crashed"
        (process exited, or answered then died/hung), "timeout" (still
        running but never answered), "cancelled", or "launch_failed"."""
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
                return "crashed"  # crashed before ever answering
            info = network_utils.query_a2s_info(self.server.bind_ip or "127.0.0.1", self.server.query_port, timeout=2.0)
            if info is not None:
                return "up" if self._confirm_stays_up() else "crashed"
            time.sleep(POLL_INTERVAL_SECONDS)
        return "timeout"  # never answered within the timeout -- hung, or still loading

    def _confirm_stays_up(self) -> bool:
        """Called right after the FIRST successful status reply: keeps
        checking for STAY_UP_CONFIRM_SECONDS longer before actually
        trusting the round as healthy, by re-querying the status port
        (not just checking the process still exists -- a hung-but-
        still-running process used to pass this). A mod that crashes
        or hangs the server a few seconds after it starts answering
        queries -- which does happen -- would otherwise read as "came
        up fine," attributing the round's result to the wrong mod (or
        to none)."""
        self.progress.emit(BisectProgress(self._round_number, [], 0, "confirming"))
        deadline = time.monotonic() + STAY_UP_CONFIRM_SECONDS
        while time.monotonic() < deadline:
            if self._cancel_requested:
                return False
            if not process_manager.is_running(self.server.install_dir):
                return False  # answered once, then died -- not actually healthy
            info = network_utils.query_a2s_info(self.server.bind_ip or "127.0.0.1", self.server.query_port, timeout=2.0)
            if info is None:
                return False  # still running, but stopped answering -- hung, not healthy
            time.sleep(POLL_INTERVAL_SECONDS)
        return True

    def _wait_for_process_exit(self) -> None:
        """graceful_stop()/stop() in process_manager.py already wait
        for the process to exit in their normal paths, but their hard-
        kill fallback used to return immediately after issuing the
        kill without confirming it actually took effect. Launching a
        NEW server before the OLD one's process (and the ports it was
        holding) has actually gone can make a perfectly good mod look
        broken (the new process fails to bind) or a genuinely broken
        one look fine (the dying old process still answers the
        query). This is a backstop regardless of what that layer
        already does."""
        deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not process_manager.is_running(self.server.install_dir):
                return
            time.sleep(0.5)
        _log.warning("Auto-bisect: the previous server process hadn't exited in time; proceeding anyway.")
