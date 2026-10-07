from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile

import pytest
from PySide6.QtWidgets import QApplication

import app_updates
import self_update
import version


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication(sys.argv)


class _Resp(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _release(tag="v1.2.0", **asset):
    a = {"name": "ConanOps-update.zip", "browser_download_url": "https://github.com/o/r/releases/download/v1.2.0/ConanOps-update.zip",
         "size": 10, "digest": "sha256:" + "a" * 64}
    a.update(asset)
    return {"tag_name": tag, "name": "Big update", "body": "Fixed stuff", "html_url": "https://github.com/o/r/releases/v1.2.0",
            "draft": False, "prerelease": False, "assets": [a]}


@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setattr(version, "UPDATE_REPO", "o/r", raising=False)
    monkeypatch.setattr(version, "VERSION", "1.0.0")


def _serve(monkeypatch, payload):
    monkeypatch.setattr(app_updates.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(json.dumps(payload).encode()))


@pytest.mark.parametrize("a,b,newer", [("v1.0.1", "1.0.0", True), ("1.0.0", "1.0.0", False),
                                       ("v0.9", "1.0.0", False), ("v2", "1.9.9", True), ("v1.1.0-beta", "1.0.0", False)])
def test_version_compare(a, b, newer):
    assert app_updates.is_newer(a, b) is newer


def test_no_repo_means_online_updates_off(monkeypatch):
    monkeypatch.setattr(version, "UPDATE_REPO", "", raising=False)
    with pytest.raises(app_updates.UpdateCheckError):
        app_updates.check_latest()


def test_newer_release_is_offered(monkeypatch, repo):
    _serve(monkeypatch, _release())
    info = app_updates.check_latest()
    assert info.version == "1.2.0" and info.sha256 == "a" * 64 and info.notes == "Fixed stuff"


@pytest.mark.parametrize("payload", [
    _release(tag="v1.0.0"),                                   # same version
    {**_release(), "prerelease": True},                         # pre-release
    _release(name="something-else.zip"),                        # no update asset
    _release(browser_download_url="https://evil.example/x.zip"),  # not on github.com
])
def test_not_offered(monkeypatch, repo, payload):
    _serve(monkeypatch, payload)
    assert app_updates.check_latest() is None


def test_rate_limit_message(monkeypatch, repo):
    import urllib.error

    def boom(req, timeout=0):
        raise urllib.error.HTTPError("u", 403, "rate", {}, None)
    monkeypatch.setattr(app_updates.urllib.request, "urlopen", boom)
    with pytest.raises(app_updates.UpdateCheckError, match="limiting"):
        app_updates.check_latest()


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("main.py", "x")
        z.writestr("models.py", "x")
    return buf.getvalue()


def test_download_verifies_checksum_and_package(monkeypatch, repo):
    data = _zip_bytes()
    info = app_updates.ReleaseInfo("1.2.0", "t", "", "", "https://github.com/x", len(data),
                                   hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(app_updates.urllib.request, "urlopen", lambda req, timeout=0: _Resp(data))
    seen = []
    path = app_updates.download(info, progress=lambda d, t: seen.append((d, t)))
    try:
        assert open(path, "rb").read() == data and seen[-1] == (len(data), len(data))
    finally:
        app_updates.cleanup(path)


def test_download_rejects_wrong_checksum(monkeypatch, repo):
    data = _zip_bytes()
    info = app_updates.ReleaseInfo("1.2.0", "t", "", "", "https://github.com/x", len(data), "0" * 64)
    monkeypatch.setattr(app_updates.urllib.request, "urlopen", lambda req, timeout=0: _Resp(data))
    with pytest.raises(app_updates.UpdateCheckError, match="checksum"):
        app_updates.download(info)


def test_download_rejects_truncated_file(monkeypatch, repo):
    data = _zip_bytes()
    info = app_updates.ReleaseInfo("1.2.0", "t", "", "", "https://github.com/x", len(data) + 5, "")
    monkeypatch.setattr(app_updates.urllib.request, "urlopen", lambda req, timeout=0: _Resp(data))
    with pytest.raises(app_updates.UpdateCheckError, match="incomplete"):
        app_updates.download(info)


def _page(monkeypatch, **cfg):
    import models
    from tests.test_app_settings_startup import _make_page
    import background_mode
    monkeypatch.setattr(background_mode, "status", lambda: None)
    return _make_page(models.AppConfig(**cfg))


def test_settings_page_shows_available_update_and_auto_installs(monkeypatch, repo):
    page = _page(monkeypatch, auto_install_app_updates=True)
    installs, notified = [], []
    page.on_app_update_available = notified.append
    monkeypatch.setattr(page, "_install_online_update", lambda confirm: installs.append(confirm))
    info = app_updates.ReleaseInfo("1.2.0", "t", "Fixed stuff", "https://github.com/o/r", "https://github.com/x", 1, "")
    page._on_update_checked(info, "", automatic=True)
    assert "1.2.0" in page.app_update_status.text()
    assert page.install_online_btn.isVisibleTo(page)
    assert notified == [info] and installs == [False]


def test_settings_page_up_to_date(monkeypatch, repo):
    page = _page(monkeypatch)
    page._on_update_checked(None, "", automatic=False)
    assert "up to date" in page.app_update_status.text()


def test_auto_install_waits_while_busy(monkeypatch, repo):
    page = _page(monkeypatch)
    page._available_release = app_updates.ReleaseInfo("1.2.0", "t", "", "", "https://github.com/x", 1, "")
    page.is_busy_for_app_update = lambda: "a server update"
    page._install_online_update(confirm=False)
    assert page._app_update_install_worker is None
