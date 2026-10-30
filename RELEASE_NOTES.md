ConanOps runs Conan Exiles dedicated servers on a Windows PC and handles their routine maintenance. To install it, download **ConanOps-Setup** below and run it; existing copies of ConanOps update themselves.

### New in this version

This version reorganises ConanOps so that it is easier to use. No settings are removed, and existing settings are kept.

- **Presets.** Most groups of settings now start with a few named choices, each described in one line, with one marked *Recommended*: for example *Standard*, *Frequent* or *Light* backups; *Official*, *Relaxed*, *Boosted* or *Hardcore* play styles; and *Hands-off*, *While I'm signed in* or *Only when I open it* for how ConanOps runs. Selecting a preset sets every setting in that group. A **?** beside each group explains it in more detail.
- **Simple and Advanced.** A switch at the top right chooses how much is shown. Simple, the default, shows the presets and the settings most hosts change. Advanced also shows every individual setting under its preset, as well as Console, ports, remote commands (RCON), the fixed web address, the Discord bot, the PIN lock and the Steam Workshop key. Changing an individual setting marks its group *Custom*. A search for a setting that exists only in Advanced opens Advanced to show it.
- **Fewer sections.** Server Settings has four sections: Server, Gameplay, Automation (backups, the daily restart, game updates and broken-mod handling) and Check for Problems. App Settings has five: Run Automatically, Remote Access, Alerts, Appearance, and Updates & Uninstall.
- **A clearer Dashboard.** Whether the server is online is shown once, at the top of the window. A stopped server has a single **Start** button; a running one has **Restart** and **Stop**. The address friends connect to is shown in full, with a **Copy** button, and the automatic tasks have plain names.
- **Shorter, plainer text.** Explanations are shorter, technical terms are replaced or explained, and errors such as a failed mod-status check are described in ordinary words.
- **The web version** keeps its current layout in this release.

### Also included (from 1.0.12 and 1.0.13, which were not released separately)

- **Check for Problems** tests whether the server can be seen from the internet, reads the server's logs and crash reports to explain why it would not start or crashed, and offers a fix button for most problems. Crash alerts include the most likely cause.
- **Firewall.** ConanOps adds a firewall rule for the server program itself, so Windows should no longer ask whether to allow it, and removes leftover rules when uninstalled or when everything is deleted.

The [User Guide](https://github.com/PeanutPenguin/ConanOPs/blob/main/USER_GUIDE.md) describes every feature in detail.

### Requirements

Windows 10 or 11 (64-bit), about 50 GB of free disk space, around 8 GB of RAM for a small group of players, and an internet connection.

ConanOps is not yet code-signed, so Windows may display "Windows protected your PC". Select **More info**, then **Run anyway**.

ConanOps is not affiliated with Funcom or Valve. Conan Exiles is a trademark of Funcom.
