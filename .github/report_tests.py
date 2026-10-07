"""Turns a pytest JUnit XML report into one GitHub Actions annotation that
lists every failed test with the first line of its error."""
import sys
import xml.etree.ElementTree as ET

try:
    root = ET.parse(sys.argv[1]).getroot()
except (OSError, ET.ParseError, IndexError):
    sys.exit(0)
lines = []
total = failed = 0
for case in root.iter("testcase"):
    total += 1
    bad = case.find("failure")
    if bad is None:
        bad = case.find("error")
    if bad is None:
        continue
    failed += 1
    msg = (bad.get("message") or bad.text or "").strip().splitlines()
    first = msg[0][:200] if msg else ""
    lines.append(f"{case.get('classname', '')}::{case.get('name', '')} -- {first}")
if failed:
    body = f"{failed} of {total} tests failed on Windows:%0A" + "%0A".join(
        l.replace("%", "%25").replace("\r", "").replace("\n", " ") for l in lines[:150])
    print(f"::warning title=Windows test failures::{body}")
else:
    print(f"::notice title=Windows tests::All {total} tests passed on Windows.")
