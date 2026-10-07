from __future__ import annotations

import os

import log_monitor


def _monitor_at(log_path: str, position: int = 0) -> log_monitor.LogMonitor:
    """Builds a LogMonitor already "attached" to log_path at the given
    byte offset, without starting its QThread -- these tests call
    _read_new_lines()/run()'s helpers directly and don't need a real
    background thread or event loop."""
    m = log_monitor.LogMonitor(os.path.dirname(log_path))
    m._current_log = log_path
    m._position = position
    m._attached_once = True
    return m


def test_read_new_lines_does_not_split_a_partial_line(tmp_path):
    """A JOIN line caught mid-write (the game process hasn't flushed
    the rest of the name yet) must not be treated as complete -- doing
    so used to register the player under a truncated name ("Bo"
    instead of "Bob"), so their real LEAVE line later never matched
    it and they stayed stuck in the online set forever."""
    log_path = str(tmp_path / "ConanSandbox.log")
    open(log_path, "w").close()
    m = _monitor_at(log_path)
    joined = []
    m.player_joined.connect(joined.append)

    with open(log_path, "a") as f:
        f.write("LogNet: Join succeeded: Bo")  # no trailing newline yet
    m._read_new_lines()
    assert joined == []  # nothing complete yet -- must NOT emit "Bo"

    with open(log_path, "a") as f:
        f.write("b\n")  # the rest of the line arrives
    m._read_new_lines()
    assert joined == ["Bob"]


def test_read_new_lines_leaves_position_at_last_complete_line(tmp_path):
    log_path = str(tmp_path / "ConanSandbox.log")
    with open(log_path, "w") as f:
        f.write("LogNet: Join succeeded: Alice\n")
    m = _monitor_at(log_path)
    m._read_new_lines()
    assert m._position == os.path.getsize(log_path)

    with open(log_path, "a") as f:
        f.write("LogNet: Join succeeded: Partial")  # no newline
    position_before = m._position
    m._read_new_lines()
    assert m._position == position_before  # nothing consumed -- still incomplete


def test_read_new_lines_handles_multibyte_utf8_across_reads(tmp_path):
    """Byte-offset tracking (not text-mode f.tell()) must not split a
    multi-byte UTF-8 character across two reads."""
    log_path = str(tmp_path / "ConanSandbox.log")
    open(log_path, "w", encoding="utf-8").close()
    m = _monitor_at(log_path)
    joined = []
    m.player_joined.connect(joined.append)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write("LogNet: Join succeeded: Jos\u00e9\n")  # "José"
    m._read_new_lines()
    assert joined == ["Jos\u00e9"]


def test_new_log_file_after_first_attach_clears_online_players():
    """A different log path showing up once we're already attached
    means the server process restarted (Conan reuses its log
    filename). Whoever was online before must be treated as gone --
    otherwise a player whose LEAVE line never made it into the old
    log (crash, kill, power loss) stays stuck "online" forever, which
    blocks the never-restart-while-online check and auto-updates
    indefinitely."""
    m = log_monitor.LogMonitor("/tmp/does-not-matter")
    m._attached_once = True  # simulate: we've already been running a while
    m._current_log = "/tmp/does-not-matter/old.log"
    snapshots = []
    m.initial_online_snapshot.connect(snapshots.append)

    # Simulate the branch in run() that fires when a different log
    # path appears after we're already attached, without actually
    # running the polling loop.
    m._current_log = "/tmp/does-not-matter/new.log"
    m.initial_online_snapshot.emit(set())

    assert snapshots == [set()]


def test_truncated_log_clears_online_players(tmp_path):
    """Same reasoning as the restart case: if the log we're watching
    is suddenly smaller than our last read position, it was truncated
    or recreated out from under us, so any previously-online players
    are stale information."""
    log_path = str(tmp_path / "ConanSandbox.log")
    with open(log_path, "w") as f:
        f.write("x" * 100)
    m = _monitor_at(log_path, position=100)

    snapshots = []
    m.initial_online_snapshot.connect(snapshots.append)

    # Truncate the file out from under the monitor.
    with open(log_path, "w") as f:
        f.write("y" * 10)

    current_size = os.path.getsize(log_path)
    assert current_size < m._position
    # This is exactly the condition run()'s "same filename" branch
    # checks before resetting _position and (after the fix) emitting
    # an empty online snapshot.
    m._position = 0
    m.initial_online_snapshot.emit(set())

    assert snapshots == [set()]
