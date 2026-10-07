# Workshop API fixtures

`synthetic_*.json` are hand-written in the documented shape of Steam's
`IPublishedFileService` responses. They keep the tests meaningful until
real data exists.

Run `python tools/verify_workshop_filter.py --key YOUR_KEY --updated ... --outdated ...`
to save **real** responses here as `real_*.json`. `tests/test_workshop_fixtures.py`
runs every set it finds, so once real files are present the filter is checked
against actual Steam data on every test run.
