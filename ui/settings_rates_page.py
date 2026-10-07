from __future__ import annotations

from ui.generic_settings_page import GenericSettingsPage
from ini_field_specs import PROGRESSION_FIELDS


class SettingsRatesPage(GenericSettingsPage):
    """The five XP-progression multipliers. (Harvest amount, decay time,
    and the old PvP/purge/ORP/stamina toggles that used to live on this
    page have moved to the categories Funcom's own settings UI actually
    groups them under -- Harvesting, Building & Decay, Server Identity,
    and Survival respectively -- see ini_field_specs.py.)"""

    def __init__(self, parent=None):
        super().__init__("Progression", PROGRESSION_FIELDS, parent)
