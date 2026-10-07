"""Step-by-step alert setup guides shown in App Settings → Alerts
(shared by the desktop app and the web UI)."""
from __future__ import annotations

from typing import List, Tuple

Guide = Tuple[str, str, List[Tuple[str, str]]]  # (title, intro, [(step title, step text)])

DISCORD: Guide = (
    "How do I get a Discord webhook link?",
    "A webhook lets ConanOps post into one channel of your Discord server. It takes about a minute, and one "
    "webhook covers all your Conan servers.",
    [
        ("Open your Discord server's settings",
         "In Discord, click your server's name at the top left and choose Server Settings. On a phone, tap the "
         "server name, then Settings. You need the \"Manage Webhooks\" permission -- the server owner has it."),
        ("Go to Integrations → Webhooks",
         "Choose Integrations in the settings list, then Webhooks (on a server with none yet, the button says "
         "Create Webhook)."),
        ("Create a webhook for the channel",
         "Click New Webhook. Give it a name like ConanOps, and pick the channel the alerts should appear in -- a "
         "private admin channel works well. Click Save Changes if Discord asks."),
        ("Copy the link",
         "Click Copy Webhook URL. It looks like https://discord.com/api/webhooks/123456789/abcDEF... Treat it like a "
         "password: anyone with it can post in that channel."),
        ("Paste it here and test",
         "Paste it into \"Discord webhook link\" in App Settings → Alerts and click Send Test -- a test message "
         "should appear in the channel within a few seconds. It's saved when you leave the box."),
        ("Optional: a live status message",
         "Turn on \"Also keep a live status message\" to keep one message per server in that channel, always "
         "showing whether it's online and how many players are on. It updates every ~5 minutes instead of posting "
         "new ones."),
    ],
)

NTFY: Guide = (
    "How do I set up ntfy phone notifications?",
    "ntfy is a free app that shows alerts as notifications on your phone -- no Discord needed.",
    [
        ("Install the ntfy app",
         "Get \"ntfy\" from the Google Play Store or the Apple App Store (or open ntfy.sh in a web browser)."),
        ("Pick a private topic name",
         "Tap + to subscribe to a topic. Use a long, hard-to-guess name such as conanops-7f3k9x2q -- anyone who "
         "knows the name can read the alerts. Leave the server as ntfy.sh."),
        ("Allow notifications",
         "When your phone asks, allow notifications for the ntfy app."),
        ("Paste the topic link here and test",
         "Enter https://ntfy.sh/ followed by your topic name (for example https://ntfy.sh/conanops-7f3k9x2q) into "
         "\"ntfy topic link\" in App Settings → Alerts and click Send Test -- your phone should buzz."),
    ],
)

ALL = {"discord": DISCORD, "ntfy": NTFY}


def as_json(guide: Guide) -> dict:
    title, intro, steps = guide
    return {"title": title, "intro": intro,
            "steps": [{"number": i, "title": t, "body": b} for i, (t, b) in enumerate(steps, 1)]}
