ConanOps runs Conan Exiles dedicated servers on your own Windows PC without the usual fiddling. Download **ConanOps-Setup** below and run it; if you already have ConanOps, it updates itself.

### New in this version

- **Takes over servers already on your PC, completely.** ConanOps now finds servers in more places: running ones, every Steam and SteamCMD library (including renamed folders), folders your old scripts and scheduled tasks point at, and, when you click "Add server", folders deep inside every drive. Can't find yours? **Browse** to its folder.
- **Switches off the old launchers.** Restart-loop .bat files, scheduled tasks, startup items, Windows services (like NSSM), monitor scripts and other server managers that also start or update the server are listed, and ConanOps switches them off so only it is in charge. Nothing is deleted: removing the server from ConanOps can turn them back on. If a service was running the server, ConanOps restarts it once under its own control.
- **Keeps everything as it was.** Launch options (like -nosteam), the bind address, mods loaded from another Steam folder and non-Workshop mod files are all kept. RCON is turned on (ConanOps needs it to save the world before restarts) and a backup is made before anything changes.
- **Safer removal.** Removing a server that was already on your PC no longer deletes its folder unless you tick the box, and never deletes a SteamCMD folder ConanOps didn't install. "Delete Everything" leaves those servers alone too.
- **Fixes:** servers taken over by 1.0.10 got no backup folder and never backed up; they do now. Server name and password are read from the Engine.ini section Conan actually uses.

### Requirements

Windows 10 or 11 (64-bit), about 50 GB of free space, around 8 GB of RAM for a small group, and an internet connection.

ConanOps isn't code-signed yet, so Windows may say "Windows protected your PC" – click **More info → Run anyway**.

ConanOps isn't affiliated with Funcom or Valve. Conan Exiles is a trademark of Funcom.
