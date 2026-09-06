# -*- coding: utf-8 -*-
"""工作台浮层共享 UI 组件（_EmptyHint / _SectionHeader）

被 app.widgets.workbench_panel（内置 fallback）和 plugins/system/ui/_artifacts_page.py
（系统插件版）共用，避免重复定义。

注意：本模块不应反向依赖 workbench_panel 或 plugins，避免循环导入。
"""

from typing import Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel
from qfluentwidgets import TransparentToolButton

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css, get_icon


class _EmptyHint(QLabel):
    """页面空态提示"""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setWordWrap(True)
        self.refresh_style()

    def refresh_style(self) -> None:
        self.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(12)}; padding: 32px 16px;"
        )


class _SectionHeader(QFrame):
    """页签内容小节头：图标 + 标题 + 右侧统计/操作"""

    def __init__(self, title: str, icon_name: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchSectionHeader")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(6)
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(16, 16)
        self._icon_label.setScaledContents(True)
        self._icon_label.setVisible(bool(icon_name))
        self._icon_name = icon_name  # 存名供 refresh_style 重取 pixmap（SVG 着色随主题）
        if icon_name:
            self.set_icon_name(icon_name)
        self._title_label = QLabel(title, self)
        self._extra_label = QLabel("", self)
        self._action_btn: Optional[TransparentToolButton] = None
        layout.addWidget(self._icon_label)
        layout.addWidget(self._title_label)
        layout.addStretch(1)
        layout.addWidget(self._extra_label)
        self.refresh_style()

    def set_icon_name(self, icon_name: str) -> None:
        self._icon_label.setPixmap(get_icon(icon_name).pixmap(16, 16))

    def set_extra(self, text: str) -> None:
        self._extra_label.setText(text)

    def set_action(self, icon_name: str, tooltip: str, callback) -> TransparentToolButton:
        """在标题右侧添加一个 icon-only 操作按钮（无文本标签）"""
        if self._action_btn is not None:
            self._action_btn.hide()
            self._action_btn.deleteLater()
            self._action_btn = None
        btn = TransparentToolButton(get_icon(icon_name), self)
        btn.setFixedSize(22, 22)
        btn.setToolTip(tooltip)
        btn.clicked.connect(callback)
        self.layout().insertWidget(self.layout().count() - 1, btn)
        self._action_btn = btn
        self._action_icon_name = icon_name
        return btn

    def hide_action(self) -> None:
        if self._action_btn is not None:
            self._action_btn.hide()

    def show_action(self) -> None:
        if self._action_btn is not None:
            self._action_btn.show()

    def refresh_style(self) -> None:
        # 重取图标 pixmap：get_icon 按当前主题着色 SVG，构造期 pixmap 已固化
        if self._icon_name:
            self._icon_label.setPixmap(get_icon(self._icon_name).pixmap(16, 16))
        # 操作按钮图标同理重建（TransparentToolButton setIcon 固化）
        if self._action_btn is not None:
            self._action_btn.setIcon(get_icon(self._action_icon_name))
        self.setStyleSheet(
            "QFrame#workbenchSectionHeader { background: transparent; border: none; }"
            f" QLabel {{ color: {Colors.TEXT_SECONDARY}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(12)}; }}"
        )
