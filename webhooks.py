"""
Outgoing alert webhooks (Discord, ntfy) via urllib. Send failures are logged
and returned, never raised, so a broken URL can't block the triggering action.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from email.header import Header
from typing import Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import applog

_log = applog.get_logger(__name__)

# Cloudflare (in front of Discord) blocks urllib's default User-Agent (403, error 1010).
try:
    import version as _version
    USER_AGENT = f"ConanOps/{_version.VERSION} (+https://github.com/PeanutPenguin/ConanOPs)"
except Exception:  # noqa: BLE001
    USER_AGENT = "ConanOps (+https://github.com/PeanutPenguin/ConanOPs)"

DISCORD_MAX_CHARS = 2000
_DISCORD_URL_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[\w-]+/?$")


def _headers(extra: Optional[dict] = None) -> dict:
    h = {"User-Agent": USER_AGENT}
    h.update(extra or {})
    return h


def _discord_payload(content: str) -> bytes:
    if len(content) > DISCORD_MAX_CHARS:
        content = content[:DISCORD_MAX_CHARS - 1] + "…"
    # Never ping: a player named "@everyone" must not notify the whole server.
    return json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode("utf-8")


def _with_url_parts(url: str, extra_path: str = "", extra_query: Optional[dict] = None) -> str:
    """Adds a path suffix and query values, keeping any existing query (?thread_id=...)."""
    parts = urlsplit(url.strip())
    query = dict(parse_qsl(parts.query))
    query.update(extra_query or {})
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + extra_path, urlencode(query), ""))


def discord_url_problem(url: str) -> str:
    """"" if `url` looks like a Discord webhook link, else what's wrong."""
    url = (url or "").strip()
    if not url:
        return "No Discord webhook link is set."
    base = urlunsplit(urlsplit(url)[:3] + ("", ""))
    if not _DISCORD_URL_RE.match(base):
        return ("That isn't a Discord webhook link -- it should look like "
                "https://discord.com/api/webhooks/123456789/abcDEF...")
    return ""


def ntfy_url_problem(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return "No ntfy topic link is set."
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or parts.path.strip("/") == "":
        return "That isn't an ntfy topic link -- it should look like https://ntfy.sh/your-topic-name"
    return ""


def _redact_url(url: str) -> str:
    """Scheme and host only: webhook URLs contain a secret token that must not reach the log."""
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}/…"
    except ValueError:
        return "(unparseable URL)"


def _send(req_factory, timeout: float) -> Tuple[bool, str]:
    """Sends, retrying once if rate-limited. Returns (ok, why-not)."""
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req_factory(), timeout=timeout) as resp:
                return 200 <= resp.status < 300, ""
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                try:
                    wait = float(e.headers.get("Retry-After") or 1)
                except ValueError:
                    wait = 1.0
                time.sleep(min(5.0, max(0.5, wait)))
                continue
            return False, _explain_http(e.code)
        except (OSError, ValueError) as e:
            return False, f"couldn't reach it ({e})"
    return False, "it's rate-limiting messages right now"


def _explain_http(code: int) -> str:
    if code in (401, 404):
        return "the link doesn't work anymore (the webhook was deleted, or the link was copied wrong)"
    if code == 403:
        return "it refused the message (HTTP 403)"
    if code == 400:
        return "it rejected the message (HTTP 400)"
    return f"it answered with an error (HTTP {code})"


def _post_json(url: str, payload: dict, timeout: float = 5.0) -> bool:
    try:
        body = (_discord_payload(payload["content"]) if set(payload) == {"content"}
                else json.dumps(payload).encode("utf-8"))
        ok, why = _send(lambda: urllib.request.Request(
            url.strip(), data=body, headers=_headers({"Content-Type": "application/json"}), method="POST"), timeout)
        if not ok:
            _log.warning(f"Failed to POST webhook to {_redact_url(url)}: {why}")
        return ok
    except (OSError, ValueError) as e:
        # ValueError: malformed URL (e.g. missing scheme).
        _log.warning(f"Failed to POST webhook to {_redact_url(url)}: {e}")
        return False


def send_discord(webhook_url: str, message: str) -> bool:
    if not webhook_url:
        return False
    return _post_json(webhook_url, {"content": message})


def update_discord_status(webhook_url: str, message_id: str, content: str, timeout: float = 5.0) -> Optional[str]:
    """Posts (empty message_id, using ?wait=true to get the id back) or edits
    (PATCH .../messages/<id>) a live status message. Returns the id to keep,
    or None on failure; the caller should then retry with an empty id."""
    if not webhook_url:
        return None
    try:
        if message_id:
            req = urllib.request.Request(
                _with_url_parts(webhook_url, f"/messages/{message_id}"),
                data=_discord_payload(content),
                headers=_headers({"Content-Type": "application/json"}),
                method="PATCH",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return message_id
                return None
        else:
            req = urllib.request.Request(
                _with_url_parts(webhook_url, extra_query={"wait": "true"}),
                data=_discord_payload(content),
                headers=_headers({"Content-Type": "application/json"}),
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if not (200 <= resp.status < 300):
                    return None
                body = json.loads(resp.read().decode("utf-8"))
                return body.get("id")
    except (OSError, ValueError, urllib.error.HTTPError) as e:
        _log.warning(f"Discord status update failed for {_redact_url(webhook_url)}: {e}")
        return None


def send_ntfy(ntfy_url: str, message: str, title: str = "ConanOps") -> bool:
    """ntfy takes a raw POST body (not JSON) with the title in a header."""
    if not ntfy_url:
        return False
    ok, why = _send_ntfy(ntfy_url, message, title)
    if not ok:
        _log.warning(f"Failed to POST ntfy webhook to {_redact_url(ntfy_url)}: {why}")
    return ok


def _header_value(text: str) -> str:
    """Headers are ASCII-only, so other text is RFC 2047 encoded (ntfy decodes it)."""
    try:
        text.encode("ascii")
        return text
    except UnicodeEncodeError:
        # One unwrapped encoded word: folding would break it.
        return Header(text, "utf-8").encode(maxlinelen=0)


def _send_ntfy(ntfy_url: str, message: str, title: str) -> Tuple[bool, str]:
    return _send(lambda: urllib.request.Request(
        ntfy_url.strip(), data=message.encode("utf-8"),
        headers=_headers({"Title": _header_value(title), "Content-Type": "text/plain; charset=utf-8"}),
        method="POST"), 5.0)


def test_discord(webhook_url: str, server_name: str) -> Tuple[bool, str]:
    """Sends a test message. (ok, plain-language result)."""
    problem = discord_url_problem(webhook_url)
    if problem:
        return False, problem
    body = _discord_payload(f"**ConanOps test** — alerts for {server_name} will show up here.")
    ok, why = _send(lambda: urllib.request.Request(
        webhook_url.strip(), data=body, headers=_headers({"Content-Type": "application/json"}), method="POST"), 8.0)
    return ok, ("Sent -- check your Discord channel." if ok else f"Didn't work: Discord {why}.")


def test_ntfy(ntfy_url: str, server_name: str) -> Tuple[bool, str]:
    problem = ntfy_url_problem(ntfy_url)
    if problem:
        return False, problem
    ok, why = _send_ntfy(ntfy_url, f"Alerts for {server_name} will show up here.", "ConanOps test — it works")
    return ok, ("Sent -- check the ntfy app." if ok else f"Didn't work: ntfy {why}.")


def notify(discord_url: Optional[str], ntfy_url: Optional[str], message: str, title: str = "ConanOps") -> bool:
    """Sends to configured webhooks. True if all configured ones succeeded
    (or none are set); callers should show failures to the person."""
    ok = True
    if discord_url:
        ok = send_discord(discord_url, f"**{title}**: {message}") and ok
    if ntfy_url:
        ok = send_ntfy(ntfy_url, message, title=title) and ok
    return ok
