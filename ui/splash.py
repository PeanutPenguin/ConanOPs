"""
Frameless startup screen: loading animation, app name and a status line.
Not shown for start-minimized launches.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from ui import assets
import version


class SplashScreen(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.SplashScreen | Qt.FramelessWindowHint)
        self.setObjectName("Splash")
        # Transparent window so the rounded corners don't show square edges.
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(420, 340)
        self.setWindowIcon(assets.app_icon())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("Card")
        outer.addWidget(card)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(32, 32, 32, 28)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignCenter)

        self.spinner = assets.LoadingSpinner(128)
        layout.addWidget(self.spinner, 0, Qt.AlignHCenter)
        layout.addSpacing(10)

        title = QLabel("ConanOps")
        title.setObjectName("HeaderTitle")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        self.status_label = QLabel("Starting…")
        self.status_label.setObjectName("Muted")
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

        ver = QLabel(f"v{version.VERSION}")
        ver.setObjectName("Dim")
        ver.setAlignment(Qt.AlignCenter)
        ver.setStyleSheet("font-size: 12px;")
        layout.addWidget(ver)

    def apply_stylesheet(self, qss: str) -> None:
        self.setStyleSheet(qss + "\n#Splash { background: transparent; }")

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)
        # Paint now: building the main window next blocks the event loop.
        self.repaint()
        QGuiApplication.processEvents()

    def show_centered(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.center().x() - self.width() // 2, geo.center().y() - self.height() // 2)
        self.show()
        QGuiApplication.processEvents()
