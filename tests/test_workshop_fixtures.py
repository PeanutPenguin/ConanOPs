"""Runs the Workshop filter against every saved response set in
tests/fixtures/workshop/ -- the synthetic ones that ship with the repo,
plus any real_*.json saved by tools/verify_workshop_filter.py."""
from __future__ import annotations

import glob
import json
import os
import urllib.request

import pytest

import steam_workshop_api as swa

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "workshop")
PREFIXES = sorted({os.path.basename(p).split("_")[0] for p in glob.glob(os.path.join(FIXTURES, "*_query.json"))})


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _load(prefix, name):
    path = os.path.join(FIXTURES, f"{prefix}_{name}.json")
    if not os.path.exists(path):
        pytest.skip(f"no {prefix}_{name}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_every_item_in_a_saved_query_gets_a_valid_status(prefix, monkeypatch):
    payload = _load(prefix, "query")
    payload["response"].pop("next_cursor", None)  # one page only
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0, **k: _Resp(payload))

    result = swa.search("KEY", "", show=set(swa.ALL_STATUSES), count=500)

    assert result.ok
    assert result.items
    assert all(it.status in swa.ALL_STATUSES for it in result.items)


@pytest.mark.parametrize("prefix", PREFIXES)
def test_known_mods_are_classified_as_expected(prefix, monkeypatch):
    payload = _load(prefix, "details")
    exp = _load(prefix, "expectations")
    monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout=8.0, **k: _Resp(payload))

    result = swa.get_details(exp["updated"] + exp["outdated"], api_key="KEY",
                             cutoff_ts=swa.cutoff_timestamp(exp["cutoff"]))

    assert result.ok
    for wid in exp["updated"]:
        assert result.items[wid].status == swa.STATUS_UPDATED, wid
    for wid in exp["outdated"]:
        assert result.items[wid].status != swa.STATUS_UPDATED, wid
