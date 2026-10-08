"""
The web version's JSON API, one method per action.

State changes run on the GUI thread via the bridge (ui/web_actions.py), for
the server the request names; slow network/disk work runs on the request
thread. Deliberately not exposed: deleting ConanOps or servers, adding a
server, the app lock PIN, the web password, the remote link and admin mode.
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
MAX_IMPORT_BYTES = 8 * 1024 ** 3


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
        from webui.settings import WebSettings
        self.web_settings = WebSettings(window)
        self._register()

    # ------------------------------------------------------------ helpers --
    def gui(self, fn: Callable[[], Any], timeout: float = 60.0) -> Tuple[Any, List[str]]:
        """Runs fn on the GUI thread; also returns notes about changes left
        waiting for someone at the PC."""
        from webui.bridge import QUEUED_NOTE
        box: Dict[str, Any] = {}

        def wrapped():
            counter = self.win.needs_pc_counter()
            try:
                return fn()
            finally:
                box["queued"] = self.win.needs_pc_queued_since(counter)
        result, messages = self.bridge.call(wrapped, timeout=timeout)
        return result, messages + [QUEUED_NOTE.format(what=l) for l in box.get("queued", [])]

    def _needs_pc(self) -> List[str]:
        labels = getattr(self.win, "needs_pc_labels", None)
        return labels() if labels else []

    def _server(self, sid: str):
        server = next((s for s in self.win.config.servers if s.id == sid), None)
        if server is None:
            raise WebActionError("That server doesn't exist anymore.")
        return server

    def _result(self, messages: List[str], ok_message: str = "Done.") -> dict:
        """A dialog the app would have shown becomes a failure message;
        changes waiting for the PC are reported as pending."""
        from webui.bridge import QUEUED_NOTE
        waiting = [m for m in messages if m.startswith(QUEUED_NOTE.split("{")[0])]
        problems = [m for m in messages if m not in waiting]
        if problems:
            return {"ok": False, "message": "\n".join(problems + waiting)}
        if waiting:
            return {"ok": True, "pending": True, "message": "\n".join([ok_message] + waiting)}
        return {"ok": True, "message": ok_message}

    def _do(self, req: Request, action: Callable[[Any], str], ok_message: str, timeout: float = 120.0) -> dict:
        """Runs a WebActionsMixin action (returns "" or why not) for the request's server."""
        def act():
            return action(self._server(req.params["sid"]))
        problem, msgs = self.gui(act, timeout=timeout)
        if problem:
            return {"ok": False, "message": "\n".join([problem] + msgs)}
        return self._result(msgs, ok_message)

    @staticmethod
    def _steam_id(req: Request) -> str:
        steam_id = str(req.body.get("steam_id", "")).strip()
        if not re.fullmatch(r"\d{5,20}", steam_id):
            raise WebActionError("Enter a Steam ID (the long number, e.g. 76561198012345678).")
        return steam_id

    @staticmethod
    def _int(req: Request, key: str, default: int) -> int:
        try:
            return int(req.body.get(key, default))
        except (TypeError, ValueError):
            raise WebActionError(f"{key} must be a number.") from None

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
        a("POST", "/api/servers/{sid}/settings/{page}/action", self.settings_action)
        a("POST", "/api/alerts/test", self.alerts_test)
        a("POST", "/api/servers/{sid}/diagnostics", self.diagnostics)
        a("GET", "/api/servers/{sid}/port-guide", self.port_guide)
        a("POST", "/api/servers/{sid}/install-vc-runtime", self.install_vc_runtime)
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
            "busy": w.server_busy(s),
            "waiting_for_mod_fix": bool(rec.get("culprits")),
        }

    def overview(self, req: Request) -> dict:
        def read():
            cfg = self.win.config
            from theme_config import load_theme
            theme = load_theme()
            return {
                "version": version.VERSION,
                "active_id": cfg.active_server_id,
                "servers": [self._summary(s) for s in cfg.servers],
                "needs_pc": self.win.needs_pc_labels(),
                "theme": {k: getattr(theme, k) for k in ("bg", "panel", "panel_alt", "border", "ivory", "muted",
                                                         "dim", "accent", "green", "red", "yellow")},
            }
        return self.gui(read)[0]

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
                "fps": status.get("fps") if w._known_running.get(sid) else None, "rcon_enabled": bool(s.rcon_enabled),
                "waiting_for": [names.get(c, c) for c in rec.get("culprits", [])],
                "needs_pc": w.needs_pc_labels(),
            }, s.install_dir
        data, install_dir = self.gui(read)[0]
        data["stats"] = self.stats.get(sid, install_dir) if data["server"]["running"] else {}
        return data

    def start(self, req):
        return self._do(req, self.win.start_server, "Starting…")

    def stop(self, req):
        return self._do(req, self.win.stop_server, "Saving the world and stopping…")

    def restart(self, req):
        return self._do(req, self.win.restart_server, "Restarting… (anyone online gets a 10-second warning)")

    def enable_rcon(self, req: Request) -> dict:
        return self._do(req, lambda s: self.win.enable_rcon(s) or "",
                        "RCON is on. It works after the server's next restart.")

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
                    "rcon_enabled": bool(s.rcon_enabled), "running": bool(w._known_running.get(sid))}
        return self.gui(read)[0]

    def kick(self, req: Request) -> dict:
        """RCON runs on this thread; the result goes to the server's alerts."""
        import banlist_manager
        name = str(req.body.get("name", "")).strip()
        if not name:
            raise WebActionError("Which player?")
        server = self.gui(lambda: self._server(req.params["sid"]))[0]
        if not self._rcon_target(server.id)[0]:
            return {"ok": False, "message": "Kicking needs RCON, which is off for this server. Turn it on (Dashboard → "
                                            "Turn On RCON); it works after the server's next restart."}
        message = banlist_manager.kick_player(server, name)
        self.gui(lambda: self.win._notify(server, message, title="Player Kicked"))
        return {"ok": message.startswith("Kicked"), "message": message}

    def ban_player(self, req: Request) -> dict:
        steam_id = self._steam_id(req)
        name = str(req.body.get("name", "")).strip()

        def act():
            return self.win.ban(self._server(req.params["sid"]), steam_id, name)
        message, msgs = self.gui(act)
        return {"ok": True, "message": "\n".join([message] + msgs)}

    # ------------------------------------------------------------- access --
    def access(self, req: Request) -> dict:
        def read():
            s = self._server(req.params["sid"])
            return {"whitelist_enabled": bool(s.whitelist_enabled), "whitelist": list(s.whitelist_ids),
                    "banned": list(s.banned_ids), "rcon_enabled": bool(s.rcon_enabled),
                    "installed": bool(s.install_dir)}
        return self.gui(read)[0]

    def whitelist_mode(self, req: Request) -> dict:
        on = bool(req.body.get("on"))

        def act(s):
            import banlist_manager
            if not s.install_dir:
                return "This server isn't set up yet -- finish setup in the app on the PC."
            s.whitelist_enabled = on
            banlist_manager.set_whitelist_enabled(s)
            self.win.access_changed(s)
            return ""
        return self._do(req, act, "Only whitelisted players can join now." if on
                        else "Anyone can join now (except banned players).")

    def whitelist_edit(self, req: Request) -> dict:
        steam_id = self._steam_id(req)
        remove = bool(req.body.get("remove"))

        def act(s):
            import banlist_manager
            if not s.install_dir:
                return "This server isn't set up yet -- finish setup in the app on the PC."
            (banlist_manager.remove_whitelist if remove else banlist_manager.add_whitelist)(s, steam_id)
            self.win.access_changed(s)
            return ""
        return self._do(req, act, "Removed from the whitelist." if remove else "Added to the whitelist.")

    def ban_id(self, req: Request) -> dict:
        steam_id = self._steam_id(req)
        message, msgs = self.gui(lambda: self.win.ban(self._server(req.params["sid"]), steam_id))
        return {"ok": "isn't set up" not in message, "message": "\n".join([message] + msgs)}

    def unban_id(self, req: Request) -> dict:
        steam_id = self._steam_id(req)
        message, msgs = self.gui(lambda: self.win.unban(self._server(req.params["sid"]), steam_id))
        return {"ok": "isn't set up" not in message, "message": "\n".join([message] + msgs)}

    # ------------------------------------------------------------ console --
    def _rcon_target(self, sid: str) -> Tuple[bool, int, str]:
        def read():
            s = self._server(sid)
            return bool(s.rcon_enabled), int(s.rcon_port), str(s.rcon_password)
        return self.gui(read)[0]

    def console(self, req: Request) -> dict:
        import rcon
        cmd = str(req.body.get("command", "")).strip()
        if not cmd:
            raise WebActionError("Type a command.")
        enabled, port, password = self._rcon_target(req.params["sid"])
        if not enabled:
            raise WebActionError("RCON is off for this server -- turn it on from the dashboard.")
        try:
            out = rcon.send_command("127.0.0.1", port, password, cmd)
            return {"ok": True, "output": out or "(no output)"}
        except rcon.RconError as e:
            return {"ok": False, "output": str(e)}

    def broadcast(self, req: Request) -> dict:
        import banlist_manager
        msg = str(req.body.get("message", "")).strip()
        if not msg:
            raise WebActionError("Type a message.")
        server = self.gui(lambda: self._server(req.params["sid"]))[0]
        if not self._rcon_target(server.id)[0]:
            raise WebActionError("RCON is off for this server -- turn it on from the dashboard.")
        ok = banlist_manager.broadcast_message(server, msg)
        return {"ok": bool(ok), "message": "Sent to everyone online." if ok else
                "Couldn't send -- is the server running (with RCON on since its last restart)?"}

    # ------------------------------------------------------------ backups --
    def backups(self, req: Request) -> dict:
        import backup_manager

        def read():
            s = self._server(req.params["sid"])
            return s.backup_destination, self.win.server_busy(s), bool(s.install_dir), s.last_backup_at or ""
        dest, busy, installed, last = self.gui(read)[0]
        entries = backup_manager.list_backups(dest) if dest else []
        return {"destination": dest, "busy": busy, "installed": installed, "last_backup": last,
                "backups": [{"name": os.path.basename(e.path), "when": e.when.isoformat(timespec="minutes"),
                             "trigger": e.trigger, "size_mb": round(e.size_bytes / 2 ** 20, 1)} for e in entries]}

    def backup_now(self, req: Request) -> dict:
        return self._do(req, self.win.back_up_now, "Backed up.", timeout=900)

    def restore(self, req: Request) -> dict:
        import backup_manager
        name = os.path.basename(str(req.body.get("name", "")))

        def act(s):
            entry = next((e for e in backup_manager.list_backups(s.backup_destination)
                          if os.path.basename(e.path) == name), None)
            if entry is None:
                return "That backup isn't there anymore."
            busy = self.win.server_busy(s)
            if busy and busy != "Checking for an update":
                return f"Wait until this finishes: {busy}."
            self.win._handle_restore(s, entry)
            return ""
        return self._do(req, act, "Restoring -- the server stops, a safety copy of the current world is made, the "
                                  "backup goes back in, and it starts again if it was running.")

    def import_backup(self, sid: str, temp_path: str, filename: str) -> dict:
        """`temp_path` is the streamed upload; the caller deletes it."""
        import backup_manager
        dest = self.gui(lambda: self._server(sid).backup_destination)[0]
        if not dest:
            raise WebActionError("Set a backup folder for this server first (Server Settings → Backups).")
        label = re.sub(r"[^A-Za-z0-9_-]+", "-", os.path.splitext(os.path.basename(filename or ""))[0])[:40]
        try:
            entry = backup_manager.import_external_backup(temp_path, dest, label=label or "imported")
        except backup_manager.ImportValidationError as e:
            raise WebActionError(f"That file isn't a usable Conan Exiles backup: {e}") from e
        except OSError as e:
            raise WebActionError(f"Couldn't copy it into the backup folder: {e}") from e
        self.gui(lambda: self.win.backups_changed(self._server(sid)))
        return {"ok": True, "message": f"Imported ({round(entry.size_bytes / 2 ** 20, 1)} MB). Restore it from the "
                                       f"list when you're ready."}

    # ------------------------------------------------------------ updates --
    def updates(self, req: Request) -> dict:
        def read():
            s = self._server(req.params["sid"])
            st = self.win.updates_page.state_for(s)
            return {
                "installed": s.installed_buildid or "Unknown",
                "auto_update": bool(s.auto_update), "interval_hours": s.auto_update_check_interval_hours,
                "interval_max": self.win.updates_page.interval_spin.maximum(),
                "state": st["build_state"][0].lstrip("● ").strip(),
                "checking": st["checking"] or s.id in self.win._update_check_workers,
                "updating": st["updating"] or self.win.server_busy(s) == "Updating",
                "pending": st["pending"], "changelog": st["changelog"] if st["pending"] else "",
                "history": [{"when": w, "change": c, "result": r} for w, c, r in st["history"]],
                "busy": self.win.server_busy(s),
                "last_check": getattr(s, "last_update_check_at", "") or "",
                "hold": s.update_hold or "",
            }
        return self.gui(read)[0]

    def update_check(self, req: Request) -> dict:
        def act(s):
            if not s.steamcmd_dir:
                return "This server has no SteamCMD folder yet -- finish setup in the app on the PC."
            if not self.win.updates_page.check_now(s):
                return "Already checking."
            return ""
        return self._do(req, act, "Checking Steam for a newer server build…")

    def update_install(self, req: Request) -> dict:
        def act(s):
            problem = self.win._ready(s, allow_busy=())
            return problem or self.win.updates_page.update_now(s, quiet=True)
        return self._do(req, act, "Updating -- the server is stopped and backed up first, and started again "
                                  "afterwards if it was running.")

    def update_settings(self, req: Request) -> dict:
        def act(s):
            page = self.win.updates_page
            if "auto_update" in req.body:
                s.auto_update = bool(req.body["auto_update"])
            if "interval_hours" in req.body:
                hours = self._int(req, "interval_hours", s.auto_update_check_interval_hours)
                s.auto_update_check_interval_hours = max(page.interval_spin.minimum(),
                                                         min(page.interval_spin.maximum(), hours))
            self.win._handle_config_changed(s)
            if page.server is s:
                page.refresh_settings()
            return ""
        return self._do(req, act, "Saved.")

    # --------------------------------------------------------------- mods --
    def mods(self, req: Request) -> dict:
        import mod_manager
        import steam_workshop_api as swa
        sid = req.params["sid"]
        fresh = req.query.get("refresh") == "1"

        def read():
            s = self._server(sid)
            rec = getattr(s, "mod_recovery", {}) or {}
            page = self.win.mods_page
            return ([dict(m) for m in s.mods], s.steamcmd_dir, self.win.config.steam_api_key,
                    self.win.config.workshop_update_cutoff, set(rec.get("culprits", [])), self.win.server_busy(s),
                    dict(page._mod_status.get(s.id) or {}), set(page._mod_status_ids.get(s.id, set())))
        mods, steamcmd_dir, api_key, cutoff, culprits, busy, known, checked_ids = self.gui(read)[0]
        # Look up unchecked mods (or all, on re-check); results are shared with the Mods page.
        want = [m["id"] for m in mods if fresh or m["id"] not in checked_ids]
        check_error = ""
        if want:
            result = swa.get_details(want, api_key=api_key, cutoff_ts=swa.cutoff_timestamp(cutoff),
                                     use_cache=not fresh)
            if result.ok:
                known = ({} if fresh else known)
                known.update(result.items)
                checked_ids = (set() if fresh else checked_ids) | set(want)
                ids_now, items_now = set(checked_ids), dict(known)
                self.gui(lambda: self.win.mods_page.store_status(sid, ids_now, items_now))
            else:
                check_error = result.error or "Couldn't reach the Steam Workshop."
        checked = bool(checked_ids)
        ids = {m["id"] for m in mods}
        out = []
        for m in mods:
            it = known.get(m["id"])
            if steamcmd_dir:
                downloaded = bool(mod_manager.find_workshop_pak(steamcmd_dir, m["id"]))
            else:
                downloaded = None
            needs = [it.child_titles.get(c) or (known[c].title if c in known else c)
                     for c in (it.children if it else []) if c not in ids]
            out.append({
                "id": m["id"], "name": m.get("name") or (it.title if it else m["id"]),
                "enabled": m.get("enabled", True), "downloaded": downloaded,
                "status": (it.status if it else ("missing" if m["id"] in checked_ids else "unchecked")),
                "updated": datetime.fromtimestamp(it.time_updated).date().isoformat() if it and it.time_updated else "",
                "needs": needs, "broken": m["id"] in culprits,
            })
        summary = ""
        if checked and mods:
            enabled = [m for m in out if m["enabled"]]
            bits = []
            for label, test in (("not updated for the current patch", lambda m: m["status"] == swa.STATUS_STALE),
                                ("Legacy", lambda m: m["status"] == swa.STATUS_LEGACY),
                                ("not found on the Workshop", lambda m: m["status"] == "missing"),
                                ("missing a required mod", lambda m: bool(m["needs"]))):
                n = sum(1 for m in enabled if test(m))
                if n:
                    bits.append(f"{n} {label}")
            summary = ("Every enabled mod is up to date for the current patch." if not bits else
                       "Enabled mods: " + ", ".join(bits) + ". Outdated mods are a common cause of a server "
                                                             "that won't start.")
        if check_error:
            summary = f"Couldn't check mod status: {check_error}"
        return {"mods": out, "busy": busy, "summary": summary, "summary_ok": summary.startswith("Every"),
                "search_available": bool(api_key), "steamcmd": bool(steamcmd_dir)}

    def mod_search(self, req: Request) -> dict:
        import steam_workshop_api as swa
        q = req.query.get("q", "").strip()
        sort = req.query.get("sort", "")
        if sort not in swa.SORT_OPTIONS:
            sort = ""
        show = {x for x in req.query.get("show", "").split(",") if x in swa.ALL_STATUSES} or None
        cursor = req.query.get("cursor", "*") or "*"
        api_key, cutoff, have = self.gui(lambda: (
            self.win.config.steam_api_key, self.win.config.workshop_update_cutoff,
            {m["id"] for m in self._server(req.params["sid"]).mods}))[0]
        if not api_key:
            raise WebActionError("Searching the Workshop needs a Steam API key -- add it under ConanOps → "
                                 "Steam Workshop.")
        result = swa.search(api_key, q, sort=sort, show=show, cursor=cursor, cutoff_ts=swa.cutoff_timestamp(cutoff),
                            use_cache=True, resolve_children=True)
        if not result.ok:
            raise WebActionError(result.error or "Workshop search failed.")
        return {"results": [{"id": i.id, "title": i.title, "subscriptions": i.subscriptions, "status": i.status,
                             "status_label": swa.STATUS_LABELS.get(i.status, i.status), "preview": i.preview_url,
                             "added": i.id in have,
                             "needs": [i.child_titles.get(c, c) for c in i.children if c not in have]}
                            for i in result.items],
                "next_cursor": result.next_cursor, "total": result.total}

    def _change_mods(self, sid: str, change) -> None:
        def act():
            s = self._server(sid)
            busy = self.win.server_busy(s)
            if busy in ("Finding a broken mod", "Testing mods (in the app)", "Downloading mods", "Updating"):
                raise WebActionError(f"Wait until this finishes: {busy}.")
            s.mods = change(s.mods)
            self.win._handle_mods_changed(s)
            self.win.mods_changed_elsewhere(s)
        self.gui(act)

    def mod_add(self, req: Request) -> dict:
        import mod_manager
        from ui.mods_page import ModsPage
        raw = str(req.body.get("id", "")).strip()
        wid = ModsPage._extract_workshop_id(raw)
        if not wid:
            raise WebActionError("Paste the mod's Workshop ID or its Workshop link.")
        have = self.gui(lambda: {m["id"] for m in self._server(req.params["sid"]).mods})[0]
        if wid in have:
            return {"ok": False, "message": "That mod is already on this server."}
        self._change_mods(req.params["sid"],
                          lambda mods: mod_manager.add_mod(mods, wid, str(req.body.get("name", "")).strip()))
        return {"ok": True, "message": "Added. Download Mods to fetch it; it loads the next time the server starts."}

    def mod_remove(self, req: Request) -> dict:
        import mod_manager
        wid = str(req.body.get("id", ""))
        self._change_mods(req.params["sid"], lambda mods: mod_manager.remove_mod(mods, wid))
        return {"ok": True, "message": "Removed. It's unloaded the next time the server starts."}

    def mod_toggle(self, req: Request) -> dict:
        import mod_manager
        wid = str(req.body.get("id", ""))
        on = bool(req.body.get("enabled"))
        self._change_mods(req.params["sid"], lambda mods: mod_manager.set_enabled(mods, wid, on))
        return {"ok": True, "message": ("Turned on." if on else "Turned off.") + " Takes effect the next time the "
                                                                                 "server starts."}

    def mod_move(self, req: Request) -> dict:
        import mod_manager
        wid = str(req.body.get("id", ""))
        direction = -1 if self._int(req, "direction", 1) < 0 else 1
        self._change_mods(req.params["sid"], lambda mods: mod_manager.move_mod(mods, wid, direction))
        return {"ok": True}

    def mod_download(self, req: Request) -> dict:
        return self._do(req, self.win.download_mods, "Downloading mods… you'll get an alert when it's done. They "
                                                     "load the next time the server starts.")

    def mod_find_broken(self, req: Request) -> dict:
        return self._do(req, self.win.find_broken_mod, "Testing the mods with your world protected. This can take a "
                                                       "while; you'll get an alert with the result.")

    # ----------------------------------------------------------- settings --
    def settings(self, req: Request) -> dict:
        return self.gui(lambda: self.web_settings.describe(self._server(req.params["sid"])))[0]

    def settings_save(self, req: Request) -> dict:
        values = req.body.get("values") or {}

        def act():
            return self.web_settings.save(self._server(req.params["sid"]), req.params["page"], values)
        result, msgs = self.gui(act)
        return self._result(msgs, "Saved." if (result or {}).get("changed") else "Nothing changed.")

    def settings_action(self, req: Request) -> dict:
        def act():
            return self.web_settings.run_action(self._server(req.params["sid"]), req.params["page"],
                                                str(req.body.get("action", "")), req.body.get("values") or {})
        return {"ok": True, "page": self.gui(act)[0]}

    def alerts_test(self, req: Request) -> dict:
        """Tests the typed link, or the saved one if none was typed."""
        import webhooks
        kind = str(req.body.get("kind", ""))
        if kind not in ("discord", "ntfy"):
            raise WebActionError("Unknown alert type.")
        typed = str(req.body.get("url") or "").strip()
        saved = self.gui(lambda: self.win.config.alert_discord_url if kind == "discord"
                         else self.win.config.alert_ntfy_url)[0]
        if kind == "discord":
            ok, text = webhooks.test_discord(typed or saved, "your servers", self.win.web_link_line())
        else:
            ok, text = webhooks.test_ntfy(typed or saved, "your servers")
        return {"ok": ok, "message": text}

    # -------------------------------------------------------- diagnostics --
    def diagnostics(self, req: Request) -> dict:
        import diagnostics as diag

        def read():
            s = self._server(req.params["sid"])
            return s, self.win.config.used_ports(exclude_id=s.id)
        server, reserved = self.gui(read)[0]
        results = diag.run_diagnostics(server, reserved_ports=reserved)
        actions = {}
        for r in results:
            if r.status == diag.STATUS_OK:
                continue
            if r.title == "Router UPnP":
                actions[r.title] = "port_guide"
            elif r.title == diag.VC_RUNTIME_TITLE:
                actions[r.title] = "install_vc"
        n_err = sum(1 for r in results if r.status == diag.STATUS_ERROR)
        n_warn = sum(1 for r in results if r.status == diag.STATUS_WARNING)
        summary = ("No issues found." if not n_err and not n_warn else
                   f"{n_err} problem{'' if n_err == 1 else 's'} found" + (f", {n_warn} more worth a look." if n_warn
                                                                          else ".") if n_err else
                   f"{n_warn} thing{'' if n_warn == 1 else 's'} worth a look.")
        return {"summary": summary, "results": [{"title": r.title, "status": r.status, "message": r.message,
                                                 "detail": getattr(r, "detail", ""), "action": actions.get(r.title, "")}
                                                for r in results]}

    def port_guide(self, req: Request) -> dict:
        import network_setup
        import network_utils
        from ui.port_forwarding_guide_dialog import guide_content
        server = self.gui(lambda: self._server(req.params["sid"]))[0]
        router_ip = public_ip = None
        try:
            router_ip = network_utils.get_default_gateway(server.bind_ip or None)
            public_ip = network_setup.get_public_ip(timeout=3.0)
        except Exception as e:  # noqa: BLE001
            _log.warning(f"Guide info lookup failed: {e}")
        intro, steps = guide_content(server.game_port, server.query_port, server.bind_ip or "", router_ip, public_ip)
        return {"intro": intro, "steps": [{"number": n, "title": t, "body": b} for n, t, b in steps]}

    def install_vc_runtime(self, req: Request) -> dict:
        """Installs now if admin; otherwise waits for someone at the PC to approve."""
        import proc_utils
        if not proc_utils.is_admin():
            def queue():
                self.win.queue_for_pc("Installing the Visual C++ runtime", self.win.diagnostics_page._install_vc_runtime)
            return self._result(self.gui(queue)[1], "OK.")
        import vcredist
        try:
            outcome = vcredist.install()
        except Exception as e:  # noqa: BLE001
            _log.error(f"Visual C++ runtime install failed: {e}")
            outcome = vcredist.INSTALL_FAILED
        messages = {
            vcredist.INSTALL_OK: (True, "Installed. Run Diagnostics again to check."),
            vcredist.INSTALL_OK_RESTART: (True, "Installed. Windows may need a restart before the server can use it."),
            vcredist.INSTALL_DECLINED: (False, "Windows didn't allow the install."),
            vcredist.INSTALL_DOWNLOAD_FAILED: (False, "Couldn't download the Visual C++ runtime from Microsoft -- "
                                                      "check the PC's internet connection."),
            vcredist.INSTALL_FAILED: (False, "Installing the Visual C++ runtime failed -- see conanops.log on the PC."),
        }
        ok, text = messages.get(outcome, (False, "Installing the Visual C++ runtime failed."))
        return {"ok": ok, "message": text}

    def repair_network(self, req: Request) -> dict:
        done = threading.Event()
        holder: dict = {}

        def act():
            s = self._server(req.params["sid"])
            self.win.prepare_network_repair(s)

            def finished(result):
                holder.update(result or {})
                done.set()
            self.win._start_network_reconcile(s, legacy_names=[s.name], on_done=finished)
        _r, msgs = self.gui(act)
        if not done.wait(180):
            return {"ok": True, "message": "Still working -- check Diagnostics in a minute."}
        lines = self.win.network_repair_lines(holder)
        ok = not holder.get("error") and not any(l.startswith("✗") for l in lines)
        result = self._result(msgs, "\n".join(lines) or "Done.")
        if not ok:
            result["ok"] = False
        return result

    # ------------------------------------------------------- app options --
    def app_options(self, req: Request) -> dict:
        import alert_guides
        import proc_utils

        def read():
            c = self.win.config
            page = self.win.app_settings_page
            return {
                "version": version.VERSION,
                "start_with_windows": c.start_with_windows, "start_minimized": c.start_minimized_to_tray,
                "keep_alive": c.keep_alive_enabled,
                "keep_awake": c.keep_pc_awake, "update_restarts": c.handle_update_restarts,
                "update_window": [c.update_restart_start, c.update_restart_end],
                "mod_recovery_mode": c.mod_recovery_mode,
                "auto_check_app_updates": c.auto_check_app_updates,
                "auto_install_app_updates": c.auto_install_app_updates,
                "app_update_status": page.app_update_status.text() if hasattr(page, "app_update_status") else "",
                "app_update_available": bool(getattr(page, "_available_release", None)),
                "app_update_busy": (page._app_update_check_worker is not None
                                    or page._app_update_install_worker is not None),
                "sign_in_note": page.sign_in_status_label.text() if not page.sign_in_status_label.isHidden() else "",
                "steam_api_key_set": bool(c.steam_api_key), "workshop_update_cutoff": c.workshop_update_cutoff,
                "duckdns_domain": c.duckdns_domain, "duckdns_token_set": bool(c.duckdns_token),
                "alert_discord_set": bool(c.alert_discord_url), "alert_ntfy_set": bool(c.alert_ntfy_url),
                "discord_status_enabled": bool(c.discord_status_enabled),
                "discord_bot_token_set": bool(c.discord_bot_token), "discord_bot_channel_id": c.discord_bot_channel_id,
                "discord_bot_admin_ids": c.discord_bot_admin_ids,
                "discord_bot_problem": getattr(self.win, "discord_bot_problem", ""),
                "alert_guides": {k: alert_guides.as_json(g) for k, g in alert_guides.ALL.items()},
                "admin": proc_utils.is_admin(), "admin_mode": bool(getattr(c, "admin_mode_enabled", False)),
                "needs_pc": self.win.needs_pc_labels(),
                "web_lan_url": self.win.web_control.url_for() or "",
                "web_remote_on": bool(getattr(c, "web_remote_enabled", False)),
                "web_remote_url": getattr(self.win.web_tunnel, "url", "") or "",
                "web_remote_status": getattr(self.win.web_tunnel, "status", "") or "",
                "web_sessions": self.win.web_control.sessions.count(),
            }
        return self.gui(read)[0]

    def app_set(self, req: Request) -> dict:
        option = req.params["option"]
        value = req.body.get("value")
        handlers = {
            "start_with_windows": self._set_start_with_windows,
            "start_minimized": lambda v: self._set_config("start_minimized_to_tray", bool(v),
                                                          "start_minimized_checkbox"),
            "keep_alive": self._set_keep_alive,
            "keep_awake": lambda v: self._set_config("keep_pc_awake", bool(v), "keep_awake_checkbox"),
            "update_restarts": self._set_update_restarts,
            "update_window": self._set_update_window,
            "auto_check_app_updates": lambda v: self._set_config("auto_check_app_updates", bool(v),
                                                                 "auto_check_updates_checkbox"),
            "auto_install_app_updates": lambda v: self._set_config("auto_install_app_updates", bool(v),
                                                                   "auto_install_updates_checkbox"),
            "mod_recovery_mode": self._set_mod_recovery_mode,
            "steam_api_key": lambda v: self._set_text("steam_api_key", str(v or "").strip(), "steam_api_key_edit"),
            "workshop_update_cutoff": self._set_cutoff,
            "duckdns_domain": lambda v: self._set_text("duckdns_domain", str(v or "").strip(), "duckdns_domain_edit"),
            "duckdns_token": lambda v: self._set_text("duckdns_token", str(v or "").strip(), "duckdns_token_edit"),
            "alert_discord_url": lambda v: self._set_alerts(discord=str(v or "")),
            "alert_ntfy_url": lambda v: self._set_alerts(ntfy=str(v or "")),
            "discord_status_enabled": lambda v: self._set_alerts(status=bool(v)),
            "discord_bot_token": lambda v: self._set_bot(token=str(v or "")),
            "discord_bot_channel_id": lambda v: self._set_bot(channel=str(v or "")),
            "discord_bot_admin_ids": lambda v: self._set_bot(admins=str(v or "")),
            "check_app_update": lambda v: self._app_update(False),
            "install_app_update": lambda v: self._app_update(True),
        }
        fn = handlers.get(option)
        if fn is None:
            raise WebActionError("Unknown option.")
        return fn(value)

    def _set_config(self, attr: str, value, widget: str) -> dict:
        def act():
            page = self.win.app_settings_page
            setattr(self.win.config, attr, value)
            self.win.config.save()
            page._set_checked_quietly(getattr(page, widget), value)
        return self._result(self.gui(act)[1], "Saved.")

    def _set_text(self, attr: str, value: str, widget: str) -> dict:
        def act():
            page = self.win.app_settings_page
            setattr(self.win.config, attr, value)
            self.win.config.save()
            edit = getattr(page, widget)
            edit.blockSignals(True)
            edit.setText(value)
            edit.blockSignals(False)
        return self._result(self.gui(act)[1], "Saved.")

    def _set_alerts(self, discord=None, ntfy=None, status=None) -> dict:
        def act():
            c = self.win.config
            try:
                self.win.app_settings_page.set_alert_links(
                    c.alert_discord_url if discord is None else discord,
                    c.alert_ntfy_url if ntfy is None else ntfy,
                    c.discord_status_enabled if status is None else status)
            except ValueError as e:
                raise WebActionError(str(e)) from None
        return self._result(self.gui(act)[1], "Saved.")

    def _set_bot(self, token=None, channel=None, admins=None) -> dict:
        def act():
            c = self.win.config
            self.win.app_settings_page.set_discord_bot(
                c.discord_bot_token if token is None else token,
                c.discord_bot_channel_id if channel is None else channel,
                c.discord_bot_admin_ids if admins is None else admins)
        return self._result(self.gui(act)[1], "Saved.")

    def _set_cutoff(self, value) -> dict:
        text = str(value or "").strip()
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            raise WebActionError("Use a date like 2026-09-01.") from None
        return self._set_text("workshop_update_cutoff", text, "workshop_cutoff_edit")

    def _set_start_with_windows(self, value) -> dict:
        import startup_registration
        on = bool(value)
        try:
            (startup_registration.register if on else startup_registration.unregister)()
        except OSError as e:
            return {"ok": False, "message": f"Windows didn't accept the change: {e}"}
        return self._set_config("start_with_windows", on, "start_with_windows_checkbox")

    def _set_keep_alive(self, value) -> dict:
        import keep_alive
        on = bool(value)
        ok = keep_alive.enable() if on else keep_alive.disable()
        if not ok:
            return {"ok": False, "message": "Windows didn't accept the change -- see conanops.log on the PC."}
        return self._set_config("keep_alive_enabled", on, "keep_alive_checkbox")

    def _set_mod_recovery_mode(self, value) -> dict:
        def act():
            combo = self.win.app_settings_page.mod_recovery_combo
            idx = combo.findData(str(value))
            if idx < 0:
                raise WebActionError("Unknown choice.")
            combo.setCurrentIndex(idx)  # its handler saves
        return self._result(self.gui(act)[1], "Saved.")

    def _windows_hours_direct(self) -> bool:
        """Active hours are machine-wide, so changing them needs admin or a prompt."""
        import proc_utils
        return proc_utils.is_admin()

    def _set_update_restarts(self, value) -> dict:
        import powershell
        import windows_update
        on = bool(value)
        cfg = self.win.config
        if on == bool(cfg.handle_update_restarts):
            return {"ok": True, "message": "Saved."}
        page = self.win.app_settings_page
        needs_prompt = on or bool(cfg.original_active_hours)
        if needs_prompt and not self._windows_hours_direct():
            def queue():
                box = page.update_restart_checkbox
                self.win.queue_for_pc("Changing Windows' update restart hours", lambda: box.setChecked(on))
            return self._result(self.gui(queue)[1], "Saved.")
        if on:
            hours = windows_update.compute_active_hours(cfg.update_restart_start, cfg.update_restart_end)
            if hours is None:
                raise WebActionError("Leave at least one hour outside the restart window.")
            original = cfg.original_active_hours or windows_update.read_active_hours()
            if windows_update.set_active_hours(*hours) != powershell.RUN_OK:
                return {"ok": False, "message": "Windows didn't accept the change -- see conanops.log on the PC."}

            def done():
                cfg.original_active_hours = cfg.original_active_hours or original
                cfg.handle_update_restarts = True
                cfg.save()
                page._set_checked_quietly(page.update_restart_checkbox, True)
            return self._result(self.gui(done)[1], "Saved.")
        original = dict(cfg.original_active_hours or {})
        restored = (not original) or windows_update.restore_active_hours(original) == powershell.RUN_OK

        def off():
            cfg.handle_update_restarts = False
            if restored:
                cfg.original_active_hours = {}
            cfg.save()
            page._set_checked_quietly(page.update_restart_checkbox, False)
        return self._result(self.gui(off)[1], "Saved.")

    def _set_update_window(self, value) -> dict:
        import powershell
        import windows_update
        try:
            start, end = (str(x).strip() for x in value)
            datetime.strptime(start, "%H:%M")
            datetime.strptime(end, "%H:%M")
        except (TypeError, ValueError):
            raise WebActionError("Use times like 04:00 and 06:00.") from None
        cfg = self.win.config
        page = self.win.app_settings_page

        def show_and_save():
            from PySide6.QtCore import QTime
            for edit, t in ((page.update_start_edit, start), (page.update_end_edit, end)):
                edit.blockSignals(True)
                edit.setTime(QTime.fromString(t, "HH:mm"))
                edit.blockSignals(False)
            cfg.update_restart_start, cfg.update_restart_end = start, end
            cfg.save()
        if not cfg.handle_update_restarts:
            return self._result(self.gui(show_and_save)[1], "Saved.")
        hours = windows_update.compute_active_hours(start, end)
        if hours is None:
            raise WebActionError("Leave at least one hour outside the restart window.")
        if not self._windows_hours_direct():
            def queue():
                from PySide6.QtCore import QTime

                def at_pc():
                    page.update_start_edit.setTime(QTime.fromString(start, "HH:mm"))
                    page.update_end_edit.setTime(QTime.fromString(end, "HH:mm"))
                    page._on_update_window_changed()
                self.win.queue_for_pc("Changing Windows' update restart hours", at_pc)
            return self._result(self.gui(queue)[1], "Saved.")
        if windows_update.set_active_hours(*hours) != powershell.RUN_OK:
            return {"ok": False, "message": "Windows didn't accept the change -- see conanops.log on the PC."}
        return self._result(self.gui(show_and_save)[1], "Saved.")

    def _app_update(self, install: bool) -> dict:
        def act():
            page = self.win.app_settings_page
            if page._app_update_install_worker is not None:
                return "ConanOps is already installing an update."
            if not install:
                if page._app_update_check_worker is not None:
                    return "Already checking."
                page.check_for_updates(automatic=False)
                return ""
            if not getattr(page, "_available_release", None):
                return "No update is waiting -- check for updates first."
            busy = page.is_busy_for_app_update() if callable(page.is_busy_for_app_update) else ""
            if busy:
                return f"Not right now: wait until {busy} finishes, then try again."
            page._install_online_update(confirm=False)
            return ""
        problem, msgs = self.gui(act)
        if problem:
            return {"ok": False, "message": problem}
        return self._result(msgs, "Installing -- ConanOps restarts itself on the PC; this page reconnects in a "
                                  "minute. Your servers keep running." if install
                            else "Checking for a new version of ConanOps…")
