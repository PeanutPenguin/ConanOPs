"""
Steam Workshop search/browse for Conan Exiles mods.

Unlike changelog.py's news fetch (a public, keyless endpoint),
Workshop search and browse go through IPublishedFileService/QueryFiles,
which requires a Steam Web API key. This is a personal, free key any
Steam account can generate at https://steamcommunity.com/dev/apikey --
ConanOps can't ship one baked into the app itself -- besides being
trivially extractable from a distributed .exe, sharing a single key
across everyone who's ever downloaded ConanOps would blow through
Steam's per-key rate limit almost immediately, and publishing/exposing
a personal key like that is against Steam's own Web API Terms of Use.
So each person pastes their own into App Settings (AppConfig.steam_api_key,
encrypted at rest -- see models.py), the same pattern the Discord/ntfy
webhook URLs already use.

Results are filtered to Enhanced mods that have actually been rebuilt
for Iris (Conan Exiles Enhanced update 2.2.0, live September 15, 2026
-- the update that moved the game to Unreal Engine 5.8.2 and replaced
its networking with Epic's Iris system; Funcom's Community Update #7
says it forced a recook of every mod because of engine serialization
changes, and that modders were told about this ahead of time). Two checks, both
confirmed against real Workshop pages before writing this, not
guessed: a mod's Workshop page shows a "Version: Enhanced" or
"Version: Legacy" tag (visible in the API as a real, structured
`tags` entry, filterable server-side via `requiredtags`) -- but that
tag alone only proves a mod was rebuilt for the ORIGINAL May 2026
Enhanced/UE5 release, not necessarily for the LATER Iris-specific
2.2.0 patch, so this also checks `time_updated` against an Iris
cutoff date client-side (Steam's search API has no "updated since"
filter to do this server-side). A mod needs to pass BOTH to be
listed -- some real, live examples of exactly this gap: multiple
mods' own comment sections show players reporting them "outdated"
immediately after 2.2.0 shipped, with the Enhanced tag already set
from months earlier but the actual rebuild still pending.

Every item gets a STATUS (see classify()) instead of a bare pass/fail:
updated for the current cutoff, Enhanced-but-stale, Legacy, or no
version tag. The browser shows only "updated" by default but can show
the others, labelled -- `time_updated` also changes on description-
only edits, so the date check is a strong hint, not proof, and a hard
filter would silently hide mods that are actually fine. The cutoff is
an App Settings value (AppConfig.workshop_update_cutoff), not just the
constant below, so the next patch doesn't need a code change.

The same classification runs on mods already on a server (get_details,
used by the Mods tab), which works even without an API key via
Steam's keyless details endpoint.
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
# Keyed details lookup -- also returns each item's required items
# ("children"), which the keyless endpoint below doesn't.
DETAILS_URL = "https://api.steampowered.com/IPublishedFileService/GetDetails/v1/"
# Keyless details lookup (POST) -- lets the Mods tab check installed
# mods' update status even for someone who never set up an API key.
KEYLESS_DETAILS_URL = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"

# EPublishedFileQueryType values (Steamworks IPublishedFileService docs).
_QUERY_TYPE_MOST_SUBSCRIBED = 9    # k_PublishedFileQueryType_RankedByTotalUniqueSubscriptions
_QUERY_TYPE_TEXT_SEARCH = 12       # k_PublishedFileQueryType_RankedByTextSearch
_QUERY_TYPE_LAST_UPDATED = 21      # k_PublishedFileQueryType_RankedByLastUpdatedDate

# EPublishedFileInfoMatchingFileType: k_PFI_MatchingFileType_Items --
# regular Workshop items only, so collections, guides, artwork, etc.
# never come back from a mod search in the first place.
_FILETYPE_ITEMS = 0

SORT_BEST_MATCH = "best_match"
SORT_POPULAR = "popular"
SORT_RECENTLY_UPDATED = "recently_updated"
SORT_OPTIONS = (SORT_BEST_MATCH, SORT_POPULAR, SORT_RECENTLY_UPDATED)

# Steam's own result code for "this item was returned successfully" --
# QueryFiles can include entries for items it couldn't actually fetch
# full details for (removed, banned, whatever), still worth filtering
# out rather than showing a broken-looking blank row for them.
_RESULT_OK = 1

# The Workshop tags marking a mod's game version. Run
# tools/verify_workshop_filter.py with your own key to confirm these
# against live Workshop data -- if Steam ever spells them differently,
# this is the only place to change.
_ENHANCED_TAG = "Enhanced"
_LEGACY_TAG = "Legacy"

# Default cutoff for "updated for Iris": September 1, 2026, midnight
# UTC -- the day the Public Beta Client build with Iris enabled went
# out (2.2.0 itself went live September 15). Modders had the 5.8 build
# and advance notice of the required recook before the live release,
# so a mod recooked and published during the beta window is
# legitimately Iris-ready. This is only the DEFAULT -- the person can
# change it in App Settings (AppConfig.workshop_update_cutoff) when the
# next patch needs a different one.
DEFAULT_CUTOFF_DATE = "2026-09-01"
IRIS_CUTOFF_TIMESTAMP = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())

# Mod statuses -- how a mod's tags and last-update date line up with
# the current cutoff. Shown as labels rather than used to silently
# hide results (see search()'s `show` parameter).
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

# How many raw results to request per page, as a multiple of how many
# filtered results are wanted -- and the most pages search() will walk
# (via Steam's cursor) trying to fill a page after client-side
# filtering, before returning what it has.
_OVERSAMPLE_FACTOR = 3
MAX_PAGES_PER_SEARCH = 5

# Results are cached briefly (only when a caller opts in -- the UI
# worker does), so flipping between sort orders or re-opening the
# browser doesn't re-hit Steam for the same thing.
CACHE_TTL_SECONDS = 300
_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


def cutoff_timestamp(date_text: str) -> int:
    """'YYYY-MM-DD' (midnight UTC) -> unix timestamp. Falls back to the
    default cutoff for anything unparseable rather than raising -- a
    typo in App Settings shouldn't break the Mods tab."""
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
    # Workshop "required items" -- ids of other Workshop items this one
    # declares it needs (only returned by keyed endpoints).
    children: List[str] = field(default_factory=list)
    # id -> title for children, where search() could resolve them.
    child_titles: Dict[str, str] = field(default_factory=dict)


@dataclass
class SearchResult:
    items: List[WorkshopItem] = field(default_factory=list)
    total: int = 0
    ok: bool = False
    error: str = ""
    # Pass back to search(cursor=...) to load the next batch; "" when
    # Steam has nothing more.
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
    """Searches (or, with an empty query, browses) the Conan Exiles
    Workshop. Never raises -- anything that goes wrong comes back as
    ok=False with a human-readable `error`.

    show: which statuses to return (default: only STATUS_UPDATED). The
      server-side tag filters are derived from it -- requiredtags
      Enhanced when only Enhanced statuses are wanted, excludedtags
      Legacy when Legacy isn't -- so Steam does as much of the
      filtering as it can before anything comes back.
    sort: SORT_BEST_MATCH (text relevance; default with a query),
      SORT_POPULAR (default with no query), or SORT_RECENTLY_UPDATED.
    cursor: Steam's paging cursor ("*" for the first batch). Pages are
      walked until `count` results pass the client-side filter, Steam
      runs out, or `max_pages` is hit; result.next_cursor continues
      from there. When `count` fills up partway through a page,
      next_cursor points back at THAT page (so the rest of it isn't
      skipped) -- pass the ids already shown as `skip_ids` when
      continuing, so they aren't returned twice.
    resolve_children: also look up titles for required items that
      aren't in the results themselves (one extra request).

    `page` is accepted for backward compatibility but ignored -- the
    cursor replaces it (Steam's page parameter caps out and is ignored
    whenever a cursor is sent)."""
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
                break  # keep what earlier pages already found
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
                filled_mid_page = True  # more matches on this page than fit
                break
            seen.add(item.id)
            items.append(item)

        new_cursor = str(response.get("next_cursor") or "")
        steam_has_more = bool(new_cursor) and new_cursor != page_cursor
        if filled_mid_page:
            next_cursor = page_cursor  # continue from THIS page; skip_ids drops what was shown
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
    """Looks up specific Workshop items -- e.g. every mod already on a
    server, to flag the outdated ones. With an API key this uses the
    keyed endpoint, which also returns each item's required items;
    without one it falls back to Steam's keyless endpoint (no
    dependency info, but update status works for everyone). Never
    raises."""
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
