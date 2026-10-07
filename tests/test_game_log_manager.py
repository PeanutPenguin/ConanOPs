from __future__ import annotations

import os
import time

import game_log_manager as glm


def _make_log(path, size_bytes, mtime_offset=0):
    with open(path, "wb") as f:
        f.write(b"x" * size_bytes)
    t = time.time() + mtime_offset
    os.utime(path, (t, t))


def test_no_logs_dir_is_a_noop(tmp_path):
    assert glm.prune_old_logs(str(tmp_path / "does-not-exist")) == []


def test_fewer_than_min_keep_never_deletes_anything(tmp_path):
    install_dir = tmp_path / "install"
    log_dir = install_dir / "ConanSandbox" / "Saved" / "Logs"
    log_dir.mkdir(parents=True)
    for i in range(2):
        _make_log(str(log_dir / f"log{i}.log"), 10 * 1024 * 1024)

    deleted = glm.prune_old_logs(str(install_dir), max_total_mb=1, min_keep=3)

    assert deleted == []
    assert len(list(log_dir.iterdir())) == 2


def test_deletes_oldest_first_until_under_the_cap(tmp_path):
    install_dir = tmp_path / "install"
    log_dir = install_dir / "ConanSandbox" / "Saved" / "Logs"
    log_dir.mkdir(parents=True)
    # 5 logs, 10 MB each, oldest to newest, min_keep=2, cap=25MB.
    for i in range(5):
        _make_log(str(log_dir / f"log{i}.log"), 10 * 1024 * 1024, mtime_offset=i)

    deleted = glm.prune_old_logs(str(install_dir), max_total_mb=25, min_keep=2)

    remaining = {p.name for p in log_dir.iterdir()}
    assert "log0.log" in {os.path.basename(d) for d in deleted}  # oldest went first
    assert "log4.log" in remaining  # newest survives
    assert "log3.log" in remaining  # second newest survives (min_keep=2)
    assert len(remaining) >= 2


def test_never_deletes_below_min_keep_even_over_the_cap(tmp_path):
    install_dir = tmp_path / "install"
    log_dir = install_dir / "ConanSandbox" / "Saved" / "Logs"
    log_dir.mkdir(parents=True)
    for i in range(4):
        _make_log(str(log_dir / f"log{i}.log"), 100 * 1024 * 1024, mtime_offset=i)  # 400MB total, way over any small cap

    glm.prune_old_logs(str(install_dir), max_total_mb=1, min_keep=3)

    remaining = list(log_dir.iterdir())
    assert len(remaining) == 3  # min_keep enforced even though still way over the cap


def test_under_the_cap_deletes_nothing(tmp_path):
    install_dir = tmp_path / "install"
    log_dir = install_dir / "ConanSandbox" / "Saved" / "Logs"
    log_dir.mkdir(parents=True)
    for i in range(4):
        _make_log(str(log_dir / f"log{i}.log"), 1024, mtime_offset=i)

    deleted = glm.prune_old_logs(str(install_dir), max_total_mb=500, min_keep=2)

    assert deleted == []


def test_skips_a_file_that_cannot_be_deleted(tmp_path, monkeypatch):
    install_dir = tmp_path / "install"
    log_dir = install_dir / "ConanSandbox" / "Saved" / "Logs"
    log_dir.mkdir(parents=True)
    for i in range(4):
        _make_log(str(log_dir / f"log{i}.log"), 10 * 1024 * 1024, mtime_offset=i)

    real_remove = os.remove

    def flaky_remove(path):
        if "log0" in path:
            raise OSError("file in use")
        return real_remove(path)
    monkeypatch.setattr(os, "remove", flaky_remove)

    deleted = glm.prune_old_logs(str(install_dir), max_total_mb=1, min_keep=1)  # must not raise

    assert all("log0" not in d for d in deleted)
