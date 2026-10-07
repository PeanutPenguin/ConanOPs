"""Mod list management, world-save snapshots for mod testing, and mod bisection.

ConanOps keeps an ordered mod list (id, name, enabled) on the ServerConfig and
writes it to modlist.txt. A Workshop mod's .pak is named by its author, not by
workshop id (e.g. content/440900/1369802940/Emberlight.pak), so
find_workshop_pak() looks in the folder; workshop_pak_path() is only a guess
for mods not downloaded yet.
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
    """Real path of a downloaded mod's .pak, or None if there isn't one yet."""
    content_dir = os.path.join(steamcmd_dir, "steamapps", "workshop", "content", str(WORKSHOP_APP_ID), str(workshop_id))
    if not os.path.isdir(content_dir):
        return None
    # Escape so "[ ]" in a path isn't read as a glob character class.
    paks = glob.glob(os.path.join(glob.escape(content_dir), "*.pak"))
    if not paks:
        return None
    # Sorted so the pick is stable if a folder ever has more than one .pak.
    return sorted(paks)[0]


def write_modlist(install_dir: str, steamcmd_dir: str, mods: List[dict]) -> None:
    """Writes modlist.txt with one .pak path per enabled mod, in order (real
    path if downloaded, else the guessed one)."""
    path = modlist_path(install_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [
        f"*{find_workshop_pak(steamcmd_dir, m['id']) or workshop_pak_path(steamcmd_dir, m['id'])}"
        for m in mods if m.get("enabled", True)
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + ("\n" if lines else ""))


# ------------------------------------------------------ world save safety --
# Used by both bisect tools so mod tests can't change the real world save.
# Only the .db files are snapshotted, never Config/: reverting the .ini files
# would undo legitimate changes such as preflight's bind-IP repair.

def world_save_files(saved_dir: str) -> List[str]:
    base = glob.escape(saved_dir)
    return (
        glob.glob(os.path.join(base, "*.db"))
        + glob.glob(os.path.join(base, "*.db-wal"))
        + glob.glob(os.path.join(base, "*.db-shm"))
    )


class WorldSaveError(Exception):
    """A snapshot or restore failed. Unlike a None snapshot ("nothing to
    protect"), callers must not keep testing after this."""


def snapshot_world_save(install_dir: str) -> Optional[str]:
    """Copies the world .db files to a temp folder and returns its path, or None
    if there's no save yet. Raises WorldSaveError on copy failure.
    Call only with the server fully stopped, or the copy may be torn."""
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
    """Puts the world .db files back from `snapshot_dir` (None = no-op). Returns
    False on failure (often: server still running); keep the snapshot then.

    Order keeps a failure from leaving no .db or a .db with a foreign -wal:
    stage copies beside the targets, delete live -wal/-shm (SQLite would replay
    them), then os.replace() the .db files first (atomic per file)."""
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


# ----------------------------- Guided bisect: binary-search for a bad mod --

@dataclass
class BisectState:
    remaining: List[str]     # candidate mod ids that might be the culprit
    known_good: List[str]    # mod ids cleared of suspicion this session
    done: bool = False
    culprit: Optional[str] = None

    @property
    def current_test_disabled(self) -> List[str]:
        """Ids to disable for the next test (first half of remaining)."""
        if len(self.remaining) <= 1:
            return list(self.remaining)
        half = len(self.remaining) // 2
        return self.remaining[:half]


def start_bisect(enabled_mod_ids: List[str]) -> BisectState:
    return BisectState(remaining=list(enabled_mod_ids), known_good=[])


def apply_bisect_test(mods: List[dict], state: BisectState) -> List[dict]:
    """New mods list for the current round: every bisect mod (remaining or
    known_good) is enabled except current_test_disabled; non-candidates are
    left as they are. Shared by the manual and automatic bisect tools."""
    bisect_ids = set(state.remaining) | set(state.known_good)
    to_disable = set(state.current_test_disabled)
    return [
        dict(m, enabled=(m["id"] not in to_disable)) if m["id"] in bisect_ids else dict(m)
        for m in mods
    ]


def apply_bisect_result(mods: List[dict], state: BisectState) -> List[dict]:
    """New mods list for a finished bisect: all bisect mods enabled except the
    culprit. Use this, not apply_bisect_test(), once the state is done."""
    bisect_ids = set(state.remaining) | set(state.known_good)
    if state.culprit:
        bisect_ids.add(state.culprit)
    return [
        dict(m, enabled=(m["id"] != state.culprit)) if m["id"] in bisect_ids else dict(m)
        for m in mods
    ]


def ddmin(candidates: List[str]):
    """Zeller's ddmin as a generator: finds a minimal subset of `candidates` that
    still fails when only it is enabled. Unlike plain bisection, this also finds
    culprits that only fail together (bisection's "all but one" tests let a
    second bad mod hide).

    Yields the ids to ENABLE for each test; send() back True if it still fails.
    The minimal subset is StopIteration.value. Assumes the full set fails."""
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

        # With two chunks the complements are the chunks already tested; skip
        # them, since each test is a real server restart.
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
            break  # can't split any finer
        n = min(n * 2, len(c))
    return c


def report_result(state: BisectState, problem_still_happens: bool) -> BisectState:
    """Updates `state` after a test with current_test_disabled turned off.
    Problem gone: culprit is in the disabled half; else in the other half."""
    if state.done:
        return state

    if not state.remaining:
        state.done = True
        state.culprit = None
        return state

    disabled = state.current_test_disabled
    other_half = [m for m in state.remaining if m not in disabled]

    if len(state.remaining) <= 1:
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
        state.known_good.extend(disabled)
        state.remaining = other_half
    else:
        state.known_good.extend(other_half)
        state.remaining = disabled

    if len(state.remaining) == 1 and not problem_still_happens:
        # Disabling exactly this mod fixed it; that was the confirming test.
        state.culprit = state.remaining[0]
        state.done = True
    elif len(state.remaining) == 1:
        # Not yet tested disabled on its own; one more round needed.
        pass
    elif len(state.remaining) == 0:
        state.done = True
        state.culprit = None

    return state
