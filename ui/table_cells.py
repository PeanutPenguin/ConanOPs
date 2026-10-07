"""
Buttons inside table rows: centered vertically with fixed gaps, never
stretched (a bare QPushButton in setCellWidget() fills its whole cell).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QHeaderView, QTableWidget, QWidget

ROW_HEIGHT = 52


def button_cell(*buttons, align=Qt.AlignRight) -> QWidget:
    cell = QWidget()
    cell.setObjectName("TransparentRow")
    layout = QHBoxLayout(cell)
    layout.setContentsMargins(8, 0, 10, 0)
    layout.setSpacing(6)
    if align == Qt.AlignRight:
        layout.addStretch(1)
    for btn in buttons:
        # Text width + the stylesheet's 14px padding and border, so the label never clips.
        btn.ensurePolished()
        btn.setMinimumWidth(btn.fontMetrics().horizontalAdvance(btn.text()) + 2 * 14 + 2 + 6)
        layout.addWidget(btn, 0, Qt.AlignVCenter)
    if align != Qt.AlignRight:
        layout.addStretch(1)
    return cell


def fit_button_column(table: QTableWidget, column: int, min_width: int = 110) -> None:
    """Fixed-width button column (ResizeToContents ignores cell widgets).
    Call size_button_column() after filling the rows."""
    table.horizontalHeader().setSectionResizeMode(column, QHeaderView.Fixed)
    table.setColumnWidth(column, min_width)


def size_button_column(table: QTableWidget, column: int, min_width: int = 110) -> None:
    width = min_width
    for row in range(table.rowCount()):
        cell = table.cellWidget(row, column)
        if cell is None:
            continue
        for child in [cell, *cell.findChildren(QWidget)]:
            child.ensurePolished()  # so the stylesheet's padding counts
        layout = cell.layout()
        margins = layout.contentsMargins().left() + layout.contentsMargins().right() if layout else 0
        buttons = [w for w in cell.findChildren(QWidget) if w.minimumWidth() > 0]
        needed = margins + sum(b.minimumWidth() for b in buttons) + (layout.spacing() * max(0, len(buttons) - 1) if layout else 0)
        width = max(width, cell.sizeHint().width(), needed)
    table.setColumnWidth(column, width)


def add_row(table: QTableWidget) -> int:
    row = table.rowCount()
    table.insertRow(row)
    table.setRowHeight(row, ROW_HEIGHT)
    return row
