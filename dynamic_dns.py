"""Dynamic DNS via DuckDNS: keeps a subdomain pointed at this PC's changing
home IP. DuckDNS was chosen because it's free and its update API is one GET."""
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
    """Points `domain`.duckdns.org (subdomain only) at the IP DuckDNS sees this
    request come from. Never raises; failures return ok=False with a message."""
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

    # Verbose reply is "OK", IPv4, IPv6, UPDATED/NOCHANGE, one per line (docs
    # sometimes show one line), so split on any whitespace. "KO" = rejected.
    tokens = body.split()
    if not tokens or tokens[0] != "OK":
        return DuckDnsResult(ok=False, message=f"DuckDNS rejected this domain/token: {body or '(empty response)'}")

    ip = next((t for t in tokens[1:] if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", t)), "")
    return DuckDnsResult(ok=True, message=" ".join(tokens), ip=ip)
