from __future__ import annotations

import os

import ini_utils


def test_apply_known_keys_updates_existing_value(tmp_path):
    path = str(tmp_path / "Engine.ini")
    with open(path, "w") as f:
        f.write("[OnlineSubsystem]\nServerName=Old Name\nUnknownKey=untouched\n")

    ini_utils.apply_known_keys(path, {"ServerName": ("OnlineSubsystem", "New Name")})

    with open(path) as f:
        content = f.read()
    assert "ServerName=New Name" in content
    assert "UnknownKey=untouched" in content  # untouched lines survive byte-for-byte


def test_apply_known_keys_appends_missing_key_under_existing_section(tmp_path):
    path = str(tmp_path / "Engine.ini")
    with open(path, "w") as f:
        f.write("[OnlineSubsystem]\nServerName=Existing\n\n[URL]\nPort=7777\n")

    ini_utils.apply_known_keys(path, {"ServerPassword": ("OnlineSubsystem", "hunter2")})

    with open(path) as f:
        lines = f.read().splitlines()
    # New key lands inside [OnlineSubsystem], not appended past [URL].
    section_idx = lines.index("[OnlineSubsystem]")
    url_idx = lines.index("[URL]")
    pw_idx = next(i for i, l in enumerate(lines) if l.startswith("ServerPassword="))
    assert section_idx < pw_idx < url_idx


def test_apply_known_keys_creates_section_when_file_is_new(tmp_path):
    path = str(tmp_path / "nested" / "Engine.ini")
    ini_utils.apply_known_keys(path, {"ServerName": ("OnlineSubsystem", "Fresh Server")})
    assert os.path.exists(path)
    with open(path) as f:
        content = f.read()
    assert "[OnlineSubsystem]" in content
    assert "ServerName=Fresh Server" in content


def test_apply_known_keys_backs_up_existing_file(tmp_path):
    path = str(tmp_path / "Engine.ini")
    with open(path, "w") as f:
        f.write("[OnlineSubsystem]\nServerName=Old\n")

    ini_utils.apply_known_keys(path, {"ServerName": ("OnlineSubsystem", "New")})

    backups = [p for p in os.listdir(tmp_path) if p.endswith(".bak")]
    assert len(backups) == 1


def test_read_known_keys_only_matches_the_right_section(tmp_path):
    path = str(tmp_path / "Engine.ini")
    with open(path, "w") as f:
        f.write("[SectionA]\nPort=1111\n\n[SectionB]\nPort=2222\n")

    found = ini_utils.read_known_keys(path, {"Port": "SectionB"})
    assert found == {"Port": "2222"}


def test_read_known_keys_missing_file_returns_empty():
    assert ini_utils.read_known_keys("/nonexistent/path.ini", {"Port": "URL"}) == {}


def test_bool_ini_round_trip():
    assert ini_utils.ini_to_bool(ini_utils.bool_to_ini(True)) is True
    assert ini_utils.ini_to_bool(ini_utils.bool_to_ini(False)) is False
    assert ini_utils.ini_to_bool("1") is True
    assert ini_utils.ini_to_bool("yes") is True
    # `default` only applies when the value itself is None (key absent) --
    # an unrecognized-but-present string still just falls through to False.
    assert ini_utils.ini_to_bool(None, default=True) is True
    assert ini_utils.ini_to_bool("garbage", default=True) is False


def test_old_ini_backups_get_pruned(tmp_path, monkeypatch):
    """Each apply_known_keys() call makes a fresh timestamped .bak --
    repeated applies (every Settings tab, every RCON toggle, and so on
    over a server's lifetime) shouldn't leave an ever-growing pile of
    them behind, since they all get zipped into every backup archive."""
    path = str(tmp_path / "Engine.ini")
    with open(path, "w") as f:
        f.write("[OnlineSubsystem]\nServerName=Old\n")

    import itertools
    fake_now = itertools.count()

    class _FakeDatetime(ini_utils.datetime):
        @classmethod
        def now(cls):
            return ini_utils.datetime(2026, 1, 1, 0, 0, next(fake_now))

    monkeypatch.setattr(ini_utils, "datetime", _FakeDatetime)

    for i in range(ini_utils._MAX_INI_BACKUPS_PER_FILE + 3):
        ini_utils.apply_known_keys(path, {"ServerName": ("OnlineSubsystem", f"New{i}")})

    backups = [p for p in os.listdir(tmp_path) if p.endswith(".bak")]
    assert len(backups) == ini_utils._MAX_INI_BACKUPS_PER_FILE


def test_apply_known_keys_matches_section_and_key_case_insensitively(tmp_path):
    """Unreal's own config parser treats section/key names case-
    insensitively, and different sources (the game itself, Funcom's
    docs, ConanOps' own maps) don't agree on casing -- e.g. the game
    writes "[/Script/Engine.GameSession]" while ConanOps' map uses
    "/script/engine.gamesession". An existing key spelled with
    different casing than what's requested must still be recognized
    and updated in place, not duplicated under a second, differently-
    cased copy of the same section."""
    path = str(tmp_path / "Game.ini")
    with open(path, "w") as f:
        f.write("[/Script/Engine.GameSession]\nMaxPlayers=40\n")

    ini_utils.apply_known_keys(path, {"MaxPlayers": ("/script/engine.gamesession", "10")})

    with open(path) as f:
        content = f.read()
    assert content.count("[") == 1  # no duplicate section was appended
    assert "MaxPlayers=10" in content
    assert "MaxPlayers=40" not in content


def test_read_known_keys_matches_section_and_key_case_insensitively(tmp_path):
    path = str(tmp_path / "Game.ini")
    with open(path, "w") as f:
        f.write("[/Script/Engine.GameSession]\nMaxPlayers=40\n")

    found = ini_utils.read_known_keys(path, {"MaxPlayers": "/script/engine.gamesession"})
    assert found == {"MaxPlayers": "40"}


def test_backup_ini_does_not_clobber_a_same_second_backup(tmp_path, monkeypatch):
    """Two apply_known_keys() calls to the same file within the same
    wall-clock second (e.g. Network & Ports writing Engine.ini and
    Game.ini back to back) must not overwrite each other's backup --
    the whole point of the backup is to preserve the state from just
    before EACH change."""
    path = str(tmp_path / "Engine.ini")
    with open(path, "w") as f:
        f.write("[OnlineSubsystem]\nServerName=Original\n")

    class _FrozenDatetime(ini_utils.datetime):
        @classmethod
        def now(cls):
            return ini_utils.datetime(2026, 1, 1, 0, 0, 0)

    monkeypatch.setattr(ini_utils, "datetime", _FrozenDatetime)

    ini_utils.apply_known_keys(path, {"ServerName": ("OnlineSubsystem", "First")})
    ini_utils.apply_known_keys(path, {"ServerName": ("OnlineSubsystem", "Second")})

    backups = sorted(p for p in os.listdir(tmp_path) if p.endswith(".bak"))
    assert len(backups) == 2
    contents = [open(tmp_path / b).read() for b in backups]
    assert any("ServerName=Original" in c for c in contents)
    assert any("ServerName=First" in c for c in contents)
