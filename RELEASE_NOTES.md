ConanOps runs Conan Exiles dedicated servers on a Windows PC and handles their routine maintenance. To install it, download **ConanOps-Setup** below and run it; existing copies of ConanOps update themselves.

## ConanOps 2.0: set it and forget it

### Players are warned before a restart
The daily restart, game updates, mod updates and scheduled events no longer wait for an empty server (or skip the day). If people are playing, ConanOps posts a warning in game chat 15, 5 and 1 minutes before, then goes ahead, or straight away if everyone leaves. The Dashboard shows the countdown, with **Call It Off**. Prefer the old way? Set **If people are playing** to *Wait until everyone has left* (Server Settings → Automation, Advanced). Warnings need RCON.

### Mods update themselves
ConanOps checks the Steam Workshop every hour. When one of a server's mods gets a new version, players are warned, and the server restarts with it, so people whose game already has the new version can keep joining. On by default; switch it off under Server Settings → Automation (Advanced).

### Backups you can count on
- **Never half-written.** The world is copied by SQLite itself, so a save happening at that moment can't tear the copy, and every backup is checked before it's kept. A backup that fails the check sends an alert.
- **In the background.** Backups no longer freeze the window, and a failed one is retried after 30 minutes instead of every minute.
- **Keeps what the labels say.** Every backup from the last day, then one a day for the days you choose, then one a week for the weeks you choose. Your own and imported backups are never deleted. Safety backups (before an update or a restore) no longer pile up forever: the newest 3 and the last 14 days are kept.
- **A second copy.** Pick another drive, a USB disk or a cloud-synced folder, and every backup is copied there too (Backups → More options).
- **Safer restores.** The backup's world is checked before it replaces yours, and nothing is restored if the server won't stop.

### History & World (new page)
- **History:** players, CPU and memory over the last day, week, month or three months, with uptime and a timeline of starts, crashes, restarts, updates, backups and events.
- **World report:** clans and how much each has built, players who haven't been on for 30+ days (and what they own), and how the world file is growing. It's read from a backup, never the live world, so it's safe while people play.

### Events (new page)
- **Settings events:** change settings on a schedule, like double XP from Friday evening to Sunday night, or a different message of the day. The server restarts (with warnings) when it starts and ends, and your usual settings come back afterwards.
- **Scheduled messages:** post a message in game chat at set times, once or every few hours.

### Weekly report
Every Monday at 9:00 (change it in App Settings → Alerts), each server's week goes to your Discord channel and ntfy: uptime, crashes, restarts, updates, players and backups.

### Also in the web version
History charts, world reports, events, the weekly report settings and calling off a countdown all work from your phone.

### Fixed
- Removing a server and its files no longer deletes the backups you chose to keep.
- **Start** could launch a second copy of a server while an update was running; it now waits.
- **Stop** said the server had stopped when it hadn't. Restarts, updates and restores no longer carry on with a server that won't stop.
- A server that takes a long time to load (big world, many mods) is no longer restarted as "frozen" before it has finished starting.
- The log watcher could stop silently after an error, so player counts went stale.
- Two SteamCMD runs could use the same folder at once and damage downloads.
- A setting that appeared twice in a settings file only had one copy changed, so the change didn't take effect.
- Passwords and links that couldn't be decrypted (for example after moving to a new PC) were wiped on the next save; they're now kept.
- "Reopen ConanOps if it closes" didn't recognise a ConanOps running with admin rights.

### Finding servers already on the PC
- **Admin password reported as missing:** ConanOps now checks the launch options and every settings file.
- **One server found twice** through two different folder paths is now recognised as one.
- Old copies and spare folders are no longer ticked by default; each server shows when its world was last saved.
- **Apply** on a server ConanOps took over changes only what you changed.
- Launch settings, start scripts, join passwords, whitelists, bans and mod lists are read more reliably, and a server ConanOps just took over isn't stopped for a needless update.
