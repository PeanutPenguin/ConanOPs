"""
Outside heartbeat: ConanOps calls a link every few minutes for as long as it
is running. A free uptime service on the other end (healthchecks.io, Uptime
Kuma, ...) alerts when the calls stop, which is the only way to hear that the
PC is off, offline, or ConanOps is gone: nothing running here can say so.
"""
from __future__ import annotations

import urllib.error
import urllib.request

import applog

_log = applog.get_logger(__name__)

INTERVAL_SECONDS = 5 * 60


def clean_url(text: str) -> str:
    """The link as saved, or ValueError saying what's wrong with it."""
    url = (text or "").strip()
    if url and not url.lower().startswith(("https://", "http://")):
        raise ValueError("a heartbeat link starts with https://")
    return url


def ping(url: str, timeout: float = 10.0) -> bool:
    """One call to the link. Never raises: a missed beat is the service's to notice."""
    if not url:
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ConanOps-heartbeat"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, OSError, ValueError) as e:
        _log.info(f"Heartbeat didn't get through this time: {e}")
        return False
