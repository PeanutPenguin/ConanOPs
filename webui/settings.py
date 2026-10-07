"""
Server settings for the web version, for any server, without touching
the app's own settings pages.

The web version keeps its OWN, never-shown copy of every settings page
(the same classes the app uses). For a request it loads the server's
saved values into that copy, describes it, and on save puts the web's
values in, runs the page's own checks (port conflicts, time format...)
and calls the app's own save function for that server. So:

  * validation and what saving does are exactly the app's;
  * the app's pages, and anything someone is half-way through editing
    on the PC, are never touched -- if that server is open in the app,
    its pages just take the new saved values for fields nobody's
    editing (WebActionsMixin.settings_saved_elsewhere);
  * it works for every server, not only the one open in the app.

Must be used on the GUI thread (webui.bridge).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from webui import fields
from webui.bridge import WebActionError

# Buttons on a page that fill in fields (without saving): action id ->
# (page key, label, method name on the page).
PAGE_ACTIONS = {
    "suggest_port": ("network", "Use suggested port", "_fix_port_conflict"),
    "detect_ip": ("network", "Auto-detect bind address", "_auto_detect_ip"),
    "use_suggestion": ("restart", "Use Suggested Window", "_use_suggestion"),
}


class WebSettings:
    def __init__(self, window):
        self.win = window
        self._sid: Optional[str] = None
        self._entries: Optional[List[tuple]] = None

    # ------------------------------------------------------------ pages --
    def _build(self) -> List[tuple]:
        if self._entries is not None:
            return self._entries
        import ini_field_specs
        from ui.generic_settings_page import GenericSettingsPage
        from ui.settings_alerts_page import SettingsAlertsPage
        from ui.settings_backups_page import SettingsBackupsPage
        from ui.settings_identity_page import SettingsIdentityPage
        from ui.settings_network_page import SettingsNetworkPage
        from ui.settings_rates_page import SettingsRatesPage
        from ui.settings_restart_page import SettingsRestartPage

        win = self.win
        entries = [
            ("identity", "Server Identity", SettingsIdentityPage()),
            ("network", "Network & Ports",
             SettingsNetworkPage(get_reserved_ports=lambda: win.config.used_ports(exclude_id=self._sid))),
            ("progression", "Progression", SettingsRatesPage()),
        ]
        for key, label, specs in ini_field_specs.CATEGORIES:
            if key != "progression":
                entries.append((key, label, GenericSettingsPage(label, specs)))
        entries += [
            ("backups", "Backups", SettingsBackupsPage()),
            ("restart", "Restart Schedule", SettingsRestartPage(get_hourly_activity=self._hourly_activity)),
            ("alerts", "RCON", SettingsAlertsPage()),
        ]
        self._entries = entries
        return entries

    def _hourly_activity(self):
        server = self._server_or_none()
        return self.win._tracker_for(server).hourly_activity_histogram() if server else []

    def _server_or_none(self):
        return next((s for s in self.win.config.servers if s.id == self._sid), None)

    def _page(self, key: str):
        page = next((p for k, _t, p in self._build() if k == key), None)
        if page is None:
            raise WebActionError("Unknown settings page.")
        return page

    def _load(self, server, key: Optional[str] = None) -> None:
        """Puts the server's saved values into the web's copy of the pages
        (all of them, or just one)."""
        self._sid = server.id
        values = self.win.settings_values(server)
        for k, _t, page in self._build():
            if key is not None and k != key:
                continue
            page.load_committed(values.get(k, {}))
            if k == "identity":
                page.set_install_dir(server.install_dir)

    # ------------------------------------------------------------- read --
    def describe(self, server) -> Dict[str, Any]:
        self._load(server)
        pages = []
        for key, title, page in self._build():
            d = fields.describe_page(key, title, page)
            d["error"] = fields.page_error(page)
            d["actions"] = [{"id": a, "label": label} for a, (k, label, _m) in PAGE_ACTIONS.items() if k == key]
            pages.append(d)
        return {"pages": pages}

    # ------------------------------------------------------------ write --
    def _fill(self, server, key: str, values: Dict[str, Any]):
        if not isinstance(values, dict):
            raise WebActionError("Nothing to save.")
        self._load(server, key)
        page = self._page(key)
        fields.put_values(page, values)
        return page

    def save(self, server, key: str, values: Dict[str, Any]) -> Dict[str, Any]:
        page = self._fill(server, key, values)
        if page.dirty_count() == 0:
            return {"changed": False}
        if not page.can_apply():
            raise WebActionError(fields.page_error(page) or "These settings can't be saved as they are.")
        current = page._current_values()
        self.win.settings_apply_handler(key)(current, server)
        self.win.settings_saved_elsewhere(server, key)
        return {"changed": True}

    def run_action(self, server, key: str, action: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """A fill-in button (suggest a free port...): applies the web's
        unsaved values, runs it, and returns the page as it then looks.
        Nothing is saved."""
        spec = PAGE_ACTIONS.get(action)
        if spec is None or spec[0] != key:
            raise WebActionError("Unknown action.")
        page = self._fill(server, key, values or {})
        getattr(page, spec[2])()
        title = next(t for k, t, _p in self._build() if k == key)
        d = fields.describe_page(key, title, page)
        d["error"] = fields.page_error(page)
        return d
