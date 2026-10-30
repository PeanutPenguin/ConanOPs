# ConanOps User Guide

This guide describes how to install ConanOps, set up a Conan Exiles dedicated server with it, and keep that server running with as little manual work as possible. It is written for people who host a server on their own Windows PC. No programming knowledge is assumed.

## Contents

1. [Overview](#1-overview)
2. [Requirements](#2-requirements)
3. [Installation](#3-installation)
4. [Setting up a new server](#4-setting-up-a-new-server)
5. [Taking over a server that is already on the PC](#5-taking-over-a-server-that-is-already-on-the-pc)
6. [The main window](#6-the-main-window)
7. [Server Settings](#7-server-settings)
8. [Mods](#8-mods)
9. [Backups and restoring](#9-backups-and-restoring)
10. [Updates](#10-updates)
11. [Unattended operation](#11-unattended-operation)
12. [Alerts and Discord](#12-alerts-and-discord)
13. [The web version](#13-the-web-version)
14. [Removing a server and uninstalling](#14-removing-a-server-and-uninstalling)
15. [Troubleshooting](#15-troubleshooting)
16. [Known limitations](#16-known-limitations)

---

## 1. Overview

ConanOps is a Windows application for running up to five Conan Exiles dedicated servers on one PC. It downloads the server software, configures the network, edits the server's settings files, makes backups, installs game and mod updates, and restarts servers when they stop unexpectedly. These tasks would otherwise require a combination of command-line tools, batch files and manual editing of configuration files.

The application is designed around two principles. First, routine maintenance should not depend on anyone being present: backups, restarts and updates run on a schedule and are postponed while players are online. Second, decisions that belong to the host, such as the server's name, rules and gameplay rates, remain in the host's hands. ConanOps changes only the settings it is asked to change and leaves every other line of the server's configuration files as it was.

ConanOps does not replace Conan Exiles, Steam or the router. It is not affiliated with Funcom or Valve.

## 2. Requirements

- Windows 10 or Windows 11 (64-bit).
- About 50 GB of free disk space for one server; 75 GB or more is advisable, since mods, backups and updates also need room.
- Around 8 GB of RAM for a small group of players. Large or heavily modded servers need more.
- An internet connection. A graphics card is not required.

Players outside the host's home network can connect only if two further conditions are met. The router must either support UPnP (which ConanOps uses to open the ports automatically) or have the ports forwarded by hand, and the internet provider must assign the connection a public IP address. Some providers place customers behind shared addresses (often called CGNAT), in which case outside players cannot connect directly. **Server Settings → Check for Problems** (Section 7) checks for this.

## 3. Installation

1. Download **ConanOps-Setup-*version*.exe** from the [latest release](https://github.com/PeanutPenguin/ConanOPs/releases/latest).
2. Run the file. Administrator rights are not needed; ConanOps installs to `C:\ConanOps`.
3. Because ConanOps is not yet code-signed, Windows may show the message "Windows protected your PC". Select **More info**, then **Run anyway**.

After the first server has been set up, ConanOps shows a short tour of the Dashboard. The tour can be replayed later from **App Settings → Run Automatically → Show the Dashboard Tour** (in Advanced mode).

At several points Windows asks for permission to make a change, for example when ConanOps adds firewall rules or installs the Microsoft Visual C++ runtime that the server requires. Each of these prompts should be accepted. Hosts who prefer not to see them can turn on **Run with admin rights** under **App Settings → Run Automatically** (in Advanced mode), which requires one confirmation and then runs ConanOps with administrator rights from that point on.

## 4. Setting up a new server

Select **+ Add Your First Server** on the Dashboard (or **Add server** in the sidebar). ConanOps first checks whether a Conan server is already installed on the PC (Section 5). If none is found, the setup wizard opens. It consists of five steps.

1. **Name and folders.** The host names the server and confirms the suggested folders for SteamCMD (Valve's download tool) and for the server itself. ConanOps checks for common problems before anything is downloaded: too little free space, a folder path that SteamCMD cannot handle, or a folder already used by another server. If a folder path contains a space, the wizard offers a suitable folder instead.
2. **Restore (optional).** The new server can start from a backup of another server, or from a backup file made elsewhere. Otherwise it starts with a new world.
3. **Installation.** ConanOps downloads SteamCMD and then the Conan Exiles dedicated server. A progress bar is shown; this step takes the longest and depends on the speed of the connection.
4. **Networking.** ConanOps detects the PC's address on the local network, chooses free ports that no other server uses, adds Windows Firewall rules for those ports and for the server program itself, and asks the router to forward the ports. If the router does not support this, the wizard shows the exact ports to forward by hand and the PC's public address.
5. **Keep it running.** One option, on by default, sets ConanOps to start with Windows, reopen itself if it closes or crashes, keep the PC awake while a server is running, and limit restarts for Windows updates to between 4 and 6 AM. Each of these can be changed later (Section 11).

The wizard also points out one Windows setting that ConanOps cannot change: **Use my sign-in info to automatically finish setting up after an update**. If it is off, the PC waits at the sign-in screen after an update restart, and the servers remain offline until someone signs in.

## 5. Taking over a server that is already on the PC

Hosts who already run a Conan server, for example one installed with SteamCMD and started from a batch file, can let ConanOps manage it without reinstalling anything.

### 5.1 How ConanOps finds existing servers

ConanOps looks for servers a few seconds after it opens and whenever **Add server** is selected. It checks:

- servers that are currently running;
- every Steam and SteamCMD library on the PC, including folders that Funcom has renamed;
- folders named in scripts, scheduled tasks and Windows services that start a Conan server;
- common install locations on each drive, and, after **Add server**, folders further down on every drive.

If a server is not found, the **Browse…** button allows the host to select its folder directly. Any folder inside the installation, or the server program itself, is accepted.

### 5.2 What is kept

Taking over a server does not reinstall or reset it. ConanOps reads and keeps the world, the server name and password, the ports, the player limit, the RCON settings, the network address the server uses, every gameplay setting, the mod list and any additional launch options (for example `-nosteam`). Mods downloaded to another Steam folder continue to load from there, and mod files that do not come from the Steam Workshop stay in the mod list in their original position.

Two changes are made. A backup is taken before anything else, and RCON (the server's remote command interface) is turned on if it was off, since ConanOps uses it to save the world and warn players before restarts. The RCON change takes effect the next time the server restarts.

Servers that keep their world in a different folder (started with `-usedir`) are listed but cannot be taken over at present.

### 5.3 Other programs that run the same server

When two programs manage one server, each tends to restart or update it while the other is using it. ConanOps therefore lists anything else that starts, restarts or updates the server: restart-loop batch files, scheduled tasks, items that start with Windows, Windows services (such as those created with NSSM), monitoring scripts and other server managers. The option **Switch off the other launchers**, selected by default, turns them off once the server has been taken over.

Nothing is deleted in this process. Scheduled tasks and services are disabled rather than removed, startup files are moved into ConanOps' data folder, and every change is recorded. If the server is later removed from ConanOps, these launchers can be turned back on (Section 14). If a Windows service was running the server, the server restarts once under ConanOps; players can reconnect within a minute or two. If the server previously started with Windows through one of these launchers, ConanOps now starts it instead.

The same check is made for servers ConanOps already manages. If another program is found running one of them, ConanOps offers to switch it off.

## 6. The main window

The sidebar lists the configured servers, each with a status indicator and player count, and the main pages: **Dashboard**, **Players**, **Mods** and **Server Settings**, followed by **App Settings**. Every page except App Settings refers to the server currently selected in the sidebar.

### 6.1 Simple and Advanced

The switch at the top right of the window chooses how much ConanOps shows. **Simple**, the default, shows presets (Section 7) and the settings most hosts change. **Advanced** also shows every individual setting under each preset, the **Console** page, ports, remote commands (RCON), the fixed web address, the Discord bot, the PIN lock and the Steam Workshop key. The choice is remembered. Nothing is lost by switching: Advanced only shows more, and settings changed there remain in force in Simple mode. When a search finds a setting that exists only in Advanced, ConanOps switches to Advanced to show it.

### 6.2 The pages

**Dashboard.** Shows the address friends connect to (with a **Copy** button), processor and memory use, server frame rate, the automatic tasks, the players currently online and recent log output. Whether the server is online is shown once, at the top of the window. While the server is stopped there is a single **Start** button; while it runs, **Restart** and **Stop**. A restart warns players in-game and saves the world before the server stops.

**Players.** Lists every player who has joined, with total playtime and number of sessions. The history is kept across restarts. Players who are online can be kicked or banned. The access tab holds the whitelist and ban list, which take SteamID64 numbers; whitelist-only mode admits listed players only. Bans apply immediately if remote commands (RCON) are on, otherwise at the next restart.

**Mods.** See Section 8.

**Console** (Advanced). Sends remote commands directly to the running server and shows the replies.

**Server Settings** and **App Settings.** See Sections 7 and 11. Both have a search box that covers every setting.

## 7. Server Settings

Most groups of settings start with a few **presets**: named choices such as *Standard*, *Frequent* or *Light* backups, each with a one-line description, and one marked *Recommended*. Selecting a preset sets all the settings in that group. The **?** next to a group's title explains it in more detail. In Advanced mode the individual settings appear under the presets; changing one of them marks the group **Custom**, unless the new values happen to match another preset.

Changes are held as pending until **Save** is selected in the bar that appears at the top, and **Discard** returns to the last saved values. Most changes take effect the next time the server restarts, since Conan Exiles reads its settings only at start-up. Server Settings has four sections.

- **Server.** The server type (*PvE*, *PvE-Conflict* or *PvP*), the server name, its password and the player limit. The ports are shown only if they clash with another program or server, together with a button that picks free ones. Advanced adds the ports, every other general setting (message of the day, admin password, BattlEye, region and so on), the network address, **Repair Networking**, and the remote-commands (RCON) settings.
- **Gameplay.** A play style: *Official* (Funcom's standard settings), *Relaxed*, *Boosted* or *Hardcore*, which set the rates for experience, gathering and crafting, the damage players take, hunger and thirst, and whether abandoned bases decay. Advanced shows every gameplay setting that Funcom's own settings screen offers, grouped by category. Numeric sliders mark the last saved value, so the extent of a change remains visible while editing.
- **Automation.** Four groups of presets:
  - *Backups*: *Standard* (every 6 hours), *Frequent* (every 2 hours) or *Light* (once a day), with the number of copies kept. The saved backups are listed below (Section 9).
  - *Daily restart*: in the *Quietest hours* found in the server's player history, in the *Early morning*, or *Never*. The restart happens only when no one is online; if players remain online for the whole window, that day's restart is skipped.
  - *Game updates*: *Automatic* or *Tell me* (Section 10).
  - *If a mod stops a server starting*: *Wait for a fix*, *Start without it* or *Just tell me* (Section 8). This choice applies to all servers.
- **Check for Problems.** Checks the common reasons a server will not start or cannot be reached from outside, and explains each problem. Problems are listed first, and most have a button that fixes them, for example **Repair Networking**, **Use Free Ports**, **Repair Game Files**, **Turn On RCON** or **Restore Latest Backup**; the check runs again once the fix is complete. Nothing is changed unless such a button is selected. Three of the checks need further explanation:
  - *Reachable from the internet.* While the server is running, ConanOps asks Steam whether it lists the server at the PC's public address, and sends a test query to that address. A server that Steam does not list after several minutes of running is very probably unreachable. A listing is a good sign but not conclusive, because some routers let Steam's replies through without a port forward; the result therefore also takes the router check into account. A friend connecting from outside remains the most reliable test.
  - *Server log.* ConanOps reads the server's latest log, its crash reports and its mod list, and explains in plain language why the server would not start or crashed, for example a missing mod file, a crash inside a particular mod, a port in use, insufficient memory or a damaged world save. Errors it does not recognise are quoted as the server reported them.
  - *Repeated crashes.* A server that has crashed several times in the last 24 hours is reported, together with the most likely cause.

ConanOps writes only the settings shown on these pages. Lines it does not recognise, including settings added by hand or by mods, are left unchanged, and a copy of each settings file is kept before it is modified.

## 8. Mods

The Mods page holds an ordered list of mods. Mods load from the top of the list downwards; where two mods change the same thing, the one loaded later usually takes precedence. Mods can be added by Workshop ID or through **Browse Workshop…**, reordered by dragging, switched on or off, and removed. **Download Mods** fetches the current versions.

Mods often stop working after a major game update until their authors rebuild them. **Fix Mod Problems** offers several tools for this situation:

- **Check for outdated mods** compares each mod's last Workshop update with the date of the current game patch. This check needs a free Steam Web API key, entered under **App Settings → Steam Workshop**.
- **Quick mod check** restarts the server once per mod, testing each one alone, to find every mod that prevents start-up.
- **Find all bad mods** does the same and also detects pairs of mods that fail only together.
- **Bisect by hand** halves the list in each round; the host reports whether the problem remains, until a single mod is identified.

The world is protected during all of these tests: it is copied beforehand and restored afterwards, so test restarts do not change it.

If a mod stops a server from starting while no one is present, ConanOps can respond in one of three ways, chosen under **Server Settings → Automation**. It can find the mod and keep the server stopped until the mod's author releases a fix, then start the server again without intervention; this keeps everything the mod added to the world. It can find the mod and start the server without it, which brings players back sooner but deletes that mod's buildings and items (a backup is made first). Or it can simply send an alert.

## 9. Backups and restoring

ConanOps backs up each server on a schedule, before every update and before it takes over an existing server. **Back Up Now** makes one immediately. Older backups are removed according to the retention settings, which keep a number of daily and weekly copies.

To restore, select **Restore** next to a backup. ConanOps stops the server, makes a safety copy of the current world, replaces the world with the backup and starts the server again.

**Import External Backup…** adds a backup created elsewhere, for instance on another PC or with another tool. ConanOps first checks that the file contains a Conan world, then adds it to the list, after which it can be restored like any other backup. Only the world and the game's settings files are imported; ConanOps' own settings for the other server, such as its mod list or schedule, are not.

If the world save is missing but backups exist, ConanOps does not start the server with an empty world. It sends an alert, and **Start** offers either **Restore Latest Backup** or **Start a New World**.

## 10. Updates

**Server updates.** Under **Server Settings → Automation**, ConanOps shows the installed build and checks Steam for a newer one. With the *Automatic* preset it installs a new build once no one is online: it backs up the server, downloads the update and restarts the server. Scheduled restarts also check for an update first. With *Tell me*, ConanOps checks every few hours, sends an alert, and offers **Back Up & Update Now**. Funcom's patch notes are shown where available, and updates that appear to be major engine changes are marked, since these are the ones most likely to break mods.

ConanOps also repairs missing or damaged game files. If the server program is missing, or the server keeps crashing after two automatic restarts, it asks Steam to verify the files, downloads anything missing and starts the server again. The world, settings and mods are not affected.

**ConanOps updates.** Under **App Settings → Updates & Uninstall**, the presets *Automatic*, *Ask me first* and *Only when I check* decide how ConanOps updates itself; the servers keep running while it does. Each download is verified before installation, and the previous version is kept, so that if the new version fails to start, the earlier one is restored automatically.

## 11. Unattended operation

**App Settings → Run Automatically** decides whether servers keep running without anyone at the PC. Three presets are offered:

- *Hands-off* (recommended): ConanOps starts with Windows, reopens itself if it closes or crashes, keeps the PC awake while a server runs, and lets Windows restart for updates only between 4 and 6 AM, when nobody is online.
- *While I'm signed in*: ConanOps starts with Windows and keeps the PC awake, but Windows restarts for updates whenever it chooses.
- *Only when I open it*: servers run only while ConanOps is open.

Windows may ask for permission once when a preset changes its update hours. In Advanced mode the individual options appear below the presets:

- **Start ConanOps when I sign into Windows**, optionally minimised to the tray.
- **Reopen ConanOps if it closes or crashes.** Quitting from the tray icon is respected until ConanOps is next opened.
- **Run with admin rights**, which removes the Windows permission prompts.
- **Keep this PC awake while a server is running.** Sleep is blocked only while a server runs; Windows power settings are not changed.
- **Only restart for Windows updates during this window.** ConanOps sets Windows' active hours around the chosen window. Inside it, and only when no one is online, ConanOps saves and stops the servers and restarts the PC itself, at most once a day and never within two hours of start-up.

In addition, ConanOps restarts a server that crashes, rechecks the router's port forwards every hour (many routers lose them when they restart), and resumes servers that were running before the PC restarted. A server stopped deliberately with **Stop** stays stopped, including after a restart of the PC.

In Advanced mode, a PIN can be required when ConanOps opens (**App Settings → PIN Lock**), and **App Settings → Remote Access** offers a fixed web address: a free DuckDNS name that keeps pointing at the PC when its public address changes, so players can use a name instead of a changing number. The Dashboard then shows that name as the address to connect to.

## 12. Alerts and Discord

Under **App Settings → Alerts**, ConanOps can send notifications to a Discord channel (through a webhook) and to the ntfy phone app. Alerts are sent when a server crashes, is updated, has a mod problem, fails a backup, and in similar situations. **Send Test** confirms that the address is correct. ConanOps can also keep a live status message for each server in the Discord channel.

In Advanced mode, a Discord bot can be added in the same section. Anyone in the channel can type `!status` to see each server's status and who is online. People on an allowed list can type `!restart`, or `!restart` followed by a server's name, to warn players, save and restart. `!help` lists the commands.

## 13. The web version

The web version lets the host check and control the servers from a phone or another computer. It is turned on under **App Settings → Remote Access** with one of three presets: *Off*, *On my Wi-Fi* or *From anywhere*. A password of at least eight characters must be set; ConanOps asks for one when the web version is first turned on. The page is then available on the home network through the link shown; if Windows blocks it, **Allow Through Firewall** adds the necessary rule.

*From anywhere* creates a secure link through Cloudflare's free tunnel service. The link changes whenever the tunnel reconnects; the current link is shown in App Settings and is sent to the ntfy alert topic when it changes.

Most of what the PC application offers is available from the web version. For safety, the following can only be done at the PC: adding or deleting servers, deleting ConanOps, changing the web password and turning the outside link on or off. Changes that require a Windows permission prompt are queued and offered the next time someone uses ConanOps at the PC.

## 14. Removing a server and uninstalling

**Removing a server.** Select the **×** button next to the server's name in the sidebar. The confirmation dialog states what will be deleted, and each item can be kept:

- For a server ConanOps installed, the server files and their SteamCMD folder are deleted by default.
- For a server that was already on the PC before ConanOps took it over, the folder is kept unless the host chooses to delete it, and a SteamCMD folder that ConanOps did not install is never deleted.
- The server's backups can be kept or deleted.
- If ConanOps switched off other launchers when it took the server over, it offers to turn them back on.

**Uninstalling.** **App Settings → Updates & Uninstall** offers two choices. **Uninstall ConanOps (Keep My Data)** removes the program and the firewall rules Windows created for it, but keeps servers, worlds and backups in `C:\ConanOps\data`, so a later reinstallation finds them again. **Delete Everything** also stops and deletes every server, its backups and SteamCMD, and reverses the Windows changes ConanOps made; servers that were on the PC before ConanOps are left in place. It also removes the firewall rules Windows created for ConanOps itself and for servers whose folders no longer exist; the rules for the game, and for servers that are kept, are not touched. Because this cannot be undone, it must be confirmed by typing DELETE. ConanOps can also be uninstalled through Windows' list of installed apps, which keeps the data folder.

## 15. Troubleshooting

**The server does not start, or keeps crashing.** ConanOps runs checks before each start and names the specific problem, such as missing files, a port in use or a missing Visual C++ runtime. **Server Settings → Check for Problems** gives a fuller report, including the cause recorded in the server's own logs, and offers a fix where one exists. Crash alerts also state the most likely cause.

**Friends cannot connect.** Run **Check for Problems** while the server is running; the *Reachable from the internet* result shows whether Steam can see the server from outside. The most common causes are a router without UPnP (forward the game and query ports by hand, using the port numbers shown under **Server Settings → Server** in Advanced mode), an internet provider that does not assign a public address, and a firewall rule that was not added because a permission prompt was declined (**Repair Networking** adds it again). Players on the same home network should connect to the PC's address on that network.

**The server stopped working after a game update.** This is usually caused by mods that have not yet been rebuilt for the new version. Use **Fix Mod Problems** (Section 8), or wait for ConanOps' automatic mod recovery if it is enabled.

**Windows asks whether to allow ConanSandboxServer.** This prompt comes from Windows Defender Firewall, not from the server. Because ConanOps adds a firewall rule for the server program during setup, it should not normally appear. If it does, select **Allow access**. Selecting **Cancel** makes Windows create rules that block the server, so that friends cannot connect; **Repair Networking** (under **Server Settings → Server** in Advanced mode, or offered by Check for Problems) switches those rules off again.

**The servers were offline after Windows restarted.** Check that **Use my sign-in info to automatically finish setting up after an update** is on in Windows (App Settings shows a warning when it is off), or turn on **Keep my servers running when nobody is signed into Windows**.

**An existing server was not found.** Use **Browse…** in the Add server dialog and select the server's folder. If the server is running as administrator, Windows may hide its location from ConanOps; turning on **Run with admin rights** resolves this.

**Alerts do not arrive.** Use **Send Test** in App Settings → Alerts to check the webhook address or ntfy topic.

**Reporting a problem.** Please open an issue on the [GitHub page](https://github.com/PeanutPenguin/ConanOPs/issues) describing what happened, and attach the file `conanops.log` from `C:\ConanOps\data` if possible.

## 16. Known limitations

- ConanOps is not code-signed, so Windows shows a warning on installation (Section 3).
- Settings that Funcom does not offer in its own settings screen are not shown. They can still be added to the settings files by hand, and ConanOps will leave them unchanged.
- The daily restart is skipped on days when players are online throughout the restart window, so it is not guaranteed to occur every day.
- Servers that keep their world in a separate folder (`-usedir`) cannot be taken over.
- Automatic router configuration depends on the router supporting UPnP. Where it does not, the ports must be forwarded by hand.
