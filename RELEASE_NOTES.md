ConanOps runs Conan Exiles dedicated servers on your own Windows PC without the usual fiddling. Download **ConanOps-Setup** below and run it; if you already have ConanOps, it updates itself.

### New in this version

- **A brand-new web version** – everything the app does, from your phone or any browser: the dashboard with live CPU, memory, players and log; start, stop and restart; players (search, kick, ban); the RCON console and broadcasts; mods (turn on/off, reorder, add, Workshop search with filters, download, find a broken mod); updates with history; backups (back up, restore, and import a backup file from your phone); whitelist and bans; every server setting; diagnostics with the port-forwarding guide; and ConanOps' own options (keep-running, Windows update hours, Steam Workshop key, DuckDNS).
- **Works on any server without disturbing the PC** – using the web version never switches which server the app shows, and never touches settings someone is still typing on the PC. Changes show up in the app straight away.
- **Use it from anywhere** – turn on "Also let me use it from anywhere" in App Settings for a secure https link through Cloudflare. No router changes, works on mobile data, and your home address stays hidden.
- **Password sign-in** – set a password in App Settings → Web Version. Your phone stays signed in, wrong guesses get locked out, and "Sign Everyone Out" ends every session. Passwords and webhook links are never sent to the browser.
- **No more stuck Windows permission prompts** – a change from the web that needs Windows' permission (firewall rules, update hours) waits for someone at the PC instead of leaving a prompt on an empty screen, and the web tells you so.
- **New: "Run with admin rights"** (App Settings) – Windows asks once; after that ConanOps never needs a permission prompt, so everything works from the web too.
- **Same layout everywhere** – the web version has the same pages, order, sections and headings as the app, so anything you know in one works in the other.
- **Redesigned App Settings** – instead of one long scrolling page, App Settings now has sections (Startup, Keep Running, Web Version, Dynamic DNS, Steam Workshop, Appearance, PIN Lock, Updates, Delete), in the app and on the web.
- **Discord and ntfy setup guides** – step-by-step guides that fold out right under the alert fields, plus a **Send Test** button to check an alert link before saving it.
- Deleting servers or ConanOps itself, adding new servers, the app lock PIN and changing the web password still need the PC.

### Fixes

- **Discord alerts:** Discord's protection could refuse ConanOps' messages; they now identify themselves properly. Long alerts are trimmed to Discord's limit, alerts never ping @everyone, and webhook links for threads work.
- **ntfy alerts** failed every time because of the "—" in alert titles. Fixed.
- The from-anywhere web link is only sent to ntfy (a private phone alert), never to Discord, where players might see it.
- Newly added mods no longer show as "Not on the Workshop" until the next check; their status is checked right away.
- Saving RCON & Alerts no longer turns off the Discord live status message.
- An available server update no longer disappears from the Updates page when you switch servers, and update history is kept per server.
- The Backups list refreshes by itself when backups are added (scheduled, before updates, before a restore).
- The Mods page shows downloads started automatically, and refreshes after them.
- Player lists clear as soon as a server stops or restarts.

### Requirements

Windows 10 or 11 (64-bit), about 50 GB of free space, around 8 GB of RAM for a small group, and an internet connection.

ConanOps isn't code-signed yet, so Windows may say "Windows protected your PC" – click **More info → Run anyway**.

ConanOps isn't affiliated with Funcom or Valve. Conan Exiles is a trademark of Funcom.
