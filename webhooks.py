"""
Outgoing alert webhooks. Both are simple POST requests, no dependency
beyond urllib. A failure to send is logged (via applog, to
~/ConanOps/conanops.log) rather than raised, since a broken webhook
URL should never crash the app or block whatever real action
triggered the alert -- but it's no longer silently lost either: the
caller (MainWindow._notify) checks notify()'s return value and shows
a tray warning when delivery actually failed.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlsplit

import applog

_log = applog.get_logger(__name__)


def _redact_url(url: str) -> str:
    """Discord/ntfy webhook URLs carry a bearer-token-equivalent secret
    in the path itself (e.g. .../webhooks/<id>/<token>), so logging the
    full URL on a failed send would leak that secret into
    conanops.log, which is far more widely shared/read than the config
    file the secret is otherwise encrypted in. Logs just the scheme and
    host, which is enough to tell webhooks apart in the log without
    exposing the token."""
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}/…"
    except ValueError:
        return "(unparseable URL)"


def _post_json(url: str, payload: dict, timeout: float = 5.0) -> bool:
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (OSError, ValueError) as e:
        # ValueError covers a malformed URL (urllib raises it from the
        # Request constructor or urlopen for things like a missing
        # scheme) -- previously only OSError was caught here, so a
        # bad/typo'd webhook URL would crash the caller instead of just
        # failing this one webhook.
        _log.warning(f"Failed to POST webhook to {_redact_url(url)}: {e}")
        return False


def send_discord(webhook_url: str, message: str) -> bool:
    if not webhook_url:
        return False
    return _post_json(webhook_url, {"content": message})


def update_discord_status(webhook_url: str, message_id: str, content: str, timeout: float = 5.0) -> Optional[str]:
    """Posts (if message_id is empty) or edits (if it isn't) a live
    status message in Discord -- used for a server's always-current
    presence (see MainWindow._check_discord_status), as opposed to
    send_discord()'s one-shot event alerts, which always post a new
    message and never touch an old one.

    A normal webhook POST discards Discord's response; getting a
    message back to edit later requires the `?wait=true` query
    parameter, which makes Discord return the created message object
    (including its id) instead of an empty 204. Editing an existing
    message is a PATCH to .../messages/{message_id} with the same
    webhook URL as its base.

    Returns the message id to persist for next time (the same one
    passed in in the "and now it's edited" case, or a new one) if
    it worked. None if it didn't -- e.g. Discord returned an error, or
    the given message_id no longer exists (someone deleted it in
    Discord, or the channel/webhook changed) -- in which case the
    caller should try again with an EMPTY message_id, which posts a
    fresh message instead of continuing to edit one that no longer
    exists. This function itself doesn't retry that automatically,
    since doing so from inside the "edit" branch would need it to know
    whether a failure means "not found" specifically versus some other
    error worth surfacing as-is."""
    if not webhook_url:
        return None
    try:
        if message_id:
            req = urllib.request.Request(
                f"{webhook_url}/messages/{message_id}",
                data=json.dumps({"content": content}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if 200 <= resp.status < 300:
                    return message_id
                return None
        else:
            req = urllib.request.Request(
                f"{webhook_url}?wait=true",
                data=json.dumps({"content": content}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
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
    """ntfy.sh takes the message as a raw POST body, not JSON, with the
    title in a header."""
    if not ntfy_url:
        return False
    try:
        req = urllib.request.Request(
            ntfy_url,
            data=message.encode("utf-8"),
            headers={"Title": title},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            return 200 <= resp.status < 300
    except (OSError, ValueError) as e:
        _log.warning(f"Failed to POST ntfy webhook to {_redact_url(ntfy_url)}: {e}")
        return False


def notify(discord_url: Optional[str], ntfy_url: Optional[str], message: str, title: str = "ConanOps") -> bool:
    """Fire-and-forget to whichever webhooks are configured. Returns
    True if every *configured* webhook succeeded (no webhooks
    configured at all also counts as success -- there was nothing to
    fail). Callers should check this and surface a failure somewhere
    the person will actually see it, since a silently-dropped crash
    alert defeats the point of having alerts."""
    ok = True
    if discord_url:
        ok = send_discord(discord_url, f"**{title}**: {message}") and ok
    if ntfy_url:
        ok = send_ntfy(ntfy_url, message, title=title) and ok
    return ok
