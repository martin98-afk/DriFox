# -*- coding: utf-8 -*-
"""
通用弹框组件 — 统一使用 MaskDialogBase 风格。

所有弹框遵循与 ImportOptionDialog / SingleInputDialog 一致的视觉规范：
- 半透明遮罩 + 圆角卡片
- 固定 widget 尺寸 + 居中
- 可拖拽、可点击遮罩关闭

推荐优先使用本模块替代 QMessageBox / qfluentwidgets MessageBox / QInputDialog。
"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout
from qfluentwidgets import BodyLabel, MaskDialogBase

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css


class ConfirmDialog(MaskDialogBase):
    """通用确认弹框 — 替代 QMessageBox.question / qfluentwidgets MessageBox

    统一 MaskDialogBase 风格：半透明遮罩 + 圆角卡片 + 可拖拽 + 可遮罩关闭。

    Usage:
        dialog = ConfirmDialog(
            title="加载团队模板",
            content="确定要加载模板「xxx」吗？\\n当前所有活跃窗口的 agent 身份将被重新分配。",
            confirm_text="确认",
            cancel_text="取消",
            parent=self.window(),
        )
        dialog.confirmed.connect(lambda: ...)
        dialog.cancelled.connect(lambda: ...)
        dialog.exec_()
    """

    confirmed = pyqtSignal()
    cancelled = pyqtSignal()

    DEFAULT_WIDTH = 400
    DEFAULT_HEIGHT = 240

    def __init__(
        self,
        title: str,
        content: str,
        confirm_text: str = "确认",
        cancel_text: str = "取消",
        parent=None,
    ):
        super().__init__(parent)
        self._init_ui(title, content, confirm_text, cancel_text)

    def _init_ui(self, title: str, content: str, confirm_text: str, cancel_text: str):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("confirmDialogWidget")
        self.widget.setStyleSheet(f"""
            #confirmDialogWidget {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(28, 28, 28, 20)
        layout.setSpacing(0)

        # 标题
        title_label = BodyLabel(title, self.widget)
        title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(16)}; font-weight: bold;"
        )
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        layout.addSpacing(12)

        # 内容
        content_label = BodyLabel(content, self.widget)
        content_label.setWordWrap(True)
        content_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(13)}; line-height: 1.6;"
        )
        layout.addWidget(content_label)

        layout.addStretch()

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        cancel_btn = QPushButton(cancel_text, self.widget)
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setFixedHeight(36)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 4px 28px;
                {get_font_family_css()} {font_size_css(13)}
            }}
            QPushButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.BORDER_ACCENT};
            }}
            QPushButton:pressed {{
                background-color: {Colors.SELECTED_BG};
            }}
        """)
        cancel_btn.clicked.connect(self._on_cancel)

        confirm_btn = QPushButton(confirm_text, self.widget)
        confirm_btn.setCursor(Qt.PointingHandCursor)
        confirm_btn.setFixedHeight(36)
        confirm_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Colors.INFO};
                color: white;
                border: none;
                border-radius: 8px;
                padding: 4px 28px;
                {get_font_family_css()} {font_size_css(13)};
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {Colors.SEND_BTN_END};
            }}
            QPushButton:pressed {{
                background-color: {Colors.SEND_BTN_HOVER_END};
            }}
        """)
        confirm_btn.clicked.connect(self._on_confirm)

        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(confirm_btn)
        layout.addLayout(btn_layout)

        self.widget.setFixedSize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        self._center_widget()

    def _on_confirm(self):
        self.close()
        self.confirmed.emit()

    def _on_cancel(self):
        self.close()
        self.cancelled.emit()

    def _center_widget(self):
        """让 widget 在 dialog 中保持居中"""
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center_widget()


class InfoDialog(MaskDialogBase):
    """通用信息提示弹框 — 单按钮确认，替代 qfluentwidgets MessageBox（仅确认按钮场景）

    统一 MaskDialogBase 风格。
    """

    confirmed = pyqtSignal()

    DEFAULT_WIDTH = 400
    DEFAULT_HEIGHT = 240

    def __init__(
        self,
        title: str,
        content: str,
        confirm_text: str = "知道了",
        parent=None,
    ):
        super().__init__(parent)
        self._init_ui(title, content, confirm_text)

    def _init_ui(self, title: str, content: str, confirm_text: str):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("infoDialogWidget")
        self.widget.setStyleSheet(f"""
            #infoDialogWidget {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(28, 28, 28, 20)
        layout.setSpacing(0)

        # 标题
        title_label = BodyLabel(title, self.widget)
        title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(16)}; font-weight: bold;"
        )
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        layout.addSpacing(12)

        # 内容
        content_label = BodyLabel(content, self.widget)
        content_label.setWordWrap(True)
        content_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(13)}; line-height: 1.6;"
        )
        layout.addWidget(content_label)

        layout.addStretch()

        # 单按钮行
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        confirm_btn = QPushButton(confirm_text, self.widget)
        confirm_btn.setCursor(Qt.PointingHandCursor)
        confirm_btn.setFixedHeight(36)
        confirm_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Colors.INFO};
                color: white;
                border: none;
                border-radius: 8px;
                padding: 4px 28px;
                {get_font_family_css()} {font_size_css(13)};
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {Colors.SEND_BTN_END};
            }}
            QPushButton:pressed {{
                background-color: {Colors.SEND_BTN_HOVER_END};
            }}
        """)
        confirm_btn.clicked.connect(self._on_confirm)

        btn_layout.addStretch()
        btn_layout.addWidget(confirm_btn)
        layout.addLayout(btn_layout)

        self.widget.setFixedSize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        self._center_widget()

    def _on_confirm(self):
        self.close()
        self.confirmed.emit()

    def _center_widget(self):
        """让 widget 在 dialog 中保持居中"""
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center_widget()
