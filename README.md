# ConanOps

A desktop app (Windows) for running and maintaining up to 5 Conan Exiles
dedicated servers: live dashboard, player tracking, scheduled backups,
update checking with changelog + major-update flagging, staged per-server
settings, a first-run setup wizard, and a background scheduler for
automatic backups and restarts.

## For players hosting a server

Download **ConanOps-Setup-<version>.exe** from
https://github.com/PeanutPenguin/ConanOPs/releases/latest and run it -- no admin rights
needed, it installs to `C:\ConanOps`. Windows may show "Windows protected
your PC" for an unsigned release: click **More info → Run anyway**.

You need: Windows 10 or 11 (64-bit), about 50 GB free (75 GB+ is
comfortable), roughly 8 GB of RAM for a small group (more for big or
modded servers), no graphics card, and an internet connection. Windows
will ask for permission a few times (firewall rules, the Microsoft Visual
C++ runtime) -- click Yes. For friends outside your home to connect, your
router needs UPnP turned on or a manual port forward (the app walks you
through it), and your internet provider must give you a public IP address.

Uninstalling keeps your servers, worlds and backups in `C:\ConanOps\data`.

## Running from source

```
pip install -r requirements.txt
python main.py
```

Requires Windows (the launch/monitoring/firewall code targets the
Windows Conan server binary, paths, and `netsh`). Python 3.10+
recommended.

## Running tests

```
pip install -r requirements-dev.txt
pytest
```

Covers the pure-logic modules -- `models` (including secrets
encryption and app-lock PIN), `secrets_store`, `network_utils`,
`preflight`, `backup_manager`, `ini_utils`, `steamcmd`, `changelog`,
`scheduler` -- with real filesystem/socket operations against
temp dirs and ephemeral ports rather than mocks wherever practical.
Doesn't cover the Qt UI pages themselves (widget-level testing wasn't
in scope for this pass); `tests/conftest.py` isolates each test's
`HOME` so nothing touches your real `~/ConanOps`.

## Packaging as a standalone .exe

On Windows, double-click **`BUILD_EXE.bat`** (needs Python on PATH) -- it
installs the build tools and produces `dist\ConanOps.exe`. On Linux,
`tools/build_exe_wine.sh` builds the same .exe through Wine (see the
script's comments for the two Wine quirks it works around). Or by hand:

```
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name ConanOps --icon assets/conanops.ico --add-data "assets;assets" main.py
```

`--add-data` bundles the `assets/` folder: the app icon, the Geist fonts (SIL Open Font License, see `assets/fonts/OFL-LICENSE.txt`) and the loading animation. Without it the app still runs, just with system fonts and no artwork.

The built exe will be in `dist/ConanOps.exe`. No Python install is needed
to run it once built. It is not code-signed, so Windows SmartScreen and
some antivirus products will likely flag it on first run -- that's a
false positive common to unsigned PyInstaller executables, not a defect
in the app, but there's no way around the warning without paying for a
code-signing certificate.

## First run

On first launch ConanOps creates one default server entry. Click
**"Set Up Server…"** on its Dashboard (or **"+ Add server"** in the
sidebar for a new one) to run the setup wizard, which:

1. Asks where SteamCMD and the server install should live (created
   automatically if they don't exist).
2. Installs SteamCMD if it isn't already there, then downloads the
   Conan Exiles Dedicated Server via `steamcmd +app_update ... validate`.
3. Auto-detects your local LAN IP and a free game/query port pair
   (checked against your other configured servers, not just the OS).
4. Adds Windows Firewall rules for both ports. **This step needs
   ConanOps to be running as Administrator** -- if it isn't, the wizard
   reports the failure rather than silently skipping it.
5. Attempts UPnP port forwarding on your router. If your router doesn't
   support UPnP (or has it disabled, which is common), the wizard shows
   the exact manual forwarding instructions and your public IP instead.

Config is stored at `~/ConanOps/config.json`. Per-server session history
is at `~/ConanOps/sessions/<server-id>.json`.

## What's implemented

**Core**
- Multi-server data model (up to 5), shared `AppConfig` JSON store.
- Real local-IP auto-detection (`network_utils.get_local_ip`) -- always
  the LAN interface, never the public/WAN IP, since `-MULTIHOME` needs a
  local address. (This mismatch was the original bug that started this
  whole project.)
- Real UDP port-availability checks and independent free-port search for
  game and query ports, including checking against your *other*
  configured servers' ports, not just OS processes.
- Real A2S_INFO query implementation (the same query Steam's server
  browser uses) to confirm a server is actually bound and answering.
- `.ini` read/write that only ever touches known keys and preserves
  everything else byte-for-byte, with an automatic timestamped backup of
  the file before every write (`ini_utils.py`).

**Monitoring**
- Log tailing + regex parsing for status/join/leave/save lines, running
  on a background `QThread` per server, for every configured server's
  whole lifetime (not just whichever one is on screen) -- this is what
  lets the scheduler's "is anyone online" check be accurate for servers
  you aren't currently viewing (`log_monitor.py`, wired per-server in
  `ui/main_window.py`).
- Player session tracking and a leaderboard with search
  (`session_tracker.py`, `ui/players_page.py`).

**Backups & updates**
- Backup create/list/restore/prune (zip-based), wired to a confirmation
  dialog before restoring (`backup_manager.py`, `ui/backups_page.py`).
- **Import an external backup** from anywhere (another server, a
  different tool) into a server's own backup list — validated first
  (rejects anything that doesn't look like a real Conan save) and
  restorable the same way as any backup ConanOps made itself. Only
  world/config data comes over, not the source's ConanOps settings.
- SteamCMD wrapper: install/bootstrap, `+app_update ... validate` with
  retry, reading `StateFlags`/`buildid` from the appmanifest
  (`steamcmd.py`).
- Update checking via Steam's public news API (no key needed) with a
  major-update heuristic (keyword + version-jump matching) -- see
  `changelog.py` for exactly how, and its limits.
- Pre-flight validation with auto-repair for a stale bind IP, plus clear
  problem reporting for anything it can't safely fix on its own
  (`preflight.py`).

**Automation**
- **First-run setup wizard** (`ui/setup_wizard.py`) -- see "First run"
  above for the full flow.
- **Networking setup** (`network_setup.py`): Windows Firewall rule
  automation via `netsh`, and a from-scratch minimal UPnP client (SSDP
  discovery + SOAP `AddPortMapping`, no third-party dependency).
- **Background scheduler** (`scheduler.py`): a `QTimer` checks every
  server once a minute and fires a scheduled backup once
  `backup_interval_hours` has elapsed, and a scheduled restart once
  during its quiet-hours window per day -- skipping entirely if anyone
  is online at check time, per the spec's safety rule.
- **Automatic updates**: on a configurable interval (default 6 hours),
  ConanOps checks SteamCMD for a new build; if one's found and nobody's
  online, it backs up, updates, and relaunches immediately. Every
  scheduled restart also checks for an update first and updates instead
  of just restarting if one's available (`update_runner.py`, wired in
  `ui/main_window.py`).
- **Update safety**: every update stops the server first (saving the
  world over RCON when it's on), THEN backs up the closed world, checks
  there's enough free disk space, and only then runs SteamCMD -- all off
  the UI thread. A failed update never relaunches the old build (players
  whose game Steam already updated couldn't join it): the server is
  held stopped, the reason is shown on the Dashboard and sent as an
  alert, and the update retries in 30 minutes. For 15 minutes after an
  update, a repeat crash or hang holds the server stopped instead of
  crash-looping and names the mods not updated for the new version.
  Clicking Start always overrides a hold.
- **RCON on by default** for new servers (random password, unique
  port), so stops can save first; existing servers with RCON off get a
  Dashboard warning with a one-click "Turn On RCON".

**Settings UI**
- All five Settings pages (Server Identity, Network & Ports, Gameplay
  Rates, Backups, Restart Schedule) follow the staged-edit pattern from
  the design spec: edits are local until **Apply on Next Reset**,
  tracked per-page via `ui/base_settings_page.py`.
- Network & Ports has live port-conflict detection with a one-click fix,
  and real hover tooltips.
- Gameplay Rates sliders show a **ghost marker** at the last-applied
  value (custom-painted `ui/ghost_slider.py`) with a highlighted span
  for the pending change, and Building Decay can go to 0 = "Never
  decays".
- Restart Schedule suggests a quiet window computed from real session
  history.

**Console, mods, access control, alerts**
- **RCON console** (`rcon.py`, `ui/console_page.py`) -- a from-scratch
  Source RCON protocol client (tested here against a mock server
  implementing the real wire protocol, since there's no live Conan
  server in this environment) with a command-line-style UI page.
- **Mod manager** (`mod_manager.py`, `ui/mods_page.py`) -- add/remove,
  enable/disable, and reorder mods (load order matters), writing
  `modlist.txt`. Includes a **guided bisect flow**
  (`ui/mods_page.BisectDialog`) that disables half the enabled mods at a
  time and, based on whether the problem persists after a restart,
  narrows down to the one broken mod in ~log2(n) rounds instead of
  testing one at a time.
- **Whitelist / ban management** (`banlist_manager.py`,
  `ui/access_page.py`) -- add/remove whitelist entries, ban/unban
  players, with immediate enforcement via RCON when it's enabled (falls
  back to "takes effect on next restart" when it isn't).
- **Alerts** (`webhooks.py`, `ui/settings_alerts_page.py`) -- Discord
  and/or ntfy.sh webhook notifications, firing on: a scheduled restart
  starting, a scheduled backup failing, an update being applied, an
  unexpected crash (see below), and any pre-flight auto-repair.
- **Crash detection** -- `MainWindow` polls every configured server's
  process state every 10 seconds; if a server was running and is no
  longer, and ConanOps didn't just intentionally stop it (manual
  restart, scheduled restart, or restore), that's treated as a crash and
  triggers an alert.
- **System tray** -- minimizes to tray on close instead of quitting
  (only when a system tray is actually available on the OS -- headless
  Linux dev environments like the one this was built in don't have one,
  which was confirmed rather than assumed during testing; real Windows
  desktops do).

## Known gaps / intentionally out of scope

Two items from the original gap list can't actually be closed by writing
more code, so they're stated plainly instead of glossed over:

- **No code signing** on the packaged `.exe`. This requires purchasing a
  code-signing certificate from a CA -- it's a money-and-process thing,
  not a software gap, so Windows SmartScreen and some antivirus products
  will flag an unsigned executable regardless of how the app itself is
  built.
- **UPnP client is implemented but untested against a real router** --
  there's no physical router available in this development environment.
  The SSDP discovery + SOAP `AddPortMapping` implementation follows the
  documented IGD protocol, but real router firmware is famously
  inconsistent about how strictly it follows that spec. It fails
  cleanly to the manual-instructions path when it doesn't work, which is
  the important part, but "works against your specific router" isn't
  something that can be verified without one.

A later hardening pass closed everything else that had accumulated on
this list: servers can now be removed from the sidebar (with firewall
rule cleanup, non-blocking); RCON/server passwords and webhook URLs are
DPAPI-encrypted at rest instead of plain text in `config.json`; an
optional startup PIN lock is available from the tray menu; failures that
used to be silently swallowed (webhook delivery, firewall rule changes,
backup pruning, SteamCMD timeouts) are now logged to
`~/ConanOps/conanops.log`; `preflight`'s bind-IP auto-repair no longer
overwrites a deliberately-chosen secondary NIC, only a genuinely stale
one; the setup wizard blocks two servers from sharing an install/SteamCMD
folder; and backup restore + firewall rule changes run on background
threads instead of freezing the UI. A `tests/` suite now covers the
pure-logic modules (see "Running tests" above).

What's *not* covered by that pass, and remains a real gap:

- **No app-level access control beyond the PIN lock** -- it's a single
  shared PIN (hashed+salted, not full account/user management), meant to
  stop someone glancing at an unlocked screen, not to resist a
  determined local attacker who can edit the two PIN fields back out of
  `config.json`.
- **The Qt UI pages themselves have no automated tests** -- only the
  pure-logic modules underneath them do.
- **No delivery retry for webhooks** -- a failed Discord/ntfy POST is
  now logged and surfaced as a tray warning instead of vanishing
  silently, but it still isn't retried.

Everything else originally on this list is now implemented (RCON
console, mod manager with bisect, whitelist/ban UI, webhook/Discord
alerts, system tray) -- see "What's implemented" above. What remains are
inherent characteristics of the approach, not missing work:

- **`changelog.is_major_update`'s keyword list is a starting point**, not
  a tuned/tested heuristic -- expect to adjust it once real patch note
  text comes through.
- **`mod_manager`'s modlist.txt format and `banlist_manager`'s RCON
  command names reflect the commonly-documented conventions** at the
  time this was written. Both modules say so directly in their own
  docstrings, since a game-content format changing in a future update is
  a different kind of problem than the infrastructure bugs pre-flight
  validation catches -- worth checking against Funcom's current docs if
  mods or bans don't take effect.
- **Firewall changes need Windows permission** -- ConanOps asks with one
  permission prompt per change (PowerShell, see `powershell.py`). If it's
  declined, the wizard and Diagnostics say so plainly, and Repair
  Networking on the Network & Ports page tries again.
- **A restart is skipped for the whole day if the quiet-hours window
  passes with anyone online the entire time** -- by design, matching the
  spec's safety rule, but not a guaranteed once-a-day restart if the
  server is always busy during that window.
- **Cancelling the setup wizard mid-download is best-effort** --
  `subprocess.run()` blocks, so closing the dialog stops the UI from
  waiting on it but won't forcibly kill an in-progress download at the
  OS level.

## Project layout

```
conanops/
  main.py                  entry point
  models.py                ServerConfig / AppConfig
  network_utils.py         local IP, port checks, A2S query
  network_setup.py         firewall rules + UPnP forwarding
  ini_utils.py              safe .ini merge read/write
  steamcmd.py                SteamCMD install/update wrapper
  backup_manager.py           backup create/list/restore/prune
  process_manager.py           launch/find/stop the server process
  log_monitor.py                background log tailer (QThread)
  session_tracker.py             player session + playtime tracking
  preflight.py                    pre-flight validation / self-heal
  changelog.py                     Steam news fetch + major-update heuristic
  scheduler.py                      background backup/restart timer
  rcon.py                             Source RCON protocol client
  mod_manager.py                       mod list + bisect helper
  banlist_manager.py                    whitelist/ban bookkeeping + RCON enforcement
  webhooks.py                             Discord/ntfy alert sending
  applog.py                                rotating file logger (~/ConanOps/conanops.log)
  secrets_store.py                          DPAPI encryption for stored passwords/webhook URLs
  backup_runner.py                           QThread worker: restore-backup off the UI thread
  network_setup_runner.py                     QThread worker: firewall rule add/remove off the UI thread
  config.example.json                       template config (no secrets)
  ui/
    theme.py                                  QSS stylesheet
    ghost_slider.py                            ghost-marker slider widget
    base_settings_page.py                       staged-edit page base class
    sidebar.py                                   server switcher + nav (incl. remove-server)
    app_lock_dialog.py                            startup PIN unlock + set/change dialogs
    settings_container.py                         settings sub-nav
    setup_wizard.py                                first-run install/network wizard
    dashboard_page.py, players_page.py, backups_page.py, updates_page.py
    mods_page.py, access_page.py, console_page.py
    settings_identity_page.py, settings_network_page.py,
    settings_rates_page.py, settings_backups_page.py,
    settings_restart_page.py, settings_alerts_page.py
    main_window.py                                  wires everything together
  tests/                                              pytest suite for the pure-logic modules
```
