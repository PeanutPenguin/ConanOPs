"""
Discord commands: "!status" and "!restart" typed in one Discord channel.

A webhook can only post, so this uses a bot (token from the Discord
Developer Portal). It polls the channel's new messages every few seconds
over Discord's REST API -- no extra libraries -- and posts replies. All the
HTTP happens on DiscordBotWorker's thread; commands are handed to the GUI
thread (`command` signal) and the answer comes back through reply().
"""
from __future__ import annotations

import json
import queue
import re
import time
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

from PySide6.QtCore import QThread, Signal

import applog
import webhooks

_log = applog.get_logger(__name__)

API = "https://discord.com/api/v10"
POLL_SECONDS = 4
HELP = ("**ConanOps commands**\n`!status` -- every server's status and who's online\n"
        "`!restart` (or `!restart <server name>`) -- warns players, saves and restarts (allowed people only)")


def parse_command(content: str) -> Optional[Tuple[str, str]]:
    """'!restart Siptah' -> ("restart", "Siptah"); None for anything else."""
    m = re.match(r"^\s*!(status|restart|help)\b\s*(.*)$", content or "", re.IGNORECASE | re.DOTALL)
    return (m.group(1).lower(), m.group(2).strip()) if m else None


def admin_ids(text: str) -> set:
    return set(re.findall(r"\d{15,21}", text or ""))


def pick_server(servers: list, name: str):
    """(server, "") or (None, why). One server needs no name; else a name or its start."""
    installed = [s for s in servers if s.install_dir]
    if not installed:
        return None, "There's no server set up yet."
    if not name:
        if len(installed) == 1:
            return installed[0], ""
        return None, "Which one? `!restart <name>` -- " + ", ".join(s.name for s in installed)
    low = name.lower()
    exact = [s for s in installed if s.name.lower() == low]
    found = exact or [s for s in installed if s.name.lower().startswith(low)] or \
        [s for s in installed if low in s.name.lower()]
    if len(found) == 1:
        return found[0], ""
    if not found:
        return None, f"No server called \"{name}\". Servers: " + ", ".join(s.name for s in installed)
    return None, "That matches more than one: " + ", ".join(s.name for s in found)


class _HttpError(Exception):
    def __init__(self, code: int, retry_after: float = 0.0):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def _call(token: str, method: str, path: str, body: Optional[dict] = None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Authorization": f"Bot {token}", "User-Agent": webhooks.USER_AGENT,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as e:
        retry = 0.0
        if e.code == 429:
            try:
                retry = float(json.loads(e.read().decode("utf-8")).get("retry_after", 1))
            except (ValueError, OSError, AttributeError):
                retry = 2.0
        raise _HttpError(e.code, retry) from None


class DiscordBotWorker(QThread):
    command = Signal(str, str, str, str)  # message id, author id, author name, content
    problem = Signal(str)                 # plain-language reason it stopped or can't read

    def __init__(self, token: str, channel_id: str, parent=None):
        super().__init__(parent)
        self.token = token.strip()
        self.channel_id = channel_id.strip()
        self._replies: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self._warned_content = False

    def reply(self, message_id: str, text: str) -> None:
        """Thread-safe: queues a reply to post on the bot's own thread."""
        self._replies.put((message_id, text))

    def stop(self) -> None:
        self.requestInterruption()

    def _send_replies(self) -> None:
        while True:
            try:
                message_id, text = self._replies.get_nowait()
            except queue.Empty:
                return
            body = {"content": text[:webhooks.DISCORD_MAX_CHARS], "allowed_mentions": {"parse": []}}
            if message_id:
                body["message_reference"] = {"message_id": message_id, "fail_if_not_exists": False}
            try:
                _call(self.token, "POST", f"/channels/{self.channel_id}/messages", body)
            except (_HttpError, OSError) as e:
                _log.warning(f"Discord bot couldn't reply: {e}")

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.isInterruptionRequested():
            self._send_replies()
            self.msleep(200)

    def run(self) -> None:
        last_id = ""
        while not self.isInterruptionRequested():
            try:
                if not last_id:
                    # Start from the newest message: never act on old commands.
                    newest = _call(self.token, "GET", f"/channels/{self.channel_id}/messages?limit=1") or []
                    last_id = newest[0]["id"] if newest else "0"
                msgs = _call(self.token, "GET", f"/channels/{self.channel_id}/messages?after={last_id}&limit=50") or []
                for m in sorted(msgs, key=lambda x: int(x["id"])):
                    last_id = m["id"]
                    author = m.get("author") or {}
                    if author.get("bot"):
                        continue
                    content = m.get("content") or ""
                    if not content and not m.get("attachments") and not m.get("embeds") and not self._warned_content:
                        self._warned_content = True
                        self.problem.emit("The bot can't read messages: turn on \"Message Content Intent\" for it in "
                                          "the Discord Developer Portal (Bot page).")
                    if parse_command(content):
                        self.command.emit(m["id"], str(author.get("id", "")),
                                          author.get("global_name") or author.get("username") or "someone", content)
            except _HttpError as e:
                if e.code == 429:
                    self._sleep(max(1.0, e.retry_after))
                    continue
                why = {401: "Discord says the bot token is wrong -- copy it again from the Developer Portal.",
                       403: "The bot can't see that channel -- invite it to your server and give it View Channel, "
                            "Send Messages and Read Message History there.",
                       404: "Discord can't find that channel ID -- right-click the channel → Copy Channel ID."
                       }.get(e.code, f"Discord answered with error {e.code}.")
                self.problem.emit(why)
                if e.code in (401, 403, 404):
                    return
                self._sleep(30)
                continue
            except (OSError, ValueError, KeyError) as e:
                _log.warning(f"Discord bot poll failed: {e}")
                self._sleep(15)
                continue
            self._sleep(POLL_SECONDS)


def status_text(rows: List[dict], link: str = "") -> str:
    """rows: {name, running, players, max_players, busy, names}."""
    if not rows:
        return "No servers set up yet."
    lines = []
    for r in rows:
        if r.get("busy"):
            state = f"🟡 {r['busy']}"
        elif r["running"]:
            cap = f"/{r['max_players']}" if r.get("max_players") else ""
            who = f": {', '.join(r['names'][:15])}" if r.get("names") else ""
            state = f"🟢 Online, {r['players']}{cap} players{who}"
        else:
            state = "🔴 Offline"
        lines.append(f"**{r['name']}** — {state}")
    if link:
        lines.append(link)
    return "\n".join(lines)
