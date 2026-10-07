"""
Dynamic DNS via DuckDNS (https://www.duckdns.org) -- for home-hosting
behind a residential ISP without a static IP: the public IP can change,
silently breaking everyone's saved server entry until someone happens
to notice and re-share a new address. A single periodic call to
DuckDNS's update endpoint keeps a chosen subdomain pointed at whatever
this machine's current public IP actually is.

DuckDNS specifically (rather than building support for every DDNS
provider) because it's free, needs no account beyond a GitHub/Google/
etc. sign-in, and has the simplest possible update API of the major
options -- a single authenticated GET request, no per-provider request
signing or OAuth flow to implement.
"""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

UPDATE_URL = "https://www.duckdns.org/update"


@dataclass
class DuckDnsResult:
    ok: bool
    message: str
    ip: str = ""


def update(domain: str, token: str, timeout: float = 8.0) -> DuckDnsResult:
    """Points `domain`.duckdns.org at whatever public IP this request
    appears to come from -- DuckDNS auto-detects it from the request
    itself, so nothing here needs to separately determine this
    machine's own public IP first (and can't be fooled by a stale
    cached value the way a separately-fetched-then-compared IP could
    be). `domain` is just the subdomain (no ".duckdns.org" suffix).

    Never raises: any network/HTTP problem comes back as ok=False with
    a human-readable message, same convention as steam_workshop_api.py
    and network_setup.py's other external calls."""
    if not domain or not token:
        return DuckDnsResult(ok=False, message="No DuckDNS domain/token configured -- add them on App Settings.")

    params = {"domains": domain, "token": token, "verbose": "true"}
    url = UPDATE_URL + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8").strip()
    except urllib.error.HTTPError as e:
        return DuckDnsResult(ok=False, message=f"DuckDNS update failed: HTTP {e.code}")
    except Exception as e:  # noqa: BLE001 - network errors of every shape land here, all reported the same way
        return DuckDnsResult(ok=False, message=f"DuckDNS update failed: {e}")

    # Verbose response: "OK", then the IPv4, the IPv6 (often blank) and
    # UPDATED/NOCHANGE -- one per LINE in DuckDNS's actual output (some
    # docs show them space-separated on one line). Tokenize across the
    # whole body so either layout parses; a plain "KO" means the
    # domain/token combination was rejected outright.
    tokens = body.split()
    if not tokens or tokens[0] != "OK":
        return DuckDnsResult(ok=False, message=f"DuckDNS rejected this domain/token: {body or '(empty response)'}")

    ip = next((t for t in tokens[1:] if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", t)), "")
    return DuckDnsResult(ok=True, message=" ".join(tokens), ip=ip)
