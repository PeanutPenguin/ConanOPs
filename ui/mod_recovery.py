"""
Automatic recovery when a mod keeps a server from starting (often right after
a game update). Finds the culprit with the world-protected search
(auto_bisect_runner), then per App Settings:
  - "wait": keep the server stopped (running without the mod deletes its
    buildings/items on the next save), check the Workshop every 30 minutes,
    and restart once the mod is updated.
  - "start_without": back up, disable the mods, start the server.
  - "alert": only report the mod.
Mixin for MainWindow, which provides _notify, _known_running,
_online_by_server, _automation_locked, _handle_mods_changed,
_begin_post_update_watch and config.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QThread, QTimer, Signal

import applog
import backup_manager
import mod_manager
import process_manager

_log = applog.get_logger(__name__)

MODE_WAIT = "wait"
MODE_START_WITHOUT = "start_without"
MODE_ALERT = "alert"


class _ModUpdateCheckWorker(QThread):
    """Asks the Workshop when each broken mod was last updated."""
    finished_check = Signal(object)  # {id: time_updated} or None on failure

    def __init__(self, mod_ids, api_key: str, parent=None):
        super().__init__(parent)
        self.mod_ids = list(mod_ids)
        self.api_key = api_key

    def run(self) -> None:
        import steam_workshop_api as swa
        try:
            result = swa.get_details(self.mod_ids, api_key=self.api_key)
            self.finished_check.emit({mid: it.time_updated for mid, it in result.items.items()} if result.ok else None)
        except Exception as e:  # noqa: BLE001
            _log.warning(f"Broken-mod update check failed: {e}")
            self.finished_check.emit(None)


class ModRecoveryMixin:
    _MOD_FIX_CHECK_MS = 30 * 60 * 1000

    def _init_mod_recovery(self) -> None:
        self._recovery_workers: dict = {}      # server id -> AutoBisectWorker
        self._recovery_manual: dict = {}       # server id -> was it running (only for checks started by hand)
        self._fix_check_workers: dict = {}     # server id -> _ModUpdateCheckWorker
        self._mod_fix_timer = QTimer(self)
        self._mod_fix_timer.timeout.connect(self._check_for_mod_fixes)
        self._mod_fix_timer.start(self._MOD_FIX_CHECK_MS)
        QTimer.singleShot(90_000, self._check_for_mod_fixes)

    # ------------------------------------------------------------ start --
    def _start_mod_recovery(self, server, what: str, manual: bool = False) -> bool:
        """Start the automatic culprit search. False means recovery doesn't
        apply and the caller should alert instead. manual: a requested check;
        a server that was running and turns out fine is restarted."""
        mode = getattr(self.config, "mod_recovery_mode", MODE_WAIT)
        enabled = [m for m in server.mods if m.get("enabled", True)]
        if ((mode == MODE_ALERT and not manual) or not enabled or not server.steamcmd_dir
                or server.id in self._recovery_workers):
            return False
        if getattr(getattr(self, "mods_page", None), "_active_bisect_dialog", None) is not None:
            return False  # someone is running the search by hand right now
        from auto_bisect_runner import AutoBisectWorker
        worker = AutoBisectWorker(
            server, [dict(m) for m in server.mods], was_running=False, find_all=True,
            is_online=lambda sid=server.id: bool(self._online_by_server.get(sid)),
        )
        self._recovery_workers[server.id] = worker
        self._automation_locked.add(server.id)
        if manual:
            self._recovery_manual[server.id] = bool(server.install_dir and process_manager.is_running(server.install_dir))
            server.update_hold = ("ConanOps is testing the mods to find a broken one (your world is protected "
                                  "while it tests).")
            notice = (f"Testing {server.name}'s mods to find a broken one -- this can take a while and restarts "
                      f"the server many times. Your world is backed up for the test and put back exactly as it was.")
        else:
            server.update_hold = (f"It {what} repeatedly. ConanOps is finding out which mod is responsible "
                                  f"(your world is protected while it tests).")
            notice = (f"{server.name} {what} repeatedly. ConanOps is testing its mods to find the broken "
                      f"one -- this can take a while. Your world is backed up for the test and put back "
                      f"exactly as it was.")
        self.config.save()
        self._notify(server, notice, title="Finding the Broken Mod")
        worker.finished_bisect.connect(lambda outcome, srv=server, w=what: self._on_mod_recovery_finished(srv, outcome, w))
        self._retire_worker(worker)
        self._start_or_run(worker)
        return True

    # ----------------------------------------------------------- finish --
    def _on_mod_recovery_finished(self, server, outcome, what: str) -> None:
        self._recovery_workers.pop(server.id, None)
        self._automation_locked.discard(server.id)
        manual = server.id in self._recovery_manual
        was_running = self._recovery_manual.pop(server.id, False)
        if server not in self.config.servers:
            return  # removed while testing
        names = {m["id"]: (m.get("name") or m["id"]) for m in server.mods}
        # Restore the person's own mod list; what to load is decided below.
        self._handle_mods_changed(server)
        if manual:
            self._finish_manual_mod_check(server, outcome, names, was_running)
            return

        if outcome.error or outcome.cancelled:
            server.update_hold = f"It {what} repeatedly, and the automatic mod check couldn't finish."
            self.config.save()
            self._notify(server, f"The automatic mod check couldn't finish: {outcome.error or 'it was stopped'}. "
                                 f"The server was left stopped -- try Mods → Find All Bad Mods.",
                         title="Mod Check Failed")
            return
        if outcome.could_not_reproduce:
            server.update_hold = ""
            server.mod_recovery = {}
            self.config.save()
            self._notify(server, "The server started fine during the mod check, so it's back up.",
                         title="Server Back Up")
            self._launch_recovered(server)
            return
        if outcome.not_mod_related:
            server.update_hold = (f"It {what} repeatedly even with every mod off, so it isn't a mod -- check "
                                  f"Diagnostics and the Console.")
            self.config.save()
            self._notify(server, f"{server.name} doesn't start even with every mod switched off, so the problem "
                                 f"isn't a mod. It was left stopped -- check Diagnostics and the Console.",
                         title="Not a Mod Problem")
            return
        culprits = list(outcome.found_culprits) or list(outcome.unresolved_suspects)
        if not culprits:
            server.update_hold = f"It {what} repeatedly; the mod check couldn't pin it on one mod."
            self.config.save()
            self._notify(server, "The mod check couldn't pin it on a specific mod. The server was left stopped.",
                         title="Mod Check Inconclusive")
            return
        culprit_names = ", ".join(names.get(c, c) for c in culprits)
        mode = getattr(self.config, "mod_recovery_mode", MODE_WAIT)

        if mode == MODE_START_WITHOUT:
            backup_note = ""
            if server.backup_destination:
                try:
                    entry = backup_manager.create_backup(server.install_dir, server.backup_destination,
                                                         backup_manager.TRIGGER_MANUAL, label="before-removing-mods")
                    if entry:
                        backup_note = " A backup from just before was saved, so restoring it brings their items back."
                except Exception as e:  # noqa: BLE001
                    _log.error(f"Backup before turning mods off failed: {e}")
                    server.update_hold = f"Broken mod(s): {culprit_names}. Not started: the backup first failed."
                    self.config.save()
                    self._notify(server, f"Broken mod(s): {culprit_names}. ConanOps didn't start the server without "
                                         f"them because the safety backup failed ({e}).", title="Broken Mod Found")
                    return
            bad = set(culprits)
            server.mods = [dict(m, enabled=(m.get("enabled", True) and m["id"] not in bad)) for m in server.mods]
            server.update_hold = ""
            server.mod_recovery = {}
            self._handle_mods_changed(server)
            if hasattr(self, "mods_changed_elsewhere"):
                self.mods_changed_elsewhere(server)
            self._notify(server, f"Broken mod(s): {culprit_names}. They were switched off and the server was started "
                                 f"without them -- anything they added to the world is gone.{backup_note} Turn them "
                                 f"back on in Mods once their authors update them.", title="Started Without Broken Mod")
            self._launch_recovered(server)
            return

        # MODE_WAIT
        server.mod_recovery = {"culprits": culprits, "since": datetime.now().isoformat(timespec="seconds"),
                               "updated": {}}
        server.update_hold = (f"Waiting for an update to: {culprit_names}. Starting without it would delete what it "
                              f"added to your world. ConanOps starts the server by itself once it's fixed.")
        self.config.save()
        self._notify(server, f"Found it: {culprit_names} broke the server (most likely it isn't updated for the new "
                             f"game version yet). The server is stopped so your world keeps that mod's buildings and "
                             f"items. ConanOps checks for a fix every 30 minutes and starts the server by itself as "
                             f"soon as there is one. To start now without it, turn it off in Mods and click Start -- "
                             f"its items will be removed.", title="Waiting for a Mod Fix")
        self._remember_culprit_versions(server)

    def _finish_manual_mod_check(self, server, outcome, names: dict, was_running: bool) -> None:
        """A check someone asked for: report what it found. Nothing is
        switched off by itself -- the person decides (Mods page)."""
        culprits = list(outcome.found_culprits) or list(outcome.unresolved_suspects)
        culprit_names = ", ".join(names.get(c, c) for c in culprits)
        server.update_hold = ""
        if outcome.error or outcome.cancelled:
            text, title = f"The mod check couldn't finish: {outcome.error or 'it was stopped'}.", "Mod Check Failed"
        elif outcome.could_not_reproduce:
            text, title = "No broken mod: the server starts fine with all of its mods.", "Mods Are Fine"
        elif outcome.not_mod_related:
            text, title = ("The server doesn't start even with every mod off, so the problem isn't a mod -- check "
                           "Diagnostics and the Console."), "Not a Mod Problem"
        elif culprits:
            server.update_hold = (f"Broken mod(s): {culprit_names}. Turn them off in Mods and start the server "
                                  f"(their items are removed from the world), or wait for their authors to "
                                  f"update them.")
            text, title = (f"Found it: {culprit_names} stops the server from starting. It was left stopped so "
                           f"your world keeps that mod's buildings and items -- turn it off in Mods and click "
                           f"Start to run without it."), "Broken Mod Found"
        else:
            text, title = "The mod check couldn't pin the problem on a specific mod.", "Mod Check Inconclusive"
        if outcome.skipped_not_downloaded:
            text += (" Not tested (not downloaded): "
                     + ", ".join(names.get(c, c) for c in outcome.skipped_not_downloaded) + ".")
        self.config.save()
        self._notify(server, text, title=title)
        if was_running and not culprits and not outcome.not_mod_related and not outcome.error:
            self._launch_recovered(server)
        if hasattr(self, "mods_changed_elsewhere"):
            self.mods_changed_elsewhere(server)

    def _launch_recovered(self, server) -> None:
        try:
            process_manager.launch(server)
            self._known_running[server.id] = True
            self._begin_post_update_watch(server)
        except (FileNotFoundError, OSError) as e:
            self._notify(server, f"Couldn't start the server: {e}", title="Start Failed")

    # ---------------------------------------------------- wait for a fix --
    def _remember_culprit_versions(self, server) -> None:
        self._query_culprits(server, baseline=True)

    def _check_for_mod_fixes(self) -> None:
        for server in self.config.servers:
            rec = server.mod_recovery or {}
            if rec.get("culprits") and server.update_hold and server.id not in self._fix_check_workers:
                self._query_culprits(server, baseline=False)

    def _query_culprits(self, server, baseline: bool) -> None:
        rec = server.mod_recovery or {}
        worker = _ModUpdateCheckWorker(rec.get("culprits", []), getattr(self.config, "steam_api_key", ""))
        self._fix_check_workers[server.id] = worker
        worker.finished_check.connect(lambda times, srv=server, b=baseline: self._on_culprit_times(srv, times, b))
        self._retire_worker(worker)
        self._start_or_run(worker)

    def _on_culprit_times(self, server, times: Optional[dict], baseline: bool) -> None:
        self._fix_check_workers.pop(server.id, None)
        rec = server.mod_recovery or {}
        if not times or not rec.get("culprits") or server not in self.config.servers:
            return
        known = rec.setdefault("updated", {})
        if baseline or not known:
            rec["updated"] = {k: int(v) for k, v in times.items()}
            self.config.save()
            return
        fixed = [mid for mid in rec["culprits"] if int(times.get(mid, 0)) > int(known.get(mid, 0))]
        if not fixed:
            return
        names = {m["id"]: (m.get("name") or m["id"]) for m in server.mods}
        _log.info(f"Broken mod(s) updated for {server.name}: {fixed}")
        rec["updated"] = {k: int(v) for k, v in times.items()}
        self.config.save()
        self._download_fix_then_start(server, [names.get(f, f) for f in fixed])

    def _download_fix_then_start(self, server, fixed_names) -> None:
        from update_runner import ModDownloadWorker
        if server.id in self._mod_refresh_workers:
            return  # a download is already running; the next check (30 min) picks this up
        worker = ModDownloadWorker(server.steamcmd_dir, [m["id"] for m in server.mods])

        def done(result, srv=server):
            self._mod_refresh_workers.pop(srv.id, None)
            self._handle_mods_changed(srv)
            if not result.success:
                self._notify(srv, f"An update for {', '.join(fixed_names)} is out, but downloading it failed "
                                  f"({result.output}). Will try again in 30 minutes.", title="Mod Fix Download Failed")
                (srv.mod_recovery or {}).get("updated", {}).clear()
                self.config.save()
                return
            srv.update_hold = ""
            srv.mod_recovery = {}
            self.config.save()
            self._notify(srv, f"{', '.join(fixed_names)} got an update -- starting the server again. If it still "
                              f"fails, ConanOps checks the mods again.", title="Mod Fixed")
            if srv.desired_running:
                self._launch_recovered(srv)

        self._mod_refresh_workers[server.id] = worker
        worker.finished_download.connect(done)
        self._retire_worker(worker)
        self._start_or_run(worker)
