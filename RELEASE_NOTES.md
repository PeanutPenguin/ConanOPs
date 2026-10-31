ConanOps runs Conan Exiles dedicated servers on a Windows PC and handles their routine maintenance. To install it, download **ConanOps-Setup** below and run it; existing copies of ConanOps update themselves.

### Fixed in this version

- **New servers kept the default name.** A server created with the setup wizard was listed in the game's server browser as "Conan Exiles Server" instead of the name it was given, because the name was not written to the server's own settings file until a setting was saved. ConanOps now writes each server's name, password, ports and player limit to the server's settings files before every start. If a server is affected, restarting it from ConanOps 1.0.16 applies its name.
- **Remote commands (RCON) on a new server.** The same step also rewrites RCON settings if the server replaced them during its first start, so a restart restores them.

The [User Guide](https://github.com/PeanutPenguin/ConanOPs/blob/main/USER_GUIDE.md) describes every feature in detail.

### Requirements

Windows 10 or 11 (64-bit), about 50 GB of free disk space, around 8 GB of RAM for a small group of players, and an internet connection.

ConanOps is not yet code-signed, so Windows may display "Windows protected your PC". Select **More info**, then **Run anyway**.

ConanOps is not affiliated with Funcom or Valve. Conan Exiles is a trademark of Funcom.
