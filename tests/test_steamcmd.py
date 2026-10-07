from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

import steamcmd


def _write_manifest(tmp_path, state_flags=4, buildid="1000001"):
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    manifest = steamapps / f"appmanifest_{steamcmd.APP_ID}.acf"
    manifest.write_text(
        '"AppState"\n{\n'
        f'\t"appid"\t\t"{steamcmd.APP_ID}"\n'
        f'\t"StateFlags"\t\t"{state_flags}"\n'
        f'\t"buildid"\t\t"{buildid}"\n'
        "}\n"
    )
    return str(tmp_path)


def test_get_install_state_reads_manifest(tmp_path):
    install_dir = _write_manifest(tmp_path, state_flags=4)
    assert steamcmd.get_install_state(install_dir) == 4


def test_get_install_state_missing_manifest_returns_none(tmp_path):
    assert steamcmd.get_install_state(str(tmp_path)) is None


def test_get_installed_buildid_reads_manifest(tmp_path):
    install_dir = _write_manifest(tmp_path, buildid="7654321")
    assert steamcmd.get_installed_buildid(install_dir) == "7654321"


def test_extract_public_buildid_picks_public_branch_not_beta():
    vdf = """
    "branches"
    {
        "public"
        {
            "buildid"      "1000001"
            "timeupdated"  "1700000000"
        }
        "beta"
        {
            "buildid"      "9999999"
            "timeupdated"  "1700000001"
        }
    }
    """
    assert steamcmd._extract_public_buildid(vdf) == "1000001"


def test_extract_public_buildid_falls_back_when_no_public_block():
    vdf = '"buildid"    "4242424"\n'
    assert steamcmd._extract_public_buildid(vdf) == "4242424"


def test_extract_public_buildid_returns_none_for_empty_text():
    assert steamcmd._extract_public_buildid("") is None


def test_download_workshop_items_no_steamcmd(tmp_path):
    result = steamcmd.download_workshop_items(str(tmp_path), ["123"])
    assert result.success is False
    assert "not installed" in result.output.lower() or "steamcmd" in result.output.lower()


def test_download_workshop_items_empty_list_is_a_noop(tmp_path):
    (tmp_path / "steamcmd.exe").write_text("fake")
    result = steamcmd.download_workshop_items(str(tmp_path), [])
    assert result.success is True


def test_download_workshop_items_fails_when_folder_exists_but_no_pak(monkeypatch, tmp_path):
    """The actual bug: SteamCMD can create the numbered content folder
    without ever placing a .pak inside it (a partial/failed download),
    which used to read as "downloaded" since only the directory's
    existence was checked."""
    (tmp_path / "steamcmd.exe").write_text("fake")
    content_dir = tmp_path / "steamapps" / "workshop" / "content" / str(steamcmd.WORKSHOP_APP_ID) / "123"
    content_dir.mkdir(parents=True)  # the folder exists...

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "", "")  # ...and SteamCMD reports success...
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = steamcmd.download_workshop_items(str(tmp_path), ["123"])

    assert result.success is False  # ...but there's no actual .pak file in it
    assert "123" in result.output


def test_download_workshop_items_succeeds_with_a_real_pak_present(monkeypatch, tmp_path):
    (tmp_path / "steamcmd.exe").write_text("fake")
    content_dir = tmp_path / "steamapps" / "workshop" / "content" / str(steamcmd.WORKSHOP_APP_ID) / "123"
    content_dir.mkdir(parents=True)
    (content_dir / "SomeAuthoredModName.pak").write_text("fake pak data")  # named by the AUTHOR, not the workshop id

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = steamcmd.download_workshop_items(str(tmp_path), ["123"])

    assert result.success is True


def test_parse_progress_percent_extracts_the_number():
    line = "Update state (0x61) downloading, progress: 45.32 (390911489 / 862992488)"
    assert steamcmd.parse_progress_percent(line) == 45.32


def test_parse_progress_percent_returns_none_for_non_progress_lines():
    assert steamcmd.parse_progress_percent("Success! App '443030' fully installed.") is None
    assert steamcmd.parse_progress_percent("") is None


def _fake_process_script(tmp_path, lines, sleep_between=0.05, exit_code=0):
    """Writes a small Python script that prints `lines` one at a time
    with a real delay between each, so _run_cancelable is exercised
    against a genuinely running subprocess rather than a mock -- the
    thing being tested here is real-time streaming, which a mocked
    Popen can't meaningfully verify."""
    script = tmp_path / "fake_steamcmd.py"
    script.write_text(
        "import time, sys\n"
        f"for line in {lines!r}:\n"
        "    print(line)\n"
        "    sys.stdout.flush()\n"
        f"    time.sleep({sleep_between})\n"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, str(script)]


def test_run_cancelable_streams_lines_live_not_only_at_the_end(tmp_path):
    """The core fix for downloads looking "stalled": on_line() must be
    called as each line is produced, not all at once only after the
    whole process has already exited."""
    args = _fake_process_script(tmp_path, ["progress: 25", "progress: 50", "progress: 100"], sleep_between=0.15)
    seen = []
    start = time.time()
    proc = steamcmd._run_cancelable(args, timeout=10, on_line=lambda l: seen.append((time.time() - start, l)))

    assert proc.returncode == 0
    assert [l for _, l in seen] == ["progress: 25", "progress: 50", "progress: 100"]
    # The first line must have arrived well before the process finished
    # (~0.45s total) -- if on_line were only called at the very end (the
    # bug this replaces), its timestamp would be indistinguishable from
    # the last one instead of arriving ~0.15s in.
    assert seen[0][0] < 0.3


def test_run_cancelable_merges_stdout_and_stderr():
    proc = steamcmd._run_cancelable([sys.executable, "-c", "print('hello')"], timeout=10)
    assert "hello" in proc.stdout
    assert proc.stderr == ""


def test_run_cancelable_cancels_promptly_not_after_the_full_timeout(tmp_path):
    args = _fake_process_script(tmp_path, [f"progress: {i}" for i in range(100)], sleep_between=0.2)
    start = time.time()
    proc = steamcmd._run_cancelable(args, timeout=30, should_cancel=lambda: time.time() - start > 0.5)
    elapsed = time.time() - start

    assert elapsed < 5  # nowhere near the full 30s timeout or the ~20s full run
    assert proc.returncode != 0


def test_run_cancelable_raises_timeout_expired(tmp_path):
    args = _fake_process_script(tmp_path, ["progress: 1"], sleep_between=5)
    with pytest.raises(subprocess.TimeoutExpired):
        steamcmd._run_cancelable(args, timeout=0.3, poll_interval=0.1)


def test_update_server_reports_progress_percent_and_throttles_log_lines(monkeypatch, tmp_path):
    """update_server()'s on_progress_percent callback should fire for
    every parsed percentage (drives the real progress bar), while the
    text log should only get one line per whole-percent change instead
    of every single SteamCMD progress line (which can print several a
    second) -- otherwise the fix for a "stalled-looking" download would
    just trade it for a flooded, unreadable log."""
    exe = tmp_path / "steamcmd.exe"
    exe.write_text("fake")
    install_dir = str(tmp_path / "install")

    fake_lines = [
        "progress: 10.00", "progress: 10.40", "progress: 10.90",  # all round to 10 -- only first should log
        "progress: 11.00",  # rounds to 11 -- should log
        "Success! App fully installed.",
    ]

    def fake_run_cancelable(args, timeout, should_cancel=None, on_line=None, poll_interval=0.25):
        for line in fake_lines:
            on_line(line)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(steamcmd, "_run_cancelable", fake_run_cancelable)
    monkeypatch.setattr(steamcmd, "get_install_state", lambda d: 4)
    monkeypatch.setattr(steamcmd, "get_installed_buildid", lambda d: "123")

    percents = []
    logged = []
    result = steamcmd.update_server(
        str(tmp_path), install_dir,
        progress=logged.append,
        on_progress_percent=percents.append,
    )

    assert result.success is True
    assert percents == [10.00, 10.40, 10.90, 11.00]  # every parsed percentage reaches the progress bar
    progress_logs = [line for line in logged if line.startswith("Downloading...")]
    assert progress_logs == ["Downloading... 10%", "Downloading... 11%"]  # but only 2 lines hit the text log


def test_update_server_forces_windows_platform_type(monkeypatch, tmp_path):
    """Every published Conan Exiles server setup guide includes
    +@sSteamCmdForcePlatformType windows, and its absence is a
    documented cause of app_update failing with "ERROR! Failed to
    install app '443030' (Missing configuration)" even when the rest
    of the command is otherwise correct."""
    exe = tmp_path / "steamcmd.exe"
    exe.write_text("fake")
    seen_args = []

    def fake_run_cancelable(args, timeout, should_cancel=None, on_line=None, poll_interval=0.25):
        seen_args.append(args)
        return subprocess.CompletedProcess(args, 0, "Success!", "")

    monkeypatch.setattr(steamcmd, "_run_cancelable", fake_run_cancelable)
    monkeypatch.setattr(steamcmd, "get_install_state", lambda d: 4)
    monkeypatch.setattr(steamcmd, "get_installed_buildid", lambda d: "123")

    steamcmd.update_server(str(tmp_path), str(tmp_path / "install"))

    assert len(seen_args) == 1
    args = seen_args[0]
    idx = args.index("+@sSteamCmdForcePlatformType")
    assert args[idx + 1] == "windows"


def test_install_steamcmd_bootstrap_forces_windows_platform_type(monkeypatch, tmp_path):
    exe = tmp_path / "steamcmd.exe"
    seen_args = []

    def fake_run_cancelable(args, timeout, should_cancel=None, on_line=None):
        seen_args.append(args)
        exe.write_text("fake")  # simulate the bootstrap actually installing it
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(steamcmd, "_run_cancelable", fake_run_cancelable)
    monkeypatch.setattr(steamcmd.urllib.request, "urlretrieve", lambda url, path: open(path, "wb").close())
    monkeypatch.setattr(
        steamcmd.zipfile, "ZipFile",
        lambda path, mode="r": type("FakeZip", (), {
            "__enter__": lambda self: self, "__exit__": lambda self, *a: None,
            "extractall": lambda self, d: None,
        })(),
    )

    monkeypatch.setattr(steamcmd, "_sleep_cancelable", lambda s, c=None: None)
    steamcmd.install_steamcmd(str(tmp_path))

    assert len(seen_args) == 2  # bootstrap + settle pass
    for args in seen_args:
        assert "+@sSteamCmdForcePlatformType" in args
        assert "windows" in args


def test_download_workshop_items_forces_windows_platform_type(monkeypatch, tmp_path):
    (tmp_path / "steamcmd.exe").write_text("fake")
    seen_args = []

    def fake_run(args, **kwargs):
        seen_args.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(steamcmd.mod_manager, "find_workshop_pak", lambda steamcmd_dir, wid: f"{wid}.pak")

    steamcmd.download_workshop_items(str(tmp_path), ["12345"])

    assert len(seen_args) == 1
    assert "+@sSteamCmdForcePlatformType" in seen_args[0]


def test_get_latest_buildid_forces_windows_platform_type(monkeypatch, tmp_path):
    (tmp_path / "steamcmd.exe").write_text("fake")
    seen_args = []

    def fake_run(args, **kwargs):
        seen_args.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    steamcmd.get_latest_buildid(str(tmp_path))

    assert len(seen_args) == 1
    assert "+@sSteamCmdForcePlatformType" in seen_args[0]


def test_remove_if_empty_dir_removes_only_empty_folders(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    full = tmp_path / "full"
    full.mkdir()
    (full / "ConanSandboxServer.exe").write_text("x")

    steamcmd._remove_if_empty_dir(str(empty))
    steamcmd._remove_if_empty_dir(str(full))
    steamcmd._remove_if_empty_dir(str(tmp_path / "missing"))  # must not raise

    assert not empty.exists()
    assert full.exists() and (full / "ConanSandboxServer.exe").exists()


def test_update_server_does_not_leave_an_empty_precreated_install_dir(monkeypatch, tmp_path):
    """SteamCMD should be the one to create install_dir -- every
    successful manual run pointed it at a folder that didn't exist yet."""
    (tmp_path / "steamcmd.exe").write_text("fake")
    install_dir = tmp_path / "server"
    install_dir.mkdir()  # left empty by an earlier failed attempt
    existed_at_launch = []

    def fake_run_cancelable(args, timeout, should_cancel=None, on_line=None, poll_interval=0.25):
        existed_at_launch.append(install_dir.exists())
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(steamcmd, "_run_cancelable", fake_run_cancelable)
    monkeypatch.setattr(steamcmd, "get_install_state", lambda d: 4)
    monkeypatch.setattr(steamcmd, "get_installed_buildid", lambda d: "1")

    steamcmd.update_server(str(tmp_path), str(install_dir))

    assert existed_at_launch == [False]


def test_update_server_retries_three_times_with_a_pause(monkeypatch, tmp_path):
    (tmp_path / "steamcmd.exe").write_text("fake")
    calls = []
    sleeps = []

    def fake_run_cancelable(args, timeout, should_cancel=None, on_line=None, poll_interval=0.25):
        calls.append(1)
        ok = len(calls) == 3
        return subprocess.CompletedProcess(args, 0 if ok else 8, "", "")

    states = iter([None, None, 4])
    monkeypatch.setattr(steamcmd, "_run_cancelable", fake_run_cancelable)
    monkeypatch.setattr(steamcmd, "get_install_state", lambda d: next(states))
    monkeypatch.setattr(steamcmd, "get_installed_buildid", lambda d: "1")
    monkeypatch.setattr(steamcmd, "_sleep_cancelable", lambda s, c=None: sleeps.append(s))

    result = steamcmd.update_server(str(tmp_path), str(tmp_path / "server"))

    assert result.success is True
    assert len(calls) == 3
    assert sleeps == [5, 5]  # paused before attempts 2 and 3, not before 1


def test_install_steamcmd_runs_a_settle_pass_after_bootstrap(monkeypatch, tmp_path):
    exe = tmp_path / "steamcmd.exe"
    runs = []

    def fake_run_cancelable(args, timeout, should_cancel=None, on_line=None):
        runs.append(args)
        exe.write_text("fake")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(steamcmd, "_run_cancelable", fake_run_cancelable)
    monkeypatch.setattr(steamcmd, "_sleep_cancelable", lambda s, c=None: None)
    monkeypatch.setattr(steamcmd.urllib.request, "urlretrieve", lambda url, path: open(path, "wb").close())
    monkeypatch.setattr(
        steamcmd.zipfile, "ZipFile",
        lambda path, mode="r": type("FakeZip", (), {
            "__enter__": lambda self: self, "__exit__": lambda self, *a: None,
            "extractall": lambda self, d: None,
        })(),
    )

    assert steamcmd.install_steamcmd(str(tmp_path)) is True
    assert len(runs) == 2  # bootstrap + settle pass
    assert all("+quit" in r for r in runs)
