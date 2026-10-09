"""
Server actions for the web version (a MainWindow mixin). They work on any
server without disturbing what's open on the PC, and return "" on success or
a plain-language reason instead of showing dialogs. All run on the GUI thread.
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
    def server_busy(self, server) -> str:
        """What's keeping a server busy right now ("" if nothing)."""
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
        """Clears a stopped server's online players right away."""
        self._online_by_server[server_id] = set()
        if self.config.active_server_id == server_id:
            self.dashboard_page.set_online_players(set())
            self.players_page.set_online_players(set())

    def _preflight_problem(self, server, verb: str) -> str:
        if server.install_dir and not os.path.isdir(server.install_dir):
            return (f"Can't {verb}: the server's files aren't on this PC anymore (deleted or moved). Download "
                    f"them again from the app on the PC.")
        result = preflight.run_preflight(server)
        if getattr(result, "files_missing", False) and self._repair_game_files(
                server, why=f"game files were missing when asked to {verb} it", then_start=True,
                stop_first=self.is_running(server)):
            return (f"Some game files were missing, so ConanOps is having Steam check and re-download them, "
                    f"then it starts the server. You'll get an alert when it's done.")
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
        """A manual Start: launch, keep running through reboots, and clear any hold."""
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
        # manual="web": failures go to alerts, not a PC dialog nobody sees.
        self._warn_then_restart(server, manual="web")
        self._refresh_chrome()
        return ""

    def enable_rcon(self, server) -> None:
        """Turns RCON on with a random password and unused port. Takes effect on next start."""
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

    def kick(self, server, name: str) -> str:
        """Returns the result message (also sent to the server's alerts)."""
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

    def back_up_now(self, server) -> str:
        """Same as the Backups page's "Back Up Now". Returns "" or why not."""
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

    def download_mods(self, server) -> str:
        """Downloads every mod, rewrites modlist.txt, and reports in the server's alerts."""
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
        """Refreshes the Mods page if it shows this server."""
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

    def settings_values(self, server) -> Dict[str, dict]:
        """A server's saved values for each settings page, by page key."""
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
        """After an outside save, refreshes the open pages for `server`
        without touching in-progress edits."""
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
    """Gives a settings page new saved values; fields being edited keep their edit."""
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
