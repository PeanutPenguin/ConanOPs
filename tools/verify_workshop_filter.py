"""
Checks ConanOps' Workshop filter against LIVE Steam data, and saves the
raw responses as test fixtures.

Run this with your own Steam Web API key before trusting the filter,
and again after any game patch:

    python tools/verify_workshop_filter.py --key YOUR_KEY \
        --updated 1111111111,2222222222 \
        --outdated 3333333333,4444444444

--updated   Workshop ids of mods you KNOW were rebuilt for the current patch.
--outdated  Workshop ids of mods you KNOW are broken/outdated on it.
--cutoff    Cutoff date to test (default: the app's default).

It prints:
  1. Every version-like tag seen on the most popular Conan Exiles
     Workshop items, so you can confirm the exact spelling of the
     "Enhanced" / "Legacy" tags the filter relies on.
  2. For each id you gave: its tags, last-update date, the status the
     filter assigns it, and whether that matches what you said.

Then it writes the raw responses (API key stripped) plus your
expectations into tests/fixtures/workshop/real_*.json, so the test
suite checks the filter against real Steam data from then on.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import urllib.parse
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import steam_workshop_api as swa  # noqa: E402
from mod_manager import WORKSHOP_APP_ID  # noqa: E402

FIXTURE_DIR = os.path.join(os.path.dirname(HERE), "tests", "fixtures", "workshop")


def _ids(text: str):
    return [x.strip() for x in (text or "").split(",") if x.strip().isdigit()]


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d") if ts else "?"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", default=os.environ.get("STEAM_API_KEY", ""))
    ap.add_argument("--updated", default="")
    ap.add_argument("--outdated", default="")
    ap.add_argument("--cutoff", default=swa.DEFAULT_CUTOFF_DATE)
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    if not args.key:
        print("Need a Steam Web API key: --key YOUR_KEY (or set STEAM_API_KEY).")
        return 2
    cutoff_ts = swa.cutoff_timestamp(args.cutoff)

    # 1. Tag survey -- no tag filter at all, so every spelling shows up.
    params = [
        ("key", args.key), ("appid", str(WORKSHOP_APP_ID)), ("creator_appid", str(WORKSHOP_APP_ID)),
        ("query_type", str(swa._QUERY_TYPE_MOST_SUBSCRIBED)), ("numperpage", "100"),
        ("filetype", str(swa._FILETYPE_ITEMS)), ("return_details", "1"), ("return_tags", "1"),
        ("return_children", "1"), ("cursor", "*"),
    ]
    query_payload = swa._get_json(swa.QUERY_URL + "?" + urllib.parse.urlencode(params), 15.0)
    details = (query_payload.get("response") or {}).get("publishedfiledetails") or []
    tag_counts = collections.Counter(t.get("tag") for d in details for t in (d.get("tags") or []))
    print(f"Tags on the top {len(details)} most-subscribed items:")
    for tag, n in tag_counts.most_common():
        mark = "  <-- filter uses this" if tag in (swa._ENHANCED_TAG, swa._LEGACY_TAG) else ""
        print(f"  {n:4d}  {tag}{mark}")
    for needed in (swa._ENHANCED_TAG, swa._LEGACY_TAG):
        if needed not in tag_counts:
            print(f"\n!! The tag {needed!r} never appeared. Check the list above for its real spelling and "
                  f"update _ENHANCED_TAG/_LEGACY_TAG in steam_workshop_api.py.")

    # 2. Known mods.
    updated, outdated = _ids(args.updated), _ids(args.outdated)
    details_payload = {}
    failures = 0
    if updated or outdated:
        dparams = [("key", args.key), ("includetags", "1"), ("includechildren", "1")]
        dparams += [(f"publishedfileids[{i}]", w) for i, w in enumerate(updated + outdated)]
        details_payload = swa._get_json(swa.DETAILS_URL + "?" + urllib.parse.urlencode(dparams), 15.0)
        got = {str(d.get("publishedfileid")): d for d in (details_payload.get("response") or {}).get("publishedfiledetails") or []}
        print(f"\nKnown mods (cutoff {args.cutoff}):")
        for wid in updated + outdated:
            raw = got.get(wid)
            if not raw or raw.get("result") != 1:
                print(f"  {wid}: not returned by Steam (removed/private?)")
                failures += 1
                continue
            item = swa._parse_item(raw, cutoff_ts)
            expected_ok = wid in updated
            correct = (item.status == swa.STATUS_UPDATED) == expected_ok
            failures += 0 if correct else 1
            print(f"  {'OK  ' if correct else 'MISS'} {item.title[:40]:40s} tags={item.tags} "
                  f"updated={_date(item.time_updated)} -> {item.status} "
                  f"(you said {'updated' if expected_ok else 'outdated'})")
        print(f"\n{failures} mismatch(es)." if failures else "\nFilter agrees with every mod you listed.")

    if not args.no_save:
        os.makedirs(FIXTURE_DIR, exist_ok=True)
        with open(os.path.join(FIXTURE_DIR, "real_query.json"), "w", encoding="utf-8") as f:
            json.dump(query_payload, f, indent=1)
        if details_payload:
            with open(os.path.join(FIXTURE_DIR, "real_details.json"), "w", encoding="utf-8") as f:
                json.dump(details_payload, f, indent=1)
            with open(os.path.join(FIXTURE_DIR, "real_expectations.json"), "w", encoding="utf-8") as f:
                json.dump({"cutoff": args.cutoff, "updated": updated, "outdated": outdated}, f, indent=1)
        print(f"\nSaved responses to {FIXTURE_DIR} (no API key in them) -- the test suite now uses them.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
