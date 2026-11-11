"""Runs each server's scheduled events (scheduled_events.py): writes an
event's settings when it starts and puts them back when it ends, restarting
the server each time (with in-game warnings), and posts scheduled messages.

The scheduler calls _handle_events_due every minute for each server that
has events. The desktop Events page and the web version both edit events
through save_scheduled_event / delete_scheduled_event.

Part of MainWindow (see ui/main_window.py)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

import applog
import banlist_manager
import process_manager
import scheduled_events as se
from models import ServerConfig
from ui.window.countdown import WAITING

_log = applog.get_logger('ui.main_window')


class EventsMixin:
    # ------------------------------------------------------------ editing --
    def scheduled_events_for(self, server: ServerConfig) -> list:
        return server.scheduled_events

    def save_scheduled_event(self, server: ServerConfig, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Adds or replaces an event (matched by id). ValueError if it doesn't
        check out. An event that's on is put back first when it's changed,
        then starts again with the new settings on the next minute."""
        existing = next((e for e in server.scheduled_events if e.get("id") and e.get("id") == raw.get("id")), None)
        if existing is None and len(server.scheduled_events) >= se.MAX_EVENTS:
            raise ValueError(f"A server can have up to {se.MAX_EVENTS} events.")
        ev = se.clean_event(raw, existing=existing)
        if existing is not None and (existing.get("state") or {}).get("active"):
            self._end_settings_event(server, existing, why="it was changed")
            ev["state"] = dict(existing.get("state") or {})
        if existing is None:
            server.scheduled_events.append(ev)
        else:
            server.scheduled_events[server.scheduled_events.index(existing)] = ev
        self.config.save()
        self._handle_events_due(server, datetime.now())
        self._events_changed(server)
        return ev

    def delete_scheduled_event(self, server: ServerConfig, event_id: str) -> bool:
        ev = next((e for e in server.scheduled_events if e.get("id") == event_id), None)
        if ev is None:
            return False
        if (ev.get("state") or {}).get("active"):
            self._end_settings_event(server, ev, why="it was deleted")
            self._restart_for_event_if_needed(server, ev)
        server.scheduled_events.remove(ev)
        self.config.save()
        self._events_changed(server)
        return True

    def _events_changed(self, server: ServerConfig) -> None:
        page = getattr(self, "events_page", None)
        if page is not None and self.config.active_server_id == server.id:
            page.refresh()

    # ------------------------------------------------------------ running --
    def _handle_events_due(self, server: ServerConfig, now: Optional[datetime] = None) -> None:
        now = now or datetime.now()
        if server.id in self._automation_locked:
            return  # a mod test needs the settings left alone
        changed = False
        for ev in list(server.scheduled_events):
            try:
                if ev.get("kind") == se.KIND_MESSAGE:
                    changed |= self._run_message_event(server, ev, now)
                elif ev.get("kind") == se.KIND_SETTINGS:
                    changed |= self._run_settings_event(server, ev, now)
            except Exception as e:  # noqa: BLE001 - one bad event mustn't stop the rest
                _log.error(f"Scheduled event {ev.get('name')!r} on {server.name} failed: {e}")
        if changed:
            self.config.save()
            self._events_changed(server)

    def _run_message_event(self, server: ServerConfig, ev: dict, now: datetime) -> bool:
        if not ev.get("enabled", True):
            return False
        key = se.message_due(ev, now)
        if not key:
            return False
        ev.setdefault("state", {})["last_sent"] = key
        if not server.rcon_enabled or not self._known_running.get(server.id, False):
            return True  # nobody to tell; counted as done so it isn't posted late
        text = ev.get("text", "")

        def sent(_r, srv=server, t=text):
            self._record_event(srv, "announcement", t)
        self._run_op(lambda srv=server, t=text: banlist_manager.broadcast_message(srv, t), on_done=sent,
                     on_error=lambda e, n=server.name, nm=ev.get("name"): _log.warning(
                         f"Couldn't post the scheduled message {nm!r} on {n}: {e}"))
        return True

    def _run_settings_event(self, server: ServerConfig, ev: dict, now: datetime) -> bool:
        state = ev.setdefault("state", {})
        want_on = bool(ev.get("enabled", True)) and se.window_on(ev, now)
        changed = False
        if want_on and not state.get("active"):
            self._start_settings_event(server, ev)
            changed = True
        elif not want_on and state.get("active"):
            self._end_settings_event(server, ev, why="")
            changed = True
        if state.get("needs_restart"):
            changed |= self._restart_for_event_if_needed(server, ev)
        return changed

    def _start_settings_event(self, server: ServerConfig, ev: dict) -> None:
        wanted, originals = se.plan_start(ev, server.gameplay)
        state = ev.setdefault("state", {})
        state.update(active=True, applied=wanted, originals=originals, started_at=datetime.now().isoformat())
        self._apply_gameplay(wanted, server)
        self.settings_saved_elsewhere(server)
        running = self._known_running.get(server.id, False)
        state["needs_restart"] = bool(running)
        self._record_event(server, "event", f"{ev.get('name')} started: {se.describe_what(ev)}")
        self._notify(server, f"“{ev.get('name')}” has started ({se.describe_what(ev)})."
                             + (" The server restarts to load it." if running else
                                " It takes effect when the server next starts."),
                     title="Scheduled Event")

    def _end_settings_event(self, server: ServerConfig, ev: dict, why: str) -> None:
        state = ev.setdefault("state", {})
        restore = se.plan_end(state, server.gameplay)
        if restore:
            self._apply_gameplay(restore, server)
            self.settings_saved_elsewhere(server)
        running = self._known_running.get(server.id, False)
        state.update(active=False, applied={}, originals={}, needs_restart=bool(running and restore))
        reason = f" ({why})" if why else ""
        self._record_event(server, "event", f"{ev.get('name')} ended{reason}")
        self._notify(server, f"“{ev.get('name')}” has ended{reason}; the usual settings are back."
                             + (" The server restarts to load them." if running and restore else ""),
                     title="Scheduled Event")

    def _restart_for_event_if_needed(self, server: ServerConfig, ev: dict) -> bool:
        """True if the event's state changed."""
        state = ev.setdefault("state", {})
        if not state.get("needs_restart"):
            return False
        running = bool(server.install_dir) and self._known_running.get(server.id, False)
        if not running:
            state["needs_restart"] = False  # the new settings load when it next starts
            return True
        if server.id in self._countdowns:
            state["needs_restart"] = False  # the restart already counting down loads them
            return True
        if self.server_busy(server):
            return False  # try again next minute
        name = ev.get("name") or "event"

        def restart(srv=server):
            if not process_manager.is_running(srv.install_dir):
                return
            self._do_restart(srv, on_error=lambda e, s=srv: self._notify(
                s, f"Restart for “{name}” failed: {e}", title="Restart Failed"))
        result = self._with_warning(server, f"restart for {name}", restart)
        if result == WAITING:
            return False
        state["needs_restart"] = False
        return True
