"""
The web version's JSON API: one small function per thing you can do in
the app. Anything that touches ConanOps' state runs on the GUI thread
through the bridge and goes through the same MainWindow handlers the
app's buttons use; slow network/disk work (RCON, diagnostics, process
stats, Workshop lookups) runs on the request's own thread.

Deliberately not available from the web: deleting ConanOps or servers,
adding a server (the setup wizard needs the PC), changing the web
password, and turning the remote link on or off.
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import applog
import version
from webui.bridge import GuiBridge, WebActionError

_log = applog.get_logger(__name__)

LOG_LINES_KEPT = 400


@dataclass
class Request:
    method: str
    path: str
    params: Dict[str, str]
    query: Dict[str, str]
    body: Dict[str, Any]
    ip: str


Route = Tuple[str, "re.Pattern[str]", Callable[[Request], dict]]


class _StatsCache:
    """CPU/memory/uptime for running servers, measured on request threads.
    psutil's CPU percent needs a previous reading, so processes are kept."""

    def __init__(self):
        self._lock = threading.Lock()
        self._procs: Dict[str, Any] = {}

    def get(self, server_id: str, install_dir: str) -> Dict[str, Any]:
        import psutil
        import process_manager
        out = {"cpu": None, "mem_mb": None, "uptime_seconds": None}
        if not install_dir:
            return out
        with self._lock:
            proc = self._procs.get(server_id)
        try:
            if proc is None or not proc.is_running():
                pid = process_manager.find_running_pid(install_dir)
                if pid is None:
                    with self._lock:
                        self._procs.pop(server_id, None)
                    return out
                proc = psutil.Process(pid)
                proc.cpu_percent(None)
                with self._lock:
                    self._procs[server_id] = proc
                cpu = None
            else:
                cpu = proc.cpu_percent(None) / max(1, psutil.cpu_count() or 1)
            out["cpu"] = round(cpu, 1) if cpu is not None else None
            out["mem_mb"] = round(proc.memory_info().rss / 2 ** 20)
            out["uptime_seconds"] = int(time.time() - proc.create_time())
        except (psutil.Error, OSError):
            with self._lock:
                self._procs.pop(server_id, None)
        return out


class WebApi:
    def __init__(self, window, bridge: GuiBridge):
        self.win = window
        self.bridge = bridge
        self.stats = _StatsCache()
        self.routes: List[Route] = []
        self._register()

    # ------------------------------------------------------------ helpers --
    def gui(self, fn: Callable[[], Any], timeout: float = 60.0) -> Tuple[Any, List[str]]:
        return self.bridge.call(fn, timeout=timeout)

    def _server(self, sid: str):
        server = next((s for s in self.win.config.servers if s.id == sid), None)
        if server is None:
            raise WebActionError("That server doesn't exist anymore.")
        return server

    def _activate(self, sid: str):
        """Per-server pages in the app show the ACTIVE server, so the web
        switches to the server it's working on first (the app follows)."""
        server = self._server(sid)
        if self.win.config.active_server_id != sid:
            self.win._on_server_selected(sid)
            self.win.sidebar.set_servers(self.win.config.servers, sid)
        return server

    def _result(self, messages: List[str], ok_message: str = "Done.") -> dict:
        """An action's outcome: a dialog the app would have shown becomes
        the message (and counts as a failure if it was a warning)."""
        if messages:
            return {"ok": False, "message": "\n".join(messages)}
        return {"ok": True, "message": ok_message}

    def add(self, method: str, pattern: str, fn: Callable[[Request], dict]) -> None:
        regex = re.compile("^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern) + "$")
        self.routes.append((method, regex, fn))

    def match(self, method: str, path: str):
        for m, regex, fn in self.routes:
            if m != method:
                continue
            hit = regex.match(path)
            if hit:
                return fn, hit.groupdict()
        return None, {}

    def _register(self) -> None:
        a = self.add
        a("GET", "/api/overview", self.overview)
        a("POST", "/api/servers/{sid}/select", self.select)
        a("GET", "/api/servers/{sid}", self.dashboard)
        a("POST", "/api/servers/{sid}/start", self.start)
        a("POST", "/api/servers/{sid}/stop", self.stop)
        a("POST", "/api/servers/{sid}/restart", self.restart)
        a("POST", "/api/servers/{sid}/enable-rcon", self.enable_rcon)
        a("GET", "/api/servers/{sid}/players", self.players)
        a("POST", "/api/servers/{sid}/players/kick", self.kick)
        a("POST", "/api/servers/{sid}/players/ban", self.ban_player)
        a("GET", "/api/servers/{sid}/access", self.access)
        a("POST", "/api/servers/{sid}/access/whitelist-mode", self.whitelist_mode)
        a("POST", "/api/servers/{sid}/access/whitelist", self.whitelist_edit)
        a("POST", "/api/servers/{sid}/access/ban", self.ban_id)
        a("POST", "/api/servers/{sid}/access/unban", self.unban_id)
        a("POST", "/api/servers/{sid}/console", self.console)
        a("POST", "/api/servers/{sid}/broadcast", self.broadcast)
        a("GET", "/api/servers/{sid}/backups", self.backups)
        a("POST", "/api/servers/{sid}/backups/create", self.backup_now)
        a("POST", "/api/servers/{sid}/backups/restore", self.restore)
        a("GET", "/api/servers/{sid}/updates", self.updates)
        a("POST", "/api/servers/{sid}/updates/check", self.update_check)
        a("POST", "/api/servers/{sid}/updates/install", self.update_install)
        a("POST", "/api/servers/{sid}/updates/settings", self.update_settings)
        a("GET", "/api/servers/{sid}/mods", self.mods)
        a("GET", "/api/servers/{sid}/mods/search", self.mod_search)
        a("POST", "/api/servers/{sid}/mods/add", self.mod_add)
        a("POST", "/api/servers/{sid}/mods/remove", self.mod_remove)
        a("POST", "/api/servers/{sid}/mods/toggle", self.mod_toggle)
        a("POST", "/api/servers/{sid}/mods/move", self.mod_move)
        a("POST", "/api/servers/{sid}/mods/download", self.mod_download)
        a("POST", "/api/servers/{sid}/mods/find-broken", self.mod_find_broken)
        a("GET", "/api/servers/{sid}/settings", self.settings)
        a("POST", "/api/servers/{sid}/settings/{page}", self.settings_save)
        a("POST", "/api/servers/{sid}/diagnostics", self.diagnostics)
        a("POST", "/api/servers/{sid}/repair-network", self.repair_network)
        a("GET", "/api/app", self.app_options)
        a("POST", "/api/app/{option}", self.app_set)

    # ----------------------------------------------------------- overview --
    def _summary(self, s) -> dict:
        w = self.win
        online = sorted(w._online_by_server.get(s.id, set()) or ())
        rec = getattr(s, "mod_recovery", {}) or {}
        return {
            "id": s.id, "name": s.name, "installed": bool(s.install_dir),
            "running": bool(w._known_running.get(s.id)),
            "players": online, "player_count": len(online), "max_players": getattr(s, "max_players", None),
            "address": f"{s.bind_ip or '(no ip)'}:{s.game_port}",
            "hold": s.update_hold or "",
            "busy": self._busy(s),
            "waiting_for_mod_fix": bool(rec.get("culprits")),
        }

    def _busy(self, s) -> str:
        w = self.win
        if s.id in getattr(w, "_recovery_workers", {}):
            return "Finding a broken mod"
        if s.id in w._update_apply_workers or (getattr(w, "_is_update_busy", None) and w._is_update_busy(s)):
            return "Updating"
        if s.id in w._restore_workers:
            return "Restoring a backup"
        if s.id in w._mod_refresh_workers:
            return "Downloading mods"
        if s.id in w._expected_stop:
            return "Stopping"
        return ""

    def overview(self, req: Request) -> dict:
        def read():
            cfg = self.win.config
            from theme_config import load_theme
            theme = load_theme()
            return {
                "version": version.VERSION,
                "active_id": cfg.active_server_id,
                "servers": [self._summary(s) for s in cfg.servers],
                "theme": {k: getattr(theme, k) for k in ("bg", "panel", "panel_alt", "border", "ivory", "muted",
                                                         "dim", "accent", "green", "red", "yellow")},
            }
        return self.gui(read)[0]

    def select(self, req: Request) -> dict:
        self.gui(lambda: self._activate(req.params["sid"]))
        return {"ok": True}

    # ---------------------------------------------------------- dashboard --
    def dashboard(self, req: Request) -> dict:
        sid = req.params["sid"]

        def read():
            w = self.win
            s = self._server(sid)
            states = w._automation_states(s)
            from ui.dashboard_page import AUTOMATION_TILES
            tiles = [{"key": key, "name": name, "on": bool(states.get(key, (False, ""))[0]),
                      "detail": states.get(key, (False, ""))[1]} for key, name, _mark, _tint in AUTOMATION_TILES]
            log = list(getattr(w, "_web_logs", {}).get(sid, ()))[-200:]
            status = dict(getattr(w, "_web_status", {}).get(sid, {}) or {})
            rec = getattr(s, "mod_recovery", {}) or {}
            names = {m["id"]: (m.get("name") or m["id"]) for m in s.mods}
            return {
                "server": self._summary(s), "automation": tiles, "log": log,
                "fps": status.get("fps"), "rcon_enabled": bool(s.rcon_enabled),
                "waiting_for": [names.get(c, c) for c in rec.get("culprits", [])],
                "install_dir": s.install_dir,
            }, s.install_dir
        data, install_dir = self.gui(read)[0]
        data["stats"] = self.stats.get(sid, install_dir) if data["server"]["running"] else {}
        return data

    def _power(self, req: Request, handler_name: str, ok_message: str) -> dict:
        sid = req.params["sid"]

        def act():
            s = self._server(sid)
            if not s.install_dir:
                raise WebActionError("This server isn't set up yet -- finish setup in the app.")
            getattr(self.win, handler_name)(s)
            self.win._refresh_chrome()
        _r, msgs = self.gui(act, timeout=120)
        return self._result(msgs, ok_message)

    def start(self, req):
        return self._power(req, "_handle_start", "Starting…")

    def stop(self, req):
        return self._power(req, "_handle_stop", "Saving the world and stopping…")

    def restart(self, req):
        return self._power(req, "_handle_restart", "Restarting…")

    def enable_rcon(self, req: Request) -> dict:
        def act():
            self._activate(req.params["sid"])
            self.win._enable_rcon_for_active()
        return self._result(self.gui(act)[1], "RCON is on.")

    # ------------------------------------------------------------ players --
    def players(self, req: Request) -> dict:
        sid = req.params["sid"]

        def read():
            w = self.win
            s = self._server(sid)
            tracker = w._tracker_for(s)
            online = w._online_by_server.get(sid, set()) or set()
            last_seen = {}
            for sess in tracker.sessions:
                end = sess.end or datetime.now().isoformat()
                if sess.name not in last_seen or end > last_seen[sess.name]:
                    last_seen[sess.name] = end
            people = [{"name": n, "online": n in online, "sessions": tracker.session_count(n),
                       "playtime_seconds": int(tracker.total_playtime_seconds(n)), "last_seen": last_seen.get(n, "")}
                      for n in tracker.all_player_names()]
            for n in online:
                if not any(p["name"] == n for p in people):
                    people.append({"name": n, "online": True, "sessions": 1, "playtime_seconds": 0, "last_seen": ""})
            people.sort(key=lambda p: (not p["online"], -p["playtime_seconds"]))
            return {"players": people, "activity": tracker.hourly_activity_histogram(),
                    "rcon_enabled": bool(s.rcon_enabled)}
        return self.gui(read)[0]

    def kick(self, req: Request) -> dict:
        name = str(req.body.get("name", "")).strip()
        if not name:
            raise WebActionError("Which player?")

        def act():
            self.win._handle_kick_player(self._server(req.params["sid"]), name)
        return self._result(self.gui(act)[1], f"Kicked {name}.")

    def ban_player(self, req: Request) -> dict:
        name = str(req.body.get("name", "")).strip()
        steam_id = str(req.body.get("steam_id", "")).strip()
        if not steam_id.isdigit():
            raise WebActionError("Enter the player's Steam ID (a long number).")

        def act():
            self.win._handle_ban_player(self._server(req.params["sid"]), name or steam_id, steam_id)
        return self._result(self.gui(act)[1], f"Banned {name or steam_id}.")

    # ------------------------------------------------------------- access --
    def access(self, req: Request) -> dict:
        def read():
            s = self._server(req.params["sid"])
            return {"whitelist_enabled": bool(s.whitelist_enabled), "whitelist": list(s.whitelist_ids),
                    "banned": list(s.banned_ids), "rcon_enabled": bool(s.rcon_enabled)}
        return self.gui(read)[0]

    def _after_access_change(self, s) -> None:
        self.win._handle_config_changed(s)
        if self.win.config.active_server_id == s.id:
            self.win.access_page.set_server(s)

    def whitelist_mode(self, req: Request) -> dict:
        on = bool(req.body.get("on"))

        def act():
            import banlist_manager
            s = self._server(req.params["sid"])
            s.whitelist_enabled = on
            banlist_manager.set_whitelist_enabled(s)
            self._after_access_change(s)
        self.gui(act)
        return {"ok": True, "message": "Whitelist-only is on." if on else "Anyone can join (except banned)."}

    def whitelist_edit(self, req: Request) -> dict:
        steam_id = str(req.body.get("steam_id", "")).strip()
        remove = bool(req.body.get("remove"))
        if not steam_id.isdigit():
            raise WebActionError("Enter a Steam ID (a long number).")

        def act():
            import banlist_manager
            s = self._server(req.params["sid"])
            (banlist_manager.remove_whitelist if remove else banlist_manager.add_whitelist)(s, steam_id)
            self._after_access_change(s)
        self.gui(act)
        return {"ok": True, "message": "Removed from the whitelist." if remove else "Added to the whitelist."}

    def _ban_action(self, req: Request, unban: bool) -> dict:
        import banlist_manager
        steam_id = str(req.body.get("steam_id", "")).strip()
        if not steam_id.isdigit():
            raise WebActionError("Enter a Steam ID (a long number).")
        server = self.gui(lambda: self._server(req.params["sid"]))[0]
        fn = banlist_manager.unban_player if unban else banlist_manager.ban_player
        status = fn(server, steam_id)  # RCON round trip: on this request's thread
        self.gui(lambda: self._after_access_change(server))
        return {"ok": True, "message": status}

    def ban_id(self, req):
        return self._ban_action(req, unban=False)

    def unban_id(self, req):
        return self._ban_action(req, unban=True)

    # ------------------------------------------------------------ console --
    def console(self, req: Request) -> dict:
        import rcon
        cmd = str(req.body.get("command", "")).strip()
        if not cmd:
            raise WebActionError("Type a command.")
        server = self.gui(lambda: self._server(req.params["sid"]))[0]
        if not server.rcon_enabled:
            raise WebActionError("RCON is off for this server -- turn it on from the dashboard.")
        try:
            out = rcon.send_command("127.0.0.1", server.rcon_port, server.rcon_password, cmd)
            return {"ok": True, "output": out or "(no output)"}
        except rcon.RconError as e:
            return {"ok": False, "output": str(e)}

    def broadcast(self, req: Request) -> dict:
        import banlist_manager
        msg = str(req.body.get("message", "")).strip()
        if not msg:
            raise WebActionError("Type a message.")
        server = self.gui(lambda: self._server(req.params["sid"]))[0]
        ok = banlist_manager.broadcast_message(server, msg)
        return {"ok": bool(ok), "message": "Sent to everyone online." if ok else "Couldn't send (is RCON on?)."}

    # ------------------------------------------------------------ backups --
    def backups(self, req: Request) -> dict:
        import backup_manager
        server = self.gui(lambda: self._server(req.params["sid"]))[0]
        entries = backup_manager.list_backups(server.backup_destination) if server.backup_destination else []
        entries.sort(key=lambda e: e.when, reverse=True)
        busy = self.gui(lambda: self._busy(server))[0]
        return {"destination": server.backup_destination, "busy": busy,
                "backups": [{"name": os.path.basename(e.path), "when": e.when.isoformat(timespec="minutes"),
                             "trigger": e.trigger, "size_mb": round(e.size_bytes / 2 ** 20, 1)} for e in entries]}

    def backup_now(self, req: Request) -> dict:
        import backup_manager
        server = self.gui(lambda: self._server(req.params["sid"]))[0]
        if not server.backup_destination or not server.install_dir:
            raise WebActionError("Set a backup folder for this server first (Settings → Backups).")
        try:
            entry = backup_manager.create_backup_for_server(server, server.backup_destination,
                                                            backup_manager.TRIGGER_MANUAL)
        except backup_manager.BackupSpaceError as e:
            raise WebActionError(str(e)) from e
        if entry is None:
            raise WebActionError("There's no world save to back up yet -- start the server once first.")
        return {"ok": True, "message": f"Backed up ({round(entry.size_bytes / 2 ** 20, 1)} MB)."}

    def restore(self, req: Request) -> dict:
        import backup_manager
        name = os.path.basename(str(req.body.get("name", "")))

        def act():
            s = self._server(req.params["sid"])
            entry = next((e for e in backup_manager.list_backups(s.backup_destination)
                          if os.path.basename(e.path) == name), None)
            if entry is None:
                raise WebActionError("That backup isn't there anymore.")
            if self._busy(s):
                raise WebActionError(f"Wait until this finishes: {self._busy(s)}.")
            self.win._handle_restore(s, entry)
        return self._result(self.gui(act)[1], "Restoring -- the server stops, the backup goes back in, and it "
                                              "starts again if it was running.")

    # ------------------------------------------------------------ updates --
    def updates(self, req: Request) -> dict:
        def read():
            w = self.win
            s = self._server(req.params["sid"])
            page = w.updates_page
            shown = page.server is s
            pending = shown and page.pending_frame.isVisibleTo(page)
            return {
                "installed": s.installed_buildid or "Unknown",
                "auto_update": bool(s.auto_update), "interval_hours": s.auto_update_check_interval_hours,
                "state": page.build_state_label.text().lstrip("● ").strip() if shown else "",
                "checking": page._check_worker is not None,
                "pending": page.pending_label.text() if pending else "",
                "changelog": page.changelog_view.toPlainText() if pending else "",
                "busy": self._busy(s),
                "last_check": getattr(s, "last_update_check_at", "") or "",
            }
        return self.gui(read)[0]

    def update_check(self, req: Request) -> dict:
        def act():
            self._activate(req.params["sid"])
            self.win.updates_page.check_now()
        self.gui(act)
        return {"ok": True, "message": "Checking Steam for a newer server build…"}

    def update_install(self, req: Request) -> dict:
        def act():
            s = self._activate(req.params["sid"])
            if self._busy(s):
                raise WebActionError(f"Wait until this finishes: {self._busy(s)}.")
            self.win.updates_page._update_now()
        return self._result(self.gui(act)[1], "Updating -- the server is backed up and stopped first, and "
                                              "started again afterwards if it was running.")

    def update_settings(self, req: Request) -> dict:
        def act():
            s = self._activate(req.params["sid"])
            if "auto_update" in req.body:
                s.auto_update = bool(req.body["auto_update"])
            if "interval_hours" in req.body:
                s.auto_update_check_interval_hours = max(1, min(168, int(req.body["interval_hours"])))
            self.win._handle_config_changed(s)
            self.win.updates_page.set_server(s)
        self.gui(act)
        return {"ok": True, "message": "Saved."}

    # --------------------------------------------------------------- mods --
    def mods(self, req: Request) -> dict:
        import mod_manager

        def read():
            s = self._server(req.params["sid"])
            rec = getattr(s, "mod_recovery", {}) or {}
            return ([dict(m) for m in s.mods], s.steamcmd_dir, self.win.config.steam_api_key,
                    self.win.config.workshop_update_cutoff, set(rec.get("culprits", [])), self._busy(s))
        mods, steamcmd_dir, api_key, cutoff, culprits, busy = self.gui(read)[0]
        info = {}
        if mods:
            import steam_workshop_api as swa
            result = swa.get_details([m["id"] for m in mods], api_key=api_key,
                                     cutoff_ts=swa.cutoff_timestamp(cutoff), use_cache=True)
            if result.ok:
                info = result.items
        out = []
        for m in mods:
            it = info.get(m["id"])
            out.append({
                "id": m["id"], "name": m.get("name") or (it.title if it else m["id"]),
                "enabled": m.get("enabled", True),
                "downloaded": bool(steamcmd_dir and mod_manager.find_workshop_pak(steamcmd_dir, m["id"])),
                "status": it.status if it else "unknown",
                "updated": datetime.fromtimestamp(it.time_updated).date().isoformat() if it and it.time_updated else "",
                "broken": m["id"] in culprits,
            })
        return {"mods": out, "busy": busy, "search_available": bool(api_key)}

    def mod_search(self, req: Request) -> dict:
        import steam_workshop_api as swa
        q = req.query.get("q", "").strip()
        api_key, cutoff = self.gui(lambda: (self.win.config.steam_api_key, self.win.config.workshop_update_cutoff))[0]
        if not api_key:
            raise WebActionError("Searching the Workshop needs a Steam API key -- add one in the app's Mods tab.")
        result = swa.search(api_key, q, cutoff_ts=swa.cutoff_timestamp(cutoff), use_cache=True)
        if not result.ok:
            raise WebActionError(result.error or "Workshop search failed.")
        return {"results": [{"id": i.id, "title": i.title, "subscriptions": i.subscriptions, "status": i.status,
                             "preview": i.preview_url} for i in result.items[:30]]}

    def _change_mods(self, sid: str, change) -> None:
        def act():
            s = self._server(sid)
            if self._busy(s) in ("Finding a broken mod", "Downloading mods"):
                raise WebActionError(f"Wait until this finishes: {self._busy(s)}.")
            s.mods = change(s.mods)
            self.win._handle_mods_changed(s)
            if self.win.config.active_server_id == sid:
                self.win.mods_page.set_server(s)
        self.gui(act)

    def mod_add(self, req: Request) -> dict:
        import mod_manager
        from ui.mods_page import ModsPage
        raw = str(req.body.get("id", "")).strip()
        wid = ModsPage._extract_workshop_id(raw)
        if not wid:
            raise WebActionError("Paste the mod's Workshop ID or its Workshop link.")
        self._change_mods(req.params["sid"], lambda mods: mod_manager.add_mod(mods, wid, str(req.body.get("name", "")).strip()))
        return {"ok": True, "message": "Added. Download mods to fetch it; it loads the next time the server starts."}

    def mod_remove(self, req: Request) -> dict:
        import mod_manager
        wid = str(req.body.get("id", ""))
        self._change_mods(req.params["sid"], lambda mods: mod_manager.remove_mod(mods, wid))
        return {"ok": True, "message": "Removed."}

    def mod_toggle(self, req: Request) -> dict:
        import mod_manager
        wid = str(req.body.get("id", ""))
        on = bool(req.body.get("enabled"))
        self._change_mods(req.params["sid"], lambda mods: mod_manager.set_enabled(mods, wid, on))
        return {"ok": True, "message": "Turned on." if on else "Turned off."}

    def mod_move(self, req: Request) -> dict:
        import mod_manager
        wid = str(req.body.get("id", ""))
        direction = -1 if int(req.body.get("direction", 1)) < 0 else 1
        self._change_mods(req.params["sid"], lambda mods: mod_manager.move_mod(mods, wid, direction))
        return {"ok": True}

    def mod_download(self, req: Request) -> dict:
        def act():
            s = self._server(req.params["sid"])
            if not s.mods:
                raise WebActionError("There are no mods to download.")
            if not s.steamcmd_dir:
                raise WebActionError("This server has no SteamCMD folder yet.")
            if s.id in self.win._mod_refresh_workers:
                raise WebActionError("Mods are already downloading.")
            self.win._handle_mod_refresh_due(s)
        self.gui(act)
        return {"ok": True, "message": "Downloading mods… they load the next time the server starts."}

    def mod_find_broken(self, req: Request) -> dict:
        def act():
            s = self._server(req.params["sid"])
            if self.win._online_by_server.get(s.id):
                raise WebActionError("Players are online -- the check restarts the server many times.")
            if not self.win._start_mod_recovery(s, "had a problem"):
                raise WebActionError("The automatic mod check can't run right now (no mods on, no SteamCMD "
                                     "folder, or it's already running).")
        self.gui(act)
        return {"ok": True, "message": "Testing the mods with your world protected. This can take a while; "
                                       "you'll get an alert with the result."}

    # ----------------------------------------------------------- settings --
    def _settings_pages(self):
        w = self.win
        return [(key, label, page) for key, label, page in w.settings_container_entries
                if key != "diagnostics" and hasattr(page, "_fields")]

    def settings(self, req: Request) -> dict:
        from webui import fields

        def read():
            self._activate(req.params["sid"])
            return {"pages": [fields.describe_page(k, t, p) for k, t, p in self._settings_pages()]}
        return self.gui(read)[0]

    def settings_save(self, req: Request) -> dict:
        from webui import fields
        values = req.body.get("values") or {}
        if not isinstance(values, dict):
            raise WebActionError("Nothing to save.")

        def act():
            self._activate(req.params["sid"])
            page = next((p for k, _t, p in self._settings_pages() if k == req.params["page"]), None)
            if page is None:
                raise WebActionError("Unknown settings page.")
            return fields.apply_page(page, values)
        result, msgs = self.gui(act)
        if msgs:
            return {"ok": False, "message": "\n".join(msgs)}
        return {"ok": True, "message": "Saved." if result.get("changed") else "Nothing changed."}

    # -------------------------------------------------------- diagnostics --
    def diagnostics(self, req: Request) -> dict:
        import diagnostics as diag
        server = self.gui(lambda: self._server(req.params["sid"]))[0]
        results = diag.run_diagnostics(server)
        return {"results": [{"title": r.title, "status": r.status, "message": r.message,
                             "detail": getattr(r, "detail", "")} for r in results]}

    def repair_network(self, req: Request) -> dict:
        done = threading.Event()
        holder: dict = {}

        def act():
            s = self._activate(req.params["sid"])

            def finished(result):
                holder.update(result or {})
                done.set()
            self.win._start_network_reconcile(s, legacy_names=[s.name], on_done=finished)
        self.gui(act)
        if not done.wait(180):
            return {"ok": True, "message": "Still working -- Windows may be asking for permission on the PC."}
        lines = [("✓ " if r.success else "✗ ") + r.message for r in holder.get("fw_results") or []]
        upnp = holder.get("upnp")
        if upnp is not None:
            lines.append("Router forwarding: " + ("done." if upnp.get("upnp_available") else
                                                   "automatic forwarding (UPnP) isn't available."))
        return {"ok": not holder.get("error"), "message": "\n".join(lines) or "Done."}

    # ------------------------------------------------------- app options --
    def app_options(self, req: Request) -> dict:
        def read():
            c = self.win.config
            page = self.win.app_settings_page
            return {
                "version": version.VERSION,
                "start_with_windows": c.start_with_windows, "keep_alive": c.keep_alive_enabled,
                "keep_awake": c.keep_pc_awake, "update_restarts": c.handle_update_restarts,
                "update_window": [c.update_restart_start, c.update_restart_end],
                "mod_recovery_mode": c.mod_recovery_mode,
                "auto_check_app_updates": c.auto_check_app_updates,
                "auto_install_app_updates": c.auto_install_app_updates,
                "app_update_status": page.app_update_status.text() if hasattr(page, "app_update_status") else "",
                "app_update_available": bool(getattr(page, "_available_release", None)),
                "sign_in_note": page.sign_in_status_label.text() if page.sign_in_status_label.isVisibleTo(page) else "",
            }
        return self.gui(read)[0]

    _APP_TOGGLES = {
        "start_with_windows": "start_with_windows_checkbox",
        "keep_alive": "keep_alive_checkbox",
        "keep_awake": "keep_awake_checkbox",
        "update_restarts": "update_restart_checkbox",
        "auto_check_app_updates": "auto_check_updates_checkbox",
        "auto_install_app_updates": "auto_install_updates_checkbox",
    }

    def app_set(self, req: Request) -> dict:
        option = req.params["option"]
        value = req.body.get("value")

        def act():
            page = self.win.app_settings_page
            if option in self._APP_TOGGLES:
                getattr(page, self._APP_TOGGLES[option]).setChecked(bool(value))
            elif option == "mod_recovery_mode":
                idx = page.mod_recovery_combo.findData(str(value))
                if idx < 0:
                    raise WebActionError("Unknown choice.")
                page.mod_recovery_combo.setCurrentIndex(idx)
            elif option == "check_app_update":
                page.check_for_updates(automatic=False)
            elif option == "install_app_update":
                if not getattr(page, "_available_release", None):
                    raise WebActionError("No update is waiting.")
                page._install_online_update(confirm=False)
            else:
                raise WebActionError("Unknown option.")
        msgs = self.gui(act)[1]
        note = ""
        if option in ("keep_alive", "update_restarts", "start_with_windows") and value:
            note = " Windows may ask for permission on the PC."
        return self._result(msgs, "Saved." + note)
