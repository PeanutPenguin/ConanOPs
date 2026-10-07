"""
Mod list management.

ConanOps keeps its own ordered bookkeeping of mods (workshop id + display
name + enabled flag) on the ServerConfig, independent of the game's own
file format, and writes that out to `modlist.txt` in the server's Saved
folder whenever it changes.

Each Workshop mod's actual .pak file (inside
steamapps/workshop/content/440900/<workshop_id>/) is named whatever the
mod's AUTHOR named it -- never the workshop id itself (confirmed against
several independent server-hosting guides and real modlist.txt examples,
e.g. .../content/440900/1369802940/Emberlight.pak, not
.../1369802940/1369802940.pak). find_workshop_pak() below is what
actually looks inside that folder to find it; workshop_pak_path() is a
best-guess fallback for a mod that hasn't been downloaded yet at all (so
there's nothing real to find), kept only because modlist.txt needs SOME
line for a not-yet-downloaded enabled mod and a guessed path is no worse
than an empty one in that specific case -- it's simply wrong, and never
used once the real file exists.
"""
from __future__ import annotations

import glob
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import List, Optional

import applog

_log = applog.get_logger(__name__)

WORKSHOP_APP_ID = 440900  # Conan Exiles' Steam Workshop app id


def modlist_path(install_dir: str) -> str:
    return os.path.join(install_dir, "ConanSandbox", "Mods", "modlist.txt")


def workshop_pak_path(steamcmd_dir: str, workshop_id: str) -> str:
    return os.path.join(
        steamcmd_dir, "steamapps", "workshop", "content", str(WORKSHOP_APP_ID),
        workshop_id, f"{workshop_id}.pak",
    )


def find_workshop_pak(steamcmd_dir: str, workshop_id: str) -> Optional[str]:
    """Returns the REAL path to a downloaded Workshop mod's .pak file,
    or None if it hasn't actually been downloaded (or the folder
    exists but SteamCMD hasn't put a .pak in it yet). Looks at what's
    actually inside steamapps/workshop/content/440900/<workshop_id>/
    rather than assuming a filename, since the .pak there is named
    whatever the mod's author named it -- see this module's own
    docstring."""
    content_dir = os.path.join(steamcmd_dir, "steamapps", "workshop", "content", str(WORKSHOP_APP_ID), str(workshop_id))
    if not os.path.isdir(content_dir):
        return None
    # glob.escape: a folder name with [ ] in it (e.g. "Steam [backup]")
    # is otherwise read as a glob character class and matches nothing,
    # making every downloaded mod look missing.
    paks = glob.glob(os.path.join(glob.escape(content_dir), "*.pak"))
    if not paks:
        return None
    # Normally exactly one -- if a mod's folder somehow ever has more
    # than one .pak, pick deterministically (alphabetically) rather
    # than relying on the filesystem's unspecified listing order, so
    # repeated calls always agree with each other and with what
    # actually gets written to modlist.txt.
    return sorted(paks)[0]


def write_modlist(install_dir: str, steamcmd_dir: str, mods: List[dict]) -> None:
    """Writes modlist.txt with one path per *enabled* mod, in order.
    Disabled mods are simply omitted, not deleted from ConanOps'
    bookkeeping -- toggling one back on later just rewrites the file.

    Prefers the mod's REAL, actually-downloaded .pak path
    (find_workshop_pak) when it exists; falls back to the guessed
    workshop_pak_path() convention only for a mod that hasn't been
    downloaded yet, since modlist.txt needs some line for it and a
    guess is no worse than nothing there -- the server won't be able
    to load that mod either way until it's actually downloaded."""
    path = modlist_path(install_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [
        f"*{find_workshop_pak(steamcmd_dir, m['id']) or workshop_pak_path(steamcmd_dir, m['id'])}"
        for m in mods if m.get("enabled", True)
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


# ------------------------------------------------------ world save safety --
# Shared by both bisect tools (auto_bisect_runner.py's automated one and
# ui/mods_page.py's manual BisectDialog): protects the actual world data
# from being mutated by whatever mod configuration is loaded WHILE
# testing, so a mod's verdict depends on the mod itself, not on what an
# earlier, unrelated test happened to do to the save. Scoped to just the
# world save DATABASE files (see world_save_files) -- never the whole
# Saved folder, which also holds Config/ (the server's own .ini
# settings: bind IP, RCON, network ports, every gameplay setting) --
# reverting those between tests would silently undo anything that
# legitimately changed in between (most notably preflight's own auto-
# repair of a stale bind IP), which could make the server fail to start
# for a reason that has nothing to do with any mod at all.

def world_save_files(saved_dir: str) -> List[str]:
    base = glob.escape(saved_dir)  # see find_workshop_pak -- [ ] in a path would break the match
    return (
        glob.glob(os.path.join(base, "*.db"))
        + glob.glob(os.path.join(base, "*.db-wal"))
        + glob.glob(os.path.join(base, "*.db-shm"))
    )


class WorldSaveError(Exception):
    """A world-save snapshot or restore couldn't be completed. Distinct
    from snapshot_world_save() returning None, which only ever means
    "there's no world save to protect yet" -- a FAILED snapshot must
    never be mistaken for that, or callers would carry on testing
    against a world they have no way to put back."""


def snapshot_world_save(install_dir: str) -> Optional[str]:
    """Copies ONLY the world save database files to a fresh temp
    folder. Returns the temp folder's path, or None if there's no
    Saved folder yet, or no world save files in it yet, to protect.
    Raises WorldSaveError if there IS a world save but it couldn't be
    copied (the partial temp folder is removed first).
    Should only be called once the server is confirmed fully stopped
    -- copying a live database mid-write can capture a torn,
    inconsistent copy, which would then get restored later."""
    saved_dir = os.path.join(install_dir, "ConanSandbox", "Saved")
    if not os.path.isdir(saved_dir):
        return None
    db_files = world_save_files(saved_dir)
    if not db_files:
        return None
    snapshot_root = None
    try:
        snapshot_root = tempfile.mkdtemp(prefix="conanops-bisect-snapshot-")
        for path in db_files:
            shutil.copy2(path, os.path.join(snapshot_root, os.path.basename(path)))
        return snapshot_root
    except OSError as e:
        _log.error(f"Couldn't snapshot the world save: {e}")
        if snapshot_root:
            shutil.rmtree(snapshot_root, ignore_errors=True)
        raise WorldSaveError(f"Couldn't make a safety copy of the world save: {e}") from e


_RESTORE_STAGING_SUFFIX = ".conanops-restoring"


def restore_world_save(install_dir: str, snapshot_dir: Optional[str]) -> bool:
    """Resets the world save database files back to `snapshot_dir`
    (from an earlier snapshot_world_save() call). Never touches
    Config/ or anything else under Saved/. A no-op if snapshot_dir is
    None (nothing was ever snapshotted).

    Returns True if the restore happened (or there was nothing to
    restore), False if it failed -- most commonly because the server
    is still running and Windows won't let the live .db be replaced.
    Callers must keep the snapshot on False: it's the only good copy.

    Ordered so a failure can't leave the world with NO database, or
    with one database paired with another one's write-ahead log:
      1. Stage every snapshot file next to its destination under a
         temporary name (doesn't touch the live files at all).
      2. Remove every live -wal/-shm sidecar, plus any live .db the
         snapshot doesn't have -- SQLite replays a leftover -wal onto
         whatever .db sits next to it, so the current one must never
         survive next to the restored .db.
      3. os.replace() each staged .db over the live one (atomic per
         file -- the live .db is always either the old one or the
         restored one, never missing), then the staged sidecars.
    Staged leftovers are cleaned up if anything fails."""
    if not snapshot_dir:
        return True
    saved_dir = os.path.join(install_dir, "ConanSandbox", "Saved")
    staged: List[tuple] = []
    try:
        names = os.listdir(snapshot_dir)
        for name in names:
            dest = os.path.join(saved_dir, name)
            tmp = dest + _RESTORE_STAGING_SUFFIX
            shutil.copy2(os.path.join(snapshot_dir, name), tmp)
            staged.append((tmp, dest))
        for stale in world_save_files(saved_dir):
            if not stale.endswith(".db") or os.path.basename(stale) not in names:
                os.remove(stale)
        staged.sort(key=lambda pair: not pair[1].endswith(".db"))  # .db files first
        while staged:
            tmp, dest = staged[0]
            os.replace(tmp, dest)
            staged.pop(0)
        return True
    except OSError as e:
        _log.error(f"Couldn't restore the world save snapshot -- the world may reflect a partway test state: {e}")
        for tmp, _dest in staged:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False


def cleanup_world_save_snapshot(snapshot_dir: Optional[str]) -> None:
    if snapshot_dir:
        shutil.rmtree(snapshot_dir, ignore_errors=True)


def add_mod(mods: List[dict], workshop_id: str, name: str = "") -> List[dict]:
    if any(m["id"] == workshop_id for m in mods):
        return mods
    return mods + [{"id": workshop_id, "name": name or workshop_id, "enabled": True}]


def remove_mod(mods: List[dict], workshop_id: str) -> List[dict]:
    return [m for m in mods if m["id"] != workshop_id]


def move_mod(mods: List[dict], workshop_id: str, direction: int) -> List[dict]:
    """direction: -1 to move up (earlier load order), +1 to move down."""
    idx = next((i for i, m in enumerate(mods) if m["id"] == workshop_id), None)
    if idx is None:
        return mods
    new_idx = idx + direction
    if not (0 <= new_idx < len(mods)):
        return mods
    result = list(mods)
    result[idx], result[new_idx] = result[new_idx], result[idx]
    return result


def set_enabled(mods: List[dict], workshop_id: str, enabled: bool) -> List[dict]:
    return [dict(m, enabled=enabled) if m["id"] == workshop_id else m for m in mods]


# --------------------------------------------------------------------- #
# Guided bisect: binary-search through enabled mods to isolate the one
# causing a problem, instead of disabling them one at a time by hand.
# --------------------------------------------------------------------- #

@dataclass
class BisectState:
    remaining: List[str]     # candidate mod ids that might be the culprit
    known_good: List[str]    # mod ids cleared of suspicion this session
    done: bool = False
    culprit: Optional[str] = None

    @property
    def current_test_disabled(self) -> List[str]:
        """The ids to disable for the next test: the first half of the
        remaining candidates."""
        if len(self.remaining) <= 1:
            return list(self.remaining)
        half = len(self.remaining) // 2
        return self.remaining[:half]


def start_bisect(enabled_mod_ids: List[str]) -> BisectState:
    return BisectState(remaining=list(enabled_mod_ids), known_good=[])


def apply_bisect_test(mods: List[dict], state: BisectState) -> List[dict]:
    """Returns a NEW mods list with `enabled` set correctly for the
    CURRENT round of `state`: every mod that's actually part of this
    bisect (state.remaining or state.known_good) is enabled EXCEPT
    this round's current_test_disabled set; a mod that was never a
    candidate (already disabled before the bisect started, or added to
    the server since) is left exactly as it already is.

    Centralized here -- rather than each caller (the manual bisect
    dialog, the automatic bisect worker) re-deriving this itself --
    after an earlier version of that per-caller logic had a real bug:
    a mod that got disabled for one round's test, then cleared
    (known_good) because the problem persisted WITHOUT it, never got
    RE-enabled for the rest of that run. The rule that avoids that:
    "enabled" for a bisect-candidate mod is always simply "not in the
    CURRENT round's disable set" -- known_good and the untested half
    of remaining are the same thing here (both currently enabled,
    both exonerated of THIS round's suspicion), so there's no separate
    case to get wrong for either of them.
    """
    bisect_ids = set(state.remaining) | set(state.known_good)
    to_disable = set(state.current_test_disabled)
    return [
        dict(m, enabled=(m["id"] not in to_disable)) if m["id"] in bisect_ids else dict(m)
        for m in mods
    ]


def apply_bisect_result(mods: List[dict], state: BisectState) -> List[dict]:
    """Returns a NEW mods list for a FINISHED bisect: every mod that
    was part of it is enabled again, except the culprit (if one was
    found), which stays disabled. Mods that were never candidates are
    left as they are.

    apply_bisect_test() only describes the state for a round still to
    be run -- once report_result() marks the state done, the last
    round's disable set is stale. On a "ruled out" finish that left
    the final (now cleared) candidate disabled for good."""
    bisect_ids = set(state.remaining) | set(state.known_good)
    if state.culprit:
        bisect_ids.add(state.culprit)
    return [
        dict(m, enabled=(m["id"] != state.culprit)) if m["id"] in bisect_ids else dict(m)
        for m in mods
    ]


def ddmin(candidates: List[str]):
    """Delta-debugging (Zeller's ddmin), as a step-by-step coroutine:
    finds a minimal subset of `candidates` that still reproduces a
    failure when that subset alone is ENABLED (everything else
    disabled) -- correctly handling BOTH a single independent culprit
    AND a combination that only fails when multiple specific mods are
    enabled TOGETHER, unlike plain bisection above.

    Why plain bisection isn't enough: apply_bisect_test()'s rule --
    every candidate is enabled except the current round's target --
    is exactly right for isolating ONE culprit, but it's also exactly
    what traps a session whenever TWO OR MORE bad mods are candidates
    at once: whichever one isn't this round's target stays enabled and
    keeps the failure signal alive regardless of what else gets
    toggled, so the round's actual target gets wrongly cleared as
    innocent -- this isn't an occasional edge case, it happens on
    EVERY round for the rest of that session once it starts, and the
    session ends with no culprit found at all despite a real one
    being right there. ddmin avoids this because every test here
    directly enables an explicit CANDIDATE SUBSET (never "everything
    except one thing"), so a still-active second culprit can't hide
    inside an "everything else stays on" default the way it can with
    plain bisection.

    Written as a generator rather than a BisectState-style step
    object (see start_bisect/report_result above) because ddmin's
    control flow -- try each chunk, then each complement, change
    granularity, possibly loop back -- doesn't reduce to a single
    "disable this, check, continue" step the way plain bisection
    does. Each iteration yields the exact list of ids to ENABLE for
    the next test; the caller runs that test in the real world (or a
    mock, in tests) and sends back True if it still fails, False if
    it doesn't, via generator.send(). Ends by raising StopIteration
    whose `.value` is the minimal failing subset found -- callers
    typically drive this via a small helper like:

        gen = ddmin(candidates)
        to_test = next(gen)
        while True:
            try:
                to_test = gen.send(run_test(to_test))
            except StopIteration as stop:
                return stop.value

    Assumes testing the FULL `candidates` set already reproduces the
    failure -- that's the premise of calling this at all (same
    assumption start_bisect() already makes for plain bisection)."""
    c = list(candidates)
    n = 2
    while len(c) >= 2:
        chunk_size = max(1, -(-len(c) // n))  # ceil(len(c) / n)
        chunks = [chunk for chunk in (c[i:i + chunk_size] for i in range(0, len(c), chunk_size)) if chunk]

        reduced = False
        for chunk in chunks:
            still_fails = yield list(chunk)
            if still_fails:
                c = chunk
                n = 2
                reduced = True
                break
        if reduced:
            continue

        # With exactly two chunks, each chunk's complement IS the other
        # chunk -- both were just tested above, and every test here is
        # a real server restart, so testing them again as complements
        # would only repeat two restarts for nothing.
        for chunk in (chunks if len(chunks) > 2 else []):
            complement = [x for x in c if x not in chunk]
            if not complement:
                continue
            still_fails = yield complement
            if still_fails:
                c = complement
                n = max(n - 1, 2)
                reduced = True
                break
        if reduced:
            continue

        if n >= len(c):
            break  # can't split any finer -- c is as minimal as ddmin can make it
        n = min(n * 2, len(c))
    return c


def report_result(state: BisectState, problem_still_happens: bool) -> BisectState:
    """Call after the person restarts with `current_test_disabled` turned
    off and reports whether the problem is still happening.

    - Problem gone -> the culprit was among the disabled half.
    - Problem persists -> the culprit is in the other half (or isn't a
      mod at all, once we've narrowed to zero candidates)."""
    if state.done:
        return state

    if not state.remaining:
        # Started with no candidates at all (every mod already
        # disabled) -- nothing to narrow down.
        state.done = True
        state.culprit = None
        return state

    disabled = state.current_test_disabled
    other_half = [m for m in state.remaining if m not in disabled]

    if len(state.remaining) <= 1:
        # We were down to one candidate already.
        if problem_still_happens:
            state.known_good.extend(state.remaining)
            state.remaining = []
            state.done = True
            state.culprit = None  # not a mod, or already-known-good was wrong
        else:
            state.culprit = state.remaining[0]
            state.done = True
        return state

    if problem_still_happens:
        # Culprit is in the half that's still enabled.
        state.known_good.extend(disabled)
        state.remaining = other_half
    else:
        # Culprit was in the disabled half.
        state.known_good.extend(other_half)
        state.remaining = disabled

    if len(state.remaining) == 1 and not problem_still_happens:
        # Narrowed to one because disabling exactly that one mod fixed
        # it -- that IS the confirming test. Another round would just
        # repeat the identical configuration (same mod off, everything
        # else on) for another restart.
        state.culprit = state.remaining[0]
        state.done = True
    elif len(state.remaining) == 1:
        # Narrowed to one because the problem persisted with the OTHER
        # half off -- this one hasn't been tested disabled on its own
        # yet, so one more round is still needed.
        pass
    elif len(state.remaining) == 0:
        state.done = True
        state.culprit = None

    return state
