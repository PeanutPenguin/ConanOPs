ConanOps runs Conan Exiles dedicated servers on a Windows PC and handles their routine maintenance. To install it, download **ConanOps-Setup** below and run it; existing copies of ConanOps update themselves.

### New in this version

- **Outside reachability check.** Diagnostics now checks whether the server can be seen from the internet. It asks Steam whether it lists the server at the PC's public address and sends a test query to that address. When Steam does not list a server that has been running for several minutes, Diagnostics reports this as a problem and names the likely cause. A listing alone is not treated as proof that players can join, because some routers let Steam's replies through without a port forward.
- **Fix buttons.** Most problems that Diagnostics finds now have a button that fixes them: Repair Networking, Use Free Ports, Repair Game Files, Install SteamCMD, Install Visual C++ Runtime, Turn On RCON, Restore Latest Backup, Set Up Backup Folder, Download Mods, Fix Mod Problems, Start or Restart Server, and others. Diagnostics runs again after a fix. Problems are listed before the checks that passed.
- **Reading the server's logs.** Diagnostics reads the server's latest log, its crash reports and its mod list, and explains in plain language why the server would not start or crashed. It recognises, among other causes, a missing mod file, a crash inside a particular mod, a port already in use, insufficient memory, a damaged world save, a full disk and a failed Steam connection. Unrecognised errors are quoted as the server reported them. It also reports when a server has crashed repeatedly in the last 24 hours.
- **Crash alerts give the likely cause.** When a server crashes, or ConanOps stops restarting it after repeated crashes, the alert now includes the most likely cause from the server's logs.
- **RCON.** Diagnostics now reports when RCON is off, since ConanOps then cannot save the world before stopping the server.
- **Fewer firewall prompts.** ConanOps now adds a firewall rule for the server program itself, covering every port the server opens (including RCON), so Windows should no longer ask whether to allow the server. Existing servers receive the rule after **Repair Networking**.
- **No leftover firewall rules.** Uninstalling and **Delete Everything** remove the rules Windows created for ConanOps itself and for server programs whose folders no longer exist. Removing a server also clears rules left by server folders deleted earlier. Rules for the game itself, and for servers that are kept, are not touched.

The [User Guide](https://github.com/PeanutPenguin/ConanOPs/blob/main/USER_GUIDE.md) describes every feature in detail.

### Requirements

Windows 10 or 11 (64-bit), about 50 GB of free disk space, around 8 GB of RAM for a small group of players, and an internet connection.

ConanOps is not yet code-signed, so Windows may display "Windows protected your PC". Select **More info**, then **Run anyway**.

ConanOps is not affiliated with Funcom or Valve. Conan Exiles is a trademark of Funcom.
