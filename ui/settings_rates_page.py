from __future__ import annotations

from ui.generic_settings_page import GenericSettingsPage
from ini_field_specs import PROGRESSION_FIELDS


class SettingsRatesPage(GenericSettingsPage):
    """The five XP-progression multipliers."""

    def __init__(self, parent=None, embedded: bool = False):
        super().__init__("Progression", PROGRESSION_FIELDS, parent, embedded=embedded)
