ConanOps runs Conan Exiles dedicated servers on a Windows PC and handles their routine maintenance. To install it, download **ConanOps-Setup** below and run it; existing copies of ConanOps update themselves.

### Fixed in this version: finding servers already on the PC

This version fixes how ConanOps finds and takes over a Conan Exiles server that was already installed on the PC.

- **Admin password reported as missing.** ConanOps only looked for the admin password in one place, so a server with one set elsewhere showed none. It now checks the server's launch options and all of its settings files. A blank entry in one file no longer hides the real password in another. If no admin password turns up, ConanOps says so.
- **One server found twice.** The same server reached by two different folder paths (a shortcut-style folder link, a short path name or a OneDrive folder) was listed and added twice. ConanOps now recognizes it as one server, and never manages the same server folder twice.
- **Old copies offered as servers.** A folder left behind when Funcom renamed the server's install folder, or a spare copy with no world, is no longer ticked by default. Each server in the list shows when its world was last saved, and why any server was left unticked.
- **Settings reset by Apply.** On a server ConanOps took over, pressing Apply on a settings page could overwrite settings ConanOps hadn't read, including the admin password. Apply now changes only what you changed.
- **Join password, whitelist and bans.** A blank line in one section of the settings file no longer hides the real join password. The server's whitelist and ban list are now kept when ConanOps takes it over.
- **Launch settings.** Settings written into the server's launch line (ports, player limit, passwords, RCON port) are now read correctly. Unfilled placeholders such as `YOUR_PUBLIC_IP` are no longer used as the server's address.
- **Launch scripts.** ConanOps reads more kinds of start scripts: PowerShell lines, comments, folder variables in .bat files, and the small `ConanSandboxServer.exe` starter.
- **Other programs.** The game itself and unrelated programs named "conan" are no longer treated as other server launchers to switch off. A launcher for one server is no longer switched off when another server is taken over.
- **No surprise update after takeover.** ConanOps now reads which game build a server is on, so it no longer stops a server it just took over to "update" it.
- **Mod list.** Mod lists saved in another text format are read correctly, and mods switched off in the mod list stay in the list, switched off.
- **"Not Now"** waits a week before asking about the same server again. Removing a server while keeping its files no longer brings it back as a found server. Clicking **Add server** in the first seconds after opening ConanOps now works.
- **The takeover message** now says that RCON may be turned on and that the daily restart, automatic updates and scheduled backups will start running. It warns when a running server can't be saved before its first restart.
