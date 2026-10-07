from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QCheckBox, QSpinBox, QComboBox, QLineEdit, QVBoxLayout, QHBoxLayout, QFrame

from ui.base_settings_page import SettingsPageBase
from ui.ghost_slider import GhostSlider
from ini_field_specs import FieldSpec


class GenericSettingsPage(SettingsPageBase):
    """Builds its form from a list of FieldSpec (ini_field_specs.py)."""

    def __init__(self, title: str, specs: List[FieldSpec], parent=None):
        super().__init__(title, parent)
        self.specs = specs
        self._sliders: dict = {}  # key -> (GhostSlider, scale)
        self._build_form()

    def _build_form(self) -> None:
        # One card, one row per setting: name left, control right.
        self.card = QFrame()
        self.card.setObjectName("Card")
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(0, 4, 0, 4)
        card_layout.setSpacing(0)
        self.form_layout.addWidget(self.card)

        for i, spec in enumerate(self.specs):
            row = QFrame()
            row.setObjectName("SettingRow" if i < len(self.specs) - 1 else "SettingRowLast")
            line = QHBoxLayout(row)
            line.setContentsMargins(18, 10, 18, 10)
            line.setSpacing(14)
            label = QLabel(spec.label)
            label.setWordWrap(True)
            if spec.tooltip:
                label.setToolTip(spec.tooltip)
            line.addWidget(label, 1)

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

            card_layout.addWidget(row)

        self.form_layout.addStretch(1)

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
