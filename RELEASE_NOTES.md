ConanOps runs Conan Exiles dedicated servers on a Windows PC and handles their routine maintenance. To install it, download **ConanOps-Setup** below and run it; existing copies of ConanOps update themselves.

### Fixed in this version: updates, backups and staying up

This version fixes eight problems found in a review of how ConanOps keeps servers running by themselves.

- **Updates installed with automatic updates off.** The daily restart checked for a game update and installed it even when **Install new server builds automatically** was off. It now only restarts; the new build waits on the Updates page.
- **Server left stopped after a failed update.** When an update failed, ConanOps held the server stopped and tried again 30 minutes later. If ConanOps itself was restarted in between, a later update succeeded but the server was never started. It now starts once the update is in.
- **Server stopped for an update that couldn't run.** Disk space was checked after the server had been stopped, so a drive that was too full took the server down and held it there on every update. The check now comes first, and the server keeps running.
- **Two servers sharing one backup folder.** Servers installed side by side were given the same default backup folder, so their backups were mixed: old ones were thinned out as one list, and a restore could put back the other server's world. New servers now get their own folder, another server's folder can't be chosen in Backups settings, and you get an alert if two of your servers already share one. Nothing is moved for you: pick a separate folder in Server Settings → Backups.
- **Updated mods not loaded.** With **Update Workshop mods automatically** on, the daily mod refresh could download a new mod version while the server was running. The server then kept running the old version until its next restart, and players with the new one couldn't join. The refresh now leaves a running server alone, so the hourly check warns players and restarts it with the new mod.
- **World save before a stop or backup.** ConanOps asks the server to save before stopping it or backing it up, but never read the answer. It now does: the answer is written to `conanops.log`, and if the server says it doesn't know the command, ConanOps no longer waits as if a save were happening and says so in the log.
- **Update rolled back, then an error.** When a ConanOps update failed to start and the previous version was put back, ConanOps carried on as the failed version and showed a second error. It now restarts itself on the restored version.
- **Servers left down after a refused Windows restart.** When Windows refused the restart for Windows Update, the servers ConanOps had stopped for it stayed stopped. They are now started again.
