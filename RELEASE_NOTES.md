ConanOps runs Conan Exiles dedicated servers on a Windows PC and handles their routine maintenance. To install it, download **ConanOps-Setup** below and run it; existing copies of ConanOps update themselves.

### New in this version: mods you can trust, and hearing about problems sooner

**Mods**

- **Mod updates are noticed within minutes, not an hour.** Players' games take a new mod version straight away and can't join until the server has it too. ConanOps now asks the Workshop every 5 minutes instead of hourly.
- **You're told even with automatic mod updates off.** Before, nothing mentioned a new mod version unless **Update Workshop mods automatically** was on. Now you get an alert, and nothing restarts.
- **Removed mods raise an alert.** A mod deleted or made private on the Workshop can't be downloaded by players any more. ConanOps now says which one.
- **"Updated for the current patch" follows real evidence.** Each time a server starts and answers, ConanOps records which version of each mod it ran on which game build. An older mod that has run on your current build shows **Working on this build** instead of a warning; one the server failed with right after it updated shows **Failed on this build**.
- **The patch date keeps itself current.** The date a mod must be updated since now comes from a list of mod-breaking game patches published with ConanOps, checked daily. The date in App Settings still works and the later of the two is used.
- **A broken mod update is named.** If the server keeps crashing right after mods updated, the alert names those mods instead of blaming the game update.
- **Fix load order.** A mod listed before a mod it needs is flagged, and **Fix load order** in the Mods menu moves required mods above the ones that need them. This uses the Workshop's "required items", which needs a Steam API key.

**Alerts**

- **Back Online.** After a PC restart or a crash, ConanOps says when it's running again and which servers it started.
- **Heartbeat link.** App Settings → Alerts has a new field: ConanOps calls that link every 5 minutes. Paste a check link from a free uptime service such as healthchecks.io and that service alerts you when the PC is off or ConanOps is gone, which nothing on the PC itself can do.

**Discord commands**

- `!players` lists everyone online, for anyone in the channel.
- `!backup`, `!update` and `!link` for people on the allowed list: back up now, check Steam and install a new build, and get the link to the web version.
- The Discord commands section now shows **Connected** once the bot can read the channel, so a bot that isn't set up is obvious.

**Fixed**

- **World report on upgraded servers.** Servers upgraded from before the Enhanced update keep their old `game.db` next to the live `game_0.db`; the world report read the old one. It now reads the live world.
- **Restart window with the same start and end time** never restarted the server. It now means the hour from that time.
- **A damaged server entry in the settings file** stopped ConanOps from starting. It's now skipped.
- **Restoring a backup** no longer clears pending changes of world files the backup doesn't contain.
- **Large mod lists** get more time to download before ConanOps gives up.
