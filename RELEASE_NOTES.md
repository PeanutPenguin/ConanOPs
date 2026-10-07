ConanOps runs Conan Exiles dedicated servers on your own Windows PC without the usual fiddling. Download **ConanOps-Setup** below and run it; if you already have ConanOps, it updates itself.

### New in this version

- **The web version now matches the app** – same pages, order, sections and headings, so anything you know in one works in the other. Server Settings has the same sections (including Diagnostics and Backups), and the web can now also import a backup from your phone, search the Workshop with filters, check for outdated mods, show update history, open the port-forwarding guide, and change ConanOps' own options (Windows update hours, Steam Workshop key, DuckDNS).
- **Redesigned App Settings** – instead of one long scrolling page, App Settings has sections: Startup, Keep Running, Web Version, Dynamic DNS, Steam Workshop, Appearance, PIN Lock, Updates and Delete. In the app and on the web.
- **Discord and ntfy setup guides** – step-by-step guides that fold out right under the alert fields, plus a **Send Test** button to check an alert link before saving it.
- **Works on any server without disturbing the PC** – using the web version never switches which server the app shows, and never touches settings someone is still typing on the PC. Changes show up in the app straight away.
- **No more stuck Windows permission prompts** – a change from the web that needs Windows' permission (firewall rules, update hours) waits for someone at the PC instead of leaving a prompt on an empty screen, and the web tells you so.
- **New: "Run with admin rights"** (App Settings → Keep Running) – Windows asks once; after that ConanOps never needs a permission prompt, so everything works from the web too.

### Fixes

- **Discord alerts** could be refused by Discord's protection; they now identify themselves properly. Long alerts are trimmed to Discord's limit, alerts never ping @everyone, and webhook links for threads work.
- **ntfy alerts** failed every time because of the "—" in alert titles.
- Saving RCON & Alerts no longer turns off the Discord live status message, and changing the Discord link starts a fresh status message in the new channel.
- The from-anywhere web link is only sent to ntfy (a private phone alert), never to Discord, where players might see it.
- Backups made from the web, on schedule or before updates now show up in the app's Backups list right away.
- Kick and ban from the web say what actually happened (no more "Kicked" when RCON is off); start, stop, restart, updates and mod downloads report real results and wait for anything else in progress.
- Newly added mods are checked on the Workshop right away instead of showing "Not on the Workshop".
- An available server update no longer disappears when you switch servers, and update history is kept per server.
- The app's Mods page shows downloads started elsewhere; player lists clear as soon as a server stops; the console keeps its output when RCON settings change.

### Requirements

Windows 10 or 11 (64-bit), about 50 GB of free space, around 8 GB of RAM for a small group, and an internet connection.

ConanOps isn't code-signed yet, so Windows may say "Windows protected your PC" – click **More info → Run anyway**.

ConanOps isn't affiliated with Funcom or Valve. Conan Exiles is a trademark of Funcom.
