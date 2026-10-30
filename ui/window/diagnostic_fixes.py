"""One-click fixes for problems the Diagnostics page finds.

Each fix uses the same handler as the matching button elsewhere in the app
(Repair Networking, Start, Restore, ...). `done(rerun=True)` tells the page
the fix finished; rerun=False just re-enables it (for fixes that finish
later, such as a restart, or that open another page).

Part of MainWindow (see ui/main_window.py)."""
from __future__ import annotations

import os
from typing import Callable

from PySide6.QtWidgets import QMessageBox

import applog
import diagnostics
from models import ServerConfig

_log = applog.get_logger('ui.main_window')

Done = Callable[..., None]


class DiagnosticFixesMixin:
    def _diagnostics_fix(self, action: str, server: ServerConfig, done: Done) -> None:
        handler = {
            diagnostics.FIX_SETUP: self._fix_setup,
            diagnostics.FIX_INSTALL_STEAMCMD: self._fix_install_steamcmd,
            diagnostics.FIX_REPAIR_FILES: self._fix_repair_files,
            diagnostics.FIX_FREE_PORT: self._fix_free_port,
            diagnostics.FIX_REPAIR_NETWORK: self._fix_repair_network,
            diagnostics.FIX_START: self._fix_start,
            diagnostics.FIX_RESTART: self._fix_restart,
            diagnostics.FIX_ENABLE_RCON: self._fix_enable_rcon,
            diagnostics.FIX_RESTORE_BACKUP: self._fix_restore_backup,
            diagnostics.FIX_BACKUP_FOLDER: self._fix_backup_folder,
            diagnostics.FIX_DOWNLOAD_MODS: self._fix_download_mods,
            diagnostics.FIX_FIND_BAD_MOD: self._fix_find_bad_mod,
            diagnostics.FIX_DISABLE_BATTLEYE: self._fix_disable_battleye,
        }.get(action)
        if handler is None:
            _log.warning(f"Unknown Diagnostics fix: {action}")
            done(rerun=False)
            return
        try:
            handler(server, done)
        except Exception as e:  # noqa: BLE001 - the page must never stay stuck on "Working…"
            _log.error(f"Diagnostics fix {action} failed: {e}")
            QMessageBox.warning(self, "Fix Failed", f"That didn't work: {e}")
            done(rerun=False)

    # ----------------------------------------------------------- install --
    def _fix_setup(self, server: ServerConfig, done: Done) -> None:
        self._open_setup_wizard(server)
        done()

    def _fix_install_steamcmd(self, server: ServerConfig, done: Done) -> None:
        import steamcmd
        if not server.steamcmd_dir:
            from ui.setup_wizard import _no_space_default_base
            server.steamcmd_dir = os.path.join(_no_space_default_base(), server.id, "steamcmd")
            self.config.save()

        def failed(e):
            QMessageBox.warning(self, "SteamCMD", f"Couldn't install SteamCMD: {e}")
            done()
        self._run_op(lambda d=server.steamcmd_dir: steamcmd.install_steamcmd(d), on_done=lambda _r: done(),
                     on_error=failed)

    def _fix_repair_files(self, server: ServerConfig, done: Done) -> None:
        self._files_repaired.discard(server.id)
        if not self._repair_game_files(server, why="requested from Diagnostics", then_start=False):
            QMessageBox.information(self, "Busy", "An update or another job is running for this server -- try "
                                                  "again in a minute.")
            done(rerun=False)
            return
        worker = self._update_apply_workers.get(server.id)
        if worker is not None:
            worker.finished_update.connect(lambda *_a: done())
        else:
            done()

    # ----------------------------------------------------------- network --
    def _fix_free_port(self, server: ServerConfig, done: Done) -> None:
        import network_utils
        reserved = self.config.used_ports(exclude_id=server.id)
        running = bool(self._known_running.get(server.id))
        if not running:
            # Ports something else on this PC holds right now count as taken too.
            reserved |= {p for p in (server.game_port, server.game_port + 1, server.query_port)
                         if not network_utils.is_udp_port_free(p)}
        start_game = server.game_port if server.game_port not in reserved else 7777
        game, query = network_utils.find_free_port_pair(start_game, reserved, start_query_port=server.query_port)
        if query in (game, game + 1):
            game, query = network_utils.find_free_port_pair(start_game, reserved | {query}, start_query_port=query + 2)
        if (game, query) == (server.game_port, server.query_port):
            QMessageBox.information(self, "Ports", "This server's ports are already free.")
            done()
            return
        answer = QMessageBox.question(
            self, "Use Free Ports",
            f"Change \"{server.name}\" from game port {server.game_port} / query port {server.query_port} to "
            f"{game} / {query}?\n\nConanOps updates the firewall rules and router forwards. Friends who connect by "
            f"address need the new game port. The change takes effect the next time the server starts.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            done(rerun=False)
            return
        self._apply_network({"game_port": game, "query_port": query}, server)
        self.settings_saved_elsewhere(server, "network")
        done()

    def _fix_repair_network(self, server: ServerConfig, done: Done) -> None:
        import network_utils
        local = network_utils.list_local_ipv4s()
        if server.bind_ip and local and server.bind_ip not in local:
            # The saved address is stale (e.g. the router handed out a new one): use the current one.
            ip = network_utils.get_local_ip()
            if network_utils.is_usable_lan_ipv4(ip):
                server.bind_ip = ip
                self.config.save()
                self.settings_saved_elsewhere(server, "network")
        self.prepare_network_repair(server)

        def finished(result, srv=server):
            QMessageBox.information(self, f"Repair Networking — {srv.name}",
                                    "\n".join(self.network_repair_lines(result)) or "Done.")
            done()
        self._start_network_reconcile(server, legacy_names=[server.name], on_done=finished)

    # ------------------------------------------------------------- power --
    def _fix_start(self, server: ServerConfig, done: Done) -> None:
        self._handle_start(server)
        done()

    def _fix_restart(self, server: ServerConfig, done: Done) -> None:
        self._handle_restart(server)
        QMessageBox.information(self, "Restarting", f"\"{server.name}\" is restarting. Run Diagnostics again in a "
                                                    f"few minutes, once it's back up.")
        done(rerun=False)

    def _fix_enable_rcon(self, server: ServerConfig, done: Done) -> None:
        self.enable_rcon(server)
        self.settings_saved_elsewhere(server, "alerts")
        if self.config.active_server_id == server.id:
            self.console_page.set_server(server)
        if self._known_running.get(server.id):
            answer = QMessageBox.question(
                self, "RCON Turned On",
                "RCON is on. It takes effect the next time the server starts. Restart it now? Players are warned "
                "first.", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer == QMessageBox.Yes:
                self._handle_restart(server)
                done(rerun=False)
                return
        done()

    # ----------------------------------------------------------- backups --
    def _fix_restore_backup(self, server: ServerConfig, done: Done) -> None:
        import backup_manager
        backups = backup_manager.list_backups(server.backup_destination) if server.backup_destination else []
        if not backups:
            QMessageBox.information(self, "No Backups", "This server has no backups to restore.")
            done(rerun=False)
            return
        latest = backups[0]
        answer = QMessageBox.question(
            self, "Restore Latest Backup",
            f"Restore \"{server.name}\" from the backup made {latest.when:%b %d, %I:%M %p}?\n\nThe server is "
            f"stopped first, and a safety copy of the current world is kept.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer != QMessageBox.Yes:
            done(rerun=False)
            return
        self._handle_restore(server, latest)
        worker = self._restore_workers.get(server.id)
        if worker is not None:
            worker.finished_restore.connect(lambda *_a: done())
        else:
            done()

    def _fix_backup_folder(self, server: ServerConfig, done: Done) -> None:
        import backup_manager
        import cleanup
        from ui.setup_wizard import _no_space_default_base
        if cleanup.is_adopted(server):
            folder = os.path.join(_no_space_default_base(), server.id, "backups")
        else:
            folder = backup_manager.default_backup_destination(server.install_dir)
        server.backup_destination = folder
        self.config.save()
        self.settings_saved_elsewhere(server, "backups")
        if self.config.active_server_id == server.id:
            self.settings_backups_page.refresh()
        QMessageBox.information(self, "Backups", f"Backups for \"{server.name}\" now go to:\n{folder}\n\nYou can "
                                                 f"change this under Server Settings → Backups.")
        done()

    # -------------------------------------------------------------- mods --
    def _fix_download_mods(self, server: ServerConfig, done: Done) -> None:
        problem = self.download_mods(server)
        if problem:
            QMessageBox.information(self, "Download Mods", problem)
        else:
            QMessageBox.information(self, "Download Mods", "Downloading the mods. You'll get a notice when it's "
                                                           "done; then run Diagnostics again.")
        done(rerun=False)

    def _fix_find_bad_mod(self, server: ServerConfig, done: Done) -> None:
        self._on_nav_selected("mods")
        done(rerun=False)
        try:
            self.mods_page.fix_btn.showMenu()  # opens the mod tools menu
        except (AttributeError, RuntimeError):
            pass

    def _fix_disable_battleye(self, server: ServerConfig, done: Done) -> None:
        answer = QMessageBox.question(
            self, "Turn Off BattlEye",
            f"Turn off BattlEye (the anti-cheat) for \"{server.name}\"? It takes effect the next time the server "
            f"starts. You can turn it back on under Server Settings → Server Identity.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer == QMessageBox.Yes:
            self._apply_gameplay({"IsBattlEyeEnabled": False}, server)
            self.settings_saved_elsewhere(server, "identity")
        done(rerun=False)
