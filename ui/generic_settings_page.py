from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QCheckBox, QSpinBox, QComboBox, QLineEdit, QVBoxLayout, QHBoxLayout, QFrame

import settings_layout
from ui.base_settings_page import SettingsPageBase
from ui.more_options import MoreOptions
from ui.ghost_slider import GhostSlider
from ini_field_specs import FieldSpec


def restyle_rows(layout) -> None:
    """Row dividers: every row but the last in a card gets a bottom border."""
    rows = [layout.itemAt(i).widget() for i in range(layout.count())]
    rows = [w for w in rows if w is not None and w.objectName() in ("SettingRow", "SettingRowLast")]
    for i, w in enumerate(rows):
        name = "SettingRow" if i < len(rows) - 1 else "SettingRowLast"
        if w.objectName() != name:
            w.setObjectName(name)
            w.style().unpolish(w)
            w.style().polish(w)


def setting_card() -> tuple:
    card = QFrame()
    card.setObjectName("Card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(0, 4, 0, 4)
    lay.setSpacing(0)
    return card, lay


class GenericSettingsPage(SettingsPageBase):
    """Builds its form from a list of FieldSpec (ini_field_specs.py).

    common_keys: settings shown up front; the rest go under "More options".
    None shows everything in one card. embedded: see SettingsPageBase."""

    def __init__(self, title: str, specs: List[FieldSpec], parent=None, common_keys=None, embedded: bool = False):
        super().__init__(title, parent, embedded=embedded)
        self.specs = specs
        self._sliders: dict = {}  # key -> (GhostSlider, scale)
        self.rows: dict = {}      # key -> row frame
        self.more: Optional[MoreOptions] = None
        self._build_form(common_keys)

    def _build_form(self, common_keys) -> None:
        # One row per setting: name and a one-line hint left, control right.
        self.card, card_layout = setting_card()
        self.form_layout.addWidget(self.card)
        more_specs = [s for s in self.specs if common_keys is not None and s.key not in common_keys]
        if more_specs:
            self.more = MoreOptions("More options", len(more_specs))
            self.form_layout.addWidget(self.more)

        for spec in self.specs:
            row = self._build_row(spec)
            self.rows[spec.key] = row
            (self.more.body_layout if spec in more_specs else card_layout).addWidget(row)
        restyle_rows(card_layout)
        if self.more is not None:
            restyle_rows(self.more.body_layout)
        self.card.setVisible(card_layout.count() > 0)
        if not self.embedded:
            self.form_layout.addStretch(1)

    def _build_row(self, spec: FieldSpec) -> QFrame:
        row = QFrame()
        row.setObjectName("SettingRow")
        line = QHBoxLayout(row)
        line.setContentsMargins(18, 10, 18, 10)
        line.setSpacing(14)
        text_col = QVBoxLayout()
        text_col.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        label = QLabel(spec.label)
        label.setWordWrap(True)
        if spec.tooltip:
            label.setToolTip(spec.tooltip)
        top.addWidget(label, 1)
        hint = settings_layout.short_help(spec.tooltip)
        if spec.tooltip and hint != " ".join(spec.tooltip.split()):
            cue = QLabel("ⓘ")
            cue.setObjectName("InfoCue")
            cue.setToolTip(spec.tooltip)
            top.addWidget(cue)
        text_col.addLayout(top)
        if hint:
            help_label = QLabel(hint)
            help_label.setObjectName("SettingHelp")
            help_label.setWordWrap(True)
            help_label.setToolTip(spec.tooltip)
            text_col.addWidget(help_label)
        line.addLayout(text_col, 1)

        if spec.kind == "bool":
            cb = QCheckBox()
            cb.setAccessibleName(spec.label)
            if spec.tooltip:
                cb.setToolTip(spec.tooltip)
            line.addWidget(cb)
            self.register_field(spec.key, cb, lambda w: w.isChecked(), lambda w, v: w.setChecked(bool(v)), cb.toggled)

        elif spec.kind == "int":
            spin = QSpinBox()
            spin.setRange(int(spec.min if spec.min is not None else -2_147_483_648),
                          int(spec.max if spec.max is not None else 2_147_483_647))
            spin.setSingleStep(int(spec.step))
            spin.setFixedWidth(130)
            spin.setAlignment(Qt.AlignRight)
            spin.setAccessibleName(spec.label)
            if spec.tooltip:
                spin.setToolTip(spec.tooltip)
            line.addWidget(spin)
            self.register_field(spec.key, spin, lambda w: w.value(), lambda w, v: w.setValue(int(v)), spin.valueChanged)

        elif spec.kind == "float":
            lo = int(round((spec.min if spec.min is not None else 0) * (10 ** spec.decimals)))
            hi = int(round((spec.max if spec.max is not None else 10) * (10 ** spec.decimals)))
            scale = 10 ** spec.decimals
            slider = GhostSlider(lo, hi)
            slider.setFixedWidth(240)
            slider.setAccessibleName(spec.label)
            value_label = QLabel("")
            value_label.setObjectName("SettingValue")
            value_label.setFixedWidth(52)
            value_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if spec.tooltip:
                slider.setToolTip(spec.tooltip)

            def make_label_updater(lbl=value_label, sc=scale, dec=spec.decimals):
                return lambda v: lbl.setText(f"{v / sc:.{dec}f}")
            update_label = make_label_updater()
            slider.valueChanged.connect(update_label)
            update_label(slider.value())
            line.addWidget(slider)
            line.addWidget(value_label)

            self._sliders[spec.key] = (slider, scale)
            self.register_field(
                spec.key, slider,
                lambda w, sc=scale: w.value() / sc,
                lambda w, v, sc=scale: w.setValue(int(round(v * sc))),
                slider.valueChanged,
            )

        elif spec.kind == "choice":
            combo = QComboBox()
            for clabel, value in (spec.choices or []):
                combo.addItem(clabel, value)
            combo.setFixedWidth(220)
            combo.setAccessibleName(spec.label)
            if spec.tooltip:
                combo.setToolTip(spec.tooltip)
            line.addWidget(combo)
            self.register_field(
                spec.key, combo,
                lambda w: w.currentData(),
                lambda w, v: w.setCurrentIndex(max(0, w.findData(v))),
                combo.currentIndexChanged,
            )

        else:  # "text"
            edit = QLineEdit()
            edit.setFixedWidth(320)
            edit.setAccessibleName(spec.label)
            if spec.tooltip:
                edit.setToolTip(spec.tooltip)
            line.addWidget(edit)
            self.register_field(spec.key, edit, lambda w: w.text(), lambda w, v: w.setText(str(v)), edit.textChanged)
        return row

    def load_committed(self, values: dict) -> None:
        super().load_committed(values)
        for key, (slider, scale) in self._sliders.items():
            if key in values:
                slider.set_committed_value(int(round(values[key] * scale)))

    def apply_own(self) -> None:
        super().apply_own()
        for key, (slider, _scale) in self._sliders.items():
            slider.set_committed_value(slider.value())

    def on_apply(self, values: dict) -> None:
        pass
