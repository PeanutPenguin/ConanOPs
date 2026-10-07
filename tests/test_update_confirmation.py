"""ConanOps' own updater: confirm only after the window is up, exit
cleanly if building the window fails, clearer "finish the last update
first" message, and removal of files a newer version no longer ships."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import zipfile

import pytest

import self_update

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _install(tmp_path, files):
    d = tmp_path / "install"
    d.mkdir(exist_ok=True)
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return d


def _zip(tmp_path, name, files):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        for rel, text in files.items():
            zf.writestr(f"conanops/{rel}", text)
    return str(path)


BASE = {"main.py": "x", "models.py": "x", "version.py": "x"}


# ------------------------------------------------------------- old files --

def test_first_update_deletes_nothing_and_starts_the_list(tmp_path):
    d = _install(tmp_path, {**BASE, "old_module.py": "x", "my_notes.txt": "mine"})
    assert self_update.apply_update(_zip(tmp_path, "a.zip", BASE), str(d)).success
    assert (d / "old_module.py").exists() and (d / "my_notes.txt").exists()
    listed = json.loads((d / ".conanops-manifest.json").read_text())["files"]
    assert set(listed) == set(BASE)


def test_next_update_removes_only_files_conanops_stopped_shipping(tmp_path):
    d = _install(tmp_path, {**BASE, "my_notes.txt": "mine"})
    (d / "data").mkdir()
    (d / "data" / "config.json").write_text("{}")
    v1 = {**BASE, "ui/theme_new.py": "x", "ui/keep.py": "x"}
    assert self_update.apply_update(_zip(tmp_path, "v1.zip", v1), str(d)).success
    self_update.confirm_update_success()
    v2 = {**BASE, "ui/keep.py": "y"}
    assert self_update.apply_update(_zip(tmp_path, "v2.zip", v2), str(d)).success
    assert not (d / "ui" / "theme_new.py").exists()       # shipped by v1, gone in v2
    assert (d / "ui" / "keep.py").read_text() == "y"
    assert (d / "my_notes.txt").exists()                   # never ConanOps' file
    assert (d / "data" / "config.json").exists()           # data folder untouchable


def test_manifest_can_never_reach_outside_the_install_folder(tmp_path):
    d = _install(tmp_path, BASE)
    (tmp_path / "outside.txt").write_text("keep")
    (d / "data").mkdir()
    (d / "data" / "x.json").write_text("keep")
    for rel in ("../outside.txt", "data/x.json", "/etc/passwd", ""):
        assert self_update._safe_target(str(d), rel) is None
    self_update._prune_files(str(d), {"../outside.txt", "data/x.json"})
    assert (tmp_path / "outside.txt").exists() and (d / "data" / "x.json").exists()


def test_empty_folders_left_behind_are_tidied(tmp_path):
    d = _install(tmp_path, {**BASE, "pkg/only.py": "x"})
    self_update._prune_files(str(d), {"pkg/only.py"})
    assert not (d / "pkg").exists() and d.exists()


def test_rollback_removes_files_only_the_failed_version_added(tmp_path):
    d = _install(tmp_path, BASE)
    assert self_update.apply_update(_zip(tmp_path, "v1.zip", BASE), str(d)).success
    self_update.confirm_update_success()
    assert self_update.apply_update(_zip(tmp_path, "v2.zip", {**BASE, "broken_new.py": "x"}), str(d)).success
    assert (d / "broken_new.py").exists()
    self_update.check_and_recover_pending_update()   # first launch of v2 starts...
    msg = self_update.check_and_recover_pending_update()  # ...and never confirmed: rollback
    assert msg and "rolled back" in msg
    assert not (d / "broken_new.py").exists()


# --------------------------------------------------------- blocked message --

def test_blocked_install_explains_why_and_what_to_do(tmp_path):
    d = _install(tmp_path, BASE)
    assert self_update.apply_update(_zip(tmp_path, "v1.zip", BASE), str(d)).success
    result = self_update.apply_update(_zip(tmp_path, "v2.zip", BASE), str(d))
    assert not result.success
    assert "hasn't finished yet" in result.message and "Quit ConanOps" in result.message


# ------------------------------------------------------------ PIN deferral --

def test_declined_pin_doesnt_count_as_a_failed_first_run(tmp_path):
    d = _install(tmp_path, BASE)
    assert self_update.apply_update(_zip(tmp_path, "v1.zip", BASE), str(d)).success
    self_update.check_and_recover_pending_update()  # first launch: marked attempted
    self_update.defer_update_confirmation()          # person closed the PIN prompt
    assert self_update.check_and_recover_pending_update() is None  # next launch: a fair retry, no rollback
    assert self_update.update_pending()


# ----------------------------------------------------- real startup runs --

_SCRIPT = textwrap.dedent("""
    import os, sys, json
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, {root!r})
    import self_update, main
    from PySide6.QtWidgets import QMessageBox, QWidget, QApplication
    from PySide6.QtCore import QTimer
    shown = []
    QMessageBox.critical = staticmethod(lambda *a, **k: shown.append(a[2]))
    QMessageBox.information = staticmethod(lambda *a, **k: shown.append(a[2]))
    import conanops_paths
    conanops_paths.no_space_root = lambda: os.environ["HOME"]  # own lock file, not the real one
    main.SPLASH_ANIMATE_MS = 0
    mode = {mode!r}
    if mode == "crash":
        def boom(config=None):
            raise RuntimeError("window build exploded")
        main.MainWindow = boom
    else:
        class Fine(QWidget):
            tray_icon = None
            def __init__(self, config=None):
                super().__init__()
            def show(self):
                super().show()
                QTimer.singleShot(400, QApplication.instance().quit)
        main.MainWindow = Fine
    self_update._write_pending_marker({backup!r}, {install!r}, attempted=False)
    code = main.main()
    print(json.dumps({{"code": code, "pending": self_update.update_pending(), "shown": shown}}))
""")


def _run_startup(tmp_path, mode):
    backup = tmp_path / "backup"
    backup.mkdir()
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    script = _SCRIPT.format(root=ROOT, mode=mode, backup=str(backup), install=str(tmp_path / "inst"))
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)
    lines = [l for l in out.stdout.splitlines() if l.startswith("{")]
    assert lines, out.stderr[-2000:]
    return json.loads(lines[-1])


@pytest.mark.skipif(not os.environ.get("QT_QPA_PLATFORM") and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"),
                    reason="needs Qt's offscreen platform")
def test_window_build_crash_exits_cleanly_and_leaves_update_unconfirmed(tmp_path):
    pytest.importorskip("PySide6")
    result = _run_startup(tmp_path, "crash")
    assert result["code"] == 1                     # exited instead of hanging
    assert result["pending"] is True               # not confirmed -> next launch rolls back
    assert result["shown"] and "window build exploded" in result["shown"][0]
    assert "previous version back" in result["shown"][0]


@pytest.mark.skipif(not os.environ.get("QT_QPA_PLATFORM") and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"),
                    reason="needs Qt's offscreen platform")
def test_update_confirmed_once_the_window_is_up(tmp_path):
    pytest.importorskip("PySide6")
    result = _run_startup(tmp_path, "ok")
    assert result["pending"] is False and result["shown"] == []


@pytest.mark.skipif(not os.environ.get("QT_QPA_PLATFORM") and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"),
                    reason="needs Qt's offscreen platform")
def test_first_launch_in_an_empty_folder_isnt_already_running(tmp_path):
    """Found by running the built .exe: the lock folder didn't exist on a
    fresh install, so the first launch said "already running"."""
    pytest.importorskip("PySide6")
    fresh = tmp_path / "brand-new" / "data"
    script = _SCRIPT.format(root=ROOT, mode="ok", backup=str(tmp_path / "b"), install=str(tmp_path / "i"))
    script = script.replace('conanops_paths.no_space_root = lambda: os.environ["HOME"]',
                            f'conanops_paths.no_space_root = lambda: {str(fresh)!r}')
    (tmp_path / "b").mkdir()
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)
    line = [l for l in out.stdout.splitlines() if l.startswith("{")][-1]
    result = json.loads(line)
    assert "ConanOps is already running." not in result["shown"]
    assert fresh.is_dir()
