ConanOps runs Conan Exiles dedicated servers on a Windows PC and handles their routine maintenance. To install it, download **ConanOps-Setup** below and run it; existing copies of ConanOps update themselves.

### New in this version

- **Fewer firewall prompts.** ConanOps now adds a firewall rule for the server program itself. The rule covers every port the server opens, including RCON, so Windows should no longer ask whether to allow the server. The rule is added the next time a server's networking is set up or repaired (**Server Settings → Network & Ports → Repair Networking**).
- **No leftover firewall rules.** Uninstalling ConanOps and **Delete Everything** now remove the rules Windows created for ConanOps itself (including copies run under other names, such as conanops(1).exe) and for server programs whose folders no longer exist. Removing a server also clears rules left behind by server folders deleted earlier. Rules for the game itself, and for servers that are kept, are not touched.
- **Delete Everything** no longer removes Windows' firewall rules for servers that were on the PC before ConanOps, since those servers are kept.

The [User Guide](https://github.com/PeanutPenguin/ConanOPs/blob/main/USER_GUIDE.md) describes every feature in detail.

### Requirements

Windows 10 or 11 (64-bit), about 50 GB of free disk space, around 8 GB of RAM for a small group of players, and an internet connection.

ConanOps is not yet code-signed, so Windows may display "Windows protected your PC". Select **More info**, then **Run anyway**.

ConanOps is not affiliated with Funcom or Valve. Conan Exiles is a trademark of Funcom.
