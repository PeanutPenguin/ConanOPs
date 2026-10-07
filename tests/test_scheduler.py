from __future__ import annotations

from datetime import datetime, time as dtime, timedelta

import pytest

import models
import scheduler


# --------------------------------------------------------------------- #
# Pure helper functions -- no QObject/QTimer involved.
# --------------------------------------------------------------------- #

def test_parse_hhmm_valid():
    assert scheduler._parse_hhmm("04:30") == dtime(4, 30)
    assert scheduler._parse_hhmm("23:59") == dtime(23, 59)


def test_parse_hhmm_invalid_falls_back_to_midnight():
    assert scheduler._parse_hhmm("garbage") == dtime(0, 0)
    assert scheduler._parse_hhmm("") == dtime(0, 0)
    assert scheduler._parse_hhmm(None) == dtime(0, 0)


def test_is_within_window_normal_range():
    start, end = dtime(2, 0), dtime(4, 0)
    assert scheduler.is_within_window(dtime(3, 0), start, end) is True
    assert scheduler.is_within_window(dtime(1, 59), start, end) is False
    assert scheduler.is_within_window(dtime(4, 0), start, end) is False  # end is exclusive
    assert scheduler.is_within_window(dtime(2, 0), start, end) is True   # start is inclusive


def test_is_within_window_crosses_midnight():
    start, end = dtime(23, 30), dtime(0, 30)
    assert scheduler.is_within_window(dtime(23, 45), start, end) is True
    assert scheduler.is_within_window(dtime(0, 15), start, end) is True
    assert scheduler.is_within_window(dtime(12, 0), start, end) is False


# --------------------------------------------------------------------- #
# Scheduler's due-logic -- these don't need a Qt event loop since we call
# the _check_* methods directly rather than going through the QTimer.
# --------------------------------------------------------------------- #

@pytest.fixture
def make_scheduler():
    def _make(online=False):
        state = {"saved": 0, "online": online}
        sch = scheduler.Scheduler(
            get_servers=lambda: [],
            is_online=lambda server: state["online"],
            on_state_changed=lambda: state.__setitem__("saved", state["saved"] + 1),
        )
        return sch, state
    return _make


def test_check_backup_fires_when_never_backed_up(make_scheduler):
    sch, state = make_scheduler()
    server = models.ServerConfig(name="t", backup_destination="/backups", backup_interval_hours=6)
    fired = []
    sch.backup_due.connect(lambda s: fired.append(s))

    sch._check_backup(server, datetime.now())

    assert len(fired) == 1
    # The scheduler itself no longer stamps last_backup_at -- the owner
    # does, and only once the backup has actually succeeded, so a
    # failed backup gets retried instead of waiting a full interval.
    assert server.last_backup_at == ""
    assert state["saved"] == 0


def test_check_backup_skips_when_no_destination_configured(make_scheduler):
    sch, state = make_scheduler()
    server = models.ServerConfig(name="t", backup_destination="", backup_interval_hours=6)
    fired = []
    sch.backup_due.connect(lambda s: fired.append(s))

    sch._check_backup(server, datetime.now())

    assert fired == []
    assert state["saved"] == 0


def test_check_backup_skips_before_interval_elapses(make_scheduler):
    sch, state = make_scheduler()
    now = datetime.now()
    server = models.ServerConfig(
        name="t", backup_destination="/backups", backup_interval_hours=6,
        last_backup_at=(now - timedelta(hours=1)).isoformat(),
    )
    fired = []
    sch.backup_due.connect(lambda s: fired.append(s))

    sch._check_backup(server, now)

    assert fired == []


def test_check_backup_fires_after_interval_elapses(make_scheduler):
    sch, state = make_scheduler()
    now = datetime.now()
    server = models.ServerConfig(
        name="t", backup_destination="/backups", backup_interval_hours=6,
        last_backup_at=(now - timedelta(hours=7)).isoformat(),
    )
    fired = []
    sch.backup_due.connect(lambda s: fired.append(s))

    sch._check_backup(server, now)

    assert len(fired) == 1


def test_check_restart_skips_when_disabled(make_scheduler):
    sch, state = make_scheduler()
    server = models.ServerConfig(name="t", restart_enabled=False)
    fired = []
    sch.restart_due.connect(lambda s: fired.append(s))

    sch._check_restart(server, datetime.now())

    assert fired == []


def test_check_restart_fires_inside_window_when_offline(make_scheduler):
    sch, state = make_scheduler(online=False)
    now = datetime(2026, 1, 1, 4, 30)  # inside a 4:00-5:00 window
    server = models.ServerConfig(name="t", install_dir="/srv", restart_enabled=True, restart_start="04:00", restart_end="05:00")
    fired = []
    sch.restart_due.connect(lambda s: fired.append(s))

    sch._check_restart(server, now)

    assert len(fired) == 1
    assert server.last_restart_date == "2026-01-01"


def test_check_restart_skips_when_someone_online(make_scheduler):
    sch, state = make_scheduler(online=True)
    now = datetime(2026, 1, 1, 4, 30)
    server = models.ServerConfig(name="t", install_dir="/srv", restart_enabled=True, restart_start="04:00", restart_end="05:00")
    fired = []
    skipped = []
    sch.restart_due.connect(lambda s: fired.append(s))
    sch.restart_skipped_online.connect(lambda s: skipped.append(s))

    sch._check_restart(server, now)

    assert fired == []
    assert len(skipped) == 1
    assert server.last_restart_date == ""  # not marked done -- can still fire later today if they leave


def test_check_restart_does_not_refire_same_day(make_scheduler):
    sch, state = make_scheduler(online=False)
    now = datetime(2026, 1, 1, 4, 30)
    server = models.ServerConfig(
        name="t", restart_enabled=True, restart_start="04:00", restart_end="05:00",
        last_restart_date="2026-01-01",
    )
    fired = []
    sch.restart_due.connect(lambda s: fired.append(s))

    sch._check_restart(server, now)

    assert fired == []  # already handled today


def test_check_restart_outside_window_does_nothing(make_scheduler):
    sch, state = make_scheduler(online=False)
    now = datetime(2026, 1, 1, 12, 0)
    server = models.ServerConfig(name="t", restart_enabled=True, restart_start="04:00", restart_end="05:00")
    fired = []
    sch.restart_due.connect(lambda s: fired.append(s))

    sch._check_restart(server, now)

    assert fired == []


def test_check_restart_skips_unconfigured_server(make_scheduler):
    sch, state = make_scheduler(online=False)
    now = datetime(2026, 1, 1, 4, 30)
    server = models.ServerConfig(name="t", install_dir="", restart_enabled=True, restart_start="04:00", restart_end="05:00")
    fired = []
    sch.restart_due.connect(lambda s: fired.append(s))

    sch._check_restart(server, now)

    assert fired == []
    assert server.last_restart_date == ""  # not stamped -- can fire later if it gets set up and started


def test_check_restart_skips_when_not_running(make_scheduler):
    state = {"saved": 0, "online": False}
    sch = scheduler.Scheduler(
        get_servers=lambda: [],
        is_online=lambda server: state["online"],
        on_state_changed=lambda: state.__setitem__("saved", state["saved"] + 1),
        is_running=lambda server: False,
    )
    now = datetime(2026, 1, 1, 4, 30)
    server = models.ServerConfig(name="t", install_dir="/srv", restart_enabled=True, restart_start="04:00", restart_end="05:00")
    fired = []
    sch.restart_due.connect(lambda s: fired.append(s))

    sch._check_restart(server, now)

    assert fired == []
    assert server.last_restart_date == ""


def test_check_auto_update_throttled_by_interval(make_scheduler):
    sch, state = make_scheduler()
    now = datetime.now()
    server = models.ServerConfig(
        name="t", auto_update=True, steamcmd_dir="/steamcmd",
        auto_update_check_interval_hours=6,
        last_update_check_at=(now - timedelta(hours=1)).isoformat(),
    )
    fired = []
    sch.update_check_due.connect(lambda s: fired.append(s))

    sch._check_auto_update(server, now)

    assert fired == []


def test_check_auto_update_fires_after_interval(make_scheduler):
    sch, state = make_scheduler()
    now = datetime.now()
    server = models.ServerConfig(
        name="t", auto_update=True, steamcmd_dir="/steamcmd",
        auto_update_check_interval_hours=6,
        last_update_check_at=(now - timedelta(hours=7)).isoformat(),
    )
    fired = []
    sch.update_check_due.connect(lambda s: fired.append(s))

    sch._check_auto_update(server, now)

    assert len(fired) == 1


def test_check_auto_update_skips_without_steamcmd_dir(make_scheduler):
    sch, state = make_scheduler()
    server = models.ServerConfig(name="t", auto_update=True, steamcmd_dir="")
    fired = []
    sch.update_check_due.connect(lambda s: fired.append(s))

    sch._check_auto_update(server, datetime.now())

    assert fired == []


def test_check_auto_update_backs_off_after_a_failed_attempt(make_scheduler):
    """A check that keeps failing (SteamCMD missing, network down)
    never gets last_update_check_at stamped -- without a separate
    attempt-level backoff, `due` would be True on every single tick
    (every 60 seconds) forever instead of easing off."""
    sch, state = make_scheduler()
    server = models.ServerConfig(name="t", auto_update=True, steamcmd_dir="/steamcmd", auto_update_check_interval_hours=6)
    fired = []
    sch.update_check_due.connect(lambda s: fired.append(s))

    now = datetime(2026, 1, 1, 12, 0, 0)
    sch._check_auto_update(server, now)  # first attempt: fires
    sch._check_auto_update(server, now + timedelta(seconds=60))  # next tick, still within backoff
    sch._check_auto_update(server, now + timedelta(seconds=120))

    assert len(fired) == 1

    # Once the backoff window has passed, it's allowed to retry.
    sch._check_auto_update(server, now + timedelta(seconds=301))
    assert len(fired) == 2


def test_check_auto_update_backoff_is_per_server(make_scheduler):
    sch, state = make_scheduler()
    a = models.ServerConfig(id="a", name="a", auto_update=True, steamcmd_dir="/steamcmd")
    b = models.ServerConfig(id="b", name="b", auto_update=True, steamcmd_dir="/steamcmd")
    fired = []
    sch.update_check_due.connect(lambda s: fired.append(s.id))

    now = datetime(2026, 1, 1, 12, 0, 0)
    sch._check_auto_update(a, now)
    sch._check_auto_update(b, now)  # a's backoff must not block b
    sch._check_auto_update(a, now + timedelta(seconds=5))  # still within a's own backoff

    assert fired == ["a", "b"]


def test_check_mod_refresh_throttled_by_interval(make_scheduler):
    sch, state = make_scheduler()
    now = datetime.now()
    server = models.ServerConfig(
        name="t", auto_update=True, steamcmd_dir="/steamcmd",
        mods=[{"id": "1", "name": "A", "enabled": True}],
        last_mod_check_at=(now - timedelta(hours=1)).isoformat(),
    )
    fired = []
    sch.mod_refresh_due.connect(lambda s: fired.append(s))

    sch._check_mod_refresh(server, now)

    assert fired == []


def test_check_mod_refresh_fires_after_interval(make_scheduler):
    sch, state = make_scheduler()
    now = datetime.now()
    server = models.ServerConfig(
        name="t", auto_update=True, steamcmd_dir="/steamcmd",
        mods=[{"id": "1", "name": "A", "enabled": True}],
        last_mod_check_at=(now - timedelta(hours=25)).isoformat(),
    )
    fired = []
    sch.mod_refresh_due.connect(lambda s: fired.append(s))

    sch._check_mod_refresh(server, now)

    assert len(fired) == 1


def test_check_mod_refresh_fires_on_first_ever_check(make_scheduler):
    sch, state = make_scheduler()
    server = models.ServerConfig(
        name="t", auto_update=True, steamcmd_dir="/steamcmd",
        mods=[{"id": "1", "name": "A", "enabled": True}],
    )
    fired = []
    sch.mod_refresh_due.connect(lambda s: fired.append(s))

    sch._check_mod_refresh(server, datetime.now())

    assert len(fired) == 1


def test_check_mod_refresh_skips_without_auto_update(make_scheduler):
    sch, state = make_scheduler()
    server = models.ServerConfig(
        name="t", auto_update=False, steamcmd_dir="/steamcmd",
        mods=[{"id": "1", "name": "A", "enabled": True}],
    )
    fired = []
    sch.mod_refresh_due.connect(lambda s: fired.append(s))

    sch._check_mod_refresh(server, datetime.now())

    assert fired == []


def test_check_mod_refresh_skips_without_steamcmd_dir(make_scheduler):
    sch, state = make_scheduler()
    server = models.ServerConfig(
        name="t", auto_update=True, steamcmd_dir="",
        mods=[{"id": "1", "name": "A", "enabled": True}],
    )
    fired = []
    sch.mod_refresh_due.connect(lambda s: fired.append(s))

    sch._check_mod_refresh(server, datetime.now())

    assert fired == []


def test_check_mod_refresh_skips_with_no_enabled_mods(make_scheduler):
    sch, state = make_scheduler()
    server = models.ServerConfig(
        name="t", auto_update=True, steamcmd_dir="/steamcmd",
        mods=[{"id": "1", "name": "A", "enabled": False}],
    )
    fired = []
    sch.mod_refresh_due.connect(lambda s: fired.append(s))

    sch._check_mod_refresh(server, datetime.now())

    assert fired == []


def test_check_mod_refresh_skips_with_no_mods_at_all(make_scheduler):
    sch, state = make_scheduler()
    server = models.ServerConfig(name="t", auto_update=True, steamcmd_dir="/steamcmd", mods=[])
    fired = []
    sch.mod_refresh_due.connect(lambda s: fired.append(s))

    sch._check_mod_refresh(server, datetime.now())

    assert fired == []


def test_check_mod_refresh_throttles_repeated_emission(make_scheduler):
    """Without a per-emission throttle, this would fire on every
    single tick (every 60s) for as long as MainWindow is still busy
    handling the previous one and hasn't stamped last_mod_check_at yet."""
    sch, state = make_scheduler()
    server = models.ServerConfig(
        name="t", auto_update=True, steamcmd_dir="/steamcmd",
        mods=[{"id": "1", "name": "A", "enabled": True}],
    )
    fired = []
    sch.mod_refresh_due.connect(lambda s: fired.append(s))

    now = datetime(2026, 1, 1, 12, 0, 0)
    sch._check_mod_refresh(server, now)
    sch._check_mod_refresh(server, now + timedelta(seconds=60))
    sch._check_mod_refresh(server, now + timedelta(seconds=120))

    assert len(fired) == 1

    sch._check_mod_refresh(server, now + timedelta(seconds=301))
    assert len(fired) == 2


def test_check_mod_refresh_throttle_is_per_server(make_scheduler):
    sch, state = make_scheduler()
    a = models.ServerConfig(id="a", name="a", auto_update=True, steamcmd_dir="/steamcmd", mods=[{"id": "1", "name": "A", "enabled": True}])
    b = models.ServerConfig(id="b", name="b", auto_update=True, steamcmd_dir="/steamcmd", mods=[{"id": "1", "name": "A", "enabled": True}])
    fired = []
    sch.mod_refresh_due.connect(lambda s: fired.append(s.id))

    now = datetime(2026, 1, 1, 12, 0, 0)
    sch._check_mod_refresh(a, now)
    sch._check_mod_refresh(b, now)  # a's throttle must not block b

    assert fired == ["a", "b"]


def test_tick_includes_mod_refresh_check(make_scheduler):
    """Confirms _check_mod_refresh is actually wired into the main
    per-minute tick, not just callable in isolation."""
    sch, state = make_scheduler()
    server = models.ServerConfig(
        name="t", install_dir="/srv", auto_update=True, steamcmd_dir="/steamcmd",
        mods=[{"id": "1", "name": "A", "enabled": True}],
    )
    sch.get_servers = lambda: [server]
    fired = []
    sch.mod_refresh_due.connect(lambda s: fired.append(s))

    sch._tick()

    assert len(fired) == 1


# -------------------------------------------------------------- is_locked --

def test_check_restart_skips_a_locked_server(make_scheduler):
    sch, state = make_scheduler()
    sch.is_running = lambda server: True
    sch.is_locked = lambda server: True
    now = datetime(2026, 1, 1, 4, 30)
    server = models.ServerConfig(name="t", install_dir="/srv", restart_enabled=True, restart_start="04:00", restart_end="05:00")
    fired = []
    sch.restart_due.connect(lambda s: fired.append(s))

    sch._check_restart(server, now)

    assert fired == []
    assert server.last_restart_date == ""  # not marked as handled -- should retry once unlocked


def test_check_restart_fires_normally_when_not_locked(make_scheduler):
    sch, state = make_scheduler()
    sch.is_running = lambda server: True
    sch.is_locked = lambda server: False
    now = datetime(2026, 1, 1, 4, 30)
    server = models.ServerConfig(name="t", install_dir="/srv", restart_enabled=True, restart_start="04:00", restart_end="05:00")
    fired = []
    sch.restart_due.connect(lambda s: fired.append(s))

    sch._check_restart(server, now)

    assert fired == [server]


def test_is_locked_defaults_to_never_locked(make_scheduler):
    """Existing callers/tests that don't pass is_locked at all must
    keep working exactly as before this was added."""
    sch, state = make_scheduler()
    sch.is_running = lambda server: True
    now = datetime(2026, 1, 1, 4, 30)
    server = models.ServerConfig(name="t", install_dir="/srv", restart_enabled=True, restart_start="04:00", restart_end="05:00")
    fired = []
    sch.restart_due.connect(lambda s: fired.append(s))

    sch._check_restart(server, now)

    assert fired == [server]


# ------------------------------------------- is_locked guards other jobs --

def test_check_backup_skips_a_locked_server(make_scheduler):
    sch, state = make_scheduler()
    sch.is_locked = lambda server: True
    server = models.ServerConfig(name="t", backup_destination="/backups", backup_interval_hours=6)
    fired = []
    sch.backup_due.connect(lambda s: fired.append(s))

    sch._check_backup(server, datetime.now())

    assert fired == []


def test_check_auto_update_skips_a_locked_server(make_scheduler):
    sch, state = make_scheduler()
    sch.is_locked = lambda server: True
    server = models.ServerConfig(name="t", auto_update=True, steamcmd_dir="/steamcmd")
    fired = []
    sch.update_check_due.connect(lambda s: fired.append(s))

    sch._check_auto_update(server, datetime.now())

    assert fired == []


def test_check_mod_refresh_skips_a_locked_server(make_scheduler):
    sch, state = make_scheduler()
    sch.is_locked = lambda server: True
    server = models.ServerConfig(
        name="t", auto_update=True, steamcmd_dir="/steamcmd",
        mods=[{"id": "1", "name": "A", "enabled": True}],
    )
    fired = []
    sch.mod_refresh_due.connect(lambda s: fired.append(s))

    sch._check_mod_refresh(server, datetime.now())

    assert fired == []


def test_check_backup_fires_normally_when_not_locked(make_scheduler):
    sch, state = make_scheduler()
    sch.is_locked = lambda server: False
    server = models.ServerConfig(name="t", backup_destination="/backups", backup_interval_hours=6)
    fired = []
    sch.backup_due.connect(lambda s: fired.append(s))

    sch._check_backup(server, datetime.now())

    assert fired == [server]
