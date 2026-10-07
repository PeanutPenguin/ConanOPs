"""Access from anywhere: the web UI via a Cloudflare quick tunnel (free, no account, HTTPS).

cloudflared is downloaded once and only used if validly signed by Cloudflare.
The trycloudflare.com address changes on each start; dropped tunnels restart.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from typing import Callable, Optional

import applog
import conanops_paths
import powershell
from proc_utils import child_env, hidden_window_kwargs

_log = applog.get_logger(__name__)

DOWNLOAD_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def exe_path() -> str:
    return os.path.join(conanops_paths.no_space_root(), "tools", "cloudflared.exe")


def _pid_file() -> str:
    return os.path.join(conanops_paths.no_space_root(), "tools", "cloudflared.pid")


def _signed_by_cloudflare(path: str) -> bool:
    if sys.platform != "win32":
        return False
    proc = powershell.run_readonly(
        f"$s = Get-AuthenticodeSignature -LiteralPath {powershell.ps_str(path)}\n"
        "if ($s.Status -eq 'Valid' -and $s.SignerCertificate.Subject -match 'Cloudflare') { 'OK' } else { 'BAD:' + $s.Status }\n"
        "exit 0\n", timeout=60)
    return bool(proc and (proc.stdout or "").strip().endswith("OK"))


def ensure_installed(progress: Callable[[str], None] = lambda _m: None) -> bool:
    """Downloads cloudflared if missing; rejects it unless signed by Cloudflare."""
    path = exe_path()
    if os.path.exists(path):
        return True
    folder = tempfile.mkdtemp(prefix="conanops-cloudflared-")
    tmp = os.path.join(folder, "cloudflared.exe")
    try:
        progress("Downloading Cloudflare's tunnel program…")
        with urllib.request.urlopen(DOWNLOAD_URL, timeout=120) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f)
        if os.path.getsize(tmp) < 5 * 1024 * 1024 or not _signed_by_cloudflare(tmp):
            _log.error("cloudflared download failed its signature check; not using it.")
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.move(tmp, path)
        return True
    except Exception as e:  # noqa: BLE001
        _log.error(f"Couldn't download cloudflared: {e}")
        return False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _kill_leftover() -> None:
    """Kills a cloudflared left over from a previous run."""
    try:
        with open(_pid_file(), "r", encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return
    try:
        import psutil
        p = psutil.Process(pid)
        if os.path.basename(p.exe()).lower() == "cloudflared.exe":
            p.terminate()
            p.wait(5)
    except Exception:  # noqa: BLE001
        pass
    try:
        os.remove(_pid_file())
    except OSError:
        pass


class Tunnel:
    """Keeps one quick tunnel to http://localhost:<port> running.
    on_change(url_or_empty, status_text) is called from a background thread."""

    def __init__(self, on_change: Callable[[str, str], None]):
        self._on_change = on_change
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.url = ""
        self.status = "Off"
        self.port: Optional[int] = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _set(self, url: str, status: str) -> None:
        changed = (url, status) != (self.url, self.status)
        self.url, self.status = url, status
        if changed:
            try:
                self._on_change(url, status)
            except Exception as e:  # noqa: BLE001
                _log.warning(f"Tunnel status callback failed: {e}")

    def start(self, port: int) -> None:
        if sys.platform != "win32":
            self._set("", "Only available on Windows.")
            return
        with self._lock:
            if self.running and self.port == port:
                return
        self.stop()
        self.port = port
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="cloudflare-tunnel", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=6)
        self._thread = None
        try:
            os.remove(_pid_file())
        except OSError:
            pass
        self._set("", "Off")

    def _run(self) -> None:
        backoff = 5
        if not ensure_installed(lambda m: self._set("", m)):
            self._set("", "Couldn't get Cloudflare's tunnel program -- check the internet connection.")
            return
        _kill_leftover()
        while not self._stop.is_set():
            self._set("", "Connecting…")
            started = time.monotonic()
            try:
                proc = subprocess.Popen(
                    [exe_path(), "tunnel", "--no-autoupdate", "--url", f"http://localhost:{self.port}"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", env=child_env(), **hidden_window_kwargs(),
                )
            except OSError as e:
                self._set("", f"Couldn't start the tunnel: {e}")
                return
            with self._lock:
                self._proc = proc
            try:
                with open(_pid_file(), "w", encoding="utf-8") as f:
                    f.write(str(proc.pid))
            except OSError:
                pass
            for line in proc.stdout:  # ends when cloudflared exits
                m = _URL_RE.search(line)
                if m and m.group(0) != self.url:
                    self._set(m.group(0), "Connected")
                if self._stop.is_set():
                    break
            proc.wait()
            if self._stop.is_set():
                break
            self._set("", f"The link dropped; reconnecting in {backoff} s…")
            _log.warning(f"cloudflared exited (code {proc.returncode}); restarting in {backoff}s")
            if self._stop.wait(backoff):
                break
            backoff = 5 if time.monotonic() - started > 600 else min(300, backoff * 2)
