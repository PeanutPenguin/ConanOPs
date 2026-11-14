ConanOps runs Conan Exiles dedicated servers on a Windows PC and handles their routine maintenance. To install it, download **ConanOps-Setup** below and run it; existing copies of ConanOps update themselves.

### New in this version: "Outdated Client" is caught straight away

When a player's game and the server are on different versions, the player sees **Outdated Client** and can't join. ConanOps used to find out only at its next timed update check, up to six hours later. It now reacts the moment it happens.

- **Checks Steam as soon as a player is turned away.** ConanOps watches the server's log for a player refused over their game version. When it sees one, it asks Steam for a newer server build at once. If there is one, it installs it the usual way (players are warned first), or tells you if automatic updates are off.
- **Tells you which side is behind.** If the server is already on the newest build, you get an alert saying so, with the two version numbers, so you know the fix is on the player's side (update the game, or switch off the Legacy branch) or that the matching server build isn't out yet.
- **"Up to date" is no longer answered from memory.** SteamCMD can reuse what it last learned about the game instead of asking Steam, which could leave a server reading "Up to date" after a new build came out. ConanOps now clears that saved copy before every check.
- **Removes a pinned game version.** Lines left in `Engine.ini` by older setups (`bUseBuildIdOverride`, `BuildIdOverride`) make a server turn everyone away even when both sides are up to date. ConanOps now removes them before starting the server, keeps a copy of the file from before, and notes it in `conanops.log`.

ConanOps can't update a player's own game, so a player who is behind still has to update; what changes is that you hear about it right away, with the cause.
