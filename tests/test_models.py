from __future__ import annotations

import os

import models


def test_server_config_round_trip_basic():
    s = models.ServerConfig(name="My Server", game_port=7777, query_port=27015)
    d = s.to_dict()
    s2 = models.ServerConfig.from_dict(d)
    assert s2.name == "My Server"
    assert s2.game_port == 7777
    assert s2.query_port == 27015


def test_secret_fields_are_not_plaintext_on_disk():
    s = models.ServerConfig(name="t", password="hunter2", rcon_password="rconpw")
    d = s.to_dict()
    assert d["password"] != "hunter2"
    assert d["rcon_password"] != "rconpw"
    assert "hunter2" not in d["password"]


def test_secret_fields_round_trip():
    s = models.ServerConfig(
        name="t",
        password="hunter2",
        rcon_password="rconpw",
        webhook_discord_url="https://discord.example/abc",
        webhook_ntfy_url="https://ntfy.sh/mytopic",
    )
    s2 = models.ServerConfig.from_dict(s.to_dict())
    assert s2.password == "hunter2"
    assert s2.rcon_password == "rconpw"
    assert s2.webhook_discord_url == "https://discord.example/abc"
    assert s2.webhook_ntfy_url == "https://ntfy.sh/mytopic"


def test_legacy_plaintext_secrets_still_load():
    """Config files saved before secrets encryption existed should
    still load correctly (and get re-protected on next save)."""
    s = models.ServerConfig(name="t", password="hunter2")
    d = s.to_dict()
    d["password"] = "plainlegacy"  # simulate an old, unprotected config.json
    s2 = models.ServerConfig.from_dict(d)
    assert s2.password == "plainlegacy"


def test_empty_secret_fields_stay_empty():
    s = models.ServerConfig(name="t")
    d = s.to_dict()
    assert d["password"] == ""
    s2 = models.ServerConfig.from_dict(d)
    assert s2.password == ""


def test_admin_password_in_gameplay_dict_is_encrypted_at_rest():
    """AdminPassword lives in the nested `gameplay` dict (it's an
    ini_field_specs-driven field, not a top-level dataclass field), so
    it needs its own handling separate from _SECRET_FIELDS -- it's just
    as sensitive as rcon_password (grants in-game admin access) and
    shouldn't sit in config.json as plain text."""
    s = models.ServerConfig(name="t")
    s.gameplay["AdminPassword"] = "supersecret"
    d = s.to_dict()
    assert d["gameplay"]["AdminPassword"] != "supersecret"
    s2 = models.ServerConfig.from_dict(d)
    assert s2.gameplay["AdminPassword"] == "supersecret"


def test_app_config_add_server_respects_max():
    cfg = models.AppConfig()
    for i in range(models.MAX_SERVERS):
        assert cfg.add_server(f"Server {i}") is not None
    assert cfg.add_server("One Too Many") is None
    assert len(cfg.servers) == models.MAX_SERVERS


def test_app_config_remove_server():
    cfg = models.AppConfig()
    a = cfg.add_server("A")
    b = cfg.add_server("B")
    cfg.remove_server(a.id)
    assert [s.id for s in cfg.servers] == [b.id]


def test_used_ports_excludes_self():
    cfg = models.AppConfig()
    a = cfg.add_server("A")
    a.game_port, a.query_port = 7777, 27015
    b = cfg.add_server("B")
    b.game_port, b.query_port = 7778, 27016
    # A has RCON on (the default for new servers), so its RCON port counts too.
    assert cfg.used_ports(exclude_id=b.id) == {7777, 7778, 27015, a.rcon_port}
    assert cfg.used_ports() == {7777, 7778, 27015, 7779, 27016, a.rcon_port, b.rcon_port}
    assert a.rcon_port != b.rcon_port  # each new server gets its own RCON port


def test_used_install_dirs_normalizes_and_excludes_self():
    cfg = models.AppConfig()
    a = cfg.add_server("A")
    a.install_dir = "/srv/conan1"
    a.steamcmd_dir = "/srv/steamcmd1"
    b = cfg.add_server("B")
    b.install_dir = "/srv/conan2"
    b.steamcmd_dir = "/srv/steamcmd2"

    reserved = cfg.used_install_dirs(exclude_id=b.id)
    assert os.path.normcase(os.path.abspath("/srv/conan1")) in reserved
    assert os.path.normcase(os.path.abspath("/srv/conan2")) not in reserved


def test_used_install_dirs_does_not_include_steamcmd_dirs():
    """Only the install folder needs to stay unique per server -- the
    SteamCMD folder (tool + Workshop mod cache) is safe to share, so
    it must not show up as "reserved" and block a second server from
    reusing it."""
    cfg = models.AppConfig()
    a = cfg.add_server("A")
    a.install_dir = "/srv/conan1"
    a.steamcmd_dir = "/srv/shared-steamcmd"
    b = cfg.add_server("B")

    reserved = cfg.used_install_dirs(exclude_id=b.id)
    assert os.path.normcase(os.path.abspath("/srv/shared-steamcmd")) not in reserved


def test_default_steamcmd_dir_returns_an_existing_servers_folder():
    cfg = models.AppConfig()
    a = cfg.add_server("A")
    a.steamcmd_dir = "/srv/shared-steamcmd"
    b = cfg.add_server("B")

    assert cfg.default_steamcmd_dir(exclude_id=b.id) == "/srv/shared-steamcmd"


def test_default_steamcmd_dir_excludes_self_and_empty_values():
    cfg = models.AppConfig()
    a = cfg.add_server("A")  # steamcmd_dir left blank
    assert cfg.default_steamcmd_dir(exclude_id=a.id) == ""

    b = cfg.add_server("B")
    b.steamcmd_dir = "/srv/only-bs-own"
    # b is excluded, and a has none set -- nothing to default to
    assert cfg.default_steamcmd_dir(exclude_id=b.id) == ""


def test_used_server_names_excludes_self():
    cfg = models.AppConfig()
    a = cfg.add_server("Chudville")
    b = cfg.add_server("Second Server")

    assert cfg.used_server_names(exclude_id=b.id) == {"Chudville"}
    assert cfg.used_server_names(exclude_id=a.id) == {"Second Server"}


def test_app_lock_pin_lifecycle():
    cfg = models.AppConfig()
    assert cfg.app_lock_enabled is False
    assert cfg.verify_app_lock_pin("anything") is True  # nothing to unlock yet

    cfg.set_app_lock_pin("1234")
    assert cfg.app_lock_enabled is True
    assert cfg.verify_app_lock_pin("1234") is True
    assert cfg.verify_app_lock_pin("0000") is False

    cfg.clear_app_lock_pin()
    assert cfg.app_lock_enabled is False
    assert cfg.verify_app_lock_pin("1234") is True  # no PIN set -> always "verified"


def test_app_lock_pin_hash_never_stores_the_pin_itself():
    cfg = models.AppConfig()
    cfg.set_app_lock_pin("1234")
    assert "1234" not in cfg.app_lock_pin_hash
    assert cfg.app_lock_pin_hash != "1234"


def test_save_load_round_trip(tmp_path):
    path = str(tmp_path / "config.json")
    cfg = models.AppConfig()
    s = cfg.add_server("A")
    s.install_dir = "/srv/conan1"
    s.rcon_password = "secretpw"
    cfg.set_app_lock_pin("4242")
    cfg.web_control_enabled = True
    cfg.save(path)

    loaded = models.AppConfig.load(path)
    assert loaded.servers[0].install_dir == "/srv/conan1"
    assert loaded.servers[0].rcon_password == "secretpw"
    assert loaded.app_lock_enabled is True
    assert loaded.verify_app_lock_pin("4242") is True
    assert loaded.web_control_enabled is True


def test_web_control_enabled_defaults_to_false():
    assert models.AppConfig().web_control_enabled is False


def test_undecryptable_secret_is_preserved_not_wiped_on_next_save(monkeypatch):
    """If a secret field can't be decrypted on load (DPAPI unavailable
    or failed -- e.g. the config was copied to a different Windows
    user account, or a transient DPAPI error), the in-memory value is
    "" -- but saving again must NOT write that "" back out, since
    every save round-trips through secrets_store.protect() and would
    otherwise permanently destroy the real (still-encrypted, just
    unreadable in THIS run) secret."""
    import secrets_store

    def _always_fails(_stored):
        raise secrets_store.DecryptionError("simulated failure")

    raw_ciphertext = "dpapi:c29tZS1yZWFsLWNpcGhlcnRleHQ="
    monkeypatch.setattr(secrets_store, "unprotect", _always_fails)
    cfg = models.ServerConfig.from_dict({"id": "x", "name": "t", "rcon_password": raw_ciphertext})

    assert cfg.rcon_password == ""  # nothing readable in memory right now

    monkeypatch.undo()  # let protect()/unprotect() work normally again for to_dict()
    out = cfg.to_dict()
    assert out["rcon_password"] == raw_ciphertext  # untouched, not wiped to ""


def test_undecryptable_secret_is_overridden_once_a_new_value_is_set():
    """The moment something sets a real new value for a field that
    previously failed to decrypt, that new value must win on save --
    the raw-ciphertext preservation from the test above is only a
    fallback for "nothing has touched this field since the failure."""
    import secrets_store

    def _always_fails(_stored):
        raise secrets_store.DecryptionError("simulated failure")

    raw_ciphertext = "dpapi:c29tZS1yZWFsLWNpcGhlcnRleHQ="
    orig_unprotect = secrets_store.unprotect
    secrets_store.unprotect = _always_fails
    try:
        cfg = models.ServerConfig.from_dict({"id": "x", "name": "t", "rcon_password": raw_ciphertext})
    finally:
        secrets_store.unprotect = orig_unprotect

    cfg.rcon_password = "brand-new-password"
    out = cfg.to_dict()
    assert out["rcon_password"] != raw_ciphertext
    assert models.secrets_store.unprotect(out["rcon_password"]) == "brand-new-password"


def test_migrate_legacy_data_dir_finds_data_at_public_fallback_root(tmp_path, monkeypatch):
    """The scenario this whole change is for: someone already running
    a version where public_fallback_root() (Public\\ConanOps) was the
    default upgrades to one where no_space_root() now prefers
    app_install_dir() instead -- their existing config must still be
    found, not just the even-older ~/ConanOps legacy location."""
    import conanops_paths

    public_root = tmp_path / "public-conanops"
    public_root.mkdir()
    new = tmp_path / "new_root"

    monkeypatch.setattr(conanops_paths, "public_fallback_root", lambda: str(public_root))
    monkeypatch.setattr(models.AppConfig, "_legacy_data_dir", staticmethod(lambda: str(tmp_path / "does-not-exist")))
    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: str(new))

    (public_root / "config.json").write_text('{"servers": [{"id": "abc", "name": "Chudville"}]}')

    models.AppConfig._migrate_legacy_data_dir()

    assert (new / "config.json").exists()
    assert (public_root / "config.json").exists()  # original never removed


def test_migrate_legacy_data_dir_prefers_public_fallback_over_older_legacy(tmp_path, monkeypatch):
    """Both old locations have a config.json -- the more recent one
    (public_fallback_root) wins, since merging two different old
    setups would be far more confusing than picking one."""
    import conanops_paths

    public_root = tmp_path / "public-conanops"
    public_root.mkdir()
    (public_root / "config.json").write_text('{"servers": [{"id": "new", "name": "FromPublic"}]}')

    old_legacy = tmp_path / "old-legacy"
    old_legacy.mkdir()
    (old_legacy / "config.json").write_text('{"servers": [{"id": "old", "name": "FromOldLegacy"}]}')

    new = tmp_path / "new_root"

    monkeypatch.setattr(conanops_paths, "public_fallback_root", lambda: str(public_root))
    monkeypatch.setattr(models.AppConfig, "_legacy_data_dir", staticmethod(lambda: str(old_legacy)))
    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: str(new))

    models.AppConfig._migrate_legacy_data_dir()

    cfg_data = (new / "config.json").read_text()
    assert "FromPublic" in cfg_data
    assert "FromOldLegacy" not in cfg_data


def test_migrate_legacy_data_dir_copies_files_without_deleting_originals(tmp_path, monkeypatch):
    import conanops_paths

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    new = tmp_path / "new_root"  # deliberately doesn't exist yet

    monkeypatch.setattr(models.AppConfig, "_legacy_data_dir", staticmethod(lambda: str(legacy)))
    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: str(new))

    (legacy / "config.json").write_text('{"servers": []}')
    (legacy / "theme.json").write_text('{"bg": "#123456"}')
    (legacy / "sessions").mkdir()
    (legacy / "sessions" / "abc.json").write_text("[]")

    models.AppConfig._migrate_legacy_data_dir()

    assert (new / "config.json").exists()
    assert (new / "theme.json").exists()
    assert (new / "sessions" / "abc.json").exists()
    assert (legacy / "config.json").exists()  # original never removed


def test_migrate_legacy_data_dir_never_overwrites_an_existing_file_at_the_new_location(tmp_path, monkeypatch):
    import conanops_paths

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    new = tmp_path / "new_root"
    new.mkdir()

    monkeypatch.setattr(models.AppConfig, "_legacy_data_dir", staticmethod(lambda: str(legacy)))
    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: str(new))

    (legacy / "config.json").write_text('{"servers": ["old"]}')
    (new / "config.json").write_text('{"servers": ["already-here"]}')

    models.AppConfig._migrate_legacy_data_dir()

    assert (new / "config.json").read_text() == '{"servers": ["already-here"]}'


def test_migrate_legacy_data_dir_is_a_noop_with_no_legacy_folder(tmp_path, monkeypatch):
    import conanops_paths

    monkeypatch.setattr(models.AppConfig, "_legacy_data_dir", staticmethod(lambda: str(tmp_path / "does-not-exist")))
    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: str(tmp_path / "new_root"))

    models.AppConfig._migrate_legacy_data_dir()  # must not raise

    assert not (tmp_path / "new_root").exists()


def test_load_migrates_from_legacy_location_when_new_config_is_missing(tmp_path, monkeypatch):
    import conanops_paths

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    new = tmp_path / "new_root"

    monkeypatch.setattr(models.AppConfig, "_legacy_data_dir", staticmethod(lambda: str(legacy)))
    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: str(new))

    (legacy / "config.json").write_text('{"servers": [{"id": "abc", "name": "Chudville"}]}')

    cfg = models.AppConfig.load(str(new / "config.json"))

    assert len(cfg.servers) == 1
    assert cfg.servers[0].name == "Chudville"


def test_data_dir_uses_the_shared_no_space_root(monkeypatch):
    import conanops_paths

    monkeypatch.setattr(conanops_paths, "no_space_root", lambda: "/fake/root")
    monkeypatch.setattr(models.os, "makedirs", lambda *a, **k: None)

    assert models.AppConfig.data_dir() == "/fake/root"
