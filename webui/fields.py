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


def _label_for(widget: QWidget) -> str:
    if isinstance(widget, QCheckBox) and widget.text().strip():
        return widget.text().strip()
    name = widget.accessibleName().strip()
    if name:
        return name
    layout = _find_layout_of(widget)
    if layout is not None:
        idx = next((i for i in range(layout.count()) if layout.itemAt(i).widget() is widget), -1)
        for i in range(idx - 1, -1, -1):
            w = layout.itemAt(i).widget()
            if isinstance(w, QLabel) and w.text().strip():
                return re.sub(r"<[^>]+>", "", w.text()).strip().rstrip(":")
    return ""


def describe_field(name: str, widget: QWidget, getter, slider_scale=None) -> Dict[str, Any]:
    f: Dict[str, Any] = {
        "key": name,
        "label": _label_for(widget) or _humanize(name),
        "help": re.sub(r"<[^>]+>", "", widget.toolTip() or "").strip(),
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
        secret = widget.echoMode() != QLineEdit.Normal or bool(re.search(r"password|token|webhook|url", name, re.I))
        f.update(type="text", value=value, placeholder=widget.placeholderText(), secret=secret)
        if re.fullmatch(r"\d{1,2}:\d{2}", str(value or "")) or "HH:MM" in widget.placeholderText().upper():
            f["type"] = "time"
    else:
        f.update(type="text", value=value if isinstance(value, (str, int, float, bool)) else str(value))
    return f


def _page_error(page) -> str:
    """The first visible error text on a page (labels named ErrorText)."""
    for lbl in page.findChildren(QLabel):
        if lbl.objectName() == "ErrorText" and lbl.isVisibleTo(page) and lbl.text().strip():
            return lbl.text().strip()
    return ""


def describe_page(key: str, title: str, page) -> Dict[str, Any]:
    sliders = getattr(page, "_sliders", {}) or {}
    fields = [describe_field(name, w, page._getters[name], (sliders.get(name) or (None, None))[1])
              for name, w in page._fields.items()]
    return {
        "key": key, "title": title, "fields": fields,
        "apply_label": page.apply_btn.text(),
        "note": "Saved now; takes effect the next time the server restarts."
        if "Restart" in page.apply_btn.text() else "",
    }


def _coerce(widget: QWidget, value: Any) -> Any:
    if isinstance(widget, QCheckBox):
        return bool(value)
    if isinstance(widget, QSpinBox):
        return int(value)
    if isinstance(widget, (QDoubleSpinBox, QAbstractSlider)):
        return float(value)
    return "" if value is None else value if not isinstance(value, str) else value


def apply_page(page, values: Dict[str, Any]) -> Dict[str, Any]:
    """Puts `values` into the page's widgets and runs its own Apply.
    Unknown keys are ignored. Raises WebActionError when the page
    refuses (its own validation), with the page's own error text."""
    for name, value in values.items():
        if name in page._fields:
            try:
                page._setters[name](page._fields[name], _coerce(page._fields[name], value))
            except (TypeError, ValueError) as e:
                page.discard()
                raise WebActionError(f"Invalid value for {name}: {e}") from e
    if page.dirty_count() == 0:
        return {"changed": False}
    if not page.can_apply():
        reason = _page_error(page) or "These settings can't be saved as they are."
        page.discard()
        raise WebActionError(reason)
    page.apply_own()
    return {"changed": True}
