# ConanOps — Full Documentation

A desktop app (Windows) for running and maintaining up to 5 Conan Exiles
dedicated servers from one place: live dashboard, player tracking,
scheduled backups, update checking, mod management, access control, RCON
console, and alerts — with a first-run wizard that automates as much of
the setup as is actually automatable.

This document is the complete reference. `README.md` in the project root
is the short version for getting started quickly.

---

## Table of contents

1. [What ConanOps is / isn't](#1-what-conanops-is--isnt)
2. [Installing and running](#2-installing-and-running)
3. [First-run setup wizard, step by step](#3-first-run-setup-wizard-step-by-step)
4. [The main window, page by page](#4-the-main-window-page-by-page)
5. [Settings pages in detail](#5-settings-pages-in-detail)
6. [What runs in the background](#6-what-runs-in-the-background)
7. [Configuration file reference](#7-configuration-file-reference)
8. [Module reference (architecture)](#8-module-reference-architecture)
9. [Data flow: how a setting reaches the game](#9-data-flow-how-a-setting-reaches-the-game)
10. [Troubleshooting](#10-troubleshooting)
11. [Known limitations](#11-known-limitations)
12. [Extending ConanOps](#12-extending-conanops)

---

## 1. What ConanOps is / isn't

ConanOps replaces three things people otherwise juggle separately for a
self-hosted Conan Exiles server:

- A terminal-based monitor script (log tailing, restart hotkeys)
- A batch file for SteamCMD updates
- Manually editing `.ini` files by hand

It does **not** replace Conan Exiles itself, SteamCMD, or your router.
It automates the parts of running a dedicated server that are
mechanical and error-prone (typing the wrong IP into a launch argument,
forgetting to back up before an update, hand-editing a config file and
breaking a line a mod depends on) and gets out of the way for the parts
that are genuinely your decisions (server name, rules, rates).

---

## 2. Installing and running

### From source

```bash
pip install -r requirements.txt
python main.py
```

Requires **Windows** — the process launch, log paths, and firewall
automation all target the real Windows Conan Exiles dedicated server
layout and `netsh`. Python 3.10+ recommended.

### As a standalone .exe

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name ConanOps --icon assets/conanops.ico --add-data "assets;assets" main.py
```

`--add-data` bundles the `assets/` folder: the app icon, the Geist fonts (SIL Open Font License, see `assets/fonts/OFL-LICENSE.txt`) and the loading animation. Without it the app still runs, just with system fonts and no artwork.

Output is `dist/ConanOps.exe`. This is not code-signed, so Windows
SmartScreen and some antivirus engines will likely flag it on first run
— this is a generic false-positive pattern for unsigned PyInstaller
executables, not a defect in ConanOps, and there is no way to avoid it
without purchasing a code-signing certificate.

**Run as Administrator** if you want the setup wizard's Windows Firewall
step to succeed — `netsh advfirewall` rule changes require elevation.

### Where ConanOps stores its data

| What | Where |
|---|---|
| App config (all servers' settings) | `~/ConanOps/config.json` |
| Per-server session/playtime history | `~/ConanOps/sessions/<server-id>.json` |
| Backups | wherever each server's "Destination folder" setting points |
| `.ini` backups (before every write) | next to the original file, timestamped `.bak` |

---

## 3. First-run setup wizard, step by step

Triggered automatically when you add a new server, or manually via
**"Set Up Server…"** on the Dashboard of a server with no install folder
yet. Implemented in `ui/setup_wizard.py`.

**Page 1 — Paths**
You choose (or accept the suggested default under `~/ConanOps/<id>/`):
- SteamCMD folder
- Server install folder

Both are created automatically if they don't exist.

**Page 2 — Installing**
Runs on a background thread so the UI stays responsive:
1. If `steamcmd.exe` isn't at the chosen path, downloads and extracts
   the official SteamCMD zip, then runs it once so it self-bootstraps.
2. Runs `steamcmd +force_install_dir <path> +login anonymous
   +app_update 443030 validate +quit`, retrying once on failure.
3. Reads back the installed build ID from the Steam appmanifest.

You see the raw SteamCMD output live in a log box.

**Page 3 — Networking**
1. Detects your **local LAN IP** (never the public IP — this is
   deliberate; see [Section 9](#9-data-flow-how-a-setting-reaches-the-game)).
2. Finds a free game port (starting at 7777) and a free query port
   (starting at 27015), checked against both the OS and every other
   server you've already configured in ConanOps.
3. Adds two Windows Firewall inbound-allow rules (UDP, one per port).
4. Attempts UPnP: discovers your router via SSDP, and if it supports
   the Internet Gateway Device protocol, requests port mappings for
   both ports.
5. If UPnP isn't available or a mapping fails, shows your public IP and
   the exact manual forwarding instructions instead.

Clicking Finish writes everything back into that server's config.

---

## 4. The main window, page by page

The sidebar (left) always shows: the ConanOps title, up to 5 servers
(status dot + player count), an "Add server" button, and the main
navigation. Everything to the right of the sidebar reflects whichever
server is currently selected.

### Dashboard
Live stat cards (CPU, memory, FPS, players), a status pill (online/
offline/not configured), the server's connect address, a Restart
button, an online-players list, and a scrolling log view. Stats come
from three independent sources so they stay honest even if one fails:
`psutil` for CPU/memory of the actual OS process, regex-parsed
`LogServerStats` lines from the server's own log for FPS, and a live
A2S_INFO query (the same one Steam's server browser uses) to confirm
the server is actually reachable, not just that a process exists.

### Players
A searchable table: name, total playtime, session count. Built entirely
from parsed join/leave log lines accumulated in
`~/ConanOps/sessions/<id>.json` — this is real history, not a live-only
view, so it survives restarts.

### Backups
Status cards (last backup, next scheduled, retention) and a list of
every backup on disk with its trigger (scheduled / manual / pre-update /
imported) and size. "Back Up Now" runs immediately. "Restore" asks for
confirmation, since it stops the server, extracts the chosen backup
over the current world, and restarts — a safety copy of whatever was
there is taken first automatically.

**"Import External Backup…"** lets you bring in a `.zip` from anywhere
— another server, a different tool, a friend's world — and restore from
it the same way as any backup ConanOps made itself. It does two things
first: validates the file actually looks like a Conan save (rejects a
non-zip or a zip with no `.db` file, no `Saved/` folder, and no
`ServerSettings.ini`/`Engine.ini`, catching the "picked the wrong file"
mistake before anything's touched), then copies it into this server's
own backup folder under ConanOps' normal naming so it just becomes an
entry in the list. It only brings over world/config data — **not** the
source server's ConanOps settings (rates, mods, schedules); the app
says so explicitly in the confirmation dialog before importing, since
that's an easy thing to assume happens automatically when it doesn't.

### Updates
Shows the installed build, a manual "Check Now", and an auto-update
toggle. When an update is available, a card shows the new build number,
a MAJOR-UPDATE badge if the heuristic in `changelog.py` thinks it's an
engine-level change, and the fetched patch notes (or "no changelog text
available yet" if Steam's news API hasn't posted anything). "Back Up &
Update Now" backs up first, then runs the SteamCMD update.

### Mods
An ordered list (load order matters — top loads first) with Add /
Remove / Move Up / Move Down / Enable-Disable. Writing any change
regenerates `modlist.txt`. The **"Bisect to Find Broken Mod"** button
opens a guided dialog: it disables half the currently-enabled mods,
tells you to restart and test, and asks whether the problem is still
happening — narrowing down to a single mod in a handful of rounds
instead of toggling them one at a time.

### Access
Two columns: Whitelist and Ban List, each a simple add/remove list of
SteamID64s. A "Whitelist-only mode" checkbox toggles whether only
listed players can join at all. Bans and unbans apply immediately via
RCON if it's enabled; otherwise they're queued to take effect on the
next restart.

### Console
A single-line RCON command box and a scrolling output log — a direct
line into the running server for anything not otherwise exposed in the
UI. Disabled with an explanatory message until RCON is turned on in
Settings.

### Settings
See [Section 5](#5-settings-pages-in-detail).

---

## 5. Settings pages in detail

There are now **15 settings pages**, covering every setting Funcom
exposes in their own server settings UI — not just a curated subset.
This was a deliberate expansion: the original build covered a handful
of representative settings to prove out the staged-edit pattern; this
version covers all ~88 of them, organized into the same categories
Funcom's own settings screen uses (per the community-maintained Conan
Exiles wiki's Server Configuration page).

Every page follows the same pattern: edits are local ("pending changes")
until you click **Apply**. Nothing is written to the real `.ini` files,
or takes effect on the running server, until then. A **Discard** button
reverts to the last-applied values.

**How this is built**: rather than hand-writing 15 nearly-identical page
files, most of them are generated from a single data table —
`ini_field_specs.py` lists every field (key, label, type, range,
tooltip, default) grouped by category, and `ui/generic_settings_page.py`
builds the actual form from that list. Two pages remain hand-built
because they need custom widgets a generic form can't provide: **Network
& Ports** (live port-conflict detection, IP auto-detect) and **Server
Identity** (which also carries one ConanOps-only field, description,
alongside the real settings — see below).

| Page | Covers | Written to |
|---|---|---|
| **Server Identity** | MOTD, admin password, BattlEye, PvP/ownership toggles, community, region, clan size, nudity cap, voice chat, +1 ConanOps-only description field | `ServerSettings.ini` (description isn't written anywhere — see note) |
| **Network & Ports** | Server name, password, game/query port (+ live conflict detection & fix), bind IP (+ auto-detect), max players | `Engine.ini` (name/password/ports/IP) + `ServerSettings.ini` (max players) |
| **Progression** | The 5 XP-rate multipliers (overall, passive, kill, harvest, craft) | `ServerSettings.ini` |
| **Day / Night Cycle** | Cycle speed, day/night/dawn-dusk speed, catch-up time | `ServerSettings.ini` |
| **Survival** | Stamina cost, hunger/thirst rates, death/loot behavior, corruption | `ServerSettings.ini` |
| **Combat** | Damage multipliers (player/NPC/thrall), durability, respawn speed, avatars | `ServerSettings.ini` |
| **Harvesting** | Spoil rate, harvest amount, resource respawn, land claim radius | `ServerSettings.ini` |
| **Crafting** | Crafting/thrall-conversion time, fuel burn, crafting cost | `ServerSettings.ini` |
| **Building & Decay** | The real "never decay" toggle + decay time multiplier | `ServerSettings.ini` |
| **Chat** | Local chat radius, message length cap, global chat toggle | `ServerSettings.ini` |
| **Purge** | Enable/level/frequency, time windows, prep time, duration, triggers | `ServerSettings.ini` |
| **Pets & Hunger** | Thrall/pet hunger system, starvation, feeding range, diet | `ServerSettings.ini` |
| **Backups** | Retention, interval, destination, back-up-before-update | ConanOps config only |
| **Restart Schedule** | Enable, quiet-hours window, session-history suggestion | ConanOps config only |
| **RCON & Alerts** | RCON enable/port/password, Discord/ntfy webhook URLs | `Engine.ini` (RCON) + ConanOps config |

**Ghost sliders everywhere**: every numeric multiplier field across all
15 pages (not just the original three) shows the ghost-marker treatment
— a ring at the last-applied value that doesn't move as you drag the
live handle, with the span between the two highlighted, implemented once
in `ui/ghost_slider.py` and reused generically.

**Building & Decay's "never decay"**: earlier versions of ConanOps
faked this with a slider that went to zero. It's now the real thing —
`DisableBuildingAbandonment` is Funcom's own dedicated on/off switch for
building decay, exposed directly as a checkbox, alongside the separate
`BuildingDecayTimeMultiplier` that scales *how fast* decay happens when
it's not disabled.

> **Note on the description field**: Server Identity's description box
> is the one field on the whole settings surface that isn't a real Conan
> setting — Funcom's server settings have no such field. It's kept
> purely as a ConanOps note for your own reference and is stored (as
> `__description` in `ServerConfig.gameplay`) but never written to any
> `.ini` file. Every gameplay dict key starting with `__` follows this
> same "persisted, never written to a game file" rule.

> **Note on what's deliberately left out**: Funcom's own wiki documents
> a further ~80 "unexposed server settings," reachable only via the
> in-game console's `GetAllServerSettings`, with their own explicit
> caution that these "can have an extremely negative impact on your
> gameplay experience." ConanOps doesn't surface those, for the same
> reason Funcom doesn't put them in their own settings UI. If you need
> one anyway, you can still hand-edit it directly into the real `.ini`
> file — `ini_utils.apply_known_keys()` is specifically built to never
> touch or remove a line it doesn't recognize, so a hand-added setting
> sits there completely unaffected by anything ConanOps writes.

---

## 6. What runs in the background

Three independent background systems keep running for the app's entire
lifetime, for **every** configured server — not just whichever one is
currently on screen:

**Per-server log monitors** (`log_monitor.py`, one `QThread` per server)
Tail the newest log file, parse it for status/join/leave/save lines,
and feed player-session tracking. This is what makes the scheduler's
"is anyone online" check accurate even for a server you're not
currently looking at.

**The scheduler** (`scheduler.py`, one `QTimer` ticking every 60s)
For each server: is a backup due (interval elapsed since last one)? Is
now inside the restart quiet-hours window, and hasn't a restart already
fired today, and is nobody online? If both, act. If someone's online
during the window, that day is skipped entirely and retried tomorrow —
this is a deliberate safety rule from the original spec, not a bug.
The scheduler also decides *when* to check for updates (throttled by
`auto_update_check_interval_hours`, since checking every 60 seconds
would mean shelling out to SteamCMD every minute); the actual SteamCMD
call and the decision of whether to apply the update happen in
`MainWindow`, not on the scheduler's timer thread, since that's a
blocking network operation.

**Automatic updates** (`update_runner.py`, driven from `ui/main_window.py`)
Two independent triggers both funnel into the same update-apply
sequence (`MainWindow._start_update_apply`):
1. **Periodic check**: on the interval above, if auto-update is on and
   a new build is found *and nobody is currently online*, it updates
   immediately — backup, SteamCMD update, relaunch if it was running.
2. **Post-scheduled-restart check**: every scheduled restart checks for
   an update first. If one's found, the restart becomes an update
   instead (same sequence); if not, it's a plain restart.

If someone's online when a periodic check finds an update, it's simply
left pending — either a later periodic check or the next scheduled
restart will catch it, rather than interrupting active players.

**The health checker** (`ui/main_window.py`, a `QTimer` every 10s)
Compares each server's running-state to what it was a moment ago. If a
server was running and suddenly isn't, and ConanOps didn't just
intentionally stop it (manual restart, scheduled restart, or a
restore), that's treated as a crash and fires a webhook alert.

---

## 7. Configuration file reference

`~/ConanOps/config.json` (see `config.example.json` for a filled-in
sample):

```jsonc
{
  "servers": [ /* array of ServerConfig objects, see below */ ],
  "active_server_id": "abc12345",
  "accent_color": "#c9752f"
}
```

Every field on a `ServerConfig` (from `models.py`):

| Field | Type | Meaning |
|---|---|---|
| `id` | str | Internal 8-char id, generated once, never shown to the user |
| `name` | str | Display name |
| `install_dir` / `steamcmd_dir` | str | Absolute paths |
| `motd` / `map` / `mode` / `region` / `description` | str | Server Identity fields |
| `password` / `admin_password` | str | Server & admin passwords |
| `game_port` / `query_port` | int | UDP ports |
| `bind_ip` | str | Local LAN IP for `-MULTIHOME` |
| `max_players` | int | |
| `xp_rate` / `harvest_multiplier` | float | Gameplay rate multipliers |
| `decay_days` | int | 0 = never decays |
| `pvp_enabled` / `purge_enabled` / `offline_raid_protection` / `stamina_drain_sprint` | bool | |
| `backup_daily_keep` / `backup_weekly_keep` | int | Retention counts |
| `backup_interval_hours` | int | |
| `backup_destination` | str | Folder for backup zips |
| `backup_before_update` | bool | |
| `restart_enabled` | bool | |
| `restart_start` / `restart_end` | str | `"HH:MM"`, 24-hour |
| `auto_update` | bool | (currently informational — see note in Section 11) |
| `installed_buildid` | str | Steam build id, updated after a successful update |
| `rcon_enabled` / `rcon_port` / `rcon_password` | bool/int/str | |
| `webhook_discord_url` / `webhook_ntfy_url` | str | |
| `whitelist_enabled` | bool | |
| `whitelist_ids` / `banned_ids` | list[str] | SteamID64 strings |
| `mods` | list[dict] | `{"id": str, "name": str, "enabled": bool}`, in load order |
| `last_backup_at` / `last_restart_date` | str | Scheduler bookkeeping, not user-facing settings |

---

## 8. Module reference (architecture)

**Non-UI (`conanops/`)**

| Module | Responsibility |
|---|---|
| `models.py` | `ServerConfig`, `AppConfig` — the entire data model and its JSON load/save |
| `network_utils.py` | Local IP detection, UDP/TCP port checks, independent free-port search, A2S_INFO query |
| `network_setup.py` | Windows Firewall rule automation, from-scratch UPnP client (SSDP + SOAP), public-IP lookup |
| `ini_utils.py` | Line-based `.ini` merge: updates/appends only known keys, never touches anything else, backs up before writing |
| `steamcmd.py` | SteamCMD install/bootstrap, `+app_update` with retry, build-id reading, latest-build check |
| `backup_manager.py` | Zip-based backup create/list/restore/prune, plus validated import of an external backup zip |
| `process_manager.py` | Launch/find/stop the actual game server process, matched by exe path (not just name) |
| `log_monitor.py` | `QThread` that tails a log file and parses it into signals |
| `session_tracker.py` | Per-player session history, playtime totals, hourly activity histogram |
| `preflight.py` | Pre-launch/pre-update validation with auto-repair for what's safely fixable |
| `changelog.py` | Steam news API fetch + major-update keyword/version heuristic |
| `scheduler.py` | The 60-second backup/restart/update-check timer and its window-crossing-midnight logic |
| `update_runner.py` | Shared background workers (`CheckWorker`, `UpdateWorker`) for SteamCMD checks/updates, used by both the manual Updates page buttons and the automatic paths |
| `rcon.py` | Source RCON protocol client (raw sockets, no dependency) |
| `mod_manager.py` | Mod list bookkeeping, `modlist.txt` writer, bisect state machine |
| `banlist_manager.py` | Whitelist/ban bookkeeping + RCON-based enforcement |
| `webhooks.py` | Discord/ntfy.sh POST requests |
| `ini_field_specs.py` | Every Conan gameplay setting Funcom exposes (~88 fields): key, section, type, range, default, tooltip, category |

**UI (`conanops/ui/`)**

| Module | Responsibility |
|---|---|
| `theme.py` | The QSS stylesheet (dark, bronze accent) |
| `ghost_slider.py` | Custom-painted `QSlider` subclass with the ghost marker |
| `base_settings_page.py` | Shared Apply/Discard/pending-count logic every Settings page inherits |
| `sidebar.py` | Server switcher + main navigation |
| `settings_container.py` | Dynamic sub-navigation inside Settings (15 pages) |
| `setup_wizard.py` | The 3-page `QWizard` covered in Section 3 |
| `dashboard_page.py`, `players_page.py`, `backups_page.py`, `updates_page.py`, `mods_page.py`, `access_page.py`, `console_page.py` | One file per main nav page |
| `generic_settings_page.py` | Builds a full page from a list of FieldSpec -- used for 12 of the 15 settings pages |
| `settings_identity_page.py`, `settings_network_page.py`, `settings_rates_page.py`, `settings_backups_page.py`, `settings_restart_page.py`, `settings_alerts_page.py` | The remaining hand-built/wrapper pages |
| `main_window.py` | Wires literally everything above together: owns the per-server monitors, the scheduler, the health checker, the tray icon, and every page's callbacks |

---

## 9. Data flow: how a setting reaches the game

Worth understanding end-to-end once, since it explains several design
choices at once:

1. You edit a field on a Settings page. This only updates that page's
   local "draft" state — nothing else happens yet.
2. Click **Apply**. The page's `on_apply(values)` callback (assigned in
   `MainWindow.__init__`) fires with a plain dict of the new values,
   keyed by the real Conan ini key name (e.g. `PlayerDamageMultiplier`).
3. For the 12 gameplay/identity pages, this all funnels into one shared
   handler, `MainWindow._apply_gameplay()` — it merges the values into
   `ServerConfig.gameplay`, saves `config.json`, looks up each key's
   section via `ini_field_specs.ALL_FIELDS_BY_KEY`, and calls
   `ini_utils.apply_known_keys()`. (Network & Ports, Backups, Restart
   Schedule, and RCON & Alerts have their own handlers instead, since
   they touch different files or aren't real game settings at all.)
4. `apply_known_keys()` backs up the target `.ini` file, then does a
   line-by-line pass: known keys get updated in place or appended under
   their section; every other line (including anything a mod or a
   hand-edit added) is left completely untouched.
5. The change takes effect the next time the server restarts — nothing
   here live-reloads a running server's config, by design (the spec's
   "Apply on Next Reset" pattern).

The **local-IP-not-public-IP** rule threads through this whole flow
deliberately: `network_utils.get_local_ip()` is the only IP source ever
written to `bind_ip` / `-MULTIHOME` / `MultiHome=`, because that was the
literal bug (`-MULTIHOME=YOUR_PUBLIC_IP`, an unfilled placeholder in the
original scripts) that started this entire project.

---

## 10. Troubleshooting

**Firewall rules failed to add**
Run ConanOps as Administrator. `netsh advfirewall firewall add rule`
requires elevation on Windows; without it, the wizard reports the exact
`netsh` error rather than failing silently.

**UPnP didn't forward my ports**
Not every router has UPnP enabled (many disable it by default for
security). Use the manual instructions the wizard prints instead —
they include your exact ports and public IP.

**Server won't start after Restart**
Check the pre-flight message ConanOps shows — it names the specific
problem (missing exe, missing SteamCMD, bad install state, etc.) rather
than just failing. If it's not caught there, check
`<install_dir>/ConanSandbox/Saved/Logs/` directly.

**RCON commands don't do anything**
Confirm RCON is actually enabled on the *server* side too — ConanOps
writes `RCONEnabled`/`RCONPort` into `Engine.ini` when you Apply on the
RCON & Alerts page, but that only takes effect on the next restart, same
as every other setting.

**Mods aren't loading**
Check `mod_manager.py`'s own docstring caveat: the `modlist.txt` path
convention it writes was accurate at the time this was built, but
that's exactly the kind of detail a future Conan Exiles update could
change. Compare against Funcom's current modding documentation.

**A ban/unban via RCON says it failed**
Same category of caveat as mods — `banlist_manager.py` uses commonly-
documented RCON command names (`banplayer`, `unbanplayer`,
`kickplayer`); if your server version uses different ones, check
`listcommands` via the Console page.

**Webhook alerts never arrive**
Test the URL directly first (Discord webhooks and ntfy topics both
accept a plain `curl -X POST` to verify the URL itself works) before
assuming ConanOps' side is broken — `webhooks.py` logs failures to
stdout rather than surfacing them in the UI.

---

## 11. Known limitations

- **No code signing** on the packaged `.exe` — requires a purchased
  certificate, not a code fix.
- **UPnP is implemented to spec but untested against real router
  hardware** in this development environment.
- **`auto_update` is now fully acted on by the scheduler**: every
  `auto_update_check_interval_hours` (default 6, adjustable on the
  Updates page), ConanOps asks SteamCMD for the latest build; if one's
  found and nobody's online, it backs up, updates, and relaunches
  immediately. A scheduled restart also always checks for an update
  first and updates instead of just restarting if one's found. (This
  replaces what used to be a stored-but-unused setting.)
- **Server Identity's description field is ConanOps-only** by design —
  Funcom's actual server settings have no such field, so it's never
  written to any `.ini` (see Section 5's note).
- **`MaxPlayers`'s exact real `.ini` location is a best guess.** None of
  the sources checked while building this pinned down definitively
  where Conan reads it from; it's currently written to
  `ServerSettings.ini`'s `[ServerSettings]` section as the most common
  convention across similar Unreal-based dedicated servers.
- **The ~80 "unexposed" settings from Funcom's own wiki are intentionally
  excluded**, matching Funcom's own caution about them. See Section 5.
- **A scheduled restart is skipped entirely for a day** if the quiet-
  hours window passes with someone online the whole time — by design,
  not a bug, but worth knowing it isn't a guaranteed daily restart.
- **Cancelling the setup wizard mid-download is best-effort** — the
  underlying `subprocess.run()` call blocks, so closing the dialog stops
  the UI from waiting on it without forcibly killing an in-progress
  download at the OS level.
- **No RCON connection pooling** — the Console page opens a fresh TCP
  connection and re-authenticates for every single command. Fine for
  occasional admin use; would want a persistent connection for anything
  high-frequency.

---

## 12. Extending ConanOps

A few concrete starting points, since the architecture was built to
make these additions follow existing patterns rather than needing new
ones:

- **Auto-updates on a timer**: implemented — see "Automatic updates" in
  Section 8 and the periodic-check/scheduled-restart-check split in
  `ui/main_window.py`.
- **A new Settings page**: copy the shape of
  `ui/settings_backups_page.py` (simplest existing example), give it a
  `on_apply` callback, add it to `SettingsContainer`'s `SUB_PAGES` list
  and constructor, and wire the callback in `MainWindow.__init__`.
- **A new main nav page**: same idea — build a `QWidget` with a
  `set_server(server)` method, add it to `NAV_ITEMS` in `ui/sidebar.py`
  and to `self.pages` in `MainWindow`.
- **Multi-machine support** (running a server on a different PC than
  ConanOps itself): would require replacing the direct
  `process_manager`/file-path assumptions with a remote-agent protocol
  — a substantially bigger change than anything above, flagged here
  only so it's not mistaken for a small one.

## Unattended operation

App Settings > **Unattended Operation**:

- **Keep my servers running when nobody is signed into Windows** -- registers a Task
  Scheduler task (`\ConanOps\ConanOps Background`, one permission prompt) that starts
  ConanOps without a window at boot and whenever no ConanOps is running (checked every
  2 minutes). Opening ConanOps normally takes over from the background copy without
  stopping servers. See `background_mode.py`.
- **Keep this PC awake while a server is running** -- blocks idle sleep while any server
  runs (`power.py`). On by default; doesn't change Windows power settings.
- **Only restart for Windows updates during this window** -- sets Windows' active hours
  around the window (one permission prompt) and, inside the window with nobody online,
  saves and stops servers and restarts the PC itself. At most once a day, never within
  2 hours of boot. See `windows_update.py`.

## Installer and signing

`BUILD_EXE.bat` also builds `dist\ConanOps-Setup-<version>.exe` when Inno Setup 6 is
installed (`installer\ConanOps.iss`): per-user install to `C:\ConanOps`, no admin prompt,
Start menu entry, uninstaller that keeps the `data` folder (servers, worlds, backups).
Code signing is optional and configured with environment variables -- see `SIGNING.md`.


## Keeping servers up on their own (1.0.2)

- **Keep it running** (last setup step, on by default): start with Windows, reopen
  ConanOps if it stops, keep the PC awake while a server runs, and Windows Update
  restarts only between 4 and 6 AM. Each can be changed in App Settings.
- **Reopen ConanOps if it closes or crashes** (`keep_alive.py`): a scheduled task that
  runs only while you're signed in (no stored password) starts a hidden watcher; if
  ConanOps is gone for two checks a minute apart it's reopened in the tray. Quitting
  from the tray icon is respected until ConanOps is opened again.
- **Router forwards** are re-checked every hour for running servers, since many
  routers forget them when they restart.
- **Sign back in after updates**: App Settings and Diagnostics warn when Windows'
  "Use my sign-in info to automatically finish setting up after an update" is off.
- **Mod-break recovery** (`ui/mod_recovery.py`): when a server keeps crashing after an
  update, or the crash watchdog runs out of retries, ConanOps finds the broken mod with
  the world protected, then (App Settings) waits for the mod's fix and restarts the
  server by itself, starts without the mod after a backup, or just tells you.
- **Dashboard tour** (`ui/tour.py`): shown once after the first setup; App Settings ->
  Show the Dashboard Tour replays it.


## Web version (1.0.3)

App Settings → Web Control. Turn it on, set a password (at least 8 characters), and open the "On your Wi-Fi" link on any device on the same network ("Allow Through Firewall" if Windows blocks it). "Also let me use it from anywhere" downloads Cloudflare's signed `cloudflared.exe` into `data/tools/` and runs a free quick tunnel; the https link appears in App Settings and is sent to the servers' ntfy topics (never Discord) whenever it changes (it changes each time the tunnel reconnects).

The web version (`assets/web/`) is a single-page app served by `web_control.py`; its API (`webui/api.py`) runs every action on the app's GUI thread through `webui/bridge.py`, using the same handlers as the app's buttons. Dialogs the app would show during a web action are returned to the browser as text instead (questions are answered "No"). Settings pages are read and written generically from each page's widgets (`webui/fields.py`).

Security: PBKDF2-SHA256 password hash in the config; random session tokens kept only as SHA-256 hashes in `web_sessions.json`; HttpOnly, SameSite=Strict cookies (Secure over the tunnel); changes require a custom header and matching Origin; 5 wrong passwords lock that address out with growing delays (to 1 hour), plus an overall limit; strict Content-Security-Policy. Not available from the web: deleting ConanOps or servers, adding servers, changing the web password, turning the remote link on/off.

### Windows permission prompts and the web version

A web action never shows a Windows permission (UAC) prompt: `powershell.no_prompts()` is active while the bridge runs it, and `run_privileged()` returns `RUN_NEEDS_PC` instead. Changes that need one (firewall rules for a port change or Repair Networking, Windows update hours, the Visual C++ runtime) are queued with `MainWindow.queue_for_pc()` and offered the next time someone activates the window on the PC; the web shows them as "Waiting for the PC".

"Run with admin rights" (`admin_mode.py`) registers the Task Scheduler task "ConanOps (admin)" (RunLevel Highest, no trigger, priority 4, no time limit). A copy of ConanOps started without admin rights starts the task (`schtasks /Run`, no prompt), hands over the single-instance lock and exits; the elevated copy carries `--elevated` so it never loops, and picks up `--keep-alive` from `admin-launch-args.json`. If the task doesn't start within 20 s, the unelevated copy carries on. The uninstaller asks an elevated copy to close via `quit.request` (an unelevated process can't end it) and removes the task. Trade-off: anything that can write to ConanOps' folder could get admin rights without a prompt.

### How the web version saves settings

`webui/settings.py` keeps its own, never-shown copies of the settings pages. A request loads the chosen server's saved values (`MainWindow.settings_values`), runs the page's own validation, and calls the app's save handler for that server (`_apply_network(values, server)` etc.). If that server is open in the app, `settings_saved_elsewhere` gives its pages the new saved values only for fields nobody is editing (`merge_committed`). Secret fields (passwords, webhook URLs, tokens) are never sent to the browser; it only learns whether one is set.
