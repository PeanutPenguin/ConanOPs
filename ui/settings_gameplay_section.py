"""
Server Settings > Gameplay: the most-changed gameplay settings up front, then
every category (Progression, Combat, ...) as a fold-out with the rest.

Each category is still its own settings page (it tracks and saves its own
values); this only arranges them. The up-front rows are moved here from their
page, so each setting appears exactly once.
"""
from __future__ import annotations

from typing import List, Tuple

from PySide6.QtWidgets import QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget

import settings_layout
from ui.generic_settings_page import restyle_rows, setting_card
from ui.more_options import MoreOptions


class GameplaySection(QWidget):
    def __init__(self, pages: List[Tuple[str, str, QWidget]], parent=None):
        super().__init__(parent)
        self.pages = pages
        self.folds: dict = {}  # page key -> MoreOptions

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(0, 0, 8, 16)
        col.setSpacing(12)
        scroll.setWidget(inner)
        root.addWidget(scroll)
        self.scroll = scroll

        common_card, common_layout = setting_card()
        col.addWidget(common_card)
        rows = {}
        for _key, _title, page in pages:
            rows.update(getattr(page, "rows", {}))
        for field_key in settings_layout.GAMEPLAY_COMMON:
            if field_key in rows:
                common_layout.addWidget(rows[field_key])
        restyle_rows(common_layout)

        heading = QLabel("Everything else, by category")
        heading.setObjectName("SectionLabel")
        col.addSpacing(4)
        col.addWidget(heading)
        for key, title, page in pages:
            page.card.setObjectName("TransparentRow")  # the fold-out is the card
            card_layout = page.card.layout()
            restyle_rows(card_layout)
            n = card_layout.count()
            fold = MoreOptions(title, n)
            fold.body_layout.addWidget(page)
            fold.setVisible(n > 0)
            col.addWidget(fold)
            self.folds[key] = fold
        col.addStretch(1)
