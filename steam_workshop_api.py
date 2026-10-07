"""
Steam Workshop search/browse for Conan Exiles mods.

Search uses IPublishedFileService/QueryFiles, which needs a personal Steam Web
API key (https://steamcommunity.com/dev/apikey). ConanOps can't ship one: it
would be extractable, shared rate limits would run out, and Steam's terms
forbid it. Each person enters their own in App Settings (encrypted at rest).

Each item gets a status from its "Enhanced"/"Legacy" Workshop tag plus
`time_updated` against a cutoff date (the tag alone predates the Iris 2.2.0
recook, and Steam has no "updated since" filter). The date is a hint, not
proof, so non-updated mods are labelled rather than hidden. get_details()
also works without a key via Steam's keyless endpoint.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Set

from mod_manager import WORKSHOP_APP_ID

QUERY_URL = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
# Keyed details lookup; also returns required items ("children").
DETAILS_URL = "https://api.steampowered.com/IPublishedFileService/GetDetails/v1/"
# Keyless details lookup (POST), so update status works without a key.
KEYLESS_DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"

# EPublishedFileQueryType values (Steamworks IPublishedFileService docs).
_QUERY_TYPE_MOST_SUBSCRIBED = 9    # k_PublishedFileQueryType_RankedByTotalUniqueSubscriptions
_QUERY_TYPE_TEXT_SEARCH = 12       # k_PublishedFileQueryType_RankedByTextSearch
_QUERY_TYPE_LAST_UPDATED = 21      # k_PublishedFileQueryType_RankedByLastUpdatedDate

# k_PFI_MatchingFileType_Items: regular items only (no collections/guides).
_FILETYPE_ITEMS = 0

SORT_BEST_MATCH = "best_match"
SORT_POPULAR = "popular"
SORT_RECENTLY_UPDATED = "recently_updated"
SORT_OPTIONS = (SORT_BEST_MATCH, SORT_POPULAR, SORT_RECENTLY_UPDATED)

# Steam's "returned successfully" result code; others are removed/banned items.
_RESULT_OK = 1

# Version tags. tools/verify_workshop_filter.py checks them against live data.
_ENHANCED_TAG = "Enhanced"
_LEGACY_TAG = "Legacy"

# Default cutoff: the Iris public beta (2026-09-01 UTC); 2.2.0 went live
# 2026-09-15 and modders could recook during the beta. Editable in App Settings.
DEFAULT_CUTOFF_DATE = "2026-09-01"
IRIS_CUTOFF_TIMESTAMP = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())

# Shown as labels rather than used to silently hide results.
STATUS_UPDATED = "updated"          # tagged Enhanced AND updated on/after the cutoff
STATUS_STALE = "stale"              # tagged Enhanced, but not updated since before the cutoff
STATUS_LEGACY = "legacy"            # tagged Legacy (the pre-Enhanced game)
STATUS_UNKNOWN = "unknown"          # no version tag at all
ALL_STATUSES = (STATUS_UPDATED, STATUS_STALE, STATUS_LEGACY, STATUS_UNKNOWN)

STATUS_LABELS = {
    STATUS_UPDATED: "Updated for current patch",
    STATUS_STALE: "Enhanced, not updated since cutoff",
    STATUS_LEGACY: "Legacy",
    STATUS_UNKNOWN: "No version tag",
}

# Raw results requested per wanted result, and max cursor pages walked to
# fill a page after client-side filtering.
_OVERSAMPLE_FACTOR = 3
MAX_PAGES_PER_SEARCH = 5

# Opt-in short cache so switching sort orders doesn't re-hit Steam.
CACHE_TTL_SECONDS = 300
_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


def cutoff_timestamp(date_text: str) -> int:
    """'YYYY-MM-DD' (midnight UTC) -> unix timestamp; falls back to the
    default cutoff instead of raising on a typo."""
    try:
        d = datetime.strptime((date_text or "").strip(), "%Y-%m-%d")
        return int(d.replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return IRIS_CUTOFF_TIMESTAMP


@dataclass
class WorkshopItem:
    id: str
    title: str
    description: str
    author_steam_id: str
    subscriptions: int
    preview_url: str = ""
    time_updated: int = 0
    tags: List[str] = field(default_factory=list)
    status: str = STATUS_UNKNOWN
    # Ids of required Workshop items (keyed endpoints only).
    children: List[str] = field(default_factory=list)
    # id -> title for children, where search() could resolve them.
    child_titles: Dict[str, str] = field(default_factory=dict)


@dataclass
class SearchResult:
    items: List[WorkshopItem] = field(default_factory=list)
    total: int = 0
    ok: bool = False
    error: str = ""
    # Pass to search(cursor=...) for the next batch; "" when done.
    next_cursor: str = ""


@dataclass
class DetailsResult:
    items: Dict[str, WorkshopItem] = field(default_factory=dict)
    ok: bool = False
    error: str = ""


def classify(tags: Iterable[str], time_updated: int, cutoff_ts: int = IRIS_CUTOFF_TIMESTAMP) -> str:
    tag_set = set(tags)
    if _ENHANCED_TAG in tag_set:
        return STATUS_UPDATED if time_updated >= cutoff_ts else STATUS_STALE
    if _LEGACY_TAG in tag_set:
        return STATUS_LEGACY
    return STATUS_UNKNOWN


def _is_iris_ready(item: WorkshopItem, cutoff_ts: int = IRIS_CUTOFF_TIMESTAMP) -> bool:
    return classify(item.tags, item.time_updated, cutoff_ts) == STATUS_UPDATED


def _parse_item(it: dict, cutoff_ts: int) -> WorkshopItem:
    tags = [t.get("tag", "") for t in (it.get("tags") or []) if isinstance(t, dict) and t.get("tag")]
    time_updated = int(it.get("time_updated", 0) or 0)
    children = [str(c.get("publishedfileid")) for c in (it.get("children") or []) if isinstance(c, dict) and c.get("publishedfileid")]
    return WorkshopItem(
        id=str(it.get("publishedfileid", "")),
        title=it.get("title") or "(untitled)",
        description=(it.get("file_description") or "").strip(),
        author_steam_id=str(it.get("creator", "")),
        subscriptions=int(it.get("subscriptions", 0) or 0),
        preview_url=it.get("preview_url", "") or "",
        time_updated=time_updated,
        tags=tags,
        status=classify(tags, time_updated, cutoff_ts),
        children=children,
    )


def _http_error_message(e: urllib.error.HTTPError, what: str, keyed: bool = True) -> str:
    if e.code in (401, 403):
        if keyed:
            return "Steam rejected this API key -- check it on App Settings."
        return f"{what} was refused by Steam (HTTP {e.code}) -- try again later, or add a Steam Web API key on App Settings."
    if e.code == 429:
        return "Steam is rate-limiting requests from this key right now -- wait a minute and try again."
    return f"{what} failed: HTTP {e.code}"


def _get_json(url: str, timeout: float, data: Optional[bytes] = None) -> dict:
    if data is None:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    with urllib.request.urlopen(url, data=data, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cache_key(parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _cache_get(key: str):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]
        _cache.pop(key, None)
    return None


def _cache_put(key: str, value) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), value)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def search(api_key: str, query: str = "", page: int = 1, count: int = 20, timeout: float = 8.0,
           sort: str = "", show: Optional[Set[str]] = None, cutoff_ts: int = IRIS_CUTOFF_TIMESTAMP,
           cursor: str = "*", max_pages: int = MAX_PAGES_PER_SEARCH, resolve_children: bool = False,
           use_cache: bool = False, skip_ids: Optional[Set[str]] = None) -> SearchResult:
    """Search (or browse, with an empty query) the Conan Exiles Workshop.
    Never raises; failures return ok=False with a readable `error`.

    show: statuses to return (default only STATUS_UPDATED); also drives
      Steam-side tag filters.
    sort: SORT_BEST_MATCH, SORT_POPULAR, or SORT_RECENTLY_UPDATED.
    cursor: walks pages until `count` results pass the filter. If a page
      fills partway, next_cursor points at that same page; pass the ids
      already shown as `skip_ids` to avoid duplicates.
    resolve_children: also fetch titles of required items (one request).
    `page` is ignored (kept for compatibility); the cursor replaces it."""
    if not api_key:
        return SearchResult(ok=False, error="No Steam Web API key configured -- add one on App Settings.")

    show = set(show) if show else {STATUS_UPDATED}
    query = query or ""
    if not sort:
        sort = SORT_BEST_MATCH if query.strip() else SORT_POPULAR
    if sort == SORT_RECENTLY_UPDATED:
        query_type = _QUERY_TYPE_LAST_UPDATED
    elif sort == SORT_BEST_MATCH and query.strip():
        query_type = _QUERY_TYPE_TEXT_SEARCH
    else:
        query_type = _QUERY_TYPE_MOST_SUBSCRIBED

    base_params = [
        ("key", api_key),
        ("appid", str(WORKSHOP_APP_ID)),
        ("creator_appid", str(WORKSHOP_APP_ID)),
        ("query_type", str(query_type)),
        ("numperpage", str(count * _OVERSAMPLE_FACTOR)),
        ("filetype", str(_FILETYPE_ITEMS)),
        ("return_details", "1"),
        ("return_tags", "1"),
        ("return_children", "1"),
        ("search_text", query),
    ]
    if show <= {STATUS_UPDATED, STATUS_STALE}:
        base_params.append(("requiredtags[0]", _ENHANCED_TAG))
    elif STATUS_LEGACY not in show:
        base_params.append(("excludedtags[0]", _LEGACY_TAG))

    skip = set(skip_ids or ())
    cache_key = _cache_key([base_params, sorted(show), cutoff_ts, cursor, count, max_pages, resolve_children, sorted(skip)])
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    items: List[WorkshopItem] = []
    seen: Set[str] = set(skip)
    next_cursor = cursor or "*"
    steam_total = 0
    for _ in range(max(1, max_pages)):
        page_cursor = next_cursor
        url = QUERY_URL + "?" + urllib.parse.urlencode(base_params + [("cursor", page_cursor)])
        try:
            data = _get_json(url, timeout)
        except urllib.error.HTTPError as e:
            if items:
                break  # keep what earlier pages found
            return SearchResult(ok=False, error=_http_error_message(e, "Steam Workshop search"))
        except Exception as e:  # noqa: BLE001 - network/JSON errors of every shape land here
            if items:
                break
            return SearchResult(ok=False, error=f"Steam Workshop search failed: {e}")

        response = data.get("response", {}) if isinstance(data, dict) else {}
        if not isinstance(response, dict) or ("publishedfiledetails" not in response and "total" not in response):
            if items:
                break
            return SearchResult(ok=False, error="Steam's response didn't look like a Workshop search result.")
        steam_total = int(response.get("total", 0) or 0)

        filled_mid_page = False
        for raw in response.get("publishedfiledetails", []) or []:
            if raw.get("result") != _RESULT_OK or not raw.get("publishedfileid"):
                continue
            item = _parse_item(raw, cutoff_ts)
            if item.id in seen or item.status not in show:
                continue
            if len(items) >= count:
                filled_mid_page = True
                break
            seen.add(item.id)
            items.append(item)

        new_cursor = str(response.get("next_cursor") or "")
        steam_has_more = bool(new_cursor) and new_cursor != page_cursor
        if filled_mid_page:
            next_cursor = page_cursor  # continue from this page; skip_ids drops what was shown
            break
        next_cursor = new_cursor if steam_has_more else ""
        if not steam_has_more or len(items) >= count:
            break

    if resolve_children:
        _resolve_child_titles(api_key, items, timeout)

    result = SearchResult(items=items, total=steam_total, ok=True, next_cursor=next_cursor)
    if use_cache:
        _cache_put(cache_key, result)
    return result


def _resolve_child_titles(api_key: str, items: List[WorkshopItem], timeout: float) -> None:
    by_id = {it.id: it.title for it in items}
    unknown = sorted({c for it in items for c in it.children if c not in by_id})
    if unknown:
        details = get_details(unknown, api_key=api_key, timeout=timeout)
        if details.ok:
            by_id.update({k: v.title for k, v in details.items.items()})
    for it in items:
        it.child_titles = {c: by_id[c] for c in it.children if c in by_id}


def get_details(workshop_ids: Iterable[str], api_key: str = "", cutoff_ts: int = IRIS_CUTOFF_TIMESTAMP,
                timeout: float = 8.0, use_cache: bool = False) -> DetailsResult:
    """Look up specific Workshop items (e.g. a server's mods). The keyed
    endpoint also returns required items; without a key it uses the
    keyless endpoint. Never raises."""
    ids = [str(i) for i in workshop_ids if str(i).isdigit()]
    if not ids:
        return DetailsResult(ok=True)
    cache_key = _cache_key(["details", bool(api_key), api_key and hashlib.sha256(api_key.encode()).hexdigest(), ids, cutoff_ts])
    if use_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    items: Dict[str, WorkshopItem] = {}
    try:
        for start in range(0, len(ids), 100):
            batch = ids[start:start + 100]
            if api_key:
                params = [("key", api_key), ("includetags", "1"), ("includechildren", "1")]
                params += [(f"publishedfileids[{i}]", wid) for i, wid in enumerate(batch)]
                data = _get_json(DETAILS_URL + "?" + urllib.parse.urlencode(params), timeout)
            else:
                params = [("itemcount", str(len(batch)))] + [(f"publishedfileids[{i}]", wid) for i, wid in enumerate(batch)]
                data = _get_json(KEYLESS_DETAILS_URL, timeout, data=urllib.parse.urlencode(params).encode("utf-8"))
            response = data.get("response", {}) if isinstance(data, dict) else {}
            if not isinstance(response, dict) or "publishedfiledetails" not in response:
                return DetailsResult(ok=False, error="Steam's response didn't look like Workshop item details.")
            for raw in response.get("publishedfiledetails") or []:
                if raw.get("result") == _RESULT_OK and raw.get("publishedfileid"):
                    item = _parse_item(raw, cutoff_ts)
                    items[item.id] = item
    except urllib.error.HTTPError as e:
        return DetailsResult(ok=False, error=_http_error_message(e, "Steam Workshop lookup", keyed=bool(api_key)))
    except Exception as e:  # noqa: BLE001
        return DetailsResult(ok=False, error=f"Steam Workshop lookup failed: {e}")

    result = DetailsResult(items=items, ok=True)
    if use_cache:
        _cache_put(cache_key, result)
    return result
