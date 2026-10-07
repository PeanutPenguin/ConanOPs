"""
Reads and writes the app's own settings pages for the web version.

Instead of a second copy of every settings form (which would drift from
the app), the web version describes each page's registered fields --
label, type, limits, choices, current value -- straight from the page's
widgets, and saves by putting the values into those same widgets and
running the page's own Apply. Validation, conflict checks and what
happens on save are therefore exactly the app's.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from PySide6.QtWidgets import (
    QAbstractSlider, QCheckBox, QComboBox, QDoubleSpinBox, QLabel, QLayout, QLineEdit, QSpinBox, QTimeEdit,
    QWidget,
)

from webui.bridge import WebActionError


def _humanize(key: str) -> str:
    key = key.strip("_")
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", key).replace("_", " ").split()
    return " ".join(words).capitalize() if words else key


def _find_layout_of(widget: QWidget) -> Optional[QLayout]:
    parent = widget.parentWidget()
    if parent is None or parent.layout() is None:
        return None

    def search(layout: QLayout) -> Optional[QLayout]:
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget() is widget:
                return layout
            if item.layout() is not None:
                found = search(item.layout())
                if found is not None:
                    return found
        return None
    return search(parent.layout())


def _preceding_label(widget: QWidget) -> Optional[QLabel]:
    layout = _find_layout_of(widget)
    if layout is None:
        return None
    idx = next((i for i in range(layout.count()) if layout.itemAt(i).widget() is widget), -1)
    for i in range(idx - 1, -1, -1):
        w = layout.itemAt(i).widget()
        if isinstance(w, QLabel) and w.text().strip():
            return w
    return None


def _label_widget(widget: QWidget) -> Optional[QLabel]:
    """The QLabel naming a field: the one before it in its layout -- or,
    when the field sits inside a small wrapper with a button (password +
    Show, address + Auto-detect), the one before that wrapper."""
    w = widget
    for _ in range(3):
        lbl = _preceding_label(w)
        if lbl is not None:
            return lbl
        parent = w.parentWidget()
        if parent is None or parent.layout() is None or parent.layout().count() > 4:
            break
        w = parent
    return None


def _clean(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _label_for(widget: QWidget) -> str:
    if isinstance(widget, QCheckBox) and widget.text().strip():
        return widget.text().strip()
    name = widget.accessibleName().strip()
    if name:
        return name
    lbl = _label_widget(widget)
    return _clean(lbl.text()).rstrip(":") if lbl is not None else ""


def _help_for(widget: QWidget) -> str:
    text = _clean(widget.toolTip())
    if not text:
        lbl = _label_widget(widget)
        text = _clean(lbl.toolTip()) if lbl is not None else ""
    return text


def _hhmm(value: Any) -> str:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else str(value or "")


def is_secret(name: str, widget: QWidget) -> bool:
    return isinstance(widget, QLineEdit) and (
        widget.echoMode() != QLineEdit.Normal or bool(re.search(r"password|token|webhook|url", name, re.I)))


def describe_field(name: str, widget: QWidget, getter, slider_scale=None) -> Dict[str, Any]:
    f: Dict[str, Any] = {
        "key": name,
        "label": _label_for(widget) or _humanize(name),
        "help": _help_for(widget),
        "enabled": widget.isEnabled(),
    }
    value = getter(widget)
    if isinstance(widget, QCheckBox):
        f.update(type="bool", value=bool(value))
    elif isinstance(widget, QComboBox):
        f.update(type="choice", value=value,
                 choices=[{"label": widget.itemText(i),
                           "value": widget.itemData(i) if widget.itemData(i) is not None else widget.itemText(i)}
                          for i in range(widget.count())])
    elif isinstance(widget, QDoubleSpinBox):
        f.update(type="float", value=value, min=widget.minimum(), max=widget.maximum(),
                 step=widget.singleStep(), decimals=widget.decimals(), suffix=widget.suffix().strip())
    elif isinstance(widget, QSpinBox):
        f.update(type="int", value=value, min=widget.minimum(), max=widget.maximum(),
                 step=widget.singleStep(), suffix=widget.suffix().strip())
    elif isinstance(widget, QAbstractSlider):
        # Generic pages scale float sliders by 10**decimals (page._sliders).
        scale = float(slider_scale or 1)
        decimals = max(0, len(str(int(scale))) - 1)
        f.update(type="float", value=value, min=widget.minimum() / scale, max=widget.maximum() / scale,
                 step=widget.singleStep() / scale, decimals=decimals, suffix="")
    elif isinstance(widget, QTimeEdit):
        f.update(type="time", value=value)
    elif isinstance(widget, QLineEdit):
        if is_secret(name, widget):
            # Never sent to the browser: it only learns whether one is set.
            # Typing a new one replaces it; leaving it alone keeps it.
            f.update(type="secret", value="", has_value=bool(str(value or "")),
                     placeholder=widget.placeholderText())
        else:
            f.update(type="text", value=value, placeholder=widget.placeholderText())
            if re.fullmatch(r"\s*\d{1,2}:\d{2}\s*", str(value or "")) or "HH:MM" in widget.placeholderText().upper():
                f.update(type="time", value=_hhmm(value))
    else:
        f.update(type="text", value=value if isinstance(value, (str, int, float, bool)) else str(value))
    return f


def page_error(page) -> str:
    """The visible error text on a page (labels named ErrorText)."""
    errors = [lbl.text().strip() for lbl in page.findChildren(QLabel)
              if lbl.objectName() == "ErrorText" and not lbl.isHidden() and lbl.text().strip()]
    return "\n".join(dict.fromkeys(errors))


def _enabled_when(page, disabled: List[str]) -> Dict[str, Dict[str, Any]]:
    """For fields greyed out until a switch on the same page is ticked
    (scheduled restart times, say): {field: {switch: value}}."""
    out: Dict[str, Dict[str, Any]] = {}
    if not disabled:
        return out
    for name, w in page._fields.items():
        if not isinstance(w, QCheckBox):
            continue
        original = w.isChecked()
        w.setChecked(not original)
        for d in disabled:
            if d not in out and page._fields[d].isEnabled():
                out[d] = {name: not original}
        w.setChecked(original)
    return out


def describe_page(key: str, title: str, page) -> Dict[str, Any]:
    sliders = getattr(page, "_sliders", {}) or {}
    fields = [describe_field(name, w, page._getters[name], (sliders.get(name) or (None, None))[1])
              for name, w in page._fields.items()]
    deps = _enabled_when(page, [f["key"] for f in fields if not f["enabled"]])
    for f in fields:
        if f["key"] in deps:
            f["enabled_when"] = deps[f["key"]]
    note = ""
    if "Restart" in page.apply_btn.text():
        note = "Saved now; takes effect the next time the server restarts."
    elif key == "alerts":
        note = "RCON changes take effect the next time the server restarts; alerts change right away."
    return {"key": key, "title": title, "fields": fields, "apply_label": page.apply_btn.text(), "note": note}


def _coerce(widget: QWidget, value: Any) -> Any:
    if isinstance(widget, QCheckBox):
        return bool(value)
    if isinstance(widget, QSpinBox):
        return int(value)
    if isinstance(widget, (QDoubleSpinBox, QAbstractSlider)):
        return float(value)
    if isinstance(widget, QComboBox):
        idx = widget.findData(value)
        if idx < 0:
            idx = widget.findText(str(value))
        if idx < 0:
            raise ValueError(f"{value!r} isn't one of the choices")
        return value
    return "" if value is None else str(value)


def put_values(page, values: Dict[str, Any]) -> None:
    """Puts `values` into a page's widgets (unknown keys are ignored).
    Raises WebActionError for a value that doesn't fit the field."""
    for name, value in values.items():
        if name not in page._fields:
            continue
        widget = page._fields[name]
        try:
            v = _coerce(widget, value)
            if isinstance(widget, QLineEdit) and widget.inputMask() == "" and _is_time_field(page, name, widget):
                v = _hhmm(v)
            page._setters[name](widget, v)
        except (TypeError, ValueError) as e:
            label = _label_for(widget) or _humanize(name)
            why = "enter a number" if "literal" in str(e) or "float" in str(e) else str(e)
            raise WebActionError(f"{label}: {why}.") from e


def _is_time_field(page, name: str, widget: QLineEdit) -> bool:
    return "HH:MM" in widget.placeholderText().upper() or name in ("restart_start", "restart_end")
