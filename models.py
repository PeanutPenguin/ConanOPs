"""
ConanOps data models.

ServerConfig holds every setting the UI can show/edit for one Conan Exiles
dedicated server. AppConfig holds the list of servers (up to MAX_SERVERS)
plus app-wide preferences, and knows how to load/save itself as JSON.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import uuid
import secrets as _secrets
from dataclasses import dataclass, field, asdict
from typing import List, Optional

import conanops_paths
import ini_field_specs
import secrets_store

MAX_SERVERS = 5

# PBKDF2 iteration count for the app-lock PIN (see AppConfig.set_app_lock_pin).
# A single unsalted-iteration SHA-256 hash -- what this used to be -- is
# fast enough that a stolen config.json's 4-character numeric PIN (a
# search space of only 10,000 values) could be brute-forced essentially
# instantly offline. PBKDF2 with a real iteration count doesn't make a
# 4-digit PIN strong on its own, but it raises the cost of that offline
# search from "instant" to "noticeable," at negligible cost to the one
# legitimate hash computed per unlock attempt.
_PIN_HASH_ITERATIONS = 310_000


def _hash_pin(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt.encode("utf-8"), _PIN_HASH_ITERATIONS).hex()
APP_ID = 443030  # Conan Exiles Dedicated Server on Steam


@dataclass
class ServerConfig:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = "New Server"

    # --- Paths ---
    install_dir: str = ""          # .../steamapps/common/Conan Exiles Dedicated Server
    steamcmd_dir: str = ""         # folder containing steamcmd.exe

    # --- Server Identity / all gameplay settings ---
    # Flat dict keyed by the real Conan Exiles ini key name (or a
    # __-prefixed ConanOps-only key, currently just "__description").
    # See ini_field_specs.py for the full list of keys, defaults, and
    # what page each one appears on. Populated with defaults by
    # AppConfig via ini_field_specs.default_gameplay_dict() so old and
    # new servers alike always have every key present.
    gameplay: dict = field(default_factory=dict)

    # --- Network & Ports ---
    password: str = ""
    game_port: int = 7777
    query_port: int = 27015
    bind_ip: str = ""               # local LAN IP for -MULTIHOME, auto-detected
    max_players: int = 40

    # --- Backup schedule ---
    backup_daily_keep: int = 7
    backup_weekly_keep: int = 4
    backup_interval_hours: int = 6
    backup_destination: str = ""
    backup_before_update: bool = True

    # --- Restart schedule ---
    restart_enabled: bool = True
    restart_start: str = "04:00"    # 24h HH:MM
    restart_end: str = "05:00"

    # --- Update tracking ---
    auto_update: bool = True
    auto_update_check_interval_hours: int = 6
    installed_buildid: str = ""
    last_update_check_at: str = ""   # ISO timestamp of the last SteamCMD update check
    last_mod_check_at: str = ""      # ISO timestamp of the last independent mod-update check (scheduler.py)

    # --- Live Discord status presence ---
    # Off by default -- not everyone wants a message that keeps
    # re-editing itself sitting in their channel. discord_status_message_id
    # is the Discord message this server's status gets EDITED into place
    # on, once one exists -- persisted so a restarted ConanOps keeps
    # updating the same message instead of posting a fresh one every
    # session (see webhooks.update_discord_status()).
    discord_status_enabled: bool = False
    discord_status_message_id: str = ""

    # --- RCON ---
    rcon_enabled: bool = False
    rcon_port: int = 25575
    rcon_password: str = ""

    # --- Alerts ---
    webhook_discord_url: str = ""
    webhook_ntfy_url: str = ""

    # --- Access control ---
    whitelist_enabled: bool = False
    whitelist_ids: list = field(default_factory=list)   # SteamID64 strings
    banned_ids: list = field(default_factory=list)      # SteamID64 strings

    # --- Mods (ordered; each entry {"id": "<workshop id>", "enabled": bool, "name": str}) ---
    mods: list = field(default_factory=list)

    # --- Scheduler runtime state (not really "settings", but persisted
    # alongside everything else so the scheduler survives app restarts) ---
    last_backup_at: str = ""       # ISO timestamp of the last scheduled/auto backup
    last_restart_date: str = ""    # "YYYY-MM-DD" -- the last date a scheduled restart fired

    # --- Auto-resume / watchdog ---
    # Set True whenever the person presses Start (Dashboard or tray),
    # False when they press Stop -- deliberately NOT the same thing as
    # "is it running right now" (process_manager.is_running() answers
    # that). This is what survives an app or PC restart: on startup,
    # MainWindow starts every server where this is True and isn't
    # already running (see _auto_resume_servers()). A crash or a
    # watchdog-triggered restart never changes this -- only an
    # intentional Stop does, which is exactly what lets the watchdog
    # keep retrying after a crash (desired_running stays True) while
    # a server the person deliberately stopped stays down through a
    # reboot instead of coming back uninvited.
    desired_running: bool = False
    # Set when an automatic or manual game update failed after the server
    # was stopped for it (see main_window._hold_after_failed_update):
    # the reason, shown to the person. While set, auto-resume and the
    # crash watchdog leave the server stopped -- the old build can't be
    # joined by players whose game Steam already updated, and a half-
    # applied update may not even start. Cleared by a successful update
    # or by the person clicking Start themselves.
    update_hold: str = ""
    # Automatic mod-break recovery (ui/mod_recovery.py) waiting for a fix:
    # {"culprits": [workshop ids], "since": iso time, "updated": {id: Workshop
    # time_updated when found}}. Empty when nothing is being waited on.
    mod_recovery: dict = field(default_factory=dict)

    # Fields that hold something sensitive and get encrypted (DPAPI) at
    # rest in config.json rather than stored as plain text -- see
    # secrets_store.py.
    _SECRET_FIELDS = ("password", "rcon_password", "webhook_discord_url", "webhook_ntfy_url")
    # AdminPassword is a nested key inside `gameplay` (it's one of
    # ini_field_specs' data-driven fields, not a top-level dataclass
    # field), so it doesn't go through _SECRET_FIELDS above -- handled
    # separately below. It's exactly as sensitive as rcon_password (it
    # grants in-game admin/console access), and a previous version left
    # it out of encryption entirely, storing it as plain text in
    # config.json alongside every other gameplay setting.
    _SECRET_GAMEPLAY_KEY = "AdminPassword"

    def __post_init__(self) -> None:
        # Runtime-only, deliberately NOT a dataclass field (so it's
        # invisible to asdict()/to_dict() and never round-trips through
        # JSON): {field name: raw stored string} for any secret field
        # that failed to decrypt on load. See to_dict()'s use of this.
        self._undecryptable: dict = {}

    def to_dict(self) -> dict:
        d = asdict(self)
        for f in self._SECRET_FIELDS:
            if f in self._undecryptable and not getattr(self, f):
                # This field failed to decrypt when the config was
                # loaded (see from_dict()) and is still blank -- nothing
                # in the UI has set a new value for it since, so write
                # the original stored string back out untouched rather
                # than protect()-ing the "" that decrypt failure left in
                # memory, which would otherwise permanently overwrite
                # the real (still-encrypted, just unreadable in THIS
                # run) secret with an empty one. The moment something
                # DOES set a new value, this field becomes non-empty and
                # falls through to the normal protect() path below,
                # which also clears the marker -- the new value wins.
                d[f] = self._undecryptable[f]
            else:
                self._undecryptable.pop(f, None)
                d[f] = secrets_store.protect(d[f])
        gameplay_key = self._SECRET_GAMEPLAY_KEY
        if gameplay_key in d.get("gameplay", {}):
            d["gameplay"] = dict(d["gameplay"])
            current = d["gameplay"][gameplay_key]
            if gameplay_key in self._undecryptable and not current:
                d["gameplay"][gameplay_key] = self._undecryptable[gameplay_key]
            else:
                self._undecryptable.pop(gameplay_key, None)
                d["gameplay"][gameplay_key] = secrets_store.protect(current)
        return d

    @staticmethod
    def from_dict(d: dict) -> "ServerConfig":
        known = {f.name for f in ServerConfig.__dataclass_fields__.values()}
        clean = {k: v for k, v in d.items() if k in known}
        undecryptable: dict = {}
        for f in ServerConfig._SECRET_FIELDS:
            if f in clean:
                try:
                    clean[f] = secrets_store.unprotect(clean[f])
                except secrets_store.DecryptionError:
                    undecryptable[f] = clean[f]  # keep the raw, still-protected string
                    clean[f] = ""
        if isinstance(clean.get("gameplay"), dict) and ServerConfig._SECRET_GAMEPLAY_KEY in clean["gameplay"]:
            clean["gameplay"] = dict(clean["gameplay"])
            raw = clean["gameplay"][ServerConfig._SECRET_GAMEPLAY_KEY]
            try:
                clean["gameplay"][ServerConfig._SECRET_GAMEPLAY_KEY] = secrets_store.unprotect(raw)
            except secrets_store.DecryptionError:
                undecryptable[ServerConfig._SECRET_GAMEPLAY_KEY] = raw
                clean["gameplay"][ServerConfig._SECRET_GAMEPLAY_KEY] = ""
        cfg = ServerConfig(**clean)
        cfg._undecryptable = undecryptable
        cfg.ensure_gameplay_defaults()
        return cfg

    def ensure_gameplay_defaults(self) -> None:
        """Backfills any missing keys in `gameplay` with their defaults
        from ini_field_specs -- covers both a brand-new server and an
        older saved config from before some field existed."""
        defaults = ini_field_specs.default_gameplay_dict()
        for k, v in defaults.items():
            self.gameplay.setdefault(k, v)


# What _migrate_legacy_data_dir() carries over to a new data folder.
_MIGRATED_NAMES = ("config.json", "theme.json", "sessions")


@dataclass
class AppConfig:
    servers: List[ServerConfig] = field(default_factory=list)
    active_server_id: Optional[str] = None
    accent_color: str = "#c9752f"

    # --- Optional startup PIN lock ---
    # ConanOps has full RCON access, can ban/whitelist players, and
    # force restarts/updates -- anyone who can open the app has all of
    # that, plus read access to the (now-encrypted-at-rest, see
    # secrets_store.py) server/RCON passwords and webhook URLs. This
    # is a lightweight single-PIN gate, not real multi-user auth: it's
    # meant to stop someone glancing at an unlocked screen or picking
    # up a shared machine, not to resist a determined local attacker
    # who can just edit/delete these two fields out of config.json.
    app_lock_pin_hash: str = ""
    app_lock_salt: str = ""

    # Off by default; persisted so an explicit choice to turn it on
    # survives an app restart, same as the PIN lock does.
    web_control_enabled: bool = False
    # The web version's password (webui/auth.py PBKDF2 hash -- never the
    # password itself) and whether the Cloudflare remote link is on.
    web_password_hash: str = ""
    web_remote_enabled: bool = False

    # --- Start with Windows ---
    # start_with_windows itself doesn't launch anything on its own --
    # it only reflects whether startup_registration.register() has
    # actually added the registry Run-key entry, kept here so the App
    # Settings checkbox shows the real current state without needing
    # a registry read every time that page opens. start_minimized_to_tray
    # is independent of it (and takes effect on EVERY launch, not just
    # a Windows-startup one -- there's no reliable way for ConanOps to
    # tell how it was launched, so "start minimized" just always means
    # exactly that, however it was started).
    start_with_windows: bool = False
    start_minimized_to_tray: bool = False

    # --- Unattended operation ---
    # background_mode_enabled mirrors whether background_mode.register()
    # succeeded (the scheduled task that keeps servers managed when
    # nobody is signed in). keep_pc_awake blocks idle sleep while any
    # server runs (power.py). handle_update_restarts lets ConanOps do
    # Windows Update restarts itself inside the update_restart_* window
    # (windows_update.py); last_update_restart ("YYYY-MM-DD") stops it
    # restarting more than once a day if Windows keeps the
    # "restart pending" flag set.
    background_mode_enabled: bool = False
    keep_pc_awake: bool = True
    # Reopen ConanOps if it stops while someone is signed in (keep_alive.py).
    keep_alive_enabled: bool = False
    admin_mode_enabled: bool = False
    # What happens when a mod stops a server from starting (ui/mod_recovery.py):
    # "wait" for the mod's author to fix it (keeps the world intact),
    # "start_without" the broken mod, or just "alert".
    mod_recovery_mode: str = "wait"
    # The dashboard tour (ui/tour.py) has been shown -- it runs once after
    # the first server is set up.
    tour_done: bool = False
    handle_update_restarts: bool = False
    update_restart_start: str = "04:00"
    update_restart_end: str = "06:00"
    last_update_restart: str = ""
    # Windows Update active-hours values from BEFORE ConanOps first changed
    # them (windows_update.read_active_hours()), so turning the feature
    # off or deleting ConanOps can put them back. Empty = never changed.
    original_active_hours: dict = field(default_factory=dict)

    # --- ConanOps' own updates (app_updates.py) ---
    auto_check_app_updates: bool = True
    auto_install_app_updates: bool = False
    last_app_update_check: float = 0.0  # time.time() of the last automatic check

    # Personal Steam Web API key (from https://steamcommunity.com/dev/apikey)
    # for Workshop search/browse (ui/workshop_browser_dialog.py) -- see
    # steam_workshop_api.py's module docstring for why this has to be a
    # per-person key ConanOps can't ship baked in. App-level, not
    # per-server, since it's tied to a Steam ACCOUNT, not a server.
    # Encrypted at rest (secrets_store.py) same as ServerConfig's
    # secret fields, though more simply -- if it ever fails to decrypt
    # (e.g. the config moved to a different Windows user account,
    # invalidating DPAPI) this just comes back empty rather than
    # ServerConfig's fields' more careful "keep the raw stored value
    # so a resave doesn't destroy it" handling; the worst case here is
    # re-entering a free key, not losing an RCON password.
    steam_api_key: str = ""
    # "YYYY-MM-DD" (UTC). A Workshop mod tagged Enhanced whose last
    # update is older than this is flagged as not updated for the
    # current patch -- see steam_workshop_api.py. Editable in App
    # Settings so the next game patch only needs a new date, not code.
    workshop_update_cutoff: str = "2026-09-01"

    # --- Dynamic DNS (DuckDNS) ---
    # For home-hosting behind a residential ISP without a static IP:
    # the public IP can change, silently breaking everyone's saved
    # server entry until someone happens to notice and re-share a new
    # address. duckdns_domain is the subdomain only (no ".duckdns.org"
    # suffix -- see dynamic_dns.py). duckdns_token is encrypted at
    # rest, same simplified approach as steam_api_key above (not
    # ServerConfig's more careful secret-field handling) -- worst case
    # on a decrypt failure is re-pasting a token, not losing an RCON
    # password.
    duckdns_domain: str = ""
    duckdns_token: str = ""

    # ---------------------------------------------------------------- io --
    @staticmethod
    def data_dir() -> str:
        """Where config.json, theme.json, and session history live --
        the same no-space root new servers' SteamCMD/install folders
        default to (see conanops_paths.no_space_root()'s docstring).
        Distinct from the app's own INSTALL directory (wherever the
        .exe/source lives, which could be anywhere) -- see
        ui/main_window.py's APP_INSTALL_DIR."""
        base = conanops_paths.no_space_root()
        os.makedirs(base, exist_ok=True)
        return base

    @staticmethod
    def _legacy_data_dir() -> str:
        """Earlier versions stored ConanOps' own data directly under
        the user's profile folder instead of data_dir()'s current
        default location. Used only as the last-resort candidate in
        _migration_candidates() below."""
        return os.path.join(os.path.expanduser("~"), "ConanOps")

    @staticmethod
    def _migration_candidates() -> list:
        """Every location data_dir() has EVER defaulted to, other than
        wherever it defaults to right now -- in order, most recent
        first, since that's the one most likely to actually hold a
        real, current setup. See _migrate_legacy_data_dir()."""
        return [conanops_paths.public_fallback_root(), AppConfig._legacy_data_dir()]

    @staticmethod
    def _migrate_legacy_data_dir() -> None:
        """One-time, best-effort migration: copies config.json,
        theme.json, and session history from wherever they used to
        live by default (see _migration_candidates()) into
        data_dir()'s CURRENT default, so upgrading doesn't silently
        orphan someone's existing setup (their servers, settings,
        everything) just because that default changed -- most
        recently, no_space_root() preferring app_install_dir() over
        public_fallback_root() when the app's own folder has no space
        in it (see conanops_paths.py). Without this, someone on that
        prior default would upgrade, launch, and find an empty server
        list -- not because anything was deleted, but because the app
        would just be looking in the wrong place and quietly creating
        a fresh config right there instead.

        Stops at the first candidate that actually has a config.json
        -- copying fields from two DIFFERENT old setups into one
        merged result would be far more confusing than just picking
        the most recent one. Never deletes any old copy -- purely
        additive, so a partial failure here never loses anything that
        wasn't already there; worst case, the app just falls back to
        creating a fresh config at the new location as it would on a
        genuinely first launch."""
        new = conanops_paths.no_space_root()
        new_norm = os.path.normcase(os.path.abspath(new))
        for legacy in AppConfig._migration_candidates():
            if os.path.normcase(os.path.abspath(legacy)) == new_norm:
                continue  # same folder on this platform -- nothing to migrate
            if not os.path.isfile(os.path.join(legacy, "config.json")):
                continue
            os.makedirs(new, exist_ok=True)
            # ONLY ConanOps' own small files. The old folder can also hold
            # whole server installs and SteamCMD (tens of GB each, under
            # per-server id folders) -- copying those would stall startup
            # for a long time and could fill the drive. They don't need to
            # move: config.json keeps pointing at them where they are.
            for name in _MIGRATED_NAMES:
                if not os.path.exists(os.path.join(legacy, name)):
                    continue
                src = os.path.join(legacy, name)
                dst = os.path.join(new, name)
                if os.path.exists(dst):
                    continue  # never overwrite something already at the new location
                try:
                    if os.path.isdir(src):
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)
                except OSError:
                    pass  # best-effort -- the old copy is untouched either way
            return  # found and migrated from the most recent candidate that had data

    @staticmethod
    def default_path() -> str:
        return os.path.join(AppConfig.data_dir(), "config.json")

    @classmethod
    def load(cls, path: Optional[str] = None) -> "AppConfig":
        path = path or cls.default_path()
        if not os.path.exists(path):
            # Cheap enough to just always attempt -- _migrate_legacy_data_dir()
            # itself checks each candidate for a config.json before
            # doing anything, so this is a no-op on a genuinely fresh
            # install (no prior candidate ever existed) just as
            # cheaply as the old single-candidate pre-check was.
            cls._migrate_legacy_data_dir()
        if not os.path.exists(path):
            cfg = cls()
            cfg.save(path)
            return cfg
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        servers = [ServerConfig.from_dict(s) for s in raw.get("servers", [])]
        cfg = cls(
            servers=servers,
            active_server_id=raw.get("active_server_id"),
            accent_color=raw.get("accent_color", "#c9752f"),
            app_lock_pin_hash=raw.get("app_lock_pin_hash", ""),
            app_lock_salt=raw.get("app_lock_salt", ""),
            web_control_enabled=raw.get("web_control_enabled", False),
            web_password_hash=raw.get("web_password_hash", ""),
            web_remote_enabled=raw.get("web_remote_enabled", False),
            start_with_windows=raw.get("start_with_windows", False),
            start_minimized_to_tray=raw.get("start_minimized_to_tray", False),
            background_mode_enabled=raw.get("background_mode_enabled", False),
            keep_pc_awake=raw.get("keep_pc_awake", True),
            keep_alive_enabled=raw.get("keep_alive_enabled", False),
            admin_mode_enabled=raw.get("admin_mode_enabled", False),
            mod_recovery_mode=raw.get("mod_recovery_mode", "wait"),
            tour_done=raw.get("tour_done", False),
            handle_update_restarts=raw.get("handle_update_restarts", False),
            update_restart_start=raw.get("update_restart_start", "04:00"),
            update_restart_end=raw.get("update_restart_end", "06:00"),
            last_update_restart=raw.get("last_update_restart", ""),
            original_active_hours=dict(raw.get("original_active_hours") or {}),
            auto_check_app_updates=raw.get("auto_check_app_updates", True),
            auto_install_app_updates=raw.get("auto_install_app_updates", False),
            last_app_update_check=float(raw.get("last_app_update_check", 0.0) or 0.0),
            duckdns_domain=raw.get("duckdns_domain", ""),
            workshop_update_cutoff=raw.get("workshop_update_cutoff", "2026-09-01"),
        )
        try:
            cfg.steam_api_key = secrets_store.unprotect(raw.get("steam_api_key", ""))
        except secrets_store.DecryptionError:
            cfg.steam_api_key = ""
        try:
            cfg.duckdns_token = secrets_store.unprotect(raw.get("duckdns_token", ""))
        except secrets_store.DecryptionError:
            cfg.duckdns_token = ""
        if not cfg.servers:
            return cfg
        if cfg.active_server_id not in {s.id for s in cfg.servers}:
            cfg.active_server_id = cfg.servers[0].id
        return cfg

    def save(self, path: Optional[str] = None) -> None:
        path = path or self.default_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        data = {
            "servers": [s.to_dict() for s in self.servers],
            "active_server_id": self.active_server_id,
            "accent_color": self.accent_color,
            "app_lock_pin_hash": self.app_lock_pin_hash,
            "app_lock_salt": self.app_lock_salt,
            "web_control_enabled": self.web_control_enabled,
            "web_password_hash": self.web_password_hash,
            "web_remote_enabled": self.web_remote_enabled,
            "start_with_windows": self.start_with_windows,
            "start_minimized_to_tray": self.start_minimized_to_tray,
            "background_mode_enabled": self.background_mode_enabled,
            "keep_pc_awake": self.keep_pc_awake,
            "keep_alive_enabled": self.keep_alive_enabled,
            "admin_mode_enabled": self.admin_mode_enabled,
            "mod_recovery_mode": self.mod_recovery_mode,
            "tour_done": self.tour_done,
            "handle_update_restarts": self.handle_update_restarts,
            "update_restart_start": self.update_restart_start,
            "update_restart_end": self.update_restart_end,
            "last_update_restart": self.last_update_restart,
            "original_active_hours": self.original_active_hours,
            "auto_check_app_updates": self.auto_check_app_updates,
            "auto_install_app_updates": self.auto_install_app_updates,
            "last_app_update_check": self.last_app_update_check,
            "steam_api_key": secrets_store.protect(self.steam_api_key),
            "duckdns_domain": self.duckdns_domain,
            "workshop_update_cutoff": self.workshop_update_cutoff,
            "duckdns_token": secrets_store.protect(self.duckdns_token),
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)  # atomic-ish on same filesystem

    # ------------------------------------------------------------ helpers --
    def get_active(self) -> Optional[ServerConfig]:
        for s in self.servers:
            if s.id == self.active_server_id:
                return s
        return self.servers[0] if self.servers else None

    def add_server(self, name: str = "New Server") -> Optional[ServerConfig]:
        if len(self.servers) >= MAX_SERVERS:
            return None
        n = 1
        existing = {s.name for s in self.servers}
        candidate = name
        while candidate in existing:
            n += 1
            candidate = f"{name} {n}"
        s = ServerConfig(name=candidate)
        s.ensure_gameplay_defaults()
        # RCON on by default for new servers: without it, ConanOps can't
        # ask the server to save before stopping it, so every stop,
        # restart and update is a hard kill. A random password (never
        # shown unless the person opens RCON & Alerts) and the first
        # RCON port no other server uses.
        s.rcon_enabled = True
        s.rcon_password = _secrets.token_urlsafe(12)
        taken = self.used_ports()
        port = 25575
        while port in taken:
            port += 1
        s.rcon_port = port
        self.servers.append(s)
        self.active_server_id = s.id
        return s

    def remove_server(self, server_id: str) -> None:
        self.servers = [s for s in self.servers if s.id != server_id]
        if self.active_server_id == server_id:
            self.active_server_id = self.servers[0].id if self.servers else None

    def used_ports(self, exclude_id: Optional[str] = None) -> set:
        """Every port a configured server (other than exclude_id) has
        claimed. Includes game_port + 1 alongside game_port itself --
        Conan Exiles' dedicated server binds a second UDP port right
        above the game port for its own networking, not just the ones
        ConanOps' Network & Ports page shows, so a port-conflict check
        that only looked at game_port/query_port could still let two
        servers collide on that extra port."""
        ports = set()
        for s in self.servers:
            if s.id == exclude_id:
                continue
            ports.add(s.game_port)
            ports.add(s.game_port + 1)
            ports.add(s.query_port)
            if s.rcon_enabled:
                ports.add(s.rcon_port)  # two servers on one RCON port: the second's RCON never binds
        return ports

    def used_server_names(self, exclude_id: Optional[str] = None) -> set:
        """Every server.name currently claimed by another configured
        server. Names are what players see in the server browser and
        what the sidebar shows, so the setup wizard keeps them unique
        (it compares case-insensitively). Firewall rules and router
        forwards are keyed by server id, not by name.
        """
        return {s.name for s in self.servers if s.id != exclude_id}

    def used_install_dirs(self, exclude_id: Optional[str] = None) -> set:
        """Every install_dir currently claimed by another configured
        server, normalized for comparison. Two servers accidentally
        sharing an install folder would make backups (they zip that
        folder's Saved/ subfolder) and process-matching
        (process_manager.find_running_pid matches on exe path under
        install_dir) collide with each other, and worse, both servers'
        live world save data lives in install_dir/ConanSandbox/Saved --
        sharing it would let two running servers corrupt each other's
        save. Used by the setup wizard to warn before that can happen.

        Deliberately does NOT include steamcmd_dir: that folder just
        holds the SteamCMD tool itself and its Workshop download cache
        (keyed by workshop item id, not by server -- see
        mod_manager.workshop_pak_path), neither of which is per-server
        state the way install_dir is. Sharing it across servers is
        safe and actually useful (mods already downloaded for one
        server don't need re-downloading for another) -- see
        default_steamcmd_dir()."""
        dirs = set()
        for s in self.servers:
            if s.id == exclude_id:
                continue
            if s.install_dir:
                dirs.add(os.path.normcase(os.path.abspath(s.install_dir)))
        return dirs

    def default_steamcmd_dir(self, exclude_id: Optional[str] = None) -> str:
        """The steamcmd_dir of an already-configured server, if any --
        used by the setup wizard to pre-fill a new server's SteamCMD
        folder field with an existing one instead of suggesting a
        fresh per-server folder. Sharing it is safe (see
        used_install_dirs()'s docstring) and means a second, third,
        etc. server doesn't need its own separate SteamCMD download or
        a re-download of any Workshop mods already fetched for another
        server. Returns "" if no configured server has one set yet."""
        for s in self.servers:
            if s.id != exclude_id and s.steamcmd_dir:
                return s.steamcmd_dir
        return ""

    # ------------------------------------------------------- app lock --
    @property
    def app_lock_enabled(self) -> bool:
        return bool(self.app_lock_pin_hash)

    def set_app_lock_pin(self, pin: str) -> None:
        salt = os.urandom(16).hex()
        self.app_lock_salt = salt
        self.app_lock_pin_hash = _hash_pin(pin, salt)

    def clear_app_lock_pin(self) -> None:
        self.app_lock_pin_hash = ""
        self.app_lock_salt = ""

    def verify_app_lock_pin(self, pin: str) -> bool:
        """True if no PIN is set at all (nothing to unlock) or the
        given PIN matches."""
        if not self.app_lock_pin_hash:
            return True
        candidate = _hash_pin(pin, self.app_lock_salt)
        # A plain == on the hash strings would short-circuit on the
        # first differing character, so how long verification takes
        # leaks how many leading hex digits of the hash a guess got
        # right -- a timing side-channel. hmac.compare_digest runs in
        # time that depends only on the strings' length, not their
        # content, which closes that off. This isn't hardened against
        # a determined local attacker either way (see the field's own
        # docstring above), but it's a one-line fix with no downside.
        return hmac.compare_digest(candidate, self.app_lock_pin_hash)
