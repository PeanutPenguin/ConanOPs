"""
Fetches the latest Conan Exiles patch notes from Steam's public news API
and flags "major" (engine-level) updates with a keyword/version heuristic.
On failure the result has no text; callers should not invent a summary.
"""
from __future__ import annotations

import re
import urllib.request
import urllib.parse
import json
from dataclasses import dataclass
from typing import Optional

from models import APP_ID

NEWS_API_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v0002/"
    "?appid={app_id}&count=1&maxlength=2000&format=json"
)

MAJOR_KEYWORDS = [
    "unreal engine", "ue5", "ue 5", "migration", "networking model",
    "engine upgrade", "engine update",
]


@dataclass
class ChangelogInfo:
    title: str = ""
    body: str = ""
    is_major: bool = False
    fetched_ok: bool = False


def fetch_latest_news(timeout: float = 5.0) -> ChangelogInfo:
    url = NEWS_API_URL.format(app_id=APP_ID)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return ChangelogInfo(fetched_ok=False)

    items = data.get("appnews", {}).get("newsitems", [])
    if not items:
        return ChangelogInfo(fetched_ok=False)

    item = items[0]
    title = item.get("title", "")
    body = re.sub(r"\[.*?\]", "", item.get("contents", ""))  # strip Steam bbcode-ish tags

    return ChangelogInfo(title=title, body=body.strip(), is_major=False, fetched_ok=True)


def is_major_update(old_version: str, new_version: str, changelog_text: str) -> bool:
    """Heuristic check on keywords and "major.minor" version strings.

    Do not pass bare Steam build ids (no dots, so they'd always look like a
    major change); pass "" for both versions to use the keyword check alone."""
    text = (changelog_text or "").lower()
    if any(kw in text for kw in MAJOR_KEYWORDS):
        return True

    def major_minor(v: str):
        parts = v.split(".")
        return parts[0] if parts else v, parts[1] if len(parts) > 1 else ""

    old_major, old_minor = major_minor(old_version)
    new_major, new_minor = major_minor(new_version)
    return old_major != new_major or (old_minor and new_minor and old_minor != new_minor)
