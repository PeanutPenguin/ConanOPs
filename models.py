"""
ConanOps data models: ServerConfig (one server's settings) and AppConfig
(the server list plus app-wide preferences, saved as JSON).
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

# PBKDF2 slows offline brute-forcing of a short PIN from a stolen config.json.
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
    # Keyed by Conan ini key name (or "__"-prefixed ConanOps-only keys).
    # See ini_field_specs.py; defaults are backfilled on load.
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
    # discord_status_message_id is persisted so the same message keeps
    # being edited across ConanOps restarts.
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

    # --- Scheduler runtime state (persisted so it survives app restarts) ---
    last_backup_at: str = ""       # ISO timestamp of the last scheduled/auto backup
    last_restart_date: str = ""    # "YYYY-MM-DD" -- the last date a scheduled restart fired

    # --- Auto-resume / watchdog ---
    # The person's intent (Start sets True, Stop sets False), not whether
    # it's running. Auto-resume and the crash watchdog use it, so a crash
    # keeps it True while a deliberate Stop stays down through a reboot.
    desired_running: bool = False
    # Reason a game update failed after stopping the server. While set,
    # auto-resume and the watchdog leave it stopped (players on the new build
    # can't join). Cleared by a successful update or a manual Start.
    update_hold: str = ""
    # Automatic mod-break recovery (ui/mod_recovery.py) waiting for a fix:
    # {"culprits": [workshop ids], "since": iso time, "updated": {id: Workshop
    # time_updated when found}}. Empty when nothing is being waited on.
    mod_recovery: dict = field(default_factory=dict)

    # Encrypted at rest with DPAPI (secrets_store.py).
    _SECRET_FIELDS = ("password", "rcon_password", "webhook_discord_url", "webhook_ntfy_url")
    # Nested in `gameplay`, so handled separately; grants in-game admin access.
    _SECRET_GAMEPLAY_KEY = "AdminPassword"

    def __post_init__(self) -> None:
        # Not a dataclass field, so it never reaches JSON: {field: raw stored
        # string} for secrets that failed to decrypt on load.
        self._undecryptable: dict = {}

    def to_dict(self) -> dict:
        d = asdict(self)
        for f in self._SECRET_FIELDS:
            if f in self._undecryptable and not getattr(self, f):
                # Failed to decrypt on load and still unset: write the raw
                # stored value back, so the real secret isn't overwritten.
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
        """Backfill missing `gameplay` keys from ini_field_specs."""
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
    # A lightweight single-PIN gate against casual access, not real auth;
    # anyone who can edit config.json can remove it.
    app_lock_pin_hash: str = ""
    app_lock_salt: str = ""

    web_control_enabled: bool = False
    # PBKDF2 hash of the web version's password (webui/auth.py).
    web_password_hash: str = ""
    web_remote_enabled: bool = False

    # --- Start with Windows ---
    # start_with_windows mirrors whether startup_registration added the Run
    # key. start_minimized_to_tray applies on every launch, since ConanOps
    # can't tell how it was started.
    start_with_windows: bool = False
    start_minimized_to_tray: bool = False

    # --- Unattended operation ---
    # background_mode_enabled mirrors whether background_mode.register()
    # succeeded. keep_pc_awake blocks idle sleep while a server runs.
    # handle_update_restarts does Windows Update restarts in the update window;
    # last_update_restart limits that to once a day.
    background_mode_enabled: bool = False
    keep_pc_awake: bool = True
    # Reopen ConanOps if it stops while someone is signed in (keep_alive.py).
    keep_alive_enabled: bool = False
    admin_mode_enabled: bool = False
    # On a mod break (ui/mod_recovery.py): "wait" for a fix, "start_without"
    # the mod, or just "alert".
    mod_recovery_mode: str = "wait"
    # The one-time dashboard tour (ui/tour.py) has been shown.
    tour_done: bool = False
    handle_update_restarts: bool = False
    update_restart_start: str = "04:00"
    update_restart_end: str = "06:00"
    last_update_restart: str = ""
    # Windows Update active hours before ConanOps changed them, so they can be
    # restored. Empty = never changed.
    original_active_hours: dict = field(default_factory=dict)

    # --- ConanOps' own updates (app_updates.py) ---
    auto_check_app_updates: bool = True
    auto_install_app_updates: bool = False
    last_app_update_check: float = 0.0  # time.time() of the last automatic check

    # Personal Steam Web API key for Workshop search (see steam_workshop_api.py).
    # Encrypted at rest; on a decrypt failure it simply comes back empty.
    steam_api_key: str = ""
    # "YYYY-MM-DD" (UTC). Enhanced-tagged mods updated before this are flagged
    # as not updated for the current patch.
    workshop_update_cutoff: str = "2026-09-01"

    # --- Dynamic DNS (DuckDNS) ---
    # For home hosting without a static IP. duckdns_domain is the subdomain
    # only. duckdns_token is encrypted; a decrypt failure just blanks it.
    duckdns_domain: str = ""
    duckdns_token: str = ""
    # App-wide alert links (secrets). load() migrates the old per-server ones.
    alert_discord_url: str = ""
    alert_ntfy_url: str = ""
    discord_status_enabled: bool = False
    # Discord bot for !status / !restart (the token is a secret).
    discord_bot_token: str = ""
    discord_bot_channel_id: str = ""
    discord_bot_admin_ids: str = ""   # Discord user IDs allowed to !restart, comma-separated

    # ---------------------------------------------------------------- io --
    @staticmethod
    def data_dir() -> str:
        """Folder for config.json, theme.json and session history
        (conanops_paths.no_space_root()); not the app's install dir."""
        base = conanops_paths.no_space_root()
        os.makedirs(base, exist_ok=True)
        return base

    @staticmethod
    def _legacy_data_dir() -> str:
        """Old default location under the user's profile."""
        return os.path.join(os.path.expanduser("~"), "ConanOps")

    @staticmethod
    def _migration_candidates() -> list:
        """Previous default data locations, most recent first."""
        return [conanops_paths.public_fallback_root(), AppConfig._legacy_data_dir()]

    @staticmethod
    def _migrate_legacy_data_dir() -> None:
        """Best-effort copy of config, theme and sessions from the most recent
        old default location that has a config.json, so an upgrade doesn't
        show an empty server list. Never overwrites or deletes anything."""
        new = conanops_paths.no_space_root()
        new_norm = os.path.normcase(os.path.abspath(new))
        for legacy in AppConfig._migration_candidates():
            if os.path.normcase(os.path.abspath(legacy)) == new_norm:
                continue  # same folder on this platform
            if not os.path.isfile(os.path.join(legacy, "config.json")):
                continue
            os.makedirs(new, exist_ok=True)
            # Only ConanOps' own small files. Server installs stay where they
            # are (config.json still points at them); copying would take ages.
            for name in _MIGRATED_NAMES:
                if not os.path.exists(os.path.join(legacy, name)):
                    continue
                src = os.path.join(legacy, name)
                dst = os.path.join(new, name)
                if os.path.exists(dst):
                    continue  # never overwrite
                try:
                    if os.path.isdir(src):
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)
                except OSError:
                    pass  # best-effort
            return

    @staticmethod
    def default_path() -> str:
        return os.path.join(AppConfig.data_dir(), "config.json")

    @classmethod
    def load(cls, path: Optional[str] = None) -> "AppConfig":
        path = path or cls.default_path()
        if not os.path.exists(path):
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
        try:
            cfg.discord_bot_token = secrets_store.unprotect(raw.get("discord_bot_token", ""))
        except secrets_store.DecryptionError:
            cfg.discord_bot_token = ""
        cfg.discord_bot_channel_id = str(raw.get("discord_bot_channel_id", "") or "")
        cfg.discord_bot_admin_ids = str(raw.get("discord_bot_admin_ids", "") or "")
        if "alert_discord_url" in raw or "alert_ntfy_url" in raw:
            for attr in ("alert_discord_url", "alert_ntfy_url"):
                try:
                    setattr(cfg, attr, secrets_store.unprotect(raw.get(attr, "")))
                except secrets_store.DecryptionError:
                    setattr(cfg, attr, "")
            cfg.discord_status_enabled = bool(raw.get("discord_status_enabled", False))
        else:
            # Older configs had per-server alert links; use the first server's.
            cfg.alert_discord_url = next((x.webhook_discord_url for x in servers if x.webhook_discord_url), "")
            cfg.alert_ntfy_url = next((x.webhook_ntfy_url for x in servers if x.webhook_ntfy_url), "")
            cfg.discord_status_enabled = any(x.discord_status_enabled and x.webhook_discord_url for x in servers)
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
            "alert_discord_url": secrets_store.protect(self.alert_discord_url),
            "alert_ntfy_url": secrets_store.protect(self.alert_ntfy_url),
            "discord_status_enabled": self.discord_status_enabled,
            "discord_bot_token": secrets_store.protect(self.discord_bot_token),
            "discord_bot_channel_id": self.discord_bot_channel_id,
            "discord_bot_admin_ids": self.discord_bot_admin_ids,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)

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
        # RCON on by default: without it ConanOps can't ask the server to save
        # before stopping, so every stop would be a hard kill.
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
        """Ports claimed by other servers, including game_port + 1, which
        Conan also binds."""
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
        """Names claimed by other servers (shown to players, so kept unique)."""
        return {s.name for s in self.servers if s.id != exclude_id}

    def used_install_dirs(self, exclude_id: Optional[str] = None) -> set:
        """Normalized install dirs claimed by other servers. Sharing one would
        let two servers corrupt each other's world save. steamcmd_dir is
        excluded on purpose: sharing it is safe and saves re-downloads."""
        dirs = set()
        for s in self.servers:
            if s.id == exclude_id:
                continue
            if s.install_dir:
                dirs.add(os.path.normcase(os.path.abspath(s.install_dir)))
        return dirs

    def default_steamcmd_dir(self, exclude_id: Optional[str] = None) -> str:
        """An existing server's steamcmd_dir to reuse for a new one, or ""."""
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
        """True if no PIN is set or the PIN matches."""
        if not self.app_lock_pin_hash:
            return True
        candidate = _hash_pin(pin, self.app_lock_salt)
        # Constant-time compare avoids a timing side-channel.
        return hmac.compare_digest(candidate, self.app_lock_pin_hash)
