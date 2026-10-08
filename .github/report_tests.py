"""Turns a pytest JUnit XML report into one GitHub Actions annotation that
lists every failed test with the first line of its error."""
import sys
import xml.etree.ElementTree as ET

try:
    root = ET.parse(sys.argv[1]).getroot()
except (OSError, ET.ParseError, IndexError):
    # No report: pytest was stopped (a test hung past --timeout, or crashed).
    # Show the end of its output instead.
    tail = []
    if len(sys.argv) > 2:
        try:
            with open(sys.argv[2], encoding="utf-8", errors="replace") as f:
                tail = [l.rstrip() for l in f.read().splitlines() if l.strip()][-40:]
        except OSError:
            pass
    body = "pytest stopped before writing its report. Last output:%0A" + "%0A".join(
        l.replace("%", "%25").replace("\r", "")[:300] for l in tail)
    print(f"::warning title=Windows tests stopped early::{body}")
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
