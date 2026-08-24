# -*- coding: utf-8 -*-
"""ReplaceTabBar — 替换类型(full 容器)卡片顶部居中切换栏

设计见 docs/superpowers/specs/2026-08-24-replace-tab.md：
- 仅当 ≥2 个 full 卡片打开时显示；单卡片/无卡片时隐藏。
- 列出所有「打开的」替换内容；点击 tab 切换显示、点击 × 关闭。
"""

from typing import Dict, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QWidget,
)

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css, get_unified_font

# 常驻「对话」tab 的特殊 card_id（点击返回对话区，无关闭按钮）
CONVERSATION_ID = "__drifox_conversation__"

# 已知「内置替换」全局卡片 card_id（与 UI 插件 full 浮动卡片共用 TOP 覆盖层、互斥显示）
KNOWN_GLOBAL_REPLACE_CARDS = frozenset(
    {"settings", "provider_edit", "hook_edit", "mcp_edit", "diff_viewer", "sub_agent_session", "file_undo"}
)
GLOBAL_REPLACE_TITLES = {
    "settings": "系统设置",
    "provider_edit": "服务商编辑",
    "hook_edit": "Hook 编辑",
    "mcp_edit": "MCP 编辑",
    "diff_viewer": "文件差异对比",
    "sub_agent_session": "子智能体会话",
    "file_undo": "文件撤销",
}


class ReplaceTabButton(QWidget):
    """单个替换内容 tab：标题 + × 关闭按钮"""

    clicked = pyqtSignal(str)  # card_id
    closeClicked = pyqtSignal(str)  # card_id

    def __init__(self, card_id: str, title: str, parent: Optional[QWidget] = None, is_conversation: bool = False):
        super().__init__(parent)
        self.setObjectName("ReplaceTabButtonConversation" if is_conversation else "ReplaceTabButton")
        self._card_id = card_id
        self._active = False
        self._is_conversation = is_conversation

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 2, 8, 2)
        layout.setSpacing(8)

        self._label = QLabel(title, self)
        self._label.setFont(get_unified_font(12))
        # 鼠标事件穿透到父 widget，使整卡点击触发切换
        self._label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self._label)

        if not is_conversation:
            self._close = QPushButton("×", self)
            self._close.setFixedSize(18, 18)
            self._close.setCursor(Qt.PointingHandCursor)
            self._close.clicked.connect(lambda: self.closeClicked.emit(self._card_id))
            layout.addWidget(self._close)

        self.setCursor(Qt.PointingHandCursor)
        self.refresh_style()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._card_id)
        super().mousePressEvent(event)

    def set_active(self, active: bool) -> None:
        if self._active != active:
            self._active = active
            self.refresh_style()

    def refresh_style(self) -> None:
        if self._active:
            # 当前所在 tab：蓝底高亮 + 蓝边 + 亮字，一眼可辨
            bg = Colors.TAB_ACTIVE_BG
            border = Colors.TAG_ACCENT
            label_color = Colors.TEXT_PRIMARY
            hover_bg = Colors.SELECTED_BG
        else:
            # 非当前 tab：透明底 + 自适应明暗的次要文字色，hover 给主题色高亮反馈
            bg = "transparent"
            border = "transparent"
            label_color = Colors.TEXT_SECONDARY
            hover_bg = Colors.HOVER_BG
        self.setStyleSheet(
            f"QWidget#{self.objectName()} {{ background: {bg}; "
            f"border: 1px solid {border}; border-radius: 7px; }}"
            f"QWidget#{self.objectName()}:hover {{ background: {hover_bg}; }}"
        )
        self._label.setStyleSheet(
            f"color: {label_color}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(12)}"
        )
        if not self._is_conversation:
            self._close.setStyleSheet(
                f"QPushButton {{ color: {Colors.TEXT_MUTED}; background: transparent; "
                f"border: none; border-radius: 9px; font-size: 14px; padding: 0px; }}"
                f"QPushButton:hover {{ color: {Colors.TEXT_PRIMARY}; "
                f"background: {Colors.HOVER_BG}; }}"
            )


class ReplaceTabBar(QWidget):
    """替换内容顶部居中切换栏"""

    tabClicked = pyqtSignal(str)  # card_id
    tabCloseClicked = pyqtSignal(str)  # card_id

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("replaceTabBar")
        self._buttons: Dict[str, ReplaceTabButton] = {}
        self._active_id: Optional[str] = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(8)
        layout.addSpacerItem(QSpacerItem(0, 0, QSizePolicy.Expanding, QSizePolicy.Minimum))
        self._btn_container = QWidget(self)
        self._btn_layout = QHBoxLayout(self._btn_container)
        self._btn_layout.setContentsMargins(0, 0, 0, 0)
        self._btn_layout.setSpacing(8)
        # 常驻「对话」按钮：始终位于 replace 按钮最左，点击返回对话区
        self._conv_btn = ReplaceTabButton(CONVERSATION_ID, "对话", is_conversation=True)
        self._conv_btn.clicked.connect(self.tabClicked.emit)
        self._btn_layout.addWidget(self._conv_btn)
        layout.addWidget(self._btn_container)
        layout.addSpacerItem(QSpacerItem(0, 0, QSizePolicy.Expanding, QSizePolicy.Minimum))
        self.refresh_style()

    def set_tabs(self, open_dict: Dict[str, str], active_id: Optional[str]) -> None:
        """用 open 集合重建 tab 栏（保留顺序）"""
        for cid in list(self._buttons.keys()):
            if cid not in open_dict:
                self.remove_tab(cid)
        for cid, title in open_dict.items():
            if cid not in self._buttons:
                self._add_button(cid, title)
        self.set_active(active_id)

    def add_tab(self, card_id: str, title: str) -> None:
        if card_id not in self._buttons:
            self._add_button(card_id, title)

    def remove_tab(self, card_id: str) -> None:
        btn = self._buttons.pop(card_id, None)
        if btn is not None:
            self._btn_layout.removeWidget(btn)
            btn.deleteLater()
        if self._active_id == card_id:
            self._active_id = None

    def set_active(self, card_id: Optional[str]) -> None:
        self._active_id = card_id
        for cid, btn in self._buttons.items():
            btn.set_active(cid == card_id)
        # 「对话」常驻按钮：active 时高亮（与 replace 按钮一致）
        self._conv_btn.set_active(card_id == CONVERSATION_ID)

    def _add_button(self, card_id: str, title: str) -> None:
        btn = ReplaceTabButton(card_id, title, self._btn_container)
        btn.clicked.connect(self.tabClicked.emit)
        btn.closeClicked.connect(self.tabCloseClicked.emit)
        self._buttons[card_id] = btn
        # 插入到常驻「对话」按钮之后（index 0 为对话按钮）
        self._btn_layout.insertWidget(1, btn)

    def refresh_style(self) -> None:
        # 顶部切换栏：淡卡片色背景 + 底边分隔线，使其"成栏"而非裸按钮漂浮
        self.setStyleSheet(
            "QWidget#replaceTabBar { "
            f"background: {Colors.CARD_BG.format(alpha=235)}; "
            f"border-bottom: 1px solid {Colors.DIVIDER_COLOR}; "
            "border-radius: 0px; }"
        )
        self._conv_btn.refresh_style()
        for btn in self._buttons.values():
            btn.refresh_style()
