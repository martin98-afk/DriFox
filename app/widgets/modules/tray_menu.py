# -*- coding: utf-8 -*-
"""托盘右键菜单（Fluent 圆角风格）。

在 qfluentwidgets ``SystemTrayMenu`` 基础上补一项能力：菜单项右侧可显示
灰色提示文本（如当前版本号），绘制风格与自带快捷键文本一致。

两个来自 qfluentwidgets 源码的约束，改动前务必知道：
1. ``RoundMenu.clear()`` 只清 action 与 subMenu，清不掉 ``addSeparator()``
   插入的裸 QListWidgetItem —— 反复重建会累积分隔线。故菜单**构建一次**，
   不再挂在 ``aboutToShow`` 上重建。
2. 提示文本必须在建项时就计入右侧留白（宽度在 addAction 时算一次），
   否则会与菜单项文字重叠 —— 见 ``_longestShortcutWidth`` 覆写。
"""

from typing import Callable

from PyQt5.QtCore import QModelIndex, Qt
from PyQt5.QtGui import QColor, QFontMetrics, QPainter
from PyQt5.QtWidgets import QAction, QStyle, QStyleOptionViewItem
from qfluentwidgets import Action, FluentIconBase, SystemTrayMenu, getFont, isDarkTheme
from qfluentwidgets.components.widgets.menu import ShortcutMenuItemDelegate

# 提示文本存放在 action 的动态属性里（Qt 原生无「右侧说明文本」概念）
HINT_PROPERTY = "hintText"

_HINT_FONT_SIZE = 12
_HINT_RIGHT_MARGIN = 20


class HintMenuItemDelegate(ShortcutMenuItemDelegate):
    """菜单项委托：在右侧绘制灰色提示文本（版本号等）"""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        hint = self._hint_of(index)
        if not hint:
            return

        painter.save()

        if not option.state & QStyle.State_Enabled:
            painter.setOpacity(0.5 if isDarkTheme() else 0.6)

        painter.setFont(getFont(_HINT_FONT_SIZE))
        painter.setPen(QColor(255, 255, 255, 200) if isDarkTheme() else QColor(0, 0, 0, 153))
        painter.drawText(
            option.rect.adjusted(0, 0, -_HINT_RIGHT_MARGIN, 0),
            Qt.AlignRight | Qt.AlignVCenter,
            hint,
        )

        painter.restore()

    def _hint_of(self, index: QModelIndex) -> str:
        """取该菜单项的提示文本（分隔线与无提示的项返回空串）"""
        if self._isSeparator(index):
            return ""

        action = index.data(Qt.UserRole)
        if not isinstance(action, QAction):
            return ""

        return action.property(HINT_PROPERTY) or ""


class TrayContextMenu(SystemTrayMenu):
    """托盘右键菜单：Fluent 圆角风格 + 菜单项右侧提示文本"""

    def __init__(self, parent=None):
        super().__init__("", parent)
        self.view.setItemDelegate(HintMenuItemDelegate(self.view))

    def add_hint_action(
        self,
        icon: FluentIconBase,
        text: str,
        hint: str,
        callback: Callable[[], None],
    ) -> Action:
        """添加右侧带提示文本的菜单项（如「检查更新        v0.6.1」）"""
        action = Action(icon, text, self)
        action.setProperty(HINT_PROPERTY, hint)
        action.triggered.connect(callback)
        self.addAction(action)
        return action

    def _longestShortcutWidth(self) -> int:
        """右侧留白取「快捷键文本」与「提示文本」的较宽者，避免文字重叠"""
        width = super()._longestShortcutWidth()
        metrics = QFontMetrics(getFont(_HINT_FONT_SIZE))
        for action in self.menuActions():
            hint = action.property(HINT_PROPERTY)
            if hint:
                width = max(width, metrics.width(hint))
        return width
