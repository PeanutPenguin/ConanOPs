ConanOps runs Conan Exiles dedicated servers on a Windows PC and handles their routine maintenance. To install it, download **ConanOps-Setup** below and run it; existing copies of ConanOps update themselves.

### New in this version

- **Taking over existing servers.** ConanOps now finds servers in more locations: running servers, every Steam and SteamCMD library (including renamed folders), folders named by existing scripts and scheduled tasks, and, after **Add server** is selected, folders further down on every drive. A server that is not found can be selected with **Browse**.
- **Other launchers are switched off.** Restart-loop batch files, scheduled tasks, startup items, Windows services (such as NSSM), monitoring scripts and other server managers that start or update the same server are listed and then switched off, so that ConanOps alone manages the server. Nothing is deleted, and removing the server from ConanOps can turn these launchers back on. If a Windows service was running the server, the server restarts once under ConanOps.
- **Existing configuration is kept.** Launch options (for example `-nosteam`), the network address, mods stored in another Steam folder and mod files that do not come from the Steam Workshop are all preserved. RCON is turned on, since ConanOps uses it to save the world before restarts, and a backup is made before any change.
- **Safer removal.** Removing a server that was on the PC before ConanOps no longer deletes its folder unless this is selected, and a SteamCMD folder that ConanOps did not install is never deleted. **Delete Everything** also leaves such servers in place.
- **Fewer firewall prompts and no leftover rules.** ConanOps now adds a firewall rule for the server program itself, which covers every port the server opens (including RCON), so Windows should no longer ask whether to allow it. Uninstalling and **Delete Everything** remove the rules Windows created for ConanOps and for servers whose folders no longer exist, and removing a server also clears rules left by server folders deleted earlier. Rules for the game itself are left alone.
- **Corrections.** Servers taken over in version 1.0.10 were not given a backup folder and were therefore never backed up; this is fixed. The server name and password are now read from the section of Engine.ini that Conan Exiles uses.

The [User Guide](https://github.com/PeanutPenguin/ConanOPs/blob/main/USER_GUIDE.md) describes every feature in detail.

### Requirements

Windows 10 or 11 (64-bit), about 50 GB of free disk space, around 8 GB of RAM for a small group of players, and an internet connection.

ConanOps is not yet code-signed, so Windows may display "Windows protected your PC". Select **More info**, then **Run anyway**.

ConanOps is not affiliated with Funcom or Valve. Conan Exiles is a trademark of Funcom.
