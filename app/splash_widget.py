# -*- coding: utf-8 -*-
"""轻量启动骨架屏：主窗口构建前先行显示，缩短感知启动时间。"""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import isDarkTheme


class SplashWidget(QWidget):
    """启动骨架屏（纯色背景 + Logo + 加载文本，零重型依赖）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        dark = isDarkTheme()
        bg = "#1e1e1e" if dark else "#f6f6f6"
        fg = "#f2f2f2" if dark else "#1f1f1f"
        sub = "#9a9a9a" if dark else "#6b6b6b"
        self.setObjectName("splashWidget")
        self.setFixedSize(420, 260)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setStyleSheet(f"#splashWidget {{ background-color: {bg}; border-radius: 16px; }}")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 36, 40, 32)
        layout.setSpacing(8)

        logo = QLabel(self)
        pix = QPixmap(":/icons/drifox.ico")
        if not pix.isNull():
            logo.setPixmap(pix.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            logo.setText("🦊")
            logo.setStyleSheet(f"font-size: 56px; color: {fg}; background: transparent;")
        logo.setAlignment(Qt.AlignCenter)
        layout.addWidget(logo, alignment=Qt.AlignHCenter)

        title = QLabel("DriFox", self)
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"font-size: 22px; font-weight: 600; color: {fg}; background: transparent; margin-top: 8px;"
        )
        layout.addWidget(title)

        status = QLabel("正在加载…", self)
        status.setAlignment(Qt.AlignCenter)
        status.setStyleSheet(f"font-size: 13px; color: {sub}; background: transparent; margin-top: 4px;")
        layout.addWidget(status)

        self._status_label = status
        self._center_on_screen()

    def set_status(self, text: str) -> None:
        """更新底部状态文本（主窗口分片构建进度）。"""
        self._status_label.setText(text)

    def _center_on_screen(self):
        screen = self.screen() or QApplication.primaryScreen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.center() - self.rect().center())
