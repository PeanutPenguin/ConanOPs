from __future__ import annotations

import os

import self_delete


def test_script_waits_for_every_pid_then_runs_uninstaller_then_deletes():
    s = self_delete.build_uninstall_script(r"C:\ConanOps", [111, 222], extra_paths=[r"C:\Users\Bob\ConanOps"],
                                           script_dir=r"C:\Temp\conanops-cleanup-x")
    assert "$pids = @(111,222)" in s
    assert s.index("Get-Process -Id") < s.index("unins") < s.index("Remove-Hard $app")
    assert s.index("Remove-Hard $app") < s.index("Bob")
    assert s.rstrip().endswith("-Recurse -Force")  # deletes its own folder last


def test_keep_data_skips_the_data_folder():
    s = self_delete.build_uninstall_script(r"C:\ConanOps", [1], preserve_name="data")
    assert "$_.Name -ne 'data'" in s and "Remove-Hard $app\n" not in s


def test_shared_folder_only_removes_conanops_files():
    s = self_delete.build_uninstall_script(r"C:\Users\Bob\Downloads", [1],
                                           own_files=[r"C:\Users\Bob\Downloads\ConanOps.exe"])
    assert "Remove-Hard $app" not in s
    assert "ConanOps.exe" in s


def test_paths_with_quotes_and_percent_are_safe():
    s = self_delete.build_uninstall_script("C:\\Bob's 100% Stuff\\ConanOps", [1])
    assert "Bob''s 100% Stuff" in s


def test_own_files_for_a_downloads_folder(tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "holiday.jpg").write_bytes(b"x")
    (downloads / "ConanOps.exe.conanops-old").write_bytes(b"x")
    monkeypatch.setattr(self_delete.sys, "frozen", True, raising=False)
    monkeypatch.setattr(self_delete.sys, "executable", str(downloads / "ConanOps.exe"))
    files = self_delete.own_files_if_shared_folder(str(downloads))
    names = {os.path.basename(f) for f in files}
    assert "ConanOps.exe" in names and "ConanOps.exe.conanops-old" in names
    assert "holiday.jpg" not in names


def test_dedicated_install_folder_is_deleted_whole(tmp_path):
    app = tmp_path / "ConanOps"
    app.mkdir()
    (app / "unins000.exe").write_bytes(b"x")
    assert self_delete.own_files_if_shared_folder(str(app)) is None


def test_spawn_writes_script_and_launches_powershell(monkeypatch, tmp_path):
    launched = []
    monkeypatch.setattr(self_delete.subprocess, "Popen", lambda args, **k: launched.append(args))
    path = self_delete.spawn_self_delete_helper(str(tmp_path / "ConanOps"), pid=4242)
    try:
        assert os.path.exists(path) and "@(4242)" in open(path, encoding="utf-8-sig").read()
        assert launched and "-Command" in launched[0]
    finally:
        import shutil
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
