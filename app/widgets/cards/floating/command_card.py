# -*- coding: utf-8 -*-
"""
斜杠命令卡片 - 输入框上方展开，显示命令和技能列表

触发方式：在输入框输入 / 后，卡片自动展开
数据来源：CommandManager 内置命令 + get_local_skills()
交互方式：↑/↓ 导航，Enter 选中，Esc 关闭
"""

import html
from typing import Any, Dict, List, Optional

from loguru import logger
from PyQt5.QtCore import QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.command_manager import CommandManager, CommandParameter, CommandType
from app.core.ui_plugin_registry import UIPluginRegistry
from app.utils.design_tokens import Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import get_font_family_css, get_local_skills, get_skill_by_name
from app.widgets.cards.card_container import CardContainer
from app.widgets.elided_label import _ElidedLabel

ITEM_HEIGHT = 36  # 每个 item 高度
MAX_VISIBLE_ITEMS = 8  # 最多同时显示 item 数


class CommandItemWidget(QWidget):
    """命令/技能列表单项"""

    clicked = pyqtSignal()
    hovered = pyqtSignal(object)  # 鼠标悬停时发射自身引用

    def __init__(self, item_data: Dict[str, str], query: str, parent=None):
        super().__init__(parent)
        self._data = item_data
        self._query = query
        self._hovered = False
        self._selected = False
        self.setFixedHeight(ITEM_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self._setup_ui()

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(8)

        # 名称标签（不压缩，显示完整名称）
        self._name_label = QLabel()
        self._name_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._name_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        layout.addWidget(self._name_label)

        # 描述标签（Elided，空间不够时省略，仅技能显示描述）
        desc = self._data.get("description", "")
        self._desc_label = _ElidedLabel(desc)
        self._desc_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._desc_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._desc_label.setMinimumWidth(0)
        layout.addWidget(self._desc_label, 1)

        # 快捷键标签（仅内建命令的 function 类型显示）
        self._shortcut_label = QLabel()
        self._shortcut_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._shortcut_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        layout.addWidget(self._shortcut_label)

        # 类型标签（始终创建，根据 item 类型动态显示/隐藏）
        self._tag_label = QLabel()
        self._tag_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._tag_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        layout.addWidget(self._tag_label)
        self._update_tag_visibility()

        # 快捷键文本（仅 command 类型且有快捷键时显示）
        # UI 插件命令不显示快捷键
        shortcut = self._data.get("shortcut", "")
        item_type = self._data["type"]
        is_ui_plugin = item_type == "command" and self._data.get("subtype") == "ui_plugin"
        if item_type == "command" and shortcut and not is_ui_plugin:
            self._shortcut_label.setText(shortcut)
            self._shortcut_label.setVisible(True)
        else:
            self._shortcut_label.setVisible(False)

        # 设置快捷键静态样式（只在创建时设置一次，避免每次导航都触发 setStyleSheet）
        # 注意：_name_label 和 _desc_label 的样式在 _apply_style 中动态更新
        self._shortcut_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_SECONDARY};
                {get_font_family_css()} {font_size_css(10)};
                background: rgba(128,128,128,0.1);
                border-radius: 3px;
                padding: 1px 5px;
                font-weight: bold;
            }}
        """)

        self._apply_style()
        self._update_display()

    def _apply_style(self):
        """应用当前状态的样式（仅更新变化的颜色，静态样式在 _setup_ui 中已设置）"""
        # Colors.refresh() 不在此处调用——颜色在 show_card 时刷新一次即可
        # 避免每次导航/悬停都读配置文件

        if self._selected:
            bg = Colors.REALTIME_TAG_BG
        elif self._hovered:
            bg = Colors.HOVER_BG
        else:
            bg = "transparent"

        self.setStyleSheet(f"""
            CommandItemWidget {{
                background-color: {bg};
                border: none;
                border-radius: 4px;
            }}
        """)

        # 描述样式（选中时变亮）
        desc_fg = Colors.TEXT_PRIMARY if self._selected else Colors.TEXT_SECONDARY
        self._desc_label.setStyleSheet(f"""
            QLabel {{
                color: {desc_fg};
                {get_font_family_css()} {font_size_css(11)};
                background: transparent;
            }}
        """)

        # 标签样式：UI 插件绿色，技能蓝色，智能体紫色，提示词橙色
        item_type = self._data["type"]
        is_ui_plugin = item_type == "command" and self._data.get("subtype") == "ui_plugin"
        if is_ui_plugin:
            tag_fg = Colors.TAG_GREEN if not self._selected else Colors.TAG_GREEN_TEXT
            self._tag_label.setStyleSheet(f"""
                QLabel {{
                    color: {tag_fg};
                    {get_font_family_css()} {font_size_css(11)};
                    font-weight: bold;
                    background: transparent;
                }}
            """)
        elif item_type == "skill":
            tag_fg = Colors.TAG_ACCENT if not self._selected else Colors.TAG_ACCENT_TEXT
            self._tag_label.setStyleSheet(f"""
                QLabel {{
                    color: {tag_fg};
                    {get_font_family_css()} {font_size_css(11)};
                    font-weight: bold;
                    background: transparent;
                }}
            """)
        elif item_type == "agent":
            tag_fg = Colors.TAG_PURPLE if not self._selected else Colors.TAG_PURPLE_TEXT
            self._tag_label.setStyleSheet(f"""
                QLabel {{
                    color: {tag_fg};
                    {get_font_family_css()} {font_size_css(11)};
                    font-weight: bold;
                    background: transparent;
                }}
            """)
        elif item_type == "prompt":
            tag_fg = Colors.TAG_ORANGE if not self._selected else Colors.TAG_ORANGE_TEXT
            self._tag_label.setStyleSheet(f"""
                QLabel {{
                    color: {tag_fg};
                    {get_font_family_css()} {font_size_css(11)};
                    font-weight: bold;
                    background: transparent;
                }}
            """)

        # 名称样式
        fg = Colors.TEXT_PRIMARY if self._selected else Colors.TEXT_PRIMARY
        self._name_label.setStyleSheet(f"""
            QLabel {{
                color: {fg};
                {get_font_family_css()} {font_size_css(13)};
                background: transparent;
            }}
        """)

        # 快捷键标签样式：类键盘键帽风格，加粗（仅非 UI 插件的命令）
        shortcut = self._data.get("shortcut", "")
        if item_type == "command" and shortcut and not is_ui_plugin:
            shortcut_fg = Colors.TEXT_SECONDARY
            self._shortcut_label.setStyleSheet(f"""
                QLabel {{
                    color: {shortcut_fg};
                    {get_font_family_css()} {font_size_css(10)};
                    background: rgba(128,128,128,0.1);
                    border-radius: 3px;
                    padding: 1px 5px;
                    font-weight: bold;
                }}
            """)

    @staticmethod
    def _all_highlight_queries(text: str, query: str) -> List[str]:
        """从多关键字 query 中提取所有能匹配到 text 的关键字"""
        if not query or not text:
            return []
        text_lower = text.lower()
        if query.lower() in text_lower:
            return [query]
        found = []
        for or_term in query.split("|"):
            or_term = or_term.strip()
            if not or_term:
                continue
            for and_part in or_term.split("&"):
                and_part = and_part.strip()
                if and_part and and_part.lower() in text_lower and and_part not in found:
                    found.append(and_part)
        return found

    def _update_display(self):
        """更新名称显示（含多关键字查询高亮）

        注意：display_text 中的 & < > " 等字符须 html.escape，
        否则混入 HTML 会破坏渲染导致卡片消失。
        """
        name = self._data["name"]
        display_name = self._data.get("display_name", name)
        item_type = self._data["type"]
        display_text = f"/{display_name}" if item_type == "command" else display_name
        query = self._query

        # 先 HTML 转义纯文本，防止 & 等字符破坏 HTML 渲染
        safe_text = html.escape(display_text)

        if query:
            hls = self._all_highlight_queries(display_text, query)
            if hls:
                # 在 safe_text（已 escape）中定位每个关键字，从原文一次构建 HTML
                lower_safe = safe_text.lower()
                spans = []
                for hl in hls:
                    escaped_hl = html.escape(hl)
                    lower_hl = escaped_hl.lower()
                    idx = lower_safe.find(lower_hl)
                    if idx >= 0:
                        spans.append((idx, idx + len(escaped_hl)))
                if spans:
                    spans.sort()
                    merged = [spans[0]]
                    for s in spans[1:]:
                        if s[0] <= merged[-1][1]:
                            merged[-1] = (merged[-1][0], max(merged[-1][1], s[1]))
                        else:
                            merged.append(s)
                    # 从 safe_text 一次构建：普通部分直接拼接，匹配部分加 <span>
                    parts = []
                    pos = 0
                    for start, end in merged:
                        if pos < start:
                            parts.append(safe_text[pos:start])
                        parts.append(
                            f'<span style="color: {Colors.SEND_BTN_START}; font-weight: bold;">'
                            f"{safe_text[start:end]}</span>"
                        )
                        pos = end
                    if pos < len(safe_text):
                        parts.append(safe_text[pos:])
                    self._name_label.setText("".join(parts))
                else:
                    self._name_label.setText(safe_text)
            else:
                self._name_label.setText(safe_text)
        else:
            self._name_label.setText(safe_text)

        # 描述标签也应用多关键字搜索高亮
        desc = self._data.get("description", "")
        self._desc_label.setText(desc)
        if query:
            hls = self._all_highlight_queries(desc, query)
            if hls:
                self._desc_label.setHighlights(hls, Colors.SEND_BTN_START)

    def _update_tag_visibility(self):
        """根据 item 类型和 subtype 更新标签文本和可见性"""
        item_type = self._data["type"]
        is_ui_plugin = item_type == "command" and self._data.get("subtype") == "ui_plugin"
        if is_ui_plugin:
            self._tag_label.setText("【UI】")
            self._tag_label.setVisible(True)
        elif item_type == "skill":
            self._tag_label.setText("【技能】")
            self._tag_label.setVisible(True)
        elif item_type == "agent":
            self._tag_label.setText("【智能体】")
            self._tag_label.setVisible(True)
        elif item_type == "prompt":
            self._tag_label.setText("【提示词】")
            self._tag_label.setVisible(True)
        else:
            self._tag_label.setVisible(False)

    def set_selected(self, selected: bool):
        """设置选中状态

        鼠标 hover 时 _selected 与 _hovered 同时为 True（hover 即选中）。
        leaveEvent 只清 _hovered，保持 _selected，确保鼠标离开后键盘仍可继续导航。
        """
        self._selected = selected
        # 不再在 selected 时清空 _hovered —— hover 即选中，两者共存
        self._apply_style()

    def reuse(self, item_data: dict, query: str):
        """复用 widget，重置状态并更新数据

        防止前一次鼠标悬停/选中状态残留到新生命周期。
        """
        self._data = item_data
        self._query = query
        self._hovered = False
        self._selected = False
        self._update_display()
        # 刷新标签可见性
        self._update_tag_visibility()
        # 刷新快捷键标签
        item_type = item_data["type"]
        is_ui_plugin = item_type == "command" and item_data.get("subtype") == "ui_plugin"
        shortcut = item_data.get("shortcut", "")
        if item_type == "command" and shortcut and not is_ui_plugin:
            self._shortcut_label.setText(shortcut)
            self._shortcut_label.setVisible(True)
        else:
            self._shortcut_label.setVisible(False)
        self._apply_style()

    def enterEvent(self, event):
        self._hovered = True
        # hover 即选中：通知父卡片同步选中索引到此 widget
        self.hovered.emit(self)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        if not self._selected:
            self._apply_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    @property
    def item_data(self) -> Dict[str, str]:
        return self._data


class ParameterItemWidget(QWidget):
    """detail 模式参数列表单项

    样式与 CommandItemWidget 一致，但更简洁（无类型标签，固定显示名称+描述）
    """

    clicked = pyqtSignal()
    hovered = pyqtSignal(object)  # 鼠标悬停时发射自身引用

    def __init__(self, param: CommandParameter, parent=None):
        super().__init__(parent)
        self._param = param
        self._hovered = False
        self._selected = False
        self._active = False
        self.setFixedHeight(ITEM_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self._setup_ui()

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(8)

        # 参数名（必填：前缀红色 ERROR 星号；可选：纯参数名）
        # 使用 HTML 让 QLabel 自动按 span 着色；color 不走 stylesheet，
        # 而由 _update_name_label_text 内的 HTML <span> 控制
        self._name_label = QLabel()
        self._name_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._name_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        layout.addWidget(self._name_label)

        # 参数说明（占据 stretch 1，最大化利用空间）
        self._desc_label = None
        if self._param.description:
            self._desc_label = _ElidedLabel(self._param.description)
            self._desc_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._desc_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            self._desc_label.setMinimumWidth(0)
            layout.addWidget(self._desc_label, 1)

        # 集中应用一次静态样式（颜色 / 字体 / 背景由主题 token 驱动）
        self._apply_subwidget_styles()

    def _apply_subwidget_styles(self):
        """刷新 ParameterItemWidget 的子标签样式（颜色、字体、背景）

        颜色 / 字体 / 背景全部由主题 token 驱动；切主题后通过 refresh_style 再次调用即可。
        字体大小/族使用 font_size_css + get_font_family_css 与全局 UI 字号联动。

        视觉规范：
        - 参数名：font-size 12、bold；默认色 SEND_BTN_START，必填时 HTML <span> 覆盖星号为 ERROR 红色
        - 参数说明：font-size 11、TEXT_SECONDARY、stretch 撑满
        """
        Colors.refresh()
        font_css = get_font_family_css()
        # 参数名（12px + bold；默认色 SEND_BTN_START，必填时 HTML <span> 覆盖星号色为 ERROR）
        if hasattr(self, "_name_label") and self._name_label is not None:
            self._name_label.setStyleSheet(f"""
                QLabel {{
                    color: {Colors.SEND_BTN_START};
                    background: transparent;
                    font-weight: bold;
                    {font_css} {font_size_css(12)};
                }}
            """)
            self._update_name_label_text()
        # 参数说明（次要：11px + TEXT_SECONDARY，stretch 撑满剩余空间）
        if getattr(self, "_desc_label", None) is not None:
            self._desc_label.setStyleSheet(f"""
                QLabel {{
                    color: {Colors.TEXT_SECONDARY};
                    background: transparent;
                    {font_css} {font_size_css(11)};
                }}
            """)

    def _update_name_label_text(self):
        """根据必填/可选和激活状态更新参数名

        必填时前缀红色 ERROR 星号；已激活参数显示 ✓ 前缀 + 次要色。
        QLabel 在 RichText 模式下，未包裹在带 color 的 span 内的文本
        会用默认调色板色（通常为黑），不会继承 stylesheet color。
        因此把参数名也用 <span> 显式包裹，统一走 SEND_BTN_START（或 TEXT_SECONDARY 当已激活）。
        """
        if not hasattr(self, "_name_label") or self._name_label is None:
            return
        escaped = html.escape(self._param.name)
        star = f'<span style="color: {Colors.ERROR};">*</span>' if self._param.required else ""
        check = f'<span style="color: {Colors.REALTIME_SUCCESS};">✓ </span>' if self._active else ""
        color = Colors.TEXT_SECONDARY if self._active else Colors.SEND_BTN_START
        self._name_label.setText(f'{check}{star}<span style="color: {color};">{escaped}</span>')

    @property
    def param_name(self) -> str:
        return self._param.name

    @property
    def param_type(self) -> str:
        return self._param.param_type

    def set_selected(self, selected: bool):
        """设置选中状态

        鼠标 hover 时 _selected 与 _hovered 同时为 True（hover 即选中）。
        leaveEvent 只清 _hovered，保持 _selected，确保鼠标离开后键盘仍可继续导航。
        """
        self._selected = selected
        # 不再在 selected 时清空 _hovered —— hover 即选中，两者共存
        self._apply_style()

    def set_active(self, active: bool):
        """设置激活状态：参数已在输入文本中出现时标记为已激活

        激活的参数保持可见（不隐藏），视觉上保留 ✓ 前缀 + 次要色参数名，
        但不再覆盖背景色，方便 hover 效果穿透，避免取消参数时看不出悬停哪个。
        """
        self._active = active
        self._apply_subwidget_styles()
        self._apply_style()

    @property
    def is_active(self) -> bool:
        return self._active

    def refresh_style(self):
        """响应主题切换：刷新子标签样式 + 自身 hover/selected 背景"""
        self._apply_subwidget_styles()
        self._apply_style()

    def _apply_style(self):
        Colors.refresh()
        if self._selected:
            bg = Colors.REALTIME_TAG_BG
        elif self._hovered:
            bg = Colors.HOVER_BG
        else:
            bg = "transparent"
        self.setStyleSheet(f"""
            QWidget {{ background: {bg}; border-radius: 4px; }}
        """)

    def enterEvent(self, event):
        self._hovered = True
        # hover 即选中：通知父卡片同步选中索引到此 widget
        self.hovered.emit(self)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        # selected 仍可能为 True（hover 即选中），此时背景仍为 REALTIME_TAG_BG
        self._apply_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class ValueItemWidget(QWidget):
    """值选择列表（枚举列表）单项

    与 CommandItemWidget 一致：hover 即选中，leaveEvent 只清 _hovered 保持 _selected。
    比 CommandItemWidget 更简单——无描述/快捷键/类型标签，只显示纯文本值。
    """

    clicked = pyqtSignal()
    hovered = pyqtSignal(object)  # 鼠标悬停时发射自身引用

    def __init__(self, value: str, parent=None):
        super().__init__(parent)
        self._value = value
        self._hovered = False
        self._selected = False
        self.setFixedHeight(ITEM_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self._setup_ui()

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(0)

        self._text_label = QLabel(self._value)
        self._text_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._text_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self._text_label)

        self._apply_style()

    def _apply_style(self):
        """应用当前状态的样式（仅自身背景，文字颜色固定为 PRIMARY）"""
        if self._selected:
            bg = Colors.REALTIME_TAG_BG
        elif self._hovered:
            bg = Colors.HOVER_BG
        else:
            bg = "transparent"

        self.setStyleSheet(f"""
            ValueItemWidget {{
                background-color: {bg};
                border: none;
                border-radius: 4px;
            }}
        """)
        self._text_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                background: transparent;
                {get_font_family_css()} {font_size_css(12)};
            }}
        """)

    def set_selected(self, selected: bool):
        """设置选中状态

        鼠标 hover 时 _selected 与 _hovered 同时为 True（hover 即选中）。
        leaveEvent 只清 _hovered，保持 _selected，确保鼠标离开后键盘仍可继续导航。
        """
        self._selected = selected
        # 不再在 selected 时清空 _hovered —— hover 即选中，两者共存
        self._apply_style()

    def enterEvent(self, event):
        self._hovered = True
        # hover 即选中：通知父卡片同步选中索引到此 widget
        self.hovered.emit(self)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        # selected 仍可能为 True（hover 即选中），此时背景仍为 REALTIME_TAG_BG
        self._apply_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    @property
    def value(self) -> str:
        return self._value


class CommandCard(QWidget):
    """斜杠命令卡片"""

    commandSelected = pyqtSignal(str, str)  # name, display_type（"command"/"prompt"/"agent"/"skill"/""）
    dismissed = pyqtSignal()  # 卡片被关闭
    parameterSelected = pyqtSignal(str, str)  # param_name, param_type — 参数项被点击
    parameterDeselected = pyqtSignal(str, str)  # param_name, param_type — 已激活参数被再次点击（取消选中）
    parameterValueSelected = pyqtSignal(str)  # value — --model= 的值被选中

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_items: List[Dict[str, str]] = []
        self._all_items_cache: List[Dict[str, str]] = []  # 缓存，避免每次敲击都读磁盘
        self._cache_dirty: bool = True  # 缓存脏标记，热重载后置 True
        self._filtered_items: List[Dict[str, str]] = []
        self._selected_index = 0
        self._last_selected_index = -1  # 上次选中索引，用于增量更新
        self._item_widgets: List[CommandItemWidget] = []
        self._dividers: List[QFrame] = []  # 分区间的分隔线列表
        self._visible = False
        self._current_query = ""
        self._current_text_query = ""  # 去除类别过滤器后的纯文本 query，用于 widget 高亮
        self._last_query = ""  # 上次过滤的 query，用于增量剪枝
        self._current_selected_type: str = ""  # 当前选中项的 display_type（用于 detail 模式）

        # Detail mode：匹配到完整命令 + 空格后显示参数提示
        self._detail_mode = False
        self._detail_cmd_name = ""
        self._detail_selected_type: str = ""  # detail 模式下的选中类型
        self._detail_has_params: bool = False  # 当前命令是否有可交互参数列表
        self._param_widgets: List["ParameterItemWidget"] = []  # 参数列表项
        self._selected_param_index: int = -1  # 参数列表选中索引
        self._value_selection_mode: bool = False  # 是否处于值选择模式
        self._value_selection_param: str = ""  # 值选择对应的参数名（如 "--model="）
        self._value_widgets: List[QWidget] = []  # 值选择列表项
        self._selected_value_index: int = -1  # 值列表选中索引
        self._last_selected_value_index: int = -1  # 上次值列表选中索引，用于增量更新
        self._data_provider: dict = {}  # 外部数据源（如 model_options）
        # 跳过容器展开/折叠动画，消除命令卡片弹出时的延迟感
        self.setProperty(CardContainer.NO_ANIMATION_PROP, True)
        self.setVisible(False)
        self._setup_ui()
        self._setup_detail_widget()

    def _setup_ui(self):
        # 自身填充父容器宽度
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # 自身样式：使用系统实时卡片背景色，底部直角与输入框融合
        Colors.refresh()
        self._apply_self_style()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 描述 tooltip（列表模式下显示当前选中项的完整描述）
        # 设计目的：item 行的 _ElidedLabel 描述在宽度不足时会省略（中间省略），
        # 用户难以阅读完整说明。此 tooltip 在卡片顶部用完整文本展示，宽度充裕，
        # 因此换行后能读完整内容；空描述/详情模式下隐藏。
        self._desc_tooltip_label = QLabel()
        self._desc_tooltip_label.setWordWrap(True)
        self._desc_tooltip_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._desc_tooltip_label.setVisible(False)
        self._desc_tooltip_label.setTextInteractionFlags(Qt.NoTextInteraction)
        layout.addWidget(self._desc_tooltip_label)

        # tooltip 与列表之间的细分隔线（视觉分组）
        self._desc_tooltip_divider = QFrame()
        self._desc_tooltip_divider.setFrameShape(QFrame.HLine)
        self._desc_tooltip_divider.setFixedHeight(1)
        self._desc_tooltip_divider.setVisible(False)
        self._desc_tooltip_divider.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self._desc_tooltip_divider)
        self._apply_desc_tooltip_style()

        # 滚动区域
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QScrollArea.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        Colors.refresh()
        self._scroll_area.setStyleSheet(f"""
            QScrollArea, QScrollArea * {{
                background: transparent;
                border: none;
                padding: 0;
                margin: 0;
            }}
{get_unified_scrollbar_style(8)}
        """)

        self._scroll_content = QWidget()
        self._scroll_content.setStyleSheet("background: transparent; border: none;")
        self._scroll_layout = QVBoxLayout(self._scroll_content)
        self._scroll_layout.setContentsMargins(0, 0, 0, 0)
        self._scroll_layout.setSpacing(0)

        self._scroll_area.setWidget(self._scroll_content)
        # 确保 viewport 没有多余的边距/内边距（这是导致顶部空白的根本原因）
        self._scroll_area.viewport().setStyleSheet("background: transparent; border: none; padding: 0; margin: 0;")
        layout.addWidget(self._scroll_area)

        # 不在此处调用 _refresh_data() —— 命令尚未注册，会导致缓存空数据
        # show_card() 会在首次显示时自动加载

        # Detail 容器（初始隐藏）
        self._detail_container = QWidget()
        self._detail_container.setVisible(False)
        self._detail_container.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self._detail_container)

    def _apply_self_style(self):
        """应用 CommandCard 自身的样式（背景/边框/圆角）。

        抽出为独立方法以便主题切换时重新调用。
        """
        Colors.refresh()
        self.setStyleSheet(f"""
            CommandCard {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }}
        """)

    def _apply_scroll_area_styles(self, scroll_area: "QScrollArea"):
        """应用列表/参数/值三个滚动区的统一样式（滚动条 + viewport）

        Args:
            scroll_area: 目标 QScrollArea（_detail_params_scroll / _detail_value_scroll）
        """
        Colors.refresh()
        scroll_area.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
{get_unified_scrollbar_style(4)}
        """)
        scroll_area.viewport().setStyleSheet("background: transparent; border: none;")

    def refresh_style(self):
        """响应主题切换：刷新 CommandCard 内所有主题相关的样式

        覆盖范围：
        - CommandCard 自身（背景 / 边框 / 圆角）
        - detail 容器内的静态 widget（描述 / 位置参数提示 / 静态 hint）
        - detail 滚动区（参数列表 + 值选择列表）
        - 命令列表 widget（CommandItemWidget._apply_style）
        - 参数列表 widget（ParameterItemWidget.refresh_style）
        - 值选择列表项（按当前选中状态）
        - 分隔线（如有，刷新颜色）
        """
        Colors.refresh()
        # 1. CommandCard 自身
        self._apply_self_style()
        # 1.5 列表顶部描述 tooltip（主题感知）
        self._apply_desc_tooltip_style()
        # 2. detail 容器内的静态 widget
        self._apply_detail_desc_style()
        self._apply_detail_positional_hint_style()
        self._apply_detail_hint_style()
        # 3. detail 滚动区
        if hasattr(self, "_detail_params_scroll") and self._detail_params_scroll is not None:
            self._apply_scroll_area_styles(self._detail_params_scroll)
        if hasattr(self, "_detail_value_scroll") and self._detail_value_scroll is not None:
            self._apply_scroll_area_styles(self._detail_value_scroll)
        # 4. 命令列表 widget（hover/selected 背景）
        for w in list(self._item_widgets):
            try:
                w._apply_style()
            except RuntimeError:
                continue
        # 5. 参数列表 widget（背景 + 子标签）
        for w in list(self._param_widgets):
            try:
                w.refresh_style()
            except RuntimeError:
                continue
        # 6. 值选择列表项（按当前选中状态）
        for i, w in enumerate(self._value_widgets):
            try:
                w.set_selected(i == self._selected_value_index)
            except RuntimeError:
                continue
        # 7. 分隔线列表
        for div in list(self._dividers):
            try:
                div.setStyleSheet(f"background: {Colors.DIVIDER_COLOR}; border: none;")
            except RuntimeError:
                pass

    def _setup_detail_widget(self):
        """构建 detail 模式下的交互式参数 UI"""
        detail_layout = QVBoxLayout(self._detail_container)
        detail_layout.setContentsMargins(12, 1, 12, 2)
        detail_layout.setSpacing(2)

        # 第一行：命令说明（始终显示）
        self._detail_desc_label = QLabel()
        self._detail_desc_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._detail_desc_label.setWordWrap(True)
        detail_layout.addWidget(self._detail_desc_label)
        self._apply_detail_desc_style()

        # 位置参数提示（交互式参数列表上方显示，如 "<query> — 研究主题"）
        self._detail_positional_hint = QLabel()
        self._detail_positional_hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._detail_positional_hint.setWordWrap(True)
        self._detail_positional_hint.setVisible(False)
        detail_layout.addWidget(self._detail_positional_hint)
        self._apply_detail_positional_hint_style()

        # 参数列表滚动区（有 parameters 时显示）
        self._detail_params_scroll = QScrollArea()
        self._detail_params_scroll.setWidgetResizable(True)
        self._detail_params_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._detail_params_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._apply_scroll_area_styles(self._detail_params_scroll)
        self._detail_params_scroll.setVisible(False)

        self._detail_params_content = QWidget()
        self._detail_params_content.setStyleSheet("background: transparent; border: none;")
        self._detail_params_layout = QVBoxLayout(self._detail_params_content)
        self._detail_params_layout.setContentsMargins(0, 0, 0, 0)
        self._detail_params_layout.setSpacing(0)
        self._detail_params_scroll.setWidget(self._detail_params_content)
        detail_layout.addWidget(self._detail_params_scroll)

        # 值选择列表滚动区（--model= 展开时显示）
        self._detail_value_scroll = QScrollArea()
        self._detail_value_scroll.setWidgetResizable(True)
        self._detail_value_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._detail_value_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._apply_scroll_area_styles(self._detail_value_scroll)
        self._detail_value_scroll.setVisible(False)

        self._detail_value_content = QWidget()
        self._detail_value_content.setStyleSheet("background: transparent; border: none;")
        self._detail_value_layout = QVBoxLayout(self._detail_value_content)
        self._detail_value_layout.setContentsMargins(0, 0, 0, 0)
        self._detail_value_layout.setSpacing(0)
        self._detail_value_scroll.setWidget(self._detail_value_content)
        detail_layout.addWidget(self._detail_value_scroll)

        # 回退：静态参数提示（命令无 parameters 时显示）
        self._detail_hint_label = QLabel()
        self._detail_hint_label.setWordWrap(True)
        detail_layout.addWidget(self._detail_hint_label)
        self._apply_detail_hint_style()

        # 点击整块等同于选中当前命令并发送
        self._detail_container.setCursor(Qt.PointingHandCursor)
        self._detail_container.mousePressEvent = self._on_detail_clicked

    def _apply_detail_desc_style(self):
        """刷新 detail 模式命令说明标签的样式"""
        Colors.refresh()
        if not hasattr(self, "_detail_desc_label") or self._detail_desc_label is None:
            return
        self._detail_desc_label.setStyleSheet(f"""
            QLabel {{ color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}; background: transparent; margin: 0; padding: 0; }}
        """)

    def _apply_desc_tooltip_style(self):
        """刷新列表顶部描述 tooltip 的样式

        视觉规范：
        - 浅色半透明背景（HOVER_BG 系），与卡片主体轻微分层形成"tooltip"质感
        - 圆角 + 上下内边距让它读起来像独立信息条
        - 文字保持 TEXT_PRIMARY，确保一眼可读
        """
        Colors.refresh()
        if not hasattr(self, "_desc_tooltip_label") or self._desc_tooltip_label is None:
            return
        self._desc_tooltip_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(11)};
                background: {Colors.HOVER_BG};
                border: none;
                border-radius: 4px;
                margin: 6px 8px 4px 8px;
                padding: 6px 10px;
            }}
        """)
        if hasattr(self, "_desc_tooltip_divider") and self._desc_tooltip_divider is not None:
            self._desc_tooltip_divider.setStyleSheet(f"background: {Colors.DIVIDER_COLOR}; border: none;")

    def _compute_desc_tooltip_height(self, max_lines: int = 4) -> int:
        """计算描述 tooltip 的高度（像素）

        - 根据当前 selected item 的 description 文本 + 卡片宽度 + 最大行数计算
        - 卡片宽度未初始化（首次构造）时返回保守默认值 0 —— 由调用方下次重试
        - 上限 max_lines 行防止长描述占用过多屏幕空间
        """
        if not hasattr(self, "_desc_tooltip_label") or self._desc_tooltip_label is None:
            return 0
        text = self._desc_tooltip_label.text()
        if not text.strip():
            return 0
        card_w = self.width()
        if card_w <= 0:
            return 0
        # 边距（与样式表中的 margin 6px top + 4px bottom = 10px，左右 8px*2 = 16px）
        inner_w = max(1, card_w - 16 - 20)  # 左右 margin + padding 安全余量
        fm = self._desc_tooltip_label.fontMetrics()
        line_height = fm.lineSpacing()
        bounding = fm.boundingRect(QRect(0, 0, inner_w, 0), Qt.TextWordWrap, text)
        line_count = max(1, (bounding.height() + line_height - 1) // line_height)
        line_count = min(line_count, max_lines)
        # 总高 = 上下 margin 10px + 上下 padding 12px + N*line_height
        return 10 + 12 + int(line_count * line_height)

    def _update_desc_tooltip(self, item: Optional[Dict[str, str]] = None):
        """根据当前选中项更新 tooltip 文本与可见性

        Args:
            item: 可选的选中项数据；为 None 时从 _filtered_items 与 _selected_index 推导
        """
        if not hasattr(self, "_desc_tooltip_label") or self._desc_tooltip_label is None:
            return
        # 仅在列表模式下显示 tooltip；detail 模式自带描述区，无需重复
        if self._detail_mode:
            self._desc_tooltip_label.setVisible(False)
            self._desc_tooltip_divider.setVisible(False)
            # detail 模式的高度由 _adjust_detail_height 控制，此处不刷新
            return
        if item is None:
            if 0 <= self._selected_index < len(self._filtered_items):
                item = self._filtered_items[self._selected_index]
            else:
                item = None
        desc = (item or {}).get("description", "") if item else ""
        if not desc.strip():
            # 空描述：隐藏（不显示空白 tooltip），并刷新卡片高度（清除旧 tooltip 占用空间）
            self._desc_tooltip_label.setVisible(False)
            self._desc_tooltip_divider.setVisible(False)
            self._apply_list_height()
            return
        # HTML 转义 + 多关键字高亮（与 _ElidedLabel._update_display 对齐）
        safe = html.escape(desc)
        if self._current_text_query:
            hls = CommandItemWidget._all_highlight_queries(desc, self._current_text_query)
            if hls:
                lower_safe = safe.lower()
                spans = []
                for hl in hls:
                    escaped_hl = html.escape(hl)
                    lower_hl = escaped_hl.lower()
                    idx = lower_safe.find(lower_hl)
                    if idx >= 0:
                        spans.append((idx, idx + len(escaped_hl)))
                if spans:
                    spans.sort()
                    merged = [spans[0]]
                    for s in spans[1:]:
                        if s[0] <= merged[-1][1]:
                            merged[-1] = (merged[-1][0], max(merged[-1][1], s[1]))
                        else:
                            merged.append(s)
                    parts = []
                    pos = 0
                    for start, end in merged:
                        if pos < start:
                            parts.append(safe[pos:start])
                        parts.append(
                            f'<span style="color: {Colors.SEND_BTN_START}; font-weight: bold;">{safe[start:end]}</span>'
                        )
                        pos = end
                    if pos < len(safe):
                        parts.append(safe[pos:])
                    safe = "".join(parts)
        self._desc_tooltip_label.setText(safe)
        self._desc_tooltip_label.setVisible(True)
        self._desc_tooltip_divider.setVisible(True)
        # 文本更新后重新计算 tooltip 自身高度，再统一刷新卡片总高度
        tip_h = self._compute_desc_tooltip_height()
        if tip_h > 0:
            self._desc_tooltip_label.setFixedHeight(tip_h)
        self._apply_list_height()

    def _apply_list_height(self):
        """统一刷新卡片总高度：列表高度 + 顶部描述 tooltip 高度（若可见）

        由 _render / 选中变更 / detail→list 切换等多个入口复用。
        当卡片空列表时直接置 0（卡片可见性由 _filtered_items 控制，调用方负责）。
        """
        item_count = len(self._item_widgets)
        divider_count = len(self._dividers)
        total_items = item_count + divider_count
        if total_items == 0:
            self.setFixedHeight(0)
            return
        visible = min(total_items, MAX_VISIBLE_ITEMS)
        list_height = visible * ITEM_HEIGHT + divider_count * 1
        # 顶部 tooltip 仅在列表模式可见；detail 模式自带描述区，不重复显示
        # 注：tooltip label 和其下的 1px 分隔线共同占 layout 空间，
        # setFixedHeight 必须包含两者，否则 scroll_area 被挤少 1px → 少显示一个 item
        # 注：判据用 isHidden() 而非 isVisible()。
        # Qt 的 isVisible() 要求自身+所有祖先可见。show_card 中
        # card.setVisible(True) 在 load_items 之后执行，此时卡片自身不可见，
        # tooltip label 即使 setVisible(True) 也返回 False → tooltip_h=0。
        tooltip_h = 0
        if (
            not self._detail_mode
            and hasattr(self, "_desc_tooltip_label")
            and self._desc_tooltip_label is not None
            and not self._desc_tooltip_label.isHidden()
        ):
            # 注：必须用 minimumHeight()，避免 layout pass 延迟。
            tooltip_h = self._desc_tooltip_label.minimumHeight()
            if (
                hasattr(self, "_desc_tooltip_divider")
                and self._desc_tooltip_divider is not None
                and not self._desc_tooltip_divider.isHidden()
            ):
                tooltip_h += self._desc_tooltip_divider.minimumHeight()
        self.setFixedHeight(list_height + tooltip_h)

    def _apply_detail_positional_hint_style(self):
        """刷新 detail 模式位置参数提示标签的样式"""
        Colors.refresh()
        if not hasattr(self, "_detail_positional_hint") or self._detail_positional_hint is None:
            return
        self._detail_positional_hint.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_ACCENT};
                {get_font_family_css()} {font_size_css(11)};
                background: {Colors.DIVIDER_COLOR};
                border-radius: 4px;
                padding: 2px 8px;
                margin: 0;
            }}
        """)

    def _apply_detail_hint_style(self):
        """刷新 detail 模式静态参数提示标签的样式"""
        Colors.refresh()
        if not hasattr(self, "_detail_hint_label") or self._detail_hint_label is None:
            return
        self._detail_hint_label.setStyleSheet(f"""
            QLabel {{ color: {Colors.SEND_BTN_START}; {get_font_family_css()} {font_size_css(12)}; background: transparent; margin: 0; padding: 0; }}
        """)

    # ---- Detail 模式 ----

    @property
    def is_detail_mode(self) -> bool:
        """是否处于 detail 模式（显示参数提示）"""
        return self._detail_mode

    @property
    def detail_cmd_name(self) -> str:
        """detail 模式匹配的命令名"""
        return self._detail_cmd_name

    def show_command_detail(
        self,
        cmd_name: str,
        selected_type: str = "",
        data_provider: dict = None,
        full_text: str = "",
        cursor_pos: int = -1,
    ):
        """切换到 detail 模式：显示指定命令/技能的参数提示

        Args:
            cmd_name: 已匹配的命令名或技能名
            selected_type: 选中项的 display_type（"command"/"prompt"/"agent"）
                          为空时使用当前选中项类型（通过 _current_selected_type）
            data_provider: 外部数据源，如 {"model_options": ["OpenAI:gpt-4o", ...]}
            full_text: 输入框完整文本（用于在重建 widgets 后立即标记 active 状态，
                      解决失焦→重新聚焦时 active 状态丢失的问题）
            cursor_pos: 光标位置（供自动检测 --model= 值选择模式用）
        """
        cmd_mgr = CommandManager.get_instance()
        self._data_provider = data_provider or {}

        # 确定使用哪个类型：优先传入参数，其次当前选中项类型
        use_type = selected_type or self._current_selected_type or ""

        # 按类型查找对应 CommandDefinition（同名多类型时只显示选中类型的 hint）
        entries = cmd_mgr._commands.get(cmd_name, {})
        cmd = None
        if use_type:
            type_map = {"command": CommandType.FUNCTION, "prompt": CommandType.PROMPT, "agent": CommandType.AGENT}
            preferred = type_map.get(use_type)
            if preferred and preferred in entries:
                cmd = entries[preferred]
        if not cmd and entries:
            cmd = next(iter(entries.values()))

        skill = get_skill_by_name(cmd_name) if not cmd else None

        if not cmd and not skill:
            return

        # 已在此命令的 detail 模式，跳过 UI 重新渲染
        # （data_provider 已在本方法开头更新，不影响值选择列表的实时性）
        if self._detail_mode and self._detail_cmd_name == cmd_name:
            return

        self._detail_mode = True
        self._detail_cmd_name = cmd_name
        self._detail_selected_type = cmd.type.name.lower() if cmd else "skill"
        self._value_selection_mode = False

        # 更新描述
        if cmd:
            desc = cmd.description
        else:
            desc = skill.get("description", "")
        max_chars = 200
        if len(desc) > max_chars:
            desc = desc[:max_chars].rstrip() + "…"
        self._detail_desc_label.setText(desc)

        # 决定显示交互参数列表
        # ── 技能动态生成 --enable/--disable 参数（互斥） ──
        skill_params = None
        if skill:
            from app.utils.config import Settings

            cfg = Settings.get_instance()
            enabled_skills = cfg.llm_enabled_skills.value or []
            is_enabled = skill.get("name") in enabled_skills
            if is_enabled:
                skill_params = [CommandParameter(name="--disable", description="禁用技能（从系统提示词中移除）")]
            else:
                skill_params = [CommandParameter(name="--enable", description="启用技能（添加到系统提示词）")]

        has_params = bool(cmd and cmd.parameters) or bool(skill_params)
        self._detail_has_params = has_params

        if has_params:
            # 交互式参数列表
            self._detail_hint_label.setVisible(False)
            self._detail_value_scroll.setVisible(False)
            params = cmd.parameters if cmd else skill_params
            self._build_param_widgets(params)
            # 位置参数提示：收集 positional 类型参数的描述显示在列表上方
            positional_text = self._build_positional_hint(params)
            if positional_text:
                self._detail_positional_hint.setText(positional_text)
                self._detail_positional_hint.setVisible(True)
            else:
                self._detail_positional_hint.setVisible(False)
            # 有交互式参数项才显示参数滚动区
            if self._param_widgets:
                self._detail_params_scroll.setVisible(True)
                self._selected_param_index = 0
                self._update_param_selection()
            else:
                self._detail_params_scroll.setVisible(False)
                self._selected_param_index = -1
        else:
            # 回退：静态 hint
            self._detail_params_scroll.setVisible(False)
            self._detail_value_scroll.setVisible(False)
            if cmd:
                if cmd.type == CommandType.AGENT:
                    hint_text = "--subagent [--with-context] [--model=<provider>:<model>] <task-desc>"
                else:
                    hint_text = cmd.argument_hint or ""
            else:
                hint_text = ""
            self._detail_hint_label.setText(hint_text)
            self._detail_hint_label.setVisible(bool(hint_text))

        # 隐藏列表，显示 detail
        self._scroll_area.setVisible(False)
        self._detail_container.setVisible(True)
        # detail 模式下隐藏顶部描述 tooltip（detail 自带描述区，无需重复）
        if hasattr(self, "_desc_tooltip_label") and self._desc_tooltip_label is not None:
            self._desc_tooltip_label.setVisible(False)
            self._desc_tooltip_divider.setVisible(False)
        self._visible = True
        self.setVisible(True)

        # 重建 widgets 后立即应用输入文本中已存在的 active 参数
        # 解决失焦→重新聚焦后 active 状态丢失的 UX 问题：
        #   失焦 → _reset_detail_mode 销毁了所有 ParameterItemWidget（_active=False）
        #   重聚焦 → 重新进入 detail 模式时新建了 widgets，但 _active 默认 False
        # 调用 update_active_params 让输入框里已有的 --model=foo 立刻显示为激活态
        if has_params and self._param_widgets and full_text:
            active = CommandManager.parse_active_params(full_text) if full_text else set()
            self.update_active_params(active, full_text=full_text, cursor_pos=cursor_pos)

        # 动态计算高度
        self._adjust_detail_height()

    def _adjust_detail_height(self):
        """根据内容动态调整 detail 容器高度"""
        # 如果宽度尚未初始化，延迟到事件循环结束后再计算
        if self.width() <= 0:
            QTimer.singleShot(0, self._adjust_detail_height)
            return
        margins = self._detail_container.layout().contentsMargins()
        v_margin = margins.top() + margins.bottom()
        spacing = self._detail_container.layout().spacing()

        # 计算描述文本高度（使用 boundingRect 精确估算 word wrap 后高度）
        fm = self._detail_desc_label.fontMetrics()
        line_height = fm.lineSpacing()
        desc_text = self._detail_desc_label.text()
        if desc_text.strip():
            label_width = self._detail_desc_label.width() or 1
            if label_width <= 0:
                label_width = self.width() - 24
            # 使用 TextWordWrap 标志精确计算多行文本高度，避免 horizontalAdvance
            # 单行估算不准确导致第一行文字被下方元素遮挡
            bounding = fm.boundingRect(QRect(0, 0, label_width, 0), Qt.TextWordWrap, desc_text)
            line_count = max(1, (bounding.height() + line_height - 1) // line_height)
            desc_height = line_height * line_count + 2  # 2px 安全边距补偿 QLabel 内部渲染偏移
        else:
            desc_height = line_height

        # 计算参数列表/值列表/提示文本高度
        pos_hint_height = 0

        if self._detail_has_params and not self._value_selection_mode:
            # 交互参数列表高度
            param_count = len(self._param_widgets)
            visible_params = sum(1 for w in self._param_widgets if w.isVisible())
            content_height = visible_params * ITEM_HEIGHT
            self._detail_params_scroll.setFixedHeight(min(content_height, 7 * ITEM_HEIGHT))
            content_height = min(content_height, 7 * ITEM_HEIGHT)
            hint_height = 0
            # 位置参数提示高度
            if self._detail_positional_hint.isVisible():
                fm_pos = self._detail_positional_hint.fontMetrics()
                pos_line_height = fm_pos.lineSpacing()
                pos_text = self._detail_positional_hint.text()
                label_width = self._detail_positional_hint.width() or 1
                if label_width <= 0:
                    label_width = self.width() - 24
                pos_bounding = fm_pos.boundingRect(QRect(0, 0, label_width, 0), Qt.TextWordWrap, pos_text)
                pos_line_count = max(1, (pos_bounding.height() + pos_line_height - 1) // pos_line_height)
                pos_hint_height = pos_line_height * pos_line_count + 4  # padding 2+2
        elif self._value_selection_mode:
            # 值选择列表高度
            value_count = len(self._value_widgets)
            content_height = min(value_count * ITEM_HEIGHT, 7 * ITEM_HEIGHT)
            self._detail_value_scroll.setFixedHeight(content_height)
            hint_height = 0
        else:
            # 静态 hint 高度
            hint_text = self._detail_hint_label.text()
            if hint_text.strip():
                fm_hint = self._detail_hint_label.fontMetrics()
                hint_line_height = fm_hint.lineSpacing()
                label_width = self._detail_hint_label.width() or 1
                if label_width <= 0:
                    label_width = self.width() - 24
                hint_bounding = fm_hint.boundingRect(QRect(0, 0, label_width, 0), Qt.TextWordWrap, hint_text)
                hint_line_count = max(1, (hint_bounding.height() + hint_line_height - 1) // hint_line_height)
                hint_height = hint_line_height * hint_line_count
                self._detail_hint_label.setVisible(True)
            else:
                hint_height = 0
            content_height = 0

        total_height = v_margin + desc_height + spacing + hint_height + content_height + pos_hint_height
        self.setFixedHeight(total_height)

    # ---- 参数列表交互 ----

    def _build_param_widgets(self, params: list):
        """根据 CommandParameter 列表创建参数项 widget"""
        # 清除旧 widget
        for w in self._param_widgets:
            try:
                self._detail_params_layout.removeWidget(w)
                w.deleteLater()
            except RuntimeError:
                pass
        self._param_widgets.clear()

        for p in params:
            # 只显示 flag 和 value 类型（positional 不显示为可点击项）
            if p.param_type == "positional":
                continue
            w = ParameterItemWidget(p)
            w.clicked.connect(self._on_param_clicked)
            w.hovered.connect(self._on_param_hovered)
            self._detail_params_layout.addWidget(w)
            self._param_widgets.append(w)

    @staticmethod
    def _build_positional_hint(params: list) -> str:
        """提取 positional 类型参数的描述文本，供 detail 模式静态显示"""
        parts = []
        for p in params:
            if p.param_type != "positional":
                continue
            if p.description:
                text = f"{p.name} — {p.description}"
            else:
                text = p.name
            parts.append(text)
        return "  ·  ".join(parts)

    def _on_param_clicked(self):
        """参数项被点击"""
        sender = self.sender()
        if sender in self._param_widgets:
            # 已激活参数再次点击 = 取消该参数（从输入框移除）
            if sender.is_active:
                self.parameterDeselected.emit(sender.param_name, sender.param_type)
                return
            idx = self._param_widgets.index(sender)
            self._selected_param_index = idx
            self._update_param_selection()
            self._execute_param_selection(sender)

    def _execute_param_selection(self, widget: "ParameterItemWidget"):
        """执行参数选中逻辑

        - flag 类型 → 发射 parameterSelected 信号
        - value 类型 → 先插入参数名（--model=），再切值选择
        - positional → 无操作（不应出现在列表中）
        """
        if widget.param_type == "flag":
            self.parameterSelected.emit(widget.param_name, widget.param_type)
        elif widget.param_type == "value":
            # 先插入参数名（--model=），光标落在 = 后，再展示值列表
            self.parameterSelected.emit(widget.param_name, widget.param_type)
            self._switch_to_value_selection(widget)

    def _switch_to_value_selection(self, widget: "ParameterItemWidget", query: str = ""):
        """切换到值选择模式：显示当前参数的可选值

        Args:
            widget: 参数项 widget（提供 param_name 和可选值来源）
            query: 搜索关键字（按子串过滤，用于实时搜索）
        """
        param_name = widget.param_name
        param = widget._param

        # 获取可选值列表
        options = []
        if param_name == "--model=":
            options = self._data_provider.get("model_options", [])
        elif param_name == "--load=":
            options = self._data_provider.get("template_options", [])
        elif param_name == "--delete=":
            options = self._data_provider.get("template_options", [])
        elif param_name == "--join=":
            options = self._data_provider.get("agent_options", [])
        else:
            options = param.value_options or []

        if not options:
            # 无可选值：--name= 已在 _execute_param_selection 中插入，无需再操作
            self._value_selection_mode = False
            return

        filtered = self._filter_value_options(options, query)

        # 清空旧 value widget
        for w in self._value_widgets:
            try:
                self._detail_value_layout.removeWidget(w)
                w.deleteLater()
            except RuntimeError:
                pass
        self._value_widgets.clear()
        self._selected_value_index = -1

        # 构建值列表
        for val in filtered:
            item = ValueItemWidget(val)
            item.clicked.connect(self._on_value_clicked)
            item.hovered.connect(self._on_value_hovered)
            self._detail_value_layout.addWidget(item)
            self._value_widgets.append(item)

        # 切换显示
        self._value_selection_mode = True
        self._value_selection_param = param_name
        self._detail_params_scroll.setVisible(False)
        self._detail_value_scroll.setVisible(True)

        # 选中第一项
        self._selected_value_index = 0 if self._value_widgets else -1
        self._update_value_selection()

        # 重算高度
        self._adjust_detail_height()

    # ---- 自动检测 --model 触发值选择 / 实时搜索 ----

    def _auto_switch_to_value_selection(self, text: str, cursor_pos: int = -1):
        """检测文本中完整的 value 参数名（含 =），自动进入/刷新值选择模式

        行为：
        - 只匹配完整的参数名 + =（如 --language=），不匹配前缀
        - 文本中有多个完整参数时取最后一个（最近输入的）
        - 不论参数当前是否可见（active 的参数会被隐藏但文本仍在）
        - 已在值选择模式时，根据光标前内容实时过滤
        - cursor_pos=-1 时按"到下一个空格/末尾"取搜索关键字

        注意：绝不通过前缀匹配触发值选择——值列表会顶掉参数列表，
        只有在参数全名 + 等号（--model=）时才应进入值选择模式。
        """
        import re

        # 1. 收集所有带 value_options 的 value 参数（不论显隐，靠文本来匹配）
        candidate_params = []
        for w in self._param_widgets:
            if w.param_type != "value":
                continue
            # --model= 使用动态数据源；其他参数需有静态 value_options
            if w.param_name == "--model=":
                candidate_params.append(w)
            elif w.param_name == "--join=":
                candidate_params.append(w)
            elif w._param.value_options:
                candidate_params.append(w)

        if not candidate_params:
            return

        # 2. 查找文本中完整的参数名（含 =），取最后一个匹配
        #    同时检查光标位置：如果光标已越过参数值后的空格（用户已输入下一个参数），
        #    则跳过此参数，不再弹出枚举列表。
        best_match = None  # (match_end, widget, query)

        for w in candidate_params:
            param_clean = w.param_name.rstrip("=")
            m = re.search(re.escape(param_clean) + r"=", text)
            if m and (best_match is None or m.end() > best_match[0]):
                token_end = m.end()
                # 光标位置检查：如果光标已越过参数值后的第一个空格，
                # 说明用户已离开此参数，不应再触发值选择
                if self._cursor_past_param_value(text, token_end, cursor_pos):
                    continue  # 用户已离开此参数，跳过
                query = self._extract_value_query(text, token_end, cursor_pos)
                best_match = (m.end(), w, query)

        if best_match is None:
            return

        _, target_widget, query = best_match

        # 3. 已在值选择模式且是同一个参数：仅刷新过滤
        if self._value_selection_mode and self._value_selection_param == target_widget.param_name:
            self._refresh_value_list(query)
            return

        # 4. 切到值选择模式
        self._switch_to_value_selection(target_widget, query=query)

    def _cursor_past_param_value(self, text: str, token_end: int, cursor_pos: int) -> bool:
        """判断光标是否已越过参数值后的第一个空格（即离开此参数）

        用于决定是否退出或跳过值选择模式。
        注意：参数值在末尾（无空格）或紧跟另一参数名时，
        cursor_pos 不会 > space_after，不会误判为"已离开"。
        """
        if cursor_pos < 0:
            return False
        space_after = text.find(" ", token_end)
        return space_after >= 0 and cursor_pos > space_after

    def _extract_value_query(self, text: str, after_token_end: int, cursor_pos: int) -> str:
        """提取 value 参数 = 之后到光标前/下一个空格前的子串作为搜索关键字"""
        if cursor_pos < 0 or cursor_pos > len(text):
            cursor_pos = len(text)
        # 右边界 = min(光标, 下一个空格)
        right = cursor_pos
        space_pos = text.find(" ", after_token_end)
        if space_pos >= 0 and space_pos < right:
            right = space_pos
        return text[after_token_end:right].lower()

    def _filter_value_options(self, options: list, query: str) -> list:
        """按子串过滤选项（不区分大小写）；空 query 返回全部"""
        if not query:
            return list(options)
        q = query.lower()
        return [opt for opt in options if q in opt.lower()]

    def _refresh_value_list(self, query: str):
        """在不重建模式状态的前提下，仅刷新值列表 widget（用于实时搜索）"""
        param_name = self._value_selection_param
        if not param_name:
            return

        # 重新取源 options
        options = []
        if param_name == "--model=":
            options = self._data_provider.get("model_options", [])
        elif param_name == "--load=":
            options = self._data_provider.get("template_options", [])
        elif param_name == "--delete=":
            options = self._data_provider.get("template_options", [])
        elif param_name == "--join=":
            options = self._data_provider.get("agent_options", [])
        else:
            # 非 --model= 的 value 参数：从 widget 反查
            for w in self._param_widgets:
                if w.param_name == param_name:
                    options = w._param.value_options or []
                    break

        filtered = self._filter_value_options(options, query)

        # 重建 widget
        for w in self._value_widgets:
            try:
                self._detail_value_layout.removeWidget(w)
                w.deleteLater()
            except RuntimeError:
                pass
        self._value_widgets.clear()

        for val in filtered:
            item = ValueItemWidget(val)
            item.clicked.connect(self._on_value_clicked)
            item.hovered.connect(self._on_value_hovered)
            self._detail_value_layout.addWidget(item)
            self._value_widgets.append(item)

        # 保持选中索引在有效范围
        if self._value_widgets:
            self._selected_value_index = min(max(self._selected_value_index, 0), len(self._value_widgets) - 1)
        else:
            self._selected_value_index = -1
        self._update_value_selection()
        self._adjust_detail_height()

    def _on_value_clicked(self):
        """值选择项被点击"""
        sender = self.sender()
        if sender is None:
            return
        self.parameterValueSelected.emit(sender.value)
        # 回退到参数列表模式
        self._exit_value_selection()

    def _exit_value_selection(self):
        """退出值选择模式，回到参数列表"""
        self._value_selection_mode = False
        self._value_selection_param = ""
        self._detail_value_scroll.setVisible(False)
        self._detail_params_scroll.setVisible(True)
        self._adjust_detail_height()

    def _extract_param_filter(self, full_text: str) -> str:
        """从输入文本提取用户当前正在输入的部分参数名

        用于参数列表过滤：当用户在输入框中输入 `--q` 时，
        参数列表只显示以 `--q` 开头的参数（如 --quick）。

        规则：
        - 如果不是 detail 模式或没有命令名 → 返回空
        - 提取命令名后的文本，取最后一个 --xxx 部分
        - 包含 = → 已进入值选择模式，不过滤
        - 末尾空格 → 刚完成一个参数，不过滤
        - -- 后至少有一个字符才过滤

        Returns:
            过滤前缀（如 "--q"），空字符串表示不应用过滤
        """
        if not full_text or not self._detail_cmd_name:
            return ""

        cmd_prefix = f"/{self._detail_cmd_name} "
        idx = full_text.find(cmd_prefix)
        if idx < 0:
            return ""

        after_cmd = full_text[idx + len(cmd_prefix) :]

        # 包含 = → 在输入值列表，不过滤参数列表
        if "=" in after_cmd:
            return ""

        # 末尾空格 → 刚完成一个参数
        if after_cmd.endswith(" "):
            return ""

        # 取最后一个 --xxx 单词
        import re

        tokens = re.findall(r"(?<!\S)(--[\w-]+)", after_cmd)
        if not tokens:
            return ""

        last = tokens[-1]
        # 至少 --x 三个字符
        if len(last) < 3:
            return ""

        return last

    def update_active_params(self, active: set, full_text: str = "", cursor_pos: int = -1):
        """根据输入中已存在的参数名列表，显隐参数项

        支持互斥参数组：当互斥组中任一参数被激活后，同组其他参数自动隐藏，
        避免用户同时选中两个互斥参数（如 --quick 和 --thorough）。
        支持输入前缀过滤：用户正在输入 --xxx 时，参数列表只显示匹配项。

        Args:
            active: 输入文本中已存在的参数名集合，如 {"--with-context", "--model="}
            full_text: 完整输入文本（用于自动检测 --model 触发值选择 + 实时搜索）
            cursor_pos: 光标位置（实时搜索关键字的右边界）
        """
        if not self._detail_mode:
            return

        if not self._detail_has_params:
            return

        # 安全兜底：_param_widgets 为空时重建
        if not self._param_widgets:
            cmd_mgr = CommandManager.get_instance()
            entries = cmd_mgr._commands.get(self._detail_cmd_name, {})
            for entry in entries.values():
                if entry.parameters:
                    self._build_param_widgets(entry.parameters)
                    break
            if not self._param_widgets:
                return

        # 值选择模式：检查对应的参数是否还在输入中，以及光标是否已离开
        if self._value_selection_mode and self._value_selection_param:
            param_clean = self._value_selection_param.rstrip("=")
            # value 参数必须有 = 才算激活（防止 --xxx 裸名也被算作激活）
            still_active = any("=" in a and a.rstrip("=") == param_clean for a in active)
            # 光标位置检查：如果光标已越过参数值后的第一个空格，说明用户已离开
            should_exit = False
            if still_active and cursor_pos >= 0 and full_text:
                # 用正则查找最后一个匹配（与 _auto_switch_to_value_selection 一致）
                import re

                matches = list(re.finditer(re.escape(param_clean) + r"=", full_text))
                if matches:
                    last_match = matches[-1]
                    token_end = last_match.end()
                    if self._cursor_past_param_value(full_text, token_end, cursor_pos):
                        should_exit = True
            if not still_active or should_exit:
                # 参数已被删掉 或 光标已离开 → 退出值选择模式，回到参数列表
                self._exit_value_selection()

        # 提取输入前缀过滤（用户正在输入的 --xxx 部分）
        param_filter = self._extract_param_filter(full_text)

        # ---- 第一遍：检测互斥组激活状态 ----
        mutex_active_groups: set = set()  # 已有激活参数的互斥组名
        for w in self._param_widgets:
            mg = getattr(w._param, "mutex_group", "")
            if not mg:
                continue
            param_clean = w.param_name.rstrip("=")
            if w.param_type == "value" and w.param_name.endswith("="):
                is_active = any("=" in a and a.rstrip("=") == param_clean for a in active)
            else:
                is_active = any(a.rstrip("=") == param_clean for a in active)
            if is_active:
                mutex_active_groups.add(mg)

        # ---- 第二遍：根据激活状态 + 互斥规则 + 输入前缀过滤 设置可见性 ----
        any_visible = False
        for w in self._param_widgets:
            param_key = w.param_name
            param_clean = param_key.rstrip("=")
            if w.param_type == "value" and param_key.endswith("="):
                is_active = any("=" in a and a.rstrip("=") == param_clean for a in active)
            else:
                is_active = any(a.rstrip("=") == param_clean for a in active)

            # 激活态：保持可见但标记为已激活（让用户能读到参数说明）
            # 不再隐藏已选中的参数
            w.set_active(is_active)

            # 互斥规则：如果此参数属于某个已被激活的互斥组，
            # 则同组非激活参数全部隐藏（防止冲突）
            mg = getattr(w._param, "mutex_group", "")
            if mg and mg in mutex_active_groups and not is_active:
                w.setVisible(False)
                continue

            # 输入前缀过滤：正在输入参数名部分时，只显示以此前缀开头的参数
            # 已激活的参数不受前缀过滤影响（始终可见，方便读描述）
            if not is_active and param_filter:
                if not param_clean.startswith(param_filter):
                    w.setVisible(False)
                    continue

            w.setVisible(True)
            any_visible = True

        # 调整选中索引：如果当前选中的参数已被隐藏，跳到第一个可见参数
        # 注意：此时还未切换 _detail_params_scroll 的可见性（见下方统一处理），
        # 但 _update_param_selection 内部的 ensureWidgetVisible 对隐藏 widget 是 noop。
        if not any_visible:
            self._selected_param_index = -1
        elif (
            self._selected_param_index < 0
            or self._selected_param_index >= len(self._param_widgets)
            or not self._param_widgets[self._selected_param_index].isVisible()
        ):
            new_idx = -1
            for i, w in enumerate(self._param_widgets):
                if w.isVisible():
                    new_idx = i
                    break
            self._selected_param_index = new_idx
        self._update_param_selection()

        # 自动检测 --model 前缀：进入/刷新值选择模式（实时搜索）
        # 注意：此方法只匹配完整参数名 + =（如 --model=），不做前缀匹配
        if full_text and any_visible:
            self._auto_switch_to_value_selection(full_text, cursor_pos)

        # 统一根据值选择模式决定参数/值列表的可见性
        # 必须在 _auto_switch_to_value_selection 之后调用：
        # 该方法在 refresh 分支（同参数 + 已值选择模式）只调 _refresh_value_list，
        # 不会隐藏参数列表；如果此时仍按 any_visible 显示参数列表，
        # 会导致参数列表与值列表同时可见的 UI bug。
        if self._value_selection_mode:
            self._detail_params_scroll.setVisible(False)
            self._detail_value_scroll.setVisible(True)
        else:
            self._detail_params_scroll.setVisible(any_visible)
            # 确保值列表隐藏（避免退出值选择模式后残留）
            self._detail_value_scroll.setVisible(False)
            if any_visible:
                self._detail_params_content.setVisible(True)
                # 从隐藏恢复时，确保 scroll area 及其 viewport 被唤醒
                self._detail_params_scroll.show()
                self._detail_params_content.show()

        # 重算高度
        self._adjust_detail_height()
        # 强制刷新布局：当 scroll area 从隐藏变为可见时，
        # Qt 布局不会自动重新分配空间，需显式失效并激活
        if any_visible:
            detail_layout = self._detail_container.layout()
            if detail_layout:
                detail_layout.invalidate()
                detail_layout.activate()
        # 通知父容器布局更新
        parent = self.parentWidget()
        if parent:
            parent.updateGeometry()

    def _update_param_selection(self):
        """更新参数列表选中高亮"""
        for i, w in enumerate(self._param_widgets):
            w.set_selected(i == self._selected_param_index)
        # 滚动到可见
        if 0 <= self._selected_param_index < len(self._param_widgets):
            self._detail_params_scroll.ensureWidgetVisible(self._param_widgets[self._selected_param_index], 0, 0)

    def _update_value_selection(self):
        """更新值列表选中高亮，滚动到可见"""
        old_idx = self._last_selected_value_index if hasattr(self, "_last_selected_value_index") else -1
        new_idx = self._selected_value_index
        self._last_selected_value_index = new_idx

        # 只更新变化的项
        if old_idx != new_idx:
            if 0 <= old_idx < len(self._value_widgets):
                self._value_widgets[old_idx].set_selected(False)
            if 0 <= new_idx < len(self._value_widgets):
                self._value_widgets[new_idx].set_selected(True)
        elif 0 <= new_idx < len(self._value_widgets):
            self._value_widgets[new_idx].set_selected(True)

        # 滚动到可见
        if 0 <= self._selected_value_index < len(self._value_widgets):
            self._detail_value_scroll.ensureWidgetVisible(self._value_widgets[self._selected_value_index], 0, 0)

    def _reset_detail_mode(self) -> bool:
        """退出 detail 模式，回到列表模式

        Returns:
            True 如果之前处于 detail 模式
        """
        if not self._detail_mode:
            return False
        self._detail_mode = False
        self._detail_cmd_name = ""
        self._detail_has_params = False
        self._value_selection_mode = False
        self._value_selection_param = ""
        self._selected_param_index = -1
        self._selected_value_index = -1
        self._last_selected_value_index = -1
        self._detail_positional_hint.setVisible(False)
        self._detail_container.setVisible(False)
        self._detail_params_scroll.setVisible(False)
        self._detail_value_scroll.setVisible(False)
        self._scroll_area.setVisible(True)
        # 回到列表模式后重新激活顶部描述 tooltip
        # （_update_desc_tooltip 内部会检测 _detail_mode 并显示）
        self._update_desc_tooltip()
        # 清除 detail 模式设置的固定高度，让列表模式自由撑开
        self.setMaximumHeight(16777215)
        self.setMinimumHeight(0)
        self.updateGeometry()
        return True

    def _refresh_data(self):
        """刷新完整数据列表（命令 + 技能）

        使用缓存避免每次敲击都读磁盘。
        只有在 _cache_dirty=True 时才重建缓存（如插件热重载后）。
        首次调用时必然重建。
        """
        if not self._cache_dirty and self._all_items_cache:
            # 安全检查：缓存必须包含命令项，防止初始化时序导致缓存了只有技能的脏数据
            if any(item["type"] == "command" for item in self._all_items_cache):
                self._all_items = self._all_items_cache
                return
            # 缓存不完整，丢弃并重新加载
            self._cache_dirty = True

        cmd_mgr = CommandManager.get_instance()
        commands = cmd_mgr.get_all_commands()

        # 标记 UI 插件命令（用于排序、分区和标签显示）
        ui_cmd_names = UIPluginRegistry.get_instance().get_ui_command_names()
        for cmd in commands:
            if cmd["name"] in ui_cmd_names:
                cmd["subtype"] = "ui_plugin"

        skills = [
            {"name": s["name"], "description": s.get("description", ""), "type": "skill"} for s in get_local_skills()
        ]
        self._all_items = commands + skills

        # 检测跨类型重名，添加 display_name 后缀以区分
        # 同名不同类型的项（如 "tdd" 同时是技能和提示词）各自加后缀
        name_type_map = {}
        for item in self._all_items:
            name_type_map.setdefault(item["name"], set()).add(item["type"])

        suffix_map = {
            "skill": "-skill",
            "prompt": "-prompt",
            "command": "-cmd",
            "agent": "-agent",
        }
        for item in self._all_items:
            if len(name_type_map.get(item["name"], set())) > 1:
                suffix = suffix_map.get(item["type"], "")
                item["display_name"] = f"{item['name']}{suffix}"
            else:
                item["display_name"] = item["name"]

        self._all_items_cache = list(self._all_items)
        self._cache_dirty = False

    @staticmethod
    def _matches_multi(item: Dict[str, str], query: str) -> bool:
        """多关键字匹配：| = OR, & = AND

        例如 query="find|search&replace" 表示匹配包含 "find"
        或同时包含 "search" 与 "replace" 的项。
        空 query 返回 True（无过滤）。
        尾部 &（如 "find&"）自动忽略空 AND 部分，不会导致全不匹配。
        """
        if not query:
            return True
        text = (item["name"] + " " + item.get("display_name", item["name"]) + " " + item["description"]).lower()

        for or_term in query.split("|"):
            or_term = or_term.strip()
            if not or_term:
                continue
            # 过滤空 AND 部分：让 "find&" 等价于 "find"（用户还在打字中）
            and_parts = [p.strip() for p in or_term.split("&") if p.strip()]
            if not and_parts:
                continue
            if all(part in text for part in and_parts):
                return True
        return False

    @staticmethod
    def _matches_type_filter(item: Dict[str, Any], type_filter: Optional[set]) -> bool:
        """判断 item 是否匹配类别过滤器集合

        type_filter 为 None 或空时不过滤。
        "ui" 不是 item.type 的字面值，而是映射到 type="command" 且 subtype="ui_plugin" 的 UI 插件命令。
        """
        if not type_filter:
            return True
        if item["type"] in type_filter:
            return True
        # 特殊处理：#ui 过滤 → 匹配 UI 插件命令
        if "ui" in type_filter and item["type"] == "command" and item.get("subtype") == "ui_plugin":
            return True
        return False

    @staticmethod
    def _parse_type_filter(query: str):
        """从 query 中提取 type:xxx 或 #xxx 过滤器

        例如：
          "type:skill tdd"      → ({"skill"}, "tdd")
          "#agent"               → ({"agent"}, "")
          "#cmd find"            → ({"command"}, "find")
          "#ui"                  → ({"ui"}, "")
          "type:skill|type:ui"  → ({"skill","ui"}, "")
          "find"                 → (None, "find")

        支持简写：cmd→command
        支持 OR：type:skill|type:agent → {"skill","agent"}
        注："ui" 类别不是 item.type 的字面值，匹配 type="command" 且 subtype="ui_plugin" 的项
        """
        if not query:
            return None, query

        type_set = set()
        clean_tokens = []
        type_map = {
            "cmd": "command",
            "skill": "skill",
            "技能": "skill",
            "agent": "agent",
            "智能体": "agent",
            "prompt": "prompt",
            "提示词": "prompt",
            "ui": "ui",
        }

        for token in query.split():
            if token.startswith("type:"):
                tf = token[5:].strip()
                for t in tf.split("|"):
                    t = t.strip()
                    # 兼容 "type:skill|type:agent" 写法：第二段可能仍带 type: 前缀
                    if t.startswith("type:"):
                        t = t[5:].strip()
                    if t in type_map:
                        type_set.add(type_map[t])
            elif token.startswith("#"):
                # #skill, #agent, #prompt, #cmd 简写
                # 支持 #agent|search、#agent&search 等无空格分隔的写法：
                # 按 | 和 & 拆分，第一部分识别为类型过滤器，其余为搜索关键字
                rest = token[1:]
                parts = [p.strip() for p in rest.replace("&", "|").split("|") if p.strip()]
                if parts and parts[0] in type_map:
                    type_set.add(type_map[parts[0]])
                    # 剩余部分是搜索关键字（如 #agent|search → "search"）
                    for part in parts[1:]:
                        clean_tokens.append(part)
                else:
                    # 不是有效的类型过滤器（如 #|search、##agent 等）→ 作为普通搜索关键字
                    clean_tokens.append(token)
            else:
                clean_tokens.append(token)

        return type_set if type_set else None, " ".join(clean_tokens)

    def load_items(self, query: str = "", incremental: bool = False):
        """根据 query 筛选并渲染列表（多关键字 + 类别过滤 + 增量剪枝）

        支持多关键字语法：
          key1|key2  → OR（含 key1 或 key2）
          key1&key2  → AND（同时含 key1 与 key2）

        支持类别过滤：
          type:skill            → 只显示技能
          #skill                → 同上（简写）
          #agent                → 只显示智能体
          #prompt               → 只显示提示词
          #cmd                  → 只显示命令
          #ui                   → 只显示 UI 插件命令
          #skill tdd            → 只显示名/描述含 "tdd" 的技能
          type:skill|type:agent → 显示技能或智能体

        Args:
            query: 搜索查询
            incremental: 是否增量更新

        增量剪枝：连续追加字符时在上次结果上继续过滤。
        含 | 时不剪枝（OR 可能扩大结果集）。
        """
        query = query.strip().lower()

        # 提取类别过滤器
        type_filter, text_query = self._parse_type_filter(query)

        if not text_query:
            # 纯类别过滤（无文本搜索）
            if type_filter:
                self._filtered_items = [
                    item for item in self._all_items if self._matches_type_filter(item, type_filter)
                ]
            else:
                self._filtered_items = list(self._all_items)
            self._last_query = ""
        else:
            # 增量剪枝：仅当新 query 是上次的扩展且不含 |
            can_prune = self._last_query and query.startswith(self._last_query) and "|" not in query
            source = self._filtered_items if can_prune else self._all_items
            self._filtered_items = [item for item in source if self._matches_multi(item, text_query)]
            # 文本匹配后再按类别过滤
            if type_filter:
                self._filtered_items = [
                    item for item in self._filtered_items if self._matches_type_filter(item, type_filter)
                ]

        # 排序：内置命令→UI 插件命令→技能→智能体，同类型按名称
        sort_order = {"command": 0, "skill": 2, "agent": 3}

        def _sort_key(item):
            base = sort_order.get(item["type"], 99)
            if item.get("subtype") == "ui_plugin":
                base = 1  # UI 插件命令排在内置命令之后、技能之前
            return (base, item["name"])

        self._filtered_items.sort(key=_sort_key)

        self._last_query = query
        # 存储去除类别过滤器后的纯文本 query，供 _render 传给 widget 用于高亮
        # 避免 #agent 等类别标签干扰 & 和 | 的解析
        self._current_text_query = text_query

        self._render(incremental=incremental)

        if len(self._filtered_items) > 0:
            # 强制重置 _last_selected_index，确保新渲染的列表始终正确选中第一项
            # 防止上次会话残留的选中索引导致 _update_selection 守卫条件异常
            self._last_selected_index = -1
            self._selected_index = 0
            self._update_selection()

    def _render(self, incremental: bool = False):
        """渲染当前筛选结果

        Args:
            incremental: 是否增量更新（保留匹配项，重用已有 widget）

        快速路径：新旧 items 完全相同（names+types 一致）时跳过全量重建，
        仅刷新 widget 高亮，避免快速敲键时反复重排布局。
        """
        new_items = self._filtered_items
        old_widgets = list(self._item_widgets)  # 复制一份

        # ---- 快速路径：新旧 items 完全一致，仅更新高亮 ----
        if len(old_widgets) == len(new_items):
            same = all(
                old_widgets[i].item_data.get("name") == new_items[i]["name"]
                and old_widgets[i].item_data.get("type") == new_items[i]["type"]
                and old_widgets[i].item_data.get("subtype") == new_items[i].get("subtype")
                for i in range(len(new_items))
            )
            if same:
                for w, item in zip(old_widgets, new_items):
                    w.reuse(item, self._current_text_query)
                # 快速路径仍需重新设置固定高度：
                # _reset_detail_mode() 会清除卡片的 minH/maxH 约束，
                # 如果不重新 setFixedHeight，layout 会算出很小的 natural_h
                # 导致容器无法展开到正常高度（见插件卡片关闭后命令卡片高度异常 bug）
                self._apply_list_height()
                # 快速路径 selected_index 不会变，仍需刷新 tooltip（描述可能因 query 高亮而变）
                if not self._detail_mode:
                    self._update_desc_tooltip()
                return

        # 构建旧 widget 的 key 映射
        old_by_key = {}
        for w in old_widgets:
            try:
                _ = w.isVisible()
                d = w.item_data
                key = (d["name"], d["type"])
                if key not in old_by_key:
                    old_by_key[key] = w
            except RuntimeError:
                continue

        # 重建 _item_widgets：根据新顺序匹配或创建 widget
        new_widgets: List[CommandItemWidget] = []
        old_by_key_copy = dict(old_by_key)  # 副本，用于消耗
        seen_keys = set()

        for item in new_items:
            key = (item["name"], item["type"])
            # 优先复用未用过的旧 widget
            if key in old_by_key_copy and key not in seen_keys:
                w = old_by_key_copy.pop(key)  # 消耗掉这个 key
                seen_keys.add(key)
                w.reuse(item, self._current_text_query)
                new_widgets.append(w)
            else:
                # 创建新 widget
                w = CommandItemWidget(item, self._current_text_query, self._scroll_content)
                w.clicked.connect(self._on_item_clicked)
                w.hovered.connect(self._on_item_hovered)
                new_widgets.append(w)

        self._item_widgets = new_widgets

        # 删除不再需要的旧 widget（未被复用的）
        for w in old_by_key_copy.values():
            try:
                self._scroll_layout.removeWidget(w)
                w.deleteLater()
            except RuntimeError:
                continue

        # 删除旧分隔线（保证 _dividers 与 scroll_layout 状态一致）
        for div in self._dividers:
            try:
                self._scroll_layout.removeWidget(div)
                div.deleteLater()
            except RuntimeError:
                pass
        self._dividers.clear()

        # 清空 layout，重新按正确顺序添加 widget
        while self._scroll_layout.count():
            child = self._scroll_layout.takeAt(0)
            if child.widget():
                pass  # 仅移除，不删除（widget 在 _item_widgets 中）

        # 计算每个 item 所属的分区编号
        # 0=内置命令, 1=UI 插件, 2=技能, 3=智能体/提示词
        def _section(item):
            t = item["type"]
            if t == "command" and item.get("subtype") == "ui_plugin":
                return 1
            if t == "command":
                return 0
            if t == "skill":
                return 2
            return 3  # agent/prompt

        # 计算需要在索引 i 前插入分隔线的位置列表
        divider_positions = []
        for i in range(1, len(new_items)):
            if _section(new_items[i]) != _section(new_items[i - 1]):
                divider_positions.append(i)

        # 添加 widget 和分隔线，按顺序
        next_div_idx = 0
        for i, widget in enumerate(self._item_widgets):
            if next_div_idx < len(divider_positions) and i == divider_positions[next_div_idx]:
                divider = QFrame()
                divider.setFrameShape(QFrame.HLine)
                divider.setFixedHeight(1)
                divider.setStyleSheet(f"background: {Colors.DIVIDER_COLOR}; border: none;")
                divider.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                self._scroll_layout.addWidget(divider)
                self._dividers.append(divider)
                next_div_idx += 1
            self._scroll_layout.addWidget(widget)

        # 计算卡片高度（列表部分 + 顶部描述 tooltip）
        self._apply_list_height()
        # 重新渲染后 selected_index 可能已被重置为 0，更新 tooltip
        if not self._detail_mode:
            self._update_desc_tooltip()

    def _on_item_clicked(self):
        """item 被鼠标点击"""
        sender = self.sender()
        if sender in self._item_widgets:
            idx = self._item_widgets.index(sender)
            self._selected_index = idx
            self._update_selection()
            self.select_current()

    def _on_item_hovered(self, widget):
        """鼠标悬停到 item → 同步选中索引

        实现 hover 即选中：鼠标悬停到哪个 item，键盘导航的起始位置就跟随到哪。
        tooltip 也会自动跟随（_update_selection 内部调用 _update_desc_tooltip）。
        """
        if self._detail_mode:
            return  # detail 模式不处理列表 hover
        try:
            idx = self._item_widgets.index(widget)
        except ValueError:
            return
        # 索引相同则跳过，避免不必要的重绘
        if idx == self._selected_index:
            return
        self._selected_index = idx
        self._update_selection()

    def _on_param_hovered(self, widget):
        """鼠标悬停到参数项 → 同步选中索引（仅在 detail 模式 + 参数列表可见时生效）

        实现 hover 即选中：鼠标悬停到哪个参数，键盘导航的起始位置就跟随到哪。
        若当前处于值选择模式，悬停参数项不打断值列表浏览（让用户先选完值）。
        """
        if not self._detail_mode or self._value_selection_mode:
            return
        try:
            idx = self._param_widgets.index(widget)
        except ValueError:
            return
        if idx == self._selected_param_index:
            return
        self._selected_param_index = idx
        self._update_param_selection()

    def _on_value_hovered(self, widget):
        """鼠标悬停到值选择项 → 同步选中索引（仅在值选择模式生效）"""
        if not self._value_selection_mode:
            return
        try:
            idx = self._value_widgets.index(widget)
        except ValueError:
            return
        if idx == self._selected_value_index:
            return
        self._selected_value_index = idx
        self._update_value_selection()

    def _on_detail_clicked(self, event):
        """detail 模式点击 → 选中当前命令（携带 detail 选中类型）

        有参数列表时不响应点击防止误触，改用参数项点击。
        """
        if self._detail_has_params or self._value_selection_mode:
            return  # 有交互列表时不响应容器点击
        self.commandSelected.emit(self._detail_cmd_name, self._detail_selected_type)
        self.dismiss()

    def _update_selection(self):
        """更新选中高亮，并记录当前选中项类型供 detail 模式使用"""
        safe_widgets = []
        for widget in self._item_widgets:
            try:
                _ = widget.isVisible()
                safe_widgets.append(widget)
            except RuntimeError:
                continue
        self._item_widgets = safe_widgets

        old_idx = self._last_selected_index
        new_idx = self._selected_index

        # 只更新变化的 widget（旧选中取消 + 新选中激活）
        if old_idx != new_idx:
            if 0 <= old_idx < len(self._item_widgets):
                self._item_widgets[old_idx].set_selected(False)
            if 0 <= new_idx < len(self._item_widgets):
                self._item_widgets[new_idx].set_selected(True)
        elif 0 <= new_idx < len(self._item_widgets):
            # 索引相同但需要刷新（如首次选中）
            self._item_widgets[new_idx].set_selected(True)

        self._last_selected_index = new_idx

        # 记录当前选中项的 display_type（用于 detail 模式显示/执行）
        if 0 <= self._selected_index < len(self._filtered_items):
            self._current_selected_type = self._filtered_items[self._selected_index].get("type", "")
        else:
            self._current_selected_type = ""

        # 刷新顶部描述 tooltip（仅列表模式；detail 模式自带描述区）
        if not self._detail_mode:
            self._update_desc_tooltip()

        # 滚动到可见区域
        if 0 <= self._selected_index < len(self._item_widgets):
            self._scroll_area.ensureWidgetVisible(self._item_widgets[self._selected_index], 0, 0)

    def select_next(self) -> bool:
        """选择下一项。返回 True 表示已处理，False 表示未处理（让按键透传）。"""
        if self._value_selection_mode:
            if self._value_widgets and self._selected_value_index < len(self._value_widgets) - 1:
                self._selected_value_index += 1
                self._update_value_selection()
            return True
        if self._detail_mode and self._detail_has_params:
            # 只对可见参数导航
            visible = [i for i, w in enumerate(self._param_widgets) if w.isVisible()]
            # 只有一个（或零个）可见参数时无需导航，让按键透传到输入区域
            if len(visible) <= 1:
                return False
            if self._selected_param_index < visible[-1]:
                # 找到下一个可见的
                current_pos = visible.index(self._selected_param_index) if self._selected_param_index in visible else -1
                if current_pos < len(visible) - 1:
                    self._selected_param_index = visible[current_pos + 1]
                    self._update_param_selection()
            return True
        # detail 模式且无交互参数 → 不处理，让按键透传到输入框
        if self._detail_mode:
            return False
        # 列表模式
        if self._item_widgets and self._selected_index < len(self._item_widgets) - 1:
            self._selected_index += 1
            self._update_selection()
        return True

    def select_prev(self) -> bool:
        """选择上一项。返回 True 表示已处理，False 表示未处理（让按键透传）。"""
        if self._value_selection_mode:
            if self._value_widgets and self._selected_value_index > 0:
                self._selected_value_index -= 1
                self._update_value_selection()
            return True
        if self._detail_mode and self._detail_has_params:
            visible = [i for i, w in enumerate(self._param_widgets) if w.isVisible()]
            # 只有一个（或零个）可见参数时无需导航，让按键透传到输入区域
            if len(visible) <= 1:
                return False
            if self._selected_param_index > visible[0]:
                current_pos = visible.index(self._selected_param_index) if self._selected_param_index in visible else -1
                if current_pos > 0:
                    self._selected_param_index = visible[current_pos - 1]
                    self._update_param_selection()
            return True
        # detail 模式且无交互参数 → 不处理，让按键透传到输入框
        if self._detail_mode:
            return False
        # 列表模式
        if self._item_widgets and self._selected_index > 0:
            self._selected_index -= 1
            self._update_selection()
        return True

    def select_current(self):
        """确认选中当前项"""
        if self._value_selection_mode:
            # 值选择模式：选中当前高亮的值
            if 0 <= self._selected_value_index < len(self._value_widgets):
                widget = self._value_widgets[self._selected_value_index]
                if widget.value:
                    self.parameterValueSelected.emit(widget.value)
                    self._exit_value_selection()
            return
        if self._detail_mode and self._detail_has_params:
            # 参数列表模式：选中当前高亮的参数（仅当可见时）
            visible_widgets = [w for w in self._param_widgets if w.isVisible()]
            if not visible_widgets:
                return  # 无可见参数，不做插入（等待用户继续操作）
            if 0 <= self._selected_param_index < len(self._param_widgets):
                widget = self._param_widgets[self._selected_param_index]
                if widget.isVisible():
                    # 已激活参数再次按 Tab/Enter → 取消该参数，而非重复补全
                    if widget.is_active:
                        self.parameterDeselected.emit(widget.param_name, widget.param_type)
                        return
                    self._execute_param_selection(widget)
            return
        if self._detail_mode:
            # detail 模式（静态 hint）：选中命令
            self.commandSelected.emit(self._detail_cmd_name, self._detail_selected_type)
            self.dismiss()
            return
        # 列表模式：选中命令/技能
        if 0 <= self._selected_index < len(self._filtered_items):
            item = self._filtered_items[self._selected_index]
            insert_name = item.get("display_name", item["name"])
            self.commandSelected.emit(insert_name, item["type"])
            # 如果 emit 触发了 textChanged → _on_slash_trigger_check → detail 模式，
            # 则不再 dismiss（卡片切换到 detail 模式继续可见）
            if not self._detail_mode:
                self.dismiss()

    def dismiss(self):
        """关闭卡片（清理状态并隐藏自身）"""
        self._reset_detail_mode()
        self._visible = False
        self.setVisible(False)
        # 关闭时清空 tooltip 文本，避免下次 show_card 闪现旧描述
        if hasattr(self, "_desc_tooltip_label") and self._desc_tooltip_label is not None:
            self._desc_tooltip_label.setVisible(False)
            self._desc_tooltip_divider.setVisible(False)
            self._desc_tooltip_label.setText("")
        self.dismissed.emit()

    def show_card(self, query: str = "", incremental: bool = True):
        """加载数据并显示（显示由 CardManager 控制，此方法只准备数据）

        Args:
            query: 搜索查询
            incremental: 是否增量更新（默认开启，可提升流畅性）
        """
        # 在入口刷新一次颜色，避免每个 widget 的 _apply_style 都读配置文件
        Colors.refresh()
        was_detail = self._reset_detail_mode()  # 回到列表模式
        self._current_query = query
        self._refresh_data()
        # 从 detail 回列表时强制全量刷新，避免首次高度异常
        self.load_items(query, incremental=incremental and not was_detail)
        has_items = len(self._filtered_items) > 0
        self._visible = has_items
        self.setVisible(has_items)
        if was_detail:
            self.updateGeometry()

    def invalidate_cache(self):
        """使缓存失效，下次 show_card 时自动重建

        由外部（如 main_widget）在插件热重载后调用。
        """
        self._cache_dirty = True

    def refresh_if_visible(self):
        """热重载后调用：仅在卡片可见时重建数据并刷新 UI

        修复：原代码在插件热重载后强制调用 show_card(query)，会触发
        _reset_detail_mode()，导致用户正在查看的 detail 模式参数提示突然
        消失。本方法改为：
        - 卡片不可见：仅标记 dirty，下次 show_card 自动重建
        - 卡片可见且处于列表模式：重建数据 + load_items 保留当前过滤
        - 卡片可见且处于 detail 模式：重建数据 + 刷新 detail 视图
          （保留参数提示，仅更新命令元数据）

        此外用 _current_query 而非 input_area.toPlainText()，避免
        破坏当前过滤上下文。
        """
        if not self._visible:
            # 卡片不可见：仅标记脏，下次 show_card 重建
            self._cache_dirty = True
            return
        # 卡片可见：重建数据
        self._refresh_data()
        if self._detail_mode:
            # detail 模式：重建 detail 视图（参数描述、参数列表）以反映命令变更
            try:
                self._refresh_detail_view()
            except Exception:
                # 重建失败时不破坏当前 detail 视图（避免参数提示消失）
                logger.warning("[CommandCard] detail 视图重建失败，保持当前状态")
            return
        # 列表模式：用当前 query 重新加载（非增量以保证排序/分组准确）
        self.load_items(self._current_query, incremental=False)

    def _refresh_detail_view(self):
        """重建 detail 模式的参数视图（命令元数据变更后调用）

        与 show_command_detail 不同：本方法强制重新渲染（即使 cmd_name 未变），
        以反映热重载后的命令/参数描述变更。
        保留 _current_query 和输入框上下文。
        """
        cmd_name = self._detail_cmd_name
        if not cmd_name:
            return
        selected_type = self._detail_selected_type or ""
        # 临时退出 detail 模式，绕过 show_command_detail 的"已在此命令则跳过"逻辑
        # 然后立即重新进入，触发完整重建
        # 注意：_reset_detail_mode 会清空 _detail_cmd_name，需要先备份
        saved_cmd_name = self._detail_cmd_name
        saved_selected_type = self._detail_selected_type
        self._reset_detail_mode()
        # 恢复 _detail_mode=False 已被 _reset_detail_mode 设置，
        # show_command_detail 会重新设置 _detail_mode=True
        self.show_command_detail(saved_cmd_name, saved_selected_type)

    @property
    def is_card_visible(self) -> bool:
        return self._visible

    @property
    def filtered_count(self) -> int:
        return len(self._filtered_items)
