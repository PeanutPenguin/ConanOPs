from __future__ import annotations

import os

import mod_manager


def _make_content_dir(steamcmd_dir, workshop_id):
    content_dir = os.path.join(steamcmd_dir, "steamapps", "workshop", "content", str(mod_manager.WORKSHOP_APP_ID), workshop_id)
    os.makedirs(content_dir, exist_ok=True)
    return content_dir


# --------------------------------------------------------- find_workshop_pak --

def test_find_workshop_pak_finds_a_real_authored_filename(tmp_path):
    """The actual bug this exists to fix: the .pak is named whatever
    the mod's AUTHOR named it, never the workshop id itself."""
    steamcmd_dir = str(tmp_path)
    content_dir = _make_content_dir(steamcmd_dir, "1369802940")
    pak_path = os.path.join(content_dir, "Emberlight.pak")
    open(pak_path, "w").close()

    result = mod_manager.find_workshop_pak(steamcmd_dir, "1369802940")

    assert result == pak_path


def test_find_workshop_pak_none_when_folder_does_not_exist(tmp_path):
    assert mod_manager.find_workshop_pak(str(tmp_path), "123") is None


def test_find_workshop_pak_none_when_folder_exists_but_empty(tmp_path):
    """SteamCMD can create the numbered folder before it's actually
    finished placing the .pak inside it, or a download can partially
    fail -- an empty folder must not read as "downloaded.\""""
    steamcmd_dir = str(tmp_path)
    _make_content_dir(steamcmd_dir, "123")

    assert mod_manager.find_workshop_pak(steamcmd_dir, "123") is None


def test_find_workshop_pak_ignores_non_pak_files(tmp_path):
    steamcmd_dir = str(tmp_path)
    content_dir = _make_content_dir(steamcmd_dir, "123")
    open(os.path.join(content_dir, "readme.txt"), "w").close()

    assert mod_manager.find_workshop_pak(steamcmd_dir, "123") is None


def test_find_workshop_pak_deterministic_with_multiple_paks(tmp_path):
    """Shouldn't normally happen, but must behave predictably (not
    depend on filesystem listing order) if it ever does."""
    steamcmd_dir = str(tmp_path)
    content_dir = _make_content_dir(steamcmd_dir, "123")
    open(os.path.join(content_dir, "Zebra.pak"), "w").close()
    open(os.path.join(content_dir, "Apple.pak"), "w").close()

    result = mod_manager.find_workshop_pak(steamcmd_dir, "123")

    assert result == os.path.join(content_dir, "Apple.pak")


# ------------------------------------------------------------- write_modlist --

def test_write_modlist_uses_the_real_pak_filename_when_downloaded(tmp_path):
    install_dir = str(tmp_path / "install")
    steamcmd_dir = str(tmp_path / "steamcmd")
    content_dir = _make_content_dir(steamcmd_dir, "1369802940")
    real_pak = os.path.join(content_dir, "Emberlight.pak")
    open(real_pak, "w").close()

    mod_manager.write_modlist(install_dir, steamcmd_dir, [{"id": "1369802940", "name": "Emberlight", "enabled": True}])

    with open(mod_manager.modlist_path(install_dir)) as f:
        content = f.read()
    assert content.strip() == f"*{real_pak}"
    assert "1369802940.pak" not in content  # the old, wrong guessed filename


def test_write_modlist_falls_back_to_guessed_path_when_not_downloaded(tmp_path):
    """A mod that isn't downloaded yet still needs SOME line in
    modlist.txt -- the guess is wrong, but the server can't load it
    either way until it's actually downloaded, so this is no worse
    than before and gives a stable line to write."""
    install_dir = str(tmp_path / "install")
    steamcmd_dir = str(tmp_path / "steamcmd")

    mod_manager.write_modlist(install_dir, steamcmd_dir, [{"id": "999", "name": "NotYetDownloaded", "enabled": True}])

    with open(mod_manager.modlist_path(install_dir)) as f:
        content = f.read()
    assert content.strip() == f"*{mod_manager.workshop_pak_path(steamcmd_dir, '999')}"


def test_write_modlist_mixes_real_and_guessed_paths_correctly(tmp_path):
    install_dir = str(tmp_path / "install")
    steamcmd_dir = str(tmp_path / "steamcmd")
    content_dir = _make_content_dir(steamcmd_dir, "1")
    real_pak = os.path.join(content_dir, "RealMod.pak")
    open(real_pak, "w").close()

    mod_manager.write_modlist(install_dir, steamcmd_dir, [
        {"id": "1", "name": "Downloaded", "enabled": True},
        {"id": "2", "name": "NotDownloaded", "enabled": True},
    ])

    with open(mod_manager.modlist_path(install_dir)) as f:
        lines = f.read().splitlines()
    assert lines[0] == f"*{real_pak}"
    assert lines[1] == f"*{mod_manager.workshop_pak_path(steamcmd_dir, '2')}"


def test_write_modlist_skips_disabled_mods(tmp_path):
    install_dir = str(tmp_path / "install")
    steamcmd_dir = str(tmp_path / "steamcmd")

    mod_manager.write_modlist(install_dir, steamcmd_dir, [
        {"id": "1", "name": "On", "enabled": True},
        {"id": "2", "name": "Off", "enabled": False},
    ])

    with open(mod_manager.modlist_path(install_dir)) as f:
        content = f.read()
    assert "1" in content
    # crude but sufficient: the disabled mod's guessed path shouldn't appear at all
    assert mod_manager.workshop_pak_path(steamcmd_dir, "2") not in content
