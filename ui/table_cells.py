"""
Buttons inside table rows, laid out the same way everywhere: centered
vertically in the row, a fixed gap from the cell edges, never stretched
to fill the cell. A QPushButton handed straight to setCellWidget()
fills its whole cell instead, which is why the Restore buttons on the
Backups page didn't line up with the row text.
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
        # Text width + the stylesheet's 14px side padding and border, so
        # a narrow column can never squeeze the label ("estor").
        btn.ensurePolished()
        btn.setMinimumWidth(btn.fontMetrics().horizontalAdvance(btn.text()) + 2 * 14 + 2 + 6)
        layout.addWidget(btn, 0, Qt.AlignVCenter)
    if align != Qt.AlignRight:
        layout.addStretch(1)
    return cell


def fit_button_column(table: QTableWidget, column: int, min_width: int = 110) -> None:
    """Fixed-width buttons column (Qt's ResizeToContents ignores cell
    widgets, so it clipped the buttons to "estor"). Call
    size_button_column() after filling the rows to fit the real buttons."""
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
