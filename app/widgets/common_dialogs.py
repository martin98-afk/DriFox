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
from PyQt5.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout
from qfluentwidgets import BodyLabel, LineEdit, MaskDialogBase, PrimaryPushButton, PushButton

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
    DEFAULT_HEIGHT = 140  # 最小高度=标题+12+1行内容+按钮+边距；高度按内容自适应
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

        # 注：不在内容/按钮间加 addStretch()，否则短内容时按钮贴底中间留白。
        # 高度完全由标题+内容+按钮+边距决定（minSize 保底，maxSize 防撑爆）。

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

        关键修复：
        1. 原 setFixedSize(W, H) 锁死尺寸 → 长文本（/team --load 角色列表、
           InfoBar 多行 bullet）溢出后被按钮行遮挡。
        2. MaskDialogBase 用 QHBoxLayout(self).addWidget(self.widget)，
           QHBoxLayout 默认会让 child 高度 = layout 高度（=dialog 全屏），
           任何 sizePolicy / maxSize 都无法阻止 widget 被拉伸到全屏高度。
           必须把 widget 从 layout 中取出，自己用 move + adjustSize 管理。

        流程：
        - 从 MaskDialogBase 的 _hBoxLayout 取出 widget（layout 仍占位但不影响 widget 几何）
        - 设 maxSize 防极端撑爆
        - adjustSize() 让 widget 按内容 sizeHint 撑到合适尺寸
        - _center_widget 居中；后续 dialog.resizeEvent 仍自动重新居中
        """
        self.layout().removeWidget(self.widget)  # 关键：从 QHBoxLayout 取出
        self.widget.setParent(self)  # 确保 parent 是 dialog（addWidget 时已是）
        self.widget.setMinimumSize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        self.widget.setMaximumSize(self.DEFAULT_MAX_WIDTH, self.DEFAULT_MAX_HEIGHT)
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


class SingleInputDialog(MaskDialogBase):
    """单输入弹框：标题 + 可选提示 + 1 个输入框 + 确认/取消按钮。

    修复 MaskDialogBase 默认 widget 被拉伸到父窗口的问题：
    - widget 大小固定（不跟随父窗口 resize 拉伸）
    - 始终居中显示

    Usage:
        dialog = SingleInputDialog(
            title="🔗 添加 URL 链接",
            hint="请输入网页链接，添加为关键文档引用",   # 可选
            placeholder="https://example.com",
            default_text="https://",                       # 可选
            confirm_text="添加",
            cancel_text="取消",
            parent=parent,
        )
        dialog.confirmed.connect(handler)
        dialog.exec_()
    """

    # 通用信号
    confirmed = pyqtSignal(str)
    # 兼容旧 URL 场景的别名
    urlConfirmed = pyqtSignal(str)

    DEFAULT_WIDTH = 420
    DEFAULT_HEIGHT = 220

    def __init__(
        self,
        title: str,
        placeholder: str = "",
        hint: str = "",
        default_text: str = "",
        confirm_text: str = "确认",
        cancel_text: str = "取消",
        parent=None,
    ):
        super().__init__(parent)
        self._init_ui(title, hint, placeholder, default_text, confirm_text, cancel_text)

    def _init_ui(self, title, hint, placeholder, default_text, confirm_text, cancel_text):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        # 对话框内容区背景
        # MaskDialogBase.widget 是 QFrame，用 objectName 选择器精确命中自身，
        # 避免 .QFrame 选择器误染所有子 QFrame（如 LineEdit 内部 QFrame 背景）。
        self.widget.setObjectName("singleInputDialogWidget")
        self.widget.setStyleSheet(f"""
            #singleInputDialogWidget {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        # 主布局
        self.vBoxLayout = QVBoxLayout(self.widget)
        self.vBoxLayout.setContentsMargins(24, 24, 24, 24)
        self.vBoxLayout.setSpacing(12)

        # 标题
        title_label = BodyLabel(title, self.widget)
        title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; {get_font_family_css()} {font_size_css(16)}"
        )
        self.vBoxLayout.addWidget(title_label)

        # 提示文字（可选）
        self.hint_label = None
        if hint:
            self.hint_label = BodyLabel(hint, self.widget)
            self.hint_label.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; background: transparent; {get_font_family_css()} {font_size_css(11)}"
            )
            self.vBoxLayout.addWidget(self.hint_label)

        # 输入框
        self.input = LineEdit(self.widget)
        if placeholder:
            self.input.setPlaceholderText(placeholder)
        if default_text:
            self.input.setText(default_text)
        self.input.setFixedHeight(36)
        self.input.setClearButtonEnabled(True)
        self.input.setStyleSheet(f"""
            LineEdit {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.INPUT_BG_START}, stop:1 {Colors.INPUT_BG_END});
                border: 1px solid {Colors.INPUT_BORDER};
                color: {Colors.INPUT_TEXT};
                padding: 4px 12px;
                border-radius: 6px;
                {get_font_family_css()} {font_size_css(13)}
            }}
            LineEdit:focus {{
                border-color: {Colors.INFO};
            }}
        """)
        # 回车键确认
        self.input.returnPressed.connect(self._on_accept)
        self.input.setFocus()
        self.vBoxLayout.addWidget(self.input)

        # 按钮行
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)
        btn_layout.addStretch()

        # 取消按钮
        cancel_btn = PushButton(cancel_text, self.widget)
        cancel_btn.setStyleSheet(f"""
            PushButton {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px 20px;
                {font_size_css(12)}
            }}
            PushButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.BORDER_ACCENT};
            }}
        """)
        cancel_btn.clicked.connect(self.close)

        # 确认按钮
        confirm_btn = PrimaryPushButton(confirm_text, self.widget)
        confirm_btn.setStyleSheet(f"""
            PrimaryPushButton {{
                background-color: {Colors.INFO};
                color: white;
                border: none;
                border-radius: 6px;
                padding: 4px 20px;
                {font_size_css(12)}
            }}
            PrimaryPushButton:hover {{
                background-color: {Colors.SEND_BTN_END};
            }}
        """)
        confirm_btn.clicked.connect(self._on_accept)

        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(confirm_btn)
        self.vBoxLayout.addLayout(btn_layout)

        # 关键：必须把 widget 从 MaskDialogBase 的 QHBoxLayout 取出，
        # 否则 QHBoxLayout 默认让 widget 高度 = layout 高度（dialog 全屏）
        self.layout().removeWidget(self.widget)
        self.widget.setParent(self)
        # 阻止 MaskDialogBase 的 QHBoxLayout 把 widget 拉伸到全屏：
        # minSize 保底，maxSize 防撑爆，adjustSize 让 widget 按内容尺寸自适应。
        # 原 setFixedSize 会让 hint/输入说明等长文本溢出被按钮遮挡。
        self.widget.setMinimumSize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        self.widget.setMaximumSize(self.DEFAULT_WIDTH + 200, self.DEFAULT_HEIGHT + 320)
        self.widget.adjustSize()
        # 初始居中显示；后续 dialog resize 时通过 _center_widget 保持居中
        self._center_widget()

    def _center_widget(self):
        """让 widget 在 dialog 中保持居中（MaskDialogBase 不自动居中）"""
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        # 先调 super 让 MaskDialogBase 把 windowMask 同步到 dialog 大小，
        # 然后重新居中 widget
        super().resizeEvent(e)
        self._center_widget()

    def _on_accept(self):
        text = self.input.text().strip()
        if not text:
            return
        self.confirmed.emit(text)
        self.urlConfirmed.emit(text)  # 兼容旧用法
        self.accept()

    def showEvent(self, e):
        super().showEvent(e)
        self.input.setFocus()
        # 选中输入框中的文本（方便直接粘贴覆盖）
        self.input.selectAll()


# 向后兼容别名（旧的 URL 引用代码）
UrlInputDialog = SingleInputDialog


class InfoDialog(MaskDialogBase):
    """通用信息提示弹框 — 单按钮确认，替代 qfluentwidgets MessageBox（仅确认按钮场景）

    统一 MaskDialogBase 风格。
    可选传 dismiss_text 增加次级「不再提醒」按钮（点击 emit dismissed）。
    """

    confirmed = pyqtSignal()
    dismissed = pyqtSignal()

    DEFAULT_WIDTH = 400
    DEFAULT_HEIGHT = 120  # 最小高度=标题+12+1行内容+按钮+边距；高度按内容自适应
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

        # 注：不在内容/按钮间加 addStretch()，短内容时按钮紧贴内容更紧凑。

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

        # 内容自适应：minSize 保底 + maxSize 防撑爆
        # 关键：必须把 widget 从 MaskDialogBase 的 QHBoxLayout 取出，
        # 否则 QHBoxLayout 默认让 widget 高度 = layout 高度（dialog 全屏）
        self.layout().removeWidget(self.widget)
        self.widget.setParent(self)
        self.widget.setMinimumSize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        self.widget.setMaximumSize(self.DEFAULT_MAX_WIDTH, self.DEFAULT_MAX_HEIGHT)
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
