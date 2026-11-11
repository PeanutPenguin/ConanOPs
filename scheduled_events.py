"""
Scheduled events for a server: what each one is, when it's on, and checking
what people type in. The window acts on them (ui/window/events.py).

Two kinds:

- "settings": a weekly (or daily) window when some settings change, such as
  double XP from Friday evening to Sunday night. The changes are written
  when it starts and put back when it ends, each time with a restart (and
  in-game warnings if players are on), since the game only reads its
  settings when it starts. It can change the message of the day too.
- "message": a message posted in game chat at set times on set days, once
  or repeating (every 2 hours, say).

Each event is a plain dict kept on the server (ServerConfig.scheduled_events)
so it's saved with the rest of the config. Its "state" holds what the app
needs to finish what it started after a restart of ConanOps itself.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import ini_field_specs

KIND_SETTINGS = "settings"
KIND_MESSAGE = "message"
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MOTD_KEY = "ServerMessageOfTheDay"
MAX_EVENTS = 30
MAX_MESSAGE = 400
# A message counts as on time if ConanOps notices within this long.
MESSAGE_GRACE_MINUTES = 10
REPEAT_CHOICES = (0, 15, 30, 60, 120, 180, 240, 360)

# Settings an event mustn't change: passwords, and ConanOps' own notes.
_NOT_FOR_EVENTS = {"AdminPassword", "ServerPassword", MOTD_KEY}


def event_settings() -> List[ini_field_specs.FieldSpec]:
    """The settings an event can change, as in the settings pages."""
    out = []
    seen = set()
    for _key, _label, specs in ini_field_specs.CATEGORIES:
        for spec in specs:
            if spec.key.startswith("__") or spec.key in _NOT_FOR_EVENTS or spec.key in seen:
                continue
            seen.add(spec.key)
            out.append(spec)
    return out


def _spec(key: str) -> Optional[ini_field_specs.FieldSpec]:
    spec = ini_field_specs.ALL_FIELDS_BY_KEY.get(key)
    if spec is None or spec.key.startswith("__") or spec.key in _NOT_FOR_EVENTS:
        return None
    return spec


def _hhmm(text: Any, what: str) -> str:
    try:
        t = datetime.strptime(str(text or "").strip(), "%H:%M")
    except ValueError:
        raise ValueError(f"{what} must be a time like 18:00.") from None
    return f"{t.hour:02d}:{t.minute:02d}"


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _day(value: Any, what: str) -> int:
    try:
        d = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Pick a day for {what}.") from None
    if not 0 <= d <= 6:
        raise ValueError(f"Pick a day for {what}.")
    return d


def coerce_value(spec: ini_field_specs.FieldSpec, value: Any) -> Any:
    """A value typed for an event, checked against the setting's own limits."""
    if spec.kind == "bool":
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "1", "yes", "on"):
                return True
            if v in ("false", "0", "no", "off"):
                return False
            raise ValueError(f"{spec.label}: choose on or off.")
        return bool(value)
    if spec.kind in ("int", "float"):
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{spec.label}: enter a number.") from None
        if spec.min is not None and num < spec.min:
            raise ValueError(f"{spec.label}: the lowest it goes is {spec.min:g}.")
        if spec.max is not None and num > spec.max:
            raise ValueError(f"{spec.label}: the highest it goes is {spec.max:g}.")
        if spec.kind == "int":
            if num != int(num):
                raise ValueError(f"{spec.label}: enter a whole number.")
            return int(num)
        return round(num, max(spec.decimals, 2))
    if spec.kind == "choice":
        allowed = [v for _label, v in (spec.choices or [])]
        for v in allowed:
            if str(v) == str(value):
                return v
        raise ValueError(f"{spec.label}: that isn't one of its choices.")
    text = str(value if value is not None else "")
    if "\n" in text or "\r" in text:
        raise ValueError(f"{spec.label}: keep it to one line.")
    return text[:500]


def new_event(kind: str = KIND_SETTINGS) -> Dict[str, Any]:
    if kind == KIND_MESSAGE:
        return {"id": uuid.uuid4().hex[:12], "name": "", "enabled": True, "kind": KIND_MESSAGE,
                "days": list(range(7)), "time": "20:00", "repeat_minutes": 0, "text": "", "state": {}}
    return {"id": uuid.uuid4().hex[:12], "name": "", "enabled": True, "kind": KIND_SETTINGS, "daily": False,
            "start_day": 4, "start": "18:00", "end_day": 6, "end": "23:00", "changes": {}, "motd": "",
            "state": {}}


def clean_event(raw: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A checked copy of an event someone entered; ValueError says what's wrong.
    The id and state come from `existing` when editing."""
    if not isinstance(raw, dict):
        raise ValueError("That event couldn't be read.")
    kind = raw.get("kind", existing.get("kind") if existing else KIND_SETTINGS)
    if kind not in (KIND_SETTINGS, KIND_MESSAGE):
        raise ValueError("Unknown kind of event.")
    name = str(raw.get("name") or "").strip()[:60]
    if not name:
        raise ValueError("Give the event a name.")
    ev = new_event(kind)
    if existing:
        ev["id"] = existing.get("id") or ev["id"]
        ev["state"] = dict(existing.get("state") or {})
    ev["name"] = name
    ev["enabled"] = bool(raw.get("enabled", True))
    if kind == KIND_MESSAGE:
        days = raw.get("days")
        if not isinstance(days, (list, tuple)):
            raise ValueError("Pick at least one day.")
        days = sorted({_day(d, "the message") for d in days})
        if not days:
            raise ValueError("Pick at least one day.")
        ev["days"] = days
        ev["time"] = _hhmm(raw.get("time"), "The time")
        try:
            repeat = int(raw.get("repeat_minutes") or 0)
        except (TypeError, ValueError):
            raise ValueError("Choose how often it repeats.") from None
        if repeat not in REPEAT_CHOICES:
            raise ValueError("Choose how often it repeats.")
        ev["repeat_minutes"] = repeat
        text = " ".join(str(raw.get("text") or "").split())
        if not text:
            raise ValueError("Write the message players will see.")
        if len(text) > MAX_MESSAGE:
            raise ValueError(f"Keep the message under {MAX_MESSAGE} characters.")
        ev["text"] = text
        return ev

    ev["daily"] = bool(raw.get("daily", False))
    ev["start"] = _hhmm(raw.get("start"), "The start time")
    ev["end"] = _hhmm(raw.get("end"), "The end time")
    if ev["daily"]:
        ev["start_day"], ev["end_day"] = 0, 0
        if ev["start"] == ev["end"]:
            raise ValueError("The start and end times are the same, so the event would never be on.")
    else:
        ev["start_day"] = _day(raw.get("start_day"), "the start")
        ev["end_day"] = _day(raw.get("end_day"), "the end")
        if (ev["start_day"], ev["start"]) == (ev["end_day"], ev["end"]):
            raise ValueError("The event starts and ends at the same moment.")
    changes_raw = raw.get("changes") or {}
    if not isinstance(changes_raw, dict):
        raise ValueError("The settings to change couldn't be read.")
    changes = {}
    for key, value in changes_raw.items():
        spec = _spec(str(key))
        if spec is None:
            raise ValueError(f"{key} can't be changed by an event.")
        changes[spec.key] = coerce_value(spec, value)
    motd = " ".join(str(raw.get("motd") or "").split())[:500]
    if not changes and not motd:
        raise ValueError("Choose at least one setting to change, or a message of the day.")
    ev["changes"] = changes
    ev["motd"] = motd
    return ev


def load_events(raw_list) -> List[Dict[str, Any]]:
    """Saved events, dropping any that no longer make sense (but keeping
    the state of one that's on, so its changes can still be put back)."""
    out = []
    for raw in raw_list or []:
        try:
            ev = clean_event(raw, existing=raw)
        except ValueError:
            state = (raw or {}).get("state") if isinstance(raw, dict) else None
            if not (isinstance(state, dict) and state.get("active")):
                continue
            ev = dict(raw)
            ev["enabled"] = False  # broken: only ever put back
        out.append(ev)
    return out[:MAX_EVENTS]


def window_on(ev: Dict[str, Any], now: datetime) -> bool:
    """Is a settings event's window on at `now`?"""
    if ev.get("kind") != KIND_SETTINGS:
        return False
    s, e = _minutes(ev["start"]), _minutes(ev["end"])
    if ev.get("daily"):
        m = now.hour * 60 + now.minute
    else:
        s += ev["start_day"] * 1440
        e += ev["end_day"] * 1440
        m = now.weekday() * 1440 + now.hour * 60 + now.minute
    return s <= m < e if s < e else (m >= s or m < e)


def message_due(ev: Dict[str, Any], now: datetime) -> str:
    """The key of a message time that has just come round and hasn't been
    posted yet (""): one of the day's times, within the last few minutes."""
    if ev.get("kind") != KIND_MESSAGE:
        return ""
    start = _minutes(ev["time"])
    step = int(ev.get("repeat_minutes") or 0)
    for back in (0, 1):  # also yesterday, for a time just before midnight
        day = (now - timedelta(days=back)).date()
        if day.weekday() not in ev.get("days", []):
            continue
        times = [start] if step <= 0 else list(range(start, 1440, step))
        for t in reversed(times):
            when = datetime.combine(day, datetime.min.time()) + timedelta(minutes=t)
            if when <= now < when + timedelta(minutes=MESSAGE_GRACE_MINUTES):
                key = when.strftime("%Y-%m-%d %H:%M")
                return "" if key == (ev.get("state") or {}).get("last_sent") else key
    return ""


def _value_text(spec: Optional[ini_field_specs.FieldSpec], value: Any) -> str:
    if spec is not None and spec.kind == "bool":
        return "on" if value else "off"
    if spec is not None and spec.kind == "choice":
        for label, v in spec.choices or []:
            if str(v) == str(value):
                return label
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def describe_when(ev: Dict[str, Any]) -> str:
    if ev.get("kind") == KIND_MESSAGE:
        days = ev.get("days", [])
        if len(days) == 7:
            day_text = "Every day"
        elif days == [5, 6]:
            day_text = "Weekends"
        elif days == [0, 1, 2, 3, 4]:
            day_text = "Weekdays"
        else:
            day_text = ", ".join(DAY_NAMES[d] for d in days)
        step = int(ev.get("repeat_minutes") or 0)
        if step:
            every = f"{step // 60} hours" if step % 60 == 0 and step > 60 else (
                "hour" if step == 60 else f"{step} minutes")
            return f"{day_text}, every {every} from {ev['time']}"
        return f"{day_text} at {ev['time']}"
    if ev.get("daily"):
        return f"Every day {ev['start']}–{ev['end']}"
    return f"{DAY_NAMES[ev['start_day']]} {ev['start']} – {DAY_NAMES[ev['end_day']]} {ev['end']}"


def describe_what(ev: Dict[str, Any]) -> str:
    if ev.get("kind") == KIND_MESSAGE:
        return f"Posts: “{ev.get('text', '')}”"
    parts = []
    for key, value in (ev.get("changes") or {}).items():
        spec = ini_field_specs.ALL_FIELDS_BY_KEY.get(key)
        parts.append(f"{spec.label if spec else key} {_value_text(spec, value)}")
    if ev.get("motd"):
        parts.append("message of the day changed")
    return ", ".join(parts)


def plan_start(ev: Dict[str, Any], gameplay: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(values to write, the values they replace) for a settings event."""
    wanted = dict(ev.get("changes") or {})
    if ev.get("motd"):
        wanted[MOTD_KEY] = ev["motd"]
    originals = {}
    for key in wanted:
        spec = ini_field_specs.ALL_FIELDS_BY_KEY.get(key)
        originals[key] = gameplay.get(key, spec.default if spec else "")
    return wanted, originals


def plan_end(state: Dict[str, Any], gameplay: Dict[str, Any]) -> Dict[str, Any]:
    """The values to put back when the event ends: only those still as the
    event left them, so a change made by hand during the event is kept."""
    applied = state.get("applied") or {}
    originals = state.get("originals") or {}
    return {k: originals[k] for k, v in applied.items() if k in originals and gameplay.get(k) == v}
