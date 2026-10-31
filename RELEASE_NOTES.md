ConanOps runs Conan Exiles dedicated servers on a Windows PC and handles their routine maintenance. To install it, download **ConanOps-Setup** below and run it; existing copies of ConanOps update themselves.

### New in this version: more reliable router forwarding

- **Finding the router.** ConanOps now searches for the router several times and for more device types, asks the router directly when the network blocks searches, tries the addresses where common routers publish their settings, and remembers the router that worked last time. The router's name is shown in Check for Problems and Repair Networking.
- **NAT-PMP and PCP.** Routers that have UPnP switched off, or that refuse some UPnP requests, often support one of these two protocols. ConanOps now uses them for any port that UPnP could not forward, and renews those forwards while the server runs.
- **Fewer failed forwards.** ConanOps tries every internet connection the router offers, retries with a simpler label on routers that reject the usual one, and reports a router that has UPnP switched on but refuses changes, with the setting to look for.
- **Forwards restored within minutes.** The forwards are now checked every five minutes instead of every hour, so a router restart or firmware update no longer leaves a server unreachable for long. The check is quick when nothing has changed.
- **Seeing your own server.** Many routers do not pass connections to the public address back into the home network (NAT loopback). On such routers the server appears in the in-game browser on the host's network and then disappears, although players on the internet see it normally. Check for Problems now reports this as *Joining from your own network*, and the Dashboard shows the address to use for Direct Connect on the home network. No program can switch NAT loopback on from the PC; the User Guide lists what can be done in the router.

### Fixed in 1.0.16 (included)

- **New servers kept the default name.** A server created with the setup wizard was listed in the server browser as "Conan Exiles Server". ConanOps now writes each server's name, password, ports, player limit and RCON settings to the server's own files before every start.

The [User Guide](https://github.com/PeanutPenguin/ConanOPs/blob/main/USER_GUIDE.md) describes every feature in detail.

### Requirements

Windows 10 or 11 (64-bit), about 50 GB of free disk space, around 8 GB of RAM for a small group of players, and an internet connection.

ConanOps is not yet code-signed, so Windows may display "Windows protected your PC". Select **More info**, then **Run anyway**.

ConanOps is not affiliated with Funcom or Valve. Conan Exiles is a trademark of Funcom.
