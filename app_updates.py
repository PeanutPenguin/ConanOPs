"""
Online updates for ConanOps itself, hosted on GitHub Releases -- so
there's no server to run. Publishing an update is: create a GitHub
release with a higher version tag and attach the update zip (see
RELEASING.md).

  check_latest()  -- asks GitHub for the newest published release
                     (drafts and pre-releases are ignored) and says
                     whether it's newer than this copy.
  download()      -- downloads the release's update zip with progress,
                     checks its size and SHA-256 (GitHub publishes a
                     digest for every release file), and validates it
                     as a ConanOps package. The existing installer
                     (self_update.apply_update) does the rest: backup,
                     install, restart, automatic rollback if the new
                     version won't start.

Only HTTPS to api.github.com / github.com is used, and nothing is
installed without passing those checks.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import applog
import self_update
import version

_log = applog.get_logger(__name__)

UPDATE_ASSET_NAME = "ConanOps-update.zip"
_API = "https://api.github.com/repos/{repo}/releases/latest"


def update_repo() -> str:
    """"owner/name" of the GitHub repository releases come from, set in
    version.py. Empty = online updates turned off (file updates still
    work)."""
    repo = (getattr(version, "UPDATE_REPO", "") or "").strip().strip("/")
    return repo if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) else ""


def parse_version(text: str) -> Optional[Tuple[int, ...]]:
    """"v1.2.3" / "1.2" / "1.2.3-beta" -> (1, 2, 3) / (1, 2, 0) / None.
    Pre-release suffixes aren't offered as updates."""
    m = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", (text or "").strip())
    if not m:
        return None
    return tuple(int(g or 0) for g in m.groups())


def is_newer(candidate: str, current: str = None) -> bool:
    new = parse_version(candidate)
    cur = parse_version(current if current is not None else version.VERSION)
    return bool(new and cur and new > cur)


@dataclass
class ReleaseInfo:
    version: str
    title: str
    notes: str
    page_url: str
    download_url: str
    size: int
    sha256: str  # lowercase hex, or "" if GitHub didn't publish one


class UpdateCheckError(Exception):
    """A check or download that couldn't complete -- message is shown as-is."""


def _request(url: str, timeout: float):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"ConanOps/{version.VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def check_latest(timeout: float = 15.0) -> Optional[ReleaseInfo]:
    """The newest release IF it's newer than this copy and has an update
    zip attached; None if this copy is current. Raises UpdateCheckError
    when the check itself fails (offline, rate-limited, no releases)."""
    repo = update_repo()
    if not repo:
        raise UpdateCheckError("Online updates aren't set up for this copy of ConanOps.")
    try:
        with _request(_API.format(repo=repo), timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateCheckError("No published releases were found.") from e
        if e.code in (403, 429):
            raise UpdateCheckError("GitHub is limiting update checks right now -- try again in an hour.") from e
        raise UpdateCheckError(f"Couldn't check for updates (GitHub said {e.code}).") from e
    except Exception as e:  # noqa: BLE001 - offline, DNS, TLS, bad JSON...
        raise UpdateCheckError("Couldn't reach GitHub to check for updates -- check the internet connection.") from e

    tag = str(data.get("tag_name", ""))
    if data.get("draft") or data.get("prerelease") or not is_newer(tag):
        return None
    asset = next((a for a in data.get("assets") or [] if a.get("name") == UPDATE_ASSET_NAME), None)
    if asset is None:
        _log.warning(f"Release {tag} has no {UPDATE_ASSET_NAME} attached -- not offered as an update.")
        return None
    url = str(asset.get("browser_download_url", ""))
    if not url.startswith("https://github.com/"):
        _log.warning(f"Release {tag}'s update file isn't hosted on github.com -- ignored.")
        return None
    digest = str(asset.get("digest") or "")
    sha = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else ""
    ver = parse_version(tag)
    return ReleaseInfo(
        version=".".join(str(p) for p in ver),
        title=str(data.get("name") or tag),
        notes=str(data.get("body") or "").strip(),
        page_url=str(data.get("html_url") or ""),
        download_url=url,
        size=int(asset.get("size") or 0),
        sha256=sha,
    )


def download(info: ReleaseInfo, progress: Optional[Callable[[int, int], None]] = None,
             should_cancel: Optional[Callable[[], bool]] = None, timeout: float = 30.0) -> str:
    """Downloads and verifies the update zip; returns its path (in a temp
    folder the caller should delete after installing). Raises
    UpdateCheckError on failure or cancel -- never returns a file that
    didn't pass every check."""
    folder = tempfile.mkdtemp(prefix="conanops-update-")
    path = os.path.join(folder, UPDATE_ASSET_NAME)
    sha = hashlib.sha256()
    done = 0
    try:
        with _request(info.download_url, timeout) as resp, open(path, "wb") as f:
            total = info.size or int(resp.headers.get("Content-Length") or 0)
            while True:
                if should_cancel and should_cancel():
                    raise UpdateCheckError("Download cancelled.")
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if info.size and done != info.size:
            raise UpdateCheckError("The download was incomplete -- try again.")
        if info.sha256 and sha.hexdigest() != info.sha256:
            raise UpdateCheckError("The downloaded file didn't match the release's checksum, so it wasn't installed.")
        try:
            self_update.validate_update_zip(path)
        except self_update.UpdateValidationError as e:
            raise UpdateCheckError(f"The release's update file isn't a valid ConanOps package: {e}") from e
        return path
    except UpdateCheckError:
        cleanup(path)
        raise
    except Exception as e:  # noqa: BLE001
        cleanup(path)
        raise UpdateCheckError("The download failed -- check the internet connection and try again.") from e


def cleanup(downloaded_path: str) -> None:
    import shutil
    if downloaded_path:
        shutil.rmtree(os.path.dirname(downloaded_path), ignore_errors=True)
