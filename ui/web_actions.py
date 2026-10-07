"""
Server actions with real results, for the web version.

The app's buttons report problems with dialogs on the PC. The web
version needs the same actions to say what happened instead, and to
work on ANY server -- not just the one the app happens to show -- so
using it from a phone never switches or disturbs what's open on the PC.

Each action returns "" when it went ahead, or a plain-language reason
it didn't. Everything here runs on the GUI thread (webui.bridge) and
reuses the same building blocks as the app's own buttons.

Written as a mixin, like ui/mod_recovery.py; MainWindow provides the
state it uses.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime
from typing import Dict, List, Optional

import applog
import backup_manager
import banlist_manager
import preflight
import process_manager

_log = applog.get_logger(__name__)


class WebActionsMixin:
    # ------------------------------------------------------------ status --
    def server_busy(self, server) -> str:
        """What's keeping a server busy right now ("" if nothing). Start,
        stop, restores, updates and mod changes wait for these."""
        sid = server.id
        if sid in getattr(self, "_recovery_workers", {}):
            return "Finding a broken mod"
        if sid in self._automation_locked:
            return "Testing mods (in the app)"
        if sid in self._restore_workers:
            return "Restoring a backup"
        if (sid in self._update_apply_workers or sid in self._manual_update_in_progress
                or sid in self.updates_page._update_workers):
            return "Updating"
        if sid in self._update_check_workers:
            return "Checking for an update"
        if sid in self._mod_refresh_workers:
            return "Downloading mods"
        if sid in self._expected_stop:
            return "Stopping"
        return ""

    def is_running(self, server) -> bool:
        return bool(server.install_dir) and process_manager.is_running(server.install_dir)

    def _clear_online(self, server_id: str) -> None:
        """Nobody can be online once a server stops; the app's player
        lists are told right away (not at the next join/leave)."""
        self._online_by_server[server_id] = set()
        if self.config.active_server_id == server_id:
            self.dashboard_page.set_online_players(set())
            self.players_page.set_online_players(set())

    def _preflight_problem(self, server, verb: str) -> str:
        if server.install_dir and not os.path.isdir(server.install_dir):
            return (f"Can't {verb}: the server's files aren't on this PC anymore (deleted or moved). Download "
                    f"them again from the app on the PC.")
        result = preflight.run_preflight(server)
        if not result.ok:
            return f"Can't {verb} -- pre-flight checks failed:\n" + "\n".join(result.problems)
        if result.repairs:
            self._notify(server, f"Auto-repaired before {verb}:\n" + "\n".join(result.repairs), title="Auto-Repair")
        return ""

    def _ready(self, server, allow_busy=("Checking for an update",)) -> str:
        if not server.install_dir:
            return "This server isn't set up yet -- finish setup in the app on the PC."
        busy = self.server_busy(server)
        if busy and busy not in allow_busy:
            return f"Wait until this finishes: {busy}."
        return ""

    # ------------------------------------------------------------- power --
    def start_server(self, server) -> str:
        problem = self._ready(server, allow_busy=())
        if problem:
            return problem
        if self.is_running(server):
            return "It's already running."
        problem = self._preflight_problem(server, "start")
        if problem:
            return problem
        try:
            self._launch_by_request(server)
        except FileNotFoundError as e:
            return f"Start failed: {e}"
        self._refresh_chrome()
        return ""

    def _launch_by_request(self, server) -> None:
        """What a person clicking Start means (shared with the app's
        Start button): run it, keep it running through reboots, and
        override any hold or wait for a mod fix."""
        process_manager.launch(server)
        self._known_running[server.id] = True
        server.desired_running = True
        server.update_hold = ""
        server.mod_recovery = {}
        self._post_update_watch.pop(server.id, None)
        self.config.save()
        self._watchdog_attempts.pop(server.id, None)

    def stop_server(self, server) -> str:
        problem = self._ready(server)
        if problem:
            return problem
        if not self.is_running(server):
            return "It isn't running."
        self._handle_stop(server)
        self._refresh_chrome()
        return ""

    def restart_server(self, server) -> str:
        problem = self._ready(server, allow_busy=())
        if problem:
            return problem
        problem = self._preflight_problem(server, "restart")
        if problem:
            return problem
        # manual="web": a failure later goes to the server's alerts, not
        # a dialog on the PC that nobody would see.
        self._warn_then_restart(server, manual="web")
        self._refresh_chrome()
        return ""

    def enable_rcon(self, server) -> None:
        """RCON on with a random password and a port no other server uses
        (the dashboard's "Turn On RCON"). Written to Game.ini now; the
        server picks it up the next time it starts."""
        server.rcon_enabled = True
        if not server.rcon_password:
            server.rcon_password = secrets.token_urlsafe(12)
        taken = self.config.used_ports(exclude_id=server.id) | {server.game_port, server.game_port + 1,
                                                                 server.query_port}
        while server.rcon_port in taken:
            server.rcon_port += 1
        self.config.save()
        if server.install_dir:
            process_manager.sync_rcon_ini(server)
        self.settings_saved_elsewhere(server, "alerts")
        if self.config.active_server_id == server.id:
            self.console_page.set_server(server)
        running = bool(self._known_running.get(server.id))
        self._notify(server, "RCON turned on." + (" It takes effect the next time the server restarts."
                                                  if running else ""), title="RCON On")
        self._refresh_chrome()

    # ----------------------------------------------------------- players --
    def kick(self, server, name: str) -> str:
        """Returns the result message (also sent to the server's alerts,
        same as the app)."""
        if not server.rcon_enabled:
            return ("Kicking needs RCON, which is off for this server. Turn it on (Dashboard → Turn On RCON); "
                    "it works after the server's next restart.")
        message = banlist_manager.kick_player(server, name)
        self._notify(server, message, title="Player Kicked")
        return message

    def ban(self, server, steam_id: str, player_name: str = "") -> str:
        if not server.install_dir:
            return "This server isn't set up yet -- finish setup in the app on the PC."
        message = banlist_manager.ban_player(server, steam_id)
        who = f"{player_name} ({steam_id})" if player_name else steam_id
        self._notify(server, f"{who}: {message}", title="Player Banned")
        self.access_changed(server)
        return message

    def unban(self, server, steam_id: str) -> str:
        if not server.install_dir:
            return "This server isn't set up yet -- finish setup in the app on the PC."
        message = banlist_manager.unban_player(server, steam_id)
        self.access_changed(server)
        return message

    def access_changed(self, server) -> None:
        self.config.save()
        if self.config.active_server_id == server.id:
            self.access_page.set_server(server)

    # ----------------------------------------------------------- backups --
    def back_up_now(self, server) -> str:
        """Same as the Backups page's "Back Up Now". Returns "" or why not.
        Runs on the GUI thread like the app's button."""
        if not server.install_dir:
            return "This server isn't set up yet -- finish setup in the app on the PC."
        if not server.backup_destination:
            return "Set a backup folder for this server first (Server Settings → Backups)."
        busy = self.server_busy(server)
        if busy in ("Restoring a backup", "Updating", "Finding a broken mod", "Testing mods (in the app)"):
            return f"Wait until this finishes: {busy}."
        try:
            entry = backup_manager.create_backup_for_server(server, server.backup_destination,
                                                            backup_manager.TRIGGER_MANUAL)
        except backup_manager.BackupSpaceError as e:
            return str(e)
        if entry is None:
            saved = backup_manager.saved_dir(server.install_dir)
            if os.path.isdir(saved):
                return ("The backup didn't pass its own integrity check after being written, so it was deleted. "
                        "This usually means a disk problem (full or failing) on the backup drive -- see conanops.log.")
            return ("There's no world save to back up yet -- Conan Exiles creates it the first time the server "
                    "starts.")
        self.backups_changed(server)
        return ""

    def backups_changed(self, server) -> None:
        if self.config.active_server_id == server.id:
            self.settings_backups_page.refresh()

    # -------------------------------------------------------------- mods --
    def download_mods(self, server) -> str:
        """The Mods page's "Download Mods", for any server: fetches every
        mod with SteamCMD, rewrites modlist.txt, and reports the result
        in the server's alerts."""
        if not server.mods:
            return "There are no mods to download."
        if not server.steamcmd_dir:
            return "This server has no SteamCMD folder yet -- finish setup in the app on the PC."
        busy = self.server_busy(server)
        if busy in ("Finding a broken mod", "Testing mods (in the app)", "Updating"):
            return f"Wait until this finishes: {busy}."
        from update_runner import ModDownloadWorker
        worker = ModDownloadWorker(server.steamcmd_dir, [m["id"] for m in server.mods])
        if not self._claim_mod_refresh_worker(server.id, worker):
            return "Mods are already downloading for this server."
        worker.finished_download.connect(lambda result, srv=server: self._on_requested_download_finished(srv, result))
        self._retire_worker(worker)
        worker.start()
        self.mods_changed_elsewhere(server)
        return ""

    def _on_requested_download_finished(self, server, result) -> None:
        self._release_mod_refresh_worker(server.id)
        self._handle_mods_changed(server)  # real .pak paths into modlist.txt
        if result.success:
            self._notify(server, "All mods were downloaded/updated. They load the next time the server starts.",
                         title="Mods Downloaded")
        else:
            self._notify(server, "One or more mods failed to download -- the server may fail to start, or start "
                                 "without them:\n" + (result.output[-1500:] if result.output else "(no details)"),
                         title="Mod Download Problems")

    def mods_changed_elsewhere(self, server) -> None:
        """Mods changed outside the Mods page (the web version, a download,
        mod recovery): show it there if that server is open."""
        if self.config.active_server_id == server.id and getattr(self.mods_page, "server", None) is server:
            self.mods_page._refresh()

    def find_broken_mod(self, server) -> str:
        if not server.install_dir:
            return "This server isn't set up yet -- finish setup in the app on the PC."
        if not any(m.get("enabled", True) for m in server.mods):
            return "No mods are turned on, so there's nothing to test."
        if not server.steamcmd_dir:
            return "This server has no SteamCMD folder yet -- finish setup in the app on the PC."
        if self._online_by_server.get(server.id):
            return "Players are online -- the check restarts the server many times. Try when nobody's on."
        busy = self.server_busy(server)
        if busy and busy != "Checking for an update":
            return f"Wait until this finishes: {busy}."
        if not self._start_mod_recovery(server, "", manual=True):
            return "The mod check couldn't start -- see conanops.log on the PC."
        return ""

    # ---------------------------------------------------------- settings --
    def settings_values(self, server) -> Dict[str, dict]:
        """A server's saved values for each settings page, by page key --
        exactly what the app's settings pages load."""
        values = {"identity": server.gameplay, "progression": server.gameplay}
        for key in self.gameplay_pages:
            values[key] = server.gameplay
        values["network"] = {
            "name": server.name, "password": server.password,
            "game_port": server.game_port, "query_port": server.query_port,
            "bind_ip": server.bind_ip, "max_players": server.max_players,
        }
        values["backups"] = {
            "backup_daily_keep": server.backup_daily_keep,
            "backup_weekly_keep": server.backup_weekly_keep,
            "backup_interval_hours": server.backup_interval_hours,
            "backup_destination": server.backup_destination,
            "backup_before_update": server.backup_before_update,
        }
        values["restart"] = {
            "restart_enabled": server.restart_enabled,
            "restart_start": server.restart_start,
            "restart_end": server.restart_end,
        }
        values["alerts"] = {
            "rcon_enabled": server.rcon_enabled,
            "rcon_port": server.rcon_port,
            "rcon_password": server.rcon_password,
            "webhook_discord_url": server.webhook_discord_url,
            "webhook_ntfy_url": server.webhook_ntfy_url,
            "discord_status_enabled": server.discord_status_enabled,
        }
        return values

    def settings_apply_handler(self, key: str):
        """The save function for a settings page, taking (values, server)."""
        if key == "network":
            return self._apply_network
        if key == "backups":
            return self._apply_backups_settings
        if key == "restart":
            return self._apply_restart
        if key == "alerts":
            return self._apply_alerts
        return self._apply_gameplay

    def settings_page_for(self, key: str):
        return next((page for k, _label, page in self.settings_container_entries if k == key), None)

    def settings_saved_elsewhere(self, server, key: Optional[str] = None) -> None:
        """Settings were saved for `server` somewhere other than the app's
        settings pages (the web version). If that server is open in the
        app, its pages take the new saved values -- without touching
        anything someone is still editing there."""
        if self.config.active_server_id != server.id:
            return
        values = self.settings_values(server)
        keys = [key] if key else list(values)
        if key in (None, "identity", "progression") or key in self.gameplay_pages:
            keys = list(dict.fromkeys(keys + ["identity", "progression", *self.gameplay_pages]))
        for k in keys:
            page = self.settings_page_for(k)
            if page is not None and k in values:
                merge_committed(page, values[k])



def merge_committed(page, values: dict) -> None:
    """Gives a settings page new saved values. A field the person hasn't
    touched shows the new value; a field they're editing keeps their
    edit (and still counts as a pending change)."""
    for name, new in values.items():
        if name not in page._fields:
            continue
        old = page._committed.get(name)
        current = page._getters[name](page._fields[name])
        page._committed[name] = new
        if current == old and current != new:
            page._setters[name](page._fields[name], new)
        slider = (getattr(page, "_sliders", {}) or {}).get(name)
        if slider is not None:
            widget, scale = slider
            widget.set_committed_value(int(round(float(new) * scale)))
    page._update_pending_ui()
