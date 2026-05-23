# -*- coding: utf-8 -*-
"""
撤销删除卡片 - 删除消息后显示，提供恢复操作

功能：
- 消息删除后显示 "消息已删除" + "恢复" 按钮
- 只缓存一步删除操作
- 5 秒后自动消失（超时后不可恢复）
- 点击恢复按钮触发 restoreRequested 信号

参考 CommandCard 的样式设计
"""
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import (
    QWidget, QHBoxLayout, QLabel, QSizePolicy,
)

from app.utils.utils import get_font_family_css
from app.utils.design_tokens import Colors, font_size_css


AUTO_DISMISS_MS = 5000  # 5秒自动消失


class UndoDeleteCard(QWidget):
    """撤销删除卡片"""

    restoreRequested = pyqtSignal()  # 用户点击恢复
    dismissed = pyqtSignal()         # 卡片自动消失或被关闭

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.setInterval(AUTO_DISMISS_MS)
        self._dismiss_timer.timeout.connect(self._on_timeout)

        self.setVisible(False)
        self._setup_ui()

    def setVisible(self, visible: bool):
        """重写 setVisible，在显示时启动计时器，隐藏时停止"""
        was_visible = self.isVisible()
        super().setVisible(visible)

        if visible and not was_visible:
            # 由隐藏变为显示：启动计时器
            self._dismiss_timer.start()
        elif not visible and was_visible:
            # 由显示变为隐藏：停止计时器并发出 dismissed 信号
            self._dismiss_timer.stop()
            self.dismissed.emit()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(40)

        Colors.refresh()
        self.setStyleSheet(f"""
            UndoDeleteCard {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 12, 0)
        layout.setSpacing(8)

        # 提示文字
        self._hint_label = QLabel("消息已删除", self)
        self._hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._hint_label.setStyleSheet(f"""
            QLabel {{
                color: rgba(255, 255, 255, 0.7);
                {get_font_family_css()} {font_size_css(13)};
                background: transparent;
            }}
        """)
        layout.addWidget(self._hint_label)

        layout.addStretch()

        # 恢复按钮
        self._restore_btn = QLabel("恢复", self)
        self._restore_btn.setCursor(Qt.PointingHandCursor)
        self._restore_btn.setStyleSheet(f"""
            QLabel {{
                color: #66c6ff;
                {get_font_family_css()} {font_size_css(13)};
                font-weight: bold;
                background: transparent;
                padding: 4px 12px;
            }}
            QLabel:hover {{
                color: #aae0ff;
            }}
        """)
        self._restore_btn.mousePressEvent = self._on_restore_clicked
        layout.addWidget(self._restore_btn)

    def _on_restore_clicked(self, event: QMouseEvent):
        """恢复按钮被点击"""
        if event.button() == Qt.LeftButton:
            self._dismiss_timer.stop()
            self.restoreRequested.emit()
            self.setVisible(False)

    def _on_timeout(self):
        """超时自动消失"""
        self.setVisible(False)
