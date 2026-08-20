# -*- coding: utf-8 -*-
"""
通用弹框组件 — 统一使用 MaskDialogBase 风格。

所有弹框遵循与 ImportOptionDialog / SingleInputDialog 一致的视觉规范：
- 半透明遮罩 + 圆角卡片
- 内部 widget 按内容自适应（最小尺寸保底，最大尺寸防撑爆）
- 可拖拽、可点击遮罩关闭

推荐优先使用本模块替代 QMessageBox / qfluentwidgets MessageBox / QInputDialog。
"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout
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
    DEFAULT_HEIGHT = 200  # 最小高度；高度按内容自适应（防止长文本被遮挡）
    DEFAULT_MAX_WIDTH = 600
    DEFAULT_MAX_HEIGHT = 720

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
        self.setMaskColor(QColor(0, 0, 0, 180))

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
        # 标题（兼容 qfluentwidgets 旧版：BodyLabel 仅接受 parent，文本用 setText 设置）
        title_label = BodyLabel(self.widget)
        title_label.setText(title)

        title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(16)}; font-weight: bold;"
        )
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        layout.addSpacing(12)

        # 内容
        content_label = BodyLabel(self.widget)
        content_label.setText(content)

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

        # 内容自适应：设最小尺寸保底 + 最大尺寸防撑爆 + 水平 Fixed 防止
        # MaskDialogBase 的 QHBoxLayout 把 widget 拉伸到全屏
        self._fit_widget_to_content()

    def _on_confirm(self):
        self.close()
        self.confirmed.emit()

    def _on_cancel(self):
        self.close()
        self.cancelled.emit()

    def _fit_widget_to_content(self):
        """让 widget 高度按内容自适应，宽度仍居中且不超过上限。

        关键修复：原实现 setFixedSize(W, H) 把 widget 锁死，导致长文本
        （如 /team --load 角色列表、InfoBar 多行提示）溢出后被按钮行遮挡。
        """
        self.widget.setMinimumSize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        self.widget.setMaximumSize(self.DEFAULT_MAX_WIDTH, self.DEFAULT_MAX_HEIGHT)
        # 防止 MaskDialogBase 的 QHBoxLayout 把 widget 拉伸到全屏
        sp = self.widget.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Fixed)
        sp.setVerticalPolicy(QSizePolicy.Preferred)
        self.widget.setSizePolicy(sp)
        # 让 layout 按内容计算 sizeHint 并把 widget 撑到合适尺寸
        self.widget.adjustSize()
        self._center_widget()

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
    可选传 dismiss_text 增加次级「不再提醒」按钮（点击 emit dismissed）。
    """

    confirmed = pyqtSignal()
    dismissed = pyqtSignal()

    DEFAULT_WIDTH = 400
    DEFAULT_HEIGHT = 180  # 最小高度；高度按内容自适应（防止长文本被遮挡）
    DEFAULT_MAX_WIDTH = 600
    DEFAULT_MAX_HEIGHT = 720

    def __init__(
        self,
        title: str,
        content: str,
        confirm_text: str = "知道了",
        dismiss_text: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._init_ui(title, content, confirm_text, dismiss_text)

    def _init_ui(self, title: str, content: str, confirm_text: str, dismiss_text: str = ""):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 140))

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
        # 标题（兼容 qfluentwidgets 旧版：BodyLabel 仅接受 parent，文本用 setText 设置）
        title_label = BodyLabel(self.widget)
        title_label.setText(title)

        title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(16)}; font-weight: bold;"
        )
        title_label.setWordWrap(True)
        layout.addWidget(title_label)

        layout.addSpacing(12)

        # 内容
        content_label = BodyLabel(self.widget)
        content_label.setText(content)

        content_label.setWordWrap(True)
        content_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(13)}; line-height: 1.6;"
        )
        layout.addWidget(content_label)

        layout.addStretch()

        # 按钮行（可选：次级「不再提醒」按钮 + 主确认按钮）
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        if dismiss_text:
            dismiss_btn = QPushButton(dismiss_text, self.widget)
            dismiss_btn.setCursor(Qt.PointingHandCursor)
            dismiss_btn.setFixedHeight(36)
            dismiss_btn.setStyleSheet(f"""
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
            dismiss_btn.clicked.connect(self._on_dismiss)
            btn_layout.addStretch()
            btn_layout.addWidget(dismiss_btn)

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

        # 内容自适应：设最小尺寸保底 + 最大尺寸防撑爆 + 水平 Fixed 防止
        # MaskDialogBase 的 QHBoxLayout 把 widget 拉伸到全屏
        self.widget.setMinimumSize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        self.widget.setMaximumSize(self.DEFAULT_MAX_WIDTH, self.DEFAULT_MAX_HEIGHT)
        sp = self.widget.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Fixed)
        sp.setVerticalPolicy(QSizePolicy.Preferred)
        self.widget.setSizePolicy(sp)
        self.widget.adjustSize()
        self._center_widget()

    def _on_confirm(self):
        self.close()
        self.confirmed.emit()

    def _on_dismiss(self):
        """点击「不再提醒」：关闭并 emit dismissed（调用方持久化设置）"""
        self.close()
        self.dismissed.emit()

    def _center_widget(self):
        """让 widget 在 dialog 中保持居中"""
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center_widget()
