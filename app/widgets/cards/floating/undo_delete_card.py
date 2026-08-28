# -*- coding: utf-8 -*-
"""
撤销删除卡片 - 删除消息后显示，提供恢复操作

功能：
- 消息删除/撤销后显示 "已删除 X 条消息" + "恢复" 按钮
- 只缓存一步删除操作
- 点击恢复按钮触发 restoreRequested 信号
- 不自动消失，等到其他卡片覆盖时被关闭

参考 CommandCard 的样式设计
"""
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QWidget,
)

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css


class UndoDeleteCard(QWidget):
    """撤销删除卡片"""

    restoreRequested = Signal()  # 用户点击恢复
    dismissed = Signal()         # 卡片被关闭

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVisible(False)
        self._setup_ui()

    def refresh_style(self):
        """刷新样式（主题切换时调用）"""
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
        if hasattr(self, "_hint_label") and self._hint_label:
            self._hint_label.setStyleSheet(f"""
                QLabel {{
                    color: {Colors.TEXT_SECONDARY};
                    {get_font_family_css()} {font_size_css(13)};
                    background: transparent;
                }}
            """)

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(30)

        self.refresh_style()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 0, 12, 0)
        layout.setSpacing(8)

        # 提示文字
        self._hint_label = QLabel("消息已删除", self)
        self._hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        if hasattr(self, "_hint_label") and self._hint_label:
            self._hint_label.setStyleSheet(f"""
                QLabel {{
                    color: {Colors.TEXT_SECONDARY};
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
                color: {Colors.TAG_ACCENT};
                {get_font_family_css()} {font_size_css(13)};
                font-weight: bold;
                background: transparent;
                padding: 0px 4px;
                border-radius: 4px;
            }}
            QLabel:hover {{
                color: {Colors.TAG_ACCENT_TEXT};
                background: {Colors.HOVER_BG};
            }}
        """)
        self._restore_btn.mousePressEvent = self._on_restore_clicked
        layout.addWidget(self._restore_btn)

    def _on_restore_clicked(self, event: QMouseEvent):
        """恢复按钮被点击"""
        if event.button() == Qt.LeftButton:
            self.restoreRequested.emit()
            self.setVisible(False)

    def set_count(self, count: int):
        """设置删除的消息条数"""
        self._hint_label.setText(f"已删除 {count} 条消息")

    def show_card(self):
        """显示卡片（由 CardManager 调用）"""
        self.setVisible(True)

    def hide_card(self):
        """隐藏卡片（由 CardManager 调用）"""
        self.setVisible(False)
        self.dismissed.emit()
