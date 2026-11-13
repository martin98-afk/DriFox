# -*- coding: utf-8 -*-
"""托盘右键菜单（Fluent 圆角风格）。

在 qfluentwidgets ``SystemTrayMenu`` 基础上补一项能力：菜单项右侧可显示
灰色提示文本（如当前版本号），绘制风格与自带快捷键文本一致。

两个来自 qfluentwidgets 源码的约束，改动前务必知道：
1. ``RoundMenu.clear()`` 只清 action 与 subMenu，清不掉 ``addSeparator()``
   插入的裸 QListWidgetItem —— 反复重建会累积分隔线。故菜单**构建一次**，
   不重建 action，改挂 ``aboutToShow`` 做**就地刷新**（见下）。
2. 提示文本必须在建项时就计入右侧留白（宽度在 addAction 时算一次），
   否则会与菜单项文字重叠 —— 见 ``_longestShortcutWidth`` 覆写。

字体刷新链为何要自己接（2026-10-13 修）：
- 菜单是独立弹窗、不在任何窗口树内，主窗口的字体刷新链
  （``apply_font_size_to_widget`` 走 findChildren）够不到它；
- qfluentwidgets 的 ``setFontFamilies`` 只在启动时同步一次，运行期改字体
  （llm_font_family / ui_font_size）不会传播到这里；
- 主题色走框架 ``dirty-qss`` 补刷链（弹出时自动重设 QSS），字体不会。
故本类在 ``aboutToShow`` 自检字体签名，变了才重设 —— 系统托盘右键
（``popup()``）与 ``exec()`` 均会触发 ``aboutToShow``，弹出前完成刷新。
"""

import sys
from typing import Callable

from PyQt5.QtCore import QModelIndex, QSize, Qt
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter
from PyQt5.QtWidgets import QAction, QStyle, QStyleOptionViewItem
from qfluentwidgets import Action, FluentIconBase, SystemTrayMenu, Theme, isDarkTheme
from qfluentwidgets.components.widgets.menu import ShortcutMenuItemDelegate

# 提示文本存放在 action 的动态属性里（Qt 原生无「右侧说明文本」概念）
HINT_PROPERTY = "hintText"

# 菜单项图标的 FluentIconBase 原体（弹出时按原生菜单明暗重设需要用）
SOURCE_ICON_PROPERTY = "sourceIcon"

_HINT_RIGHT_MARGIN = 20

# 菜单文字基础字号（与 qfluentwidgets menu.qss 的 font: 14px 对齐，并叠加
# DriFox 用户字号档位 delta）
_MENU_BASE_FONT_SIZE = 14


def _native_menu_icon_theme():
    """菜单项图标的取色主题；``None`` = 沿用 App 主题（原行为）

    macOS 上 ``QSystemTrayIcon`` 的菜单被 Qt 桥接成原生 NSMenu
    （``QCocoaSystemTrayIcon::statusItemClicked`` → ``popUpStatusItemMenu:``），
    背景与文字由系统按 NSApp 外观绘制，与 App 主题无关；图标却是我们传进去的
    位图，按 App 主题取色就会出现「App 深色 + 系统菜单浅色 → 白图标贴浅底」的
    不可见问题。故 macOS 单独探测原生菜单明暗，其余平台返回 None 走原逻辑。
    """
    if sys.platform != "darwin":
        return None
    try:
        from AppKit import NSAppearanceNameDarkAqua, NSApplication

        appearance = NSApplication.sharedApplication().effectiveAppearance()
        if appearance is None:
            return None
        return Theme.DARK if appearance.name() == NSAppearanceNameDarkAqua else Theme.LIGHT
    except Exception:
        return None


def _menu_font_sig() -> tuple:
    """当前配置的菜单字体签名（字族 + 实际像素字号）"""
    from app.utils.design_tokens import _get_global_font, scale_font_size

    return (_get_global_font(), scale_font_size(_MENU_BASE_FONT_SIZE))


def _hint_font() -> QFont:
    """提示文本字体：字族随菜单、字号减 2

    不用 qfluentwidgets 的 getFont：它的字族取自 qconfig.fontFamilies，
    该值只在启动时同步一次，运行期改字体不会更新。
    """
    font = QFont()
    font.setFamily(_menu_font_sig()[0])
    font.setPixelSize(_hint_font_px())
    return font


def _hint_font_px() -> int:
    """提示文本字号：菜单字号 - 2（不低于 8）"""
    return max(8, _menu_font_sig()[1] - 2)


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

        # 提示字号跟随菜单字号（与 _longestShortcutWidth 的计算保持一致）
        painter.setFont(_hint_font())
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
        # 上次应用的字体签名（family, 像素字号）；None = 尚未应用过
        self._applied_font_sig: tuple | None = None
        # 上次应用的图标取色主题；None = 尚未应用过
        self._applied_icon_theme = None
        # 弹出时自检字体与图标（系统托盘右键 popup() / exec() 均触发 aboutToShow）
        self.aboutToShow.connect(self._refresh_font_if_changed)
        self.aboutToShow.connect(self._refresh_icons_if_needed)

    # ========== 字体刷新（弹出时自检） ==========

    def _refresh_font_if_changed(self) -> None:
        """字体配置变化时重设 view 字体 QSS 并重算条目宽度（幂等）"""
        sig = _menu_font_sig()
        if sig == self._applied_font_sig:
            return
        self._apply_menu_font(sig)

    def _apply_menu_font(self, sig: tuple) -> None:
        """应用字体签名：view 字体 QSS + 条目宽度重算"""
        family, px = sig
        # view 自带 QSS（MenuActionListWidget{font: ...}）优先级高于 setFont，
        # 必须重写该 QSS 才能让字体生效 —— 与底部输入区同一坑（见
        # tests/widgets/test_input_area_font_style_refresh.py）。
        self.view.setStyleSheet(f"MenuActionListWidget{{font: {px}px '{family}'}}")
        # QSS 字体要经 style polish 才反映到 widget.font()/fontMetrics()；
        # aboutToShow 后紧接渲染，等不到下一轮事件循环，这里强制重 polish，
        # 让紧随其后的宽度重算吃到新字体度量（实测 setFont 会被 QSS 压制）。
        style = self.view.style()
        if style is not None:
            style.unpolish(self.view)
            style.polish(self.view)
        self.setItemHeight(max(28, px + 14))

        # 条目宽度按新字体度量重算（_adjustItemText 内自会重设文本与宽度）
        max_w = 0
        for action in self.menuActions():
            item = action.property("item")
            if item is not None:
                max_w = max(max_w, self._adjustItemText(item, action))

        # 分隔线宽度跟随重算后的条目宽度（否则大字号下分隔线短一截）
        sep_w = max(1, max_w + 2)
        for i in range(self.view.count()):
            row_item = self.view.item(i)
            if row_item is not None and row_item.data(Qt.DecorationRole) == "seperator":
                row_item.setSizeHint(QSize(sep_w, 9))

        # QListWidget.adjustSize 依赖 minimumSizeHint、只增不减（本方法内
        # 重算时 item 变小也不会收缩），先把 view 与 menu 的固定尺寸解锁再重算，
        # 否则字号调小后菜单宽度卡在大字号状态（实测 294→179 的 item 收缩
        # 反映不到 view/menu 上）。
        _QWIDGETSIZE_MAX = 16777215
        self.view.setMinimumSize(0, 0)
        self.view.setMaximumSize(_QWIDGETSIZE_MAX, _QWIDGETSIZE_MAX)
        self.view.adjustSize()
        self.setMinimumSize(0, 0)
        self.setMaximumSize(_QWIDGETSIZE_MAX, _QWIDGETSIZE_MAX)
        self.adjustSize()
        self._applied_font_sig = sig

    def add_hint_action(
        self,
        icon: FluentIconBase,
        text: str,
        hint: str,
        callback: Callable[[], None],
    ) -> Action:
        """添加右侧带提示文本的菜单项（如「检查更新        v0.6.1」）"""
        action = Action(icon, text, self)
        action.setProperty(SOURCE_ICON_PROPERTY, icon)
        action.setProperty(HINT_PROPERTY, hint)
        action.triggered.connect(callback)
        self.addAction(action)
        return action

    def add_icon_action(
        self,
        icon: FluentIconBase,
        text: str,
        callback: Callable[[], None],
    ) -> Action:
        """添加带图标的菜单项（macOS 上图标颜色由弹出前按原生菜单外观重设）"""
        action = Action(icon, text, self)
        action.setProperty(SOURCE_ICON_PROPERTY, icon)
        action.triggered.connect(callback)
        self.addAction(action)
        return action

    def _refresh_icons_if_needed(self) -> None:
        """按原生菜单的实际明暗重设菜单项图标（幂等）

        非 macOS 或外观未变 -> 直接返回，零开销。macOS 必须在每次弹出前重设：
        用户可能在会话中途切了系统外观，而菜单是构建一次、长期复用的对象。
        """
        theme = _native_menu_icon_theme()
        if theme is None or theme == self._applied_icon_theme:
            return

        for action in self.menuActions():
            source = action.property(SOURCE_ICON_PROPERTY)
            if source is not None:
                # 必须清掉 fluentIcon：Action.icon() 有它就走 FluentIconEngine，
                # 绘制时按 App 主题重算颜色，setIcon 传进去的颜色会被覆盖。
                action.fluentIcon = None
                action.setIcon(source.icon(theme))

        self._applied_icon_theme = theme

    def _longestShortcutWidth(self) -> int:
        """右侧留白取「快捷键文本」与「提示文本」的较宽者，避免文字重叠

        提示文本按菜单当前字号缩放（不再固定 getFont(12)），否则改大字号后
        提示文本会与菜单项文字重叠（本方法只在建项时被调用一次）。
        """
        width = super()._longestShortcutWidth()
        metrics = QFontMetrics(_hint_font())
        for action in self.menuActions():
            hint = action.property(HINT_PROPERTY)
            if hint:
                width = max(width, metrics.width(hint))
        return width
