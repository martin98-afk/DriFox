# -*- coding: utf-8 -*-
"""
斜杠命令卡片 - 输入框上方展开，显示命令和技能列表

触发方式：在输入框输入 / 后，卡片自动展开
数据来源：CommandManager 内置命令 + get_local_skills()
交互方式：↑/↓ 导航，Enter 选中，Esc 关闭
"""

import bisect
import html
import math
from typing import Any, Dict, List, Optional

from loguru import logger
from PyQt5.QtCore import QEvent, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QMouseEvent, QPainter, QPen, QTextDocument
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import ScrollArea
from app.core.command_manager import CommandManager, CommandParameter, CommandType
from app.plugins.registries.ui_plugin_registry import UIPluginRegistry
from app.utils.design_tokens import Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import get_font_family_css, get_local_skills, get_skill_by_name
from app.widgets.cards.card_container import CardContainer
from app.widgets.elided_label import _ElidedLabel

ITEM_HEIGHT = 36  # 每个 item 高度
MAX_VISIBLE_ITEMS = 8  # 最多同时显示 item 数

# ── 虚拟化渲染参数 ──
# 列表只渲染可见窗口内的 widget（可见项 + 上下缓冲），滚动时复用池绑定数据，
# 避免命令/技能总数多时（100+ 项）每次敲键全量创建数百个 widget 导致卡顿。
VIRTUAL_BUFFER_SLOTS = 2  # 可见窗口上下各多渲染的缓冲槽数（滚动提前量）
VIRTUAL_POOL_SIZE = MAX_VISIBLE_ITEMS + VIRTUAL_BUFFER_SLOTS * 2  # item widget 池上限

# ── 矮窗口自适应参数 ──
# 命令卡片高度本身不受窗口约束（tooltip + 至多 MAX_VISIBLE_ITEMS 项）。
# 当窗口很矮时，自然高度会超过可用空间，挤掉聊天区/输入框导致整体显示很差。
# 策略：预算 = 顶层窗口高度 - 输入区实际高度 - 工具栏/最小聊天区预留；
# 仅当自然高度 > 预算时进入"压缩"——优先保留列表（主内容），牺牲次要的
# 顶部描述 tooltip；若仍放不下则减少可见项数量（剩余项在滚动区内滚动）。
# 正常/高窗口下不约束。无窗口环境（单元测试构造的无父 CommandCard）返回极大值。
CARD_MIN_VISIBLE_ITEMS = 1  # 矮到极致时仍保留的最少可见 item 数
CARD_MIN_HEIGHT = ITEM_HEIGHT * 2 + 1  # 矮到极致时至少保留 2 行 item 的高度
CARD_RESIZE_RESERVE = 120  # 顶部工具栏 + 最小聊天区预留高度（px）


def _qcolor_from_rgba(s: str) -> QColor:
    """将 Colors 中的 rgba(...) 字符串解析为 QColor

    Qt5 的 QColor 仅支持 #RRGGBB / 颜色名，不支持 rgba(r,g,b,a) 函数式写法，
    直接 QColor("rgba(33,33,38,250)") 会得到无效颜色（渲染成黑色）。
    这里手动解析元组构造 QColor，兼容 rgb()/rgba() 与十六进制/颜色名回退。
    """
    s = (s or "").strip()
    if s.startswith("rgb(") or s.startswith("rgba("):
        inner = s[s.index("(") + 1 : s.rindex(")")]
        parts = [p.strip() for p in inner.split(",")]
        if len(parts) >= 3:
            r, g, b = int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))
            a = 255
            if len(parts) >= 4:
                af = float(parts[3])
                a = int(af * 255) if af <= 1.0 else int(af)
            return QColor(r, g, b, a)
    return QColor(s)


def _split_value_entry(entry) -> tuple:
    """将枚举值条目拆分为 (value, description)

    兼容两种格式：
    - 纯字符串："gpt-4o" → ("gpt-4o", "")
    - dict：{"value": "gpt-4o", "description": "..."} → ("gpt-4o", "...")
    保持向后兼容：旧数据源（纯字符串列表）无需改动即可继续工作。
    """
    if isinstance(entry, dict):
        return str(entry.get("value", "")), str(entry.get("description", "") or "")
    return str(entry), ""


# ── 动态枚举值参数注册表（param_name → data_provider key） ──
# 走 data_provider 动态取值的 value 参数清单。新增动态枚举参数只需在此登记一行，
# _auto_switch_to_value_selection（候选过滤）与 _switch_to_value_selection /
# _refresh_value_list（取选项）三处共用本表，避免散在多处维护。
_DYNAMIC_VALUE_PROVIDERS: Dict[str, str] = {
    "--model=": "model_options",
    "--join=": "agent_options",
    "--plugin=": "plugin_options",
    "--load=": "template_options",
    "--delete=": "template_options",
}


def _get_value_options(
    param_name: str,
    data_provider: Dict[str, Any],
    param_widgets: Optional[List["ParameterItemWidget"]] = None,
) -> List[Any]:
    """统一从 data_provider / 静态 value_options 解析参数的可选值

    优先级：
    1. param_name 在 _DYNAMIC_VALUE_PROVIDERS 中 → 查 data_provider[key]
    2. 否则从 param_widgets 中匹配 param_name 的 widget 取 value_options
    3. 都没有 → 返回空列表

    三个值选择入口（_auto_switch_to_value_selection 候选过滤、
    _switch_to_value_selection 取值、_refresh_value_list 取值）都走本函数，
    保证数据源映射单一事实源。
    """
    provider_key = _DYNAMIC_VALUE_PROVIDERS.get(param_name)
    if provider_key is not None:
        return data_provider.get(provider_key, []) or []
    if param_widgets is not None:
        for w in param_widgets:
            if w.param_name == param_name:
                return w._param.value_options or []
    return []


class _DescTooltipBubble(QLabel):
    """悬浮描述气泡：自绘圆角主题实底背景

    背景说明（关键）：顶层窗口设置了 WA_TranslucentBackground 后，Qt 会跳过
    QStyle 的背景填充，导致样式表的 background 不绘制、窗口完全透明、文字看不清。
    因此这里在 paintEvent 中手动用 QPainter 绘制圆角实底背景 + 边框，保证背景
    始终可见；文字仍由基类 QLabel 渲染（含富文本关键字高亮）。
    圆角外的区域保持透明。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bg = _qcolor_from_rgba(Colors.CARD_BG_SOLID)
        self._border = _qcolor_from_rgba(Colors.DIVIDER_COLOR)
        self._radius = 6

    def setBrushColors(self, bg: str, border: str):
        """刷新背景/边框颜色（主题切换时调用）"""
        self._bg = _qcolor_from_rgba(bg)
        self._border = _qcolor_from_rgba(border)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(0, 0, -1, -1)
        r = self._radius
        # 背景实底
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._bg)
        painter.drawRoundedRect(rect, r, r)
        # 边框
        pen = QPen(self._border)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, r, r)
        # 文字（含富文本高亮）由基类绘制，位于背景之上
        super().paintEvent(event)


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
        self._name_label.setObjectName("nameLabel")
        # 强制 RichText：_update_display 对名称 html.escape 后 setText，
        # 无高亮时（不含 <span>）AutoText 会判定 PlainText，&quot;/&amp; 等
        # 实体字面显示（与 tooltip 的 &quot; 问题同源）；此处一次设置，所有分支生效
        self._name_label.setTextFormat(Qt.RichText)
        self._name_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._name_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        layout.addWidget(self._name_label)

        # 描述标签（Elided，空间不够时省略，仅技能显示描述）
        desc = self._data.get("description", "")
        self._desc_label = _ElidedLabel(desc)
        self._desc_label.setObjectName("descLabel")
        self._desc_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._desc_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._desc_label.setMinimumWidth(0)
        layout.addWidget(self._desc_label, 1)

        # 快捷键标签（仅内建命令的 function 类型显示）
        self._shortcut_label = QLabel()
        self._shortcut_label.setObjectName("shortcutLabel")
        self._shortcut_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._shortcut_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        layout.addWidget(self._shortcut_label)

        # 类型标签（始终创建，根据 item 类型动态显示/隐藏）
        self._tag_label = QLabel()
        self._tag_label.setObjectName("tagLabel")
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

        self._apply_style()
        self._update_display()

    def _apply_style(self):
        """应用当前状态的样式 — 单次 setStyleSheet 合并所有子标签样式

        性能优化：所有子标签的样式通过对象选择器级联，统一写在父级样式表中。
        避免每次 hover/selected 切换时对 4~5 个子标签分别调用 setStyleSheet
        （每个 setStyleSheet 触发完整的样式重算和布局刷新）。
        """
        # Colors.refresh() 不在此处调用——颜色在 show_card 时刷新一次即可
        # 避免每次导航/悬停都读配置文件

        if self._selected:
            bg = Colors.REALTIME_TAG_BG
        elif self._hovered:
            bg = Colors.HOVER_BG
        else:
            bg = "transparent"

        desc_fg = Colors.TEXT_PRIMARY if self._selected else Colors.TEXT_SECONDARY

        item_type = self._data["type"]
        is_ui_plugin = item_type == "command" and self._data.get("subtype") == "ui_plugin"

        # 标签颜色（根据类型 + 选中状态）
        if is_ui_plugin:
            tag_fg = Colors.TAG_GREEN if not self._selected else Colors.TAG_GREEN_TEXT
        elif item_type == "skill":
            tag_fg = Colors.TAG_ACCENT if not self._selected else Colors.TAG_ACCENT_TEXT
        elif item_type == "agent":
            tag_fg = Colors.TAG_PURPLE if not self._selected else Colors.TAG_PURPLE_TEXT
        elif item_type == "prompt":
            tag_fg = Colors.TAG_ORANGE if not self._selected else Colors.TAG_ORANGE_TEXT
        else:
            tag_fg = "transparent"

        ff = get_font_family_css()

        # 单次 setStyleSheet：父级背景 + 所有子标签样式通过 #objectName 级联
        self.setStyleSheet(f"""
            CommandItemWidget {{
                background-color: {bg};
                border: none;
                border-radius: 4px;
            }}
            CommandItemWidget QLabel#nameLabel {{
                color: {Colors.TEXT_PRIMARY};
                {ff} {font_size_css(13)};
                background: transparent;
            }}
            CommandItemWidget QLabel#descLabel {{
                color: {desc_fg};
                {ff} {font_size_css(11)};
                background: transparent;
            }}
            CommandItemWidget QLabel#shortcutLabel {{
                color: {Colors.TEXT_SECONDARY};
                {ff} {font_size_css(10)};
                background: rgba(128,128,128,0.1);
                border-radius: 3px;
                padding: 1px 5px;
                font-weight: bold;
            }}
            CommandItemWidget QLabel#tagLabel {{
                color: {tag_fg};
                {ff} {font_size_css(11)};
                font-weight: bold;
                background: transparent;
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

    def showEvent(self, event):
        """widget 首次显示时补一次样式应用

        新建 widget 在 _setup_ui 调用 _apply_style 时尚未挂载到可见布局，
        Qt 的 QStyleSheetStyle::repolish 对不可见 widget 跳过，级联 QSS
        （QLabel#tagLabel / #shortcutLabel 等子标签样式）不会应用，导致新项
        右侧类型标签/快捷键胶囊使用默认样式（黑字、无背景），hover 后才恢复。
        首次显示（showEvent）时在此补一次 _apply_style，保证级联样式落地；
        复用路径不经过此处（widget 保持可见），不会重复触发。
        """
        super().showEvent(event)
        self._apply_style()

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
    description 为可选元数据（来自枚举值条目），用于值选择模式下
    顶部 tooltip 显示当前枚举值的描述（复用 CommandCard 的悬浮气泡系统）。
    """

    clicked = pyqtSignal()
    hovered = pyqtSignal(object)  # 鼠标悬停时发射自身引用

    def __init__(self, value: str, description: str = "", parent=None):
        super().__init__(parent)
        self._value = value
        self._description = description or ""
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
        self._text_label.setObjectName("valueLabel")
        self._text_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._text_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self._text_label)

        self._apply_style()

    def _apply_style(self):
        """应用当前状态的样式 — 单次 setStyleSheet

        性能优化：文本标签样式（固定 PRIMARY）合并到父级样式表中，
        避免每次 hover/selected 时对子标签单独调用 setStyleSheet。
        """
        if self._selected:
            bg = Colors.REALTIME_TAG_BG
        elif self._hovered:
            bg = Colors.HOVER_BG
        else:
            bg = "transparent"

        ff = get_font_family_css()
        self.setStyleSheet(f"""
            ValueItemWidget {{
                background-color: {bg};
                border: none;
                border-radius: 4px;
            }}
            ValueItemWidget QLabel#valueLabel {{
                color: {Colors.TEXT_PRIMARY};
                background: transparent;
                {ff} {font_size_css(12)};
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

    def showEvent(self, event):
        """widget 首次显示时补一次样式应用（同 CommandItemWidget）

        新建 ValueItemWidget 在 _setup_ui 调用 _apply_style 时尚未可见，
        级联 QSS（ValueItemWidget QLabel#valueLabel）不会应用；首次显示时补一次，
        保证文字颜色/字体正确。
        """
        super().showEvent(event)
        self._apply_style()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    @property
    def value(self) -> str:
        return self._value

    @property
    def description(self) -> str:
        return self._description


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

        # ── 虚拟化渲染状态 ──
        # 列表采用虚拟化：_scroll_content 高度设为虚拟总高度，只创建/绑定
        # 可见窗口（含缓冲）内的 widget 池，滚动时复用池 widget 移动定位，
        # 不随匹配项总数增长（内容再多也只渲染十几个 widget）。
        # 复用循环只操作 _free_item_widgets（绑定弹池 / 解绑回池）；
        # _item_pool 仅记录「曾创建过的所有 widget」（调试/统计用），
        # 溢出保护新建时受 VIRTUAL_POOL_SIZE 上限约束。
        self._item_pool: List[CommandItemWidget] = []  # 曾创建过的所有 widget（非复用池）
        self._free_item_widgets: List[CommandItemWidget] = []  # 未绑定槽的空闲池
        self._divider_pool: List[QFrame] = []  # 分隔线池（数量极少，复用免重建）
        self._slot_widgets: Dict[int, QWidget] = {}  # 虚拟槽索引 → 已绑定 widget
        self._virtual_slots: List[tuple] = []  # (kind, item_idx/None, y)：kind ∈ {"item","divider"}
        self._virtual_slot_ys: List[int] = []  # 每个槽的 y 偏移（bisect 快速定位可见范围）
        self._virtual_total_height: int = 0  # 虚拟内容总高度（含分隔线）
        self._divider_count: int = 0  # 分区分隔线数量
        self._last_render_keys: Optional[List[tuple]] = None  # 上次渲染的 (name,type,subtype) 列表
        self._dummy_item: Dict[str, str] = {"name": "", "description": "", "type": "command"}
        self._bound_content_width: int = -1  # 上次同步给池 widget 的内容宽度（宽度变化时全量同步）
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
        self._value_just_selected_param: str = ""  # 刚通过枚举选中退出的参数名（防抖回声抑制标记）
        self._value_widgets: List[QWidget] = []  # 值选择列表项
        self._selected_value_index: int = -1  # 值列表选中索引
        self._last_selected_value_index: int = -1  # 上次值列表选中索引，用于增量更新
        self._data_provider: dict = {}  # 外部数据源（如 model_options）
        # 跳过容器展开/折叠动画，消除命令卡片弹出时的延迟感
        self.setProperty(CardContainer.NO_ANIMATION_PROP, True)
        # 声明高度严格跟随内容：dock 模式下容器也按卡片高度收缩，
        # 否则过滤后卡片变矮、容器保持旧高度 → 最后一行下方留白（同 Question 卡片）。
        self.setProperty(CardContainer.FOLLOW_CONTENT_PROP, True)
        # 卡片当前目标高度（heightForWidth 供容器 follow_content 分支锁定高度用）
        self._card_target_height = 0
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
        # 用户难以阅读完整说明。此 tooltip 以「悬浮气泡」形式浮在卡片上方
        # （覆盖聊天区），不参与卡片布局，因此无论描述多长都不会挤占命令列表
        # 的剩余空间；空描述/详情模式下隐藏。
        # 延迟到首次显示时再创建（需要顶层窗口作为父级，__init__ 阶段尚无窗口）。
        self._desc_tooltip_label: Optional[QLabel] = None
        self._last_tip_width = -1
        self._has_tooltip_text = False  # 当前选中项是否有可展示的描述文本
        self._window_resize_hooked = False  # 是否已给顶层窗口安装 resize 事件过滤器
        self._parent_move_hooked = False  # 是否已给父控件安装 move 事件过滤器
        self._resize_recompute_timer: Optional[QTimer] = None  # 窗口 resize 防抖重算

        # 滚动区域
        self._scroll_area = ScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(ScrollArea.NoFrame)
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
        # 注意：content 不挂 QVBoxLayout —— 虚拟化列表用 move 绝对定位池 widget，
        # 由 _scroll_content.setFixedHeight(虚拟总高度) 撑起滚动范围。

        self._scroll_area.setWidget(self._scroll_content)
        # 确保 viewport 没有多余的边距/内边距（这是导致顶部空白的根本原因）
        self._scroll_area.viewport().setStyleSheet("background: transparent; border: none; padding: 0; margin: 0;")
        layout.addWidget(self._scroll_area)

        # 虚拟化：滚动时按需绑定/解绑可见窗口内的池 widget
        self._scroll_area.verticalScrollBar().valueChanged.connect(self._sync_visible_slots)
        # 内容宽度跟随 viewport 变化时，同步池 widget 宽度（仅绑定一次，防重复闭包）
        if not getattr(self._scroll_content, "_resize_hooked", False):
            _orig_content_resize = self._scroll_content.resizeEvent

            def _on_content_resize(event):
                _orig_content_resize(event)
                self._sync_visible_slots()

            self._scroll_content.resizeEvent = _on_content_resize
            self._scroll_content._resize_hooked = True

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

    def _apply_scroll_area_styles(self, scroll_area: "ScrollArea"):
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
        # 1.4 主命令列表滚动区（滚动条颜色随主题，保持原始 8px 宽度）
        if hasattr(self, "_scroll_area") and self._scroll_area is not None:
            self._scroll_area.setStyleSheet(f"""
                QScrollArea, QScrollArea * {{
                    background: transparent;
                    border: none;
                    padding: 0;
                    margin: 0;
                }}
                {get_unified_scrollbar_style(8)}
            """)
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
        # 4. 命令列表 widget（hover/selected 背景）—— 虚拟化：遍历已绑定槽
        for w in list(self._slot_widgets.values()):
            if not isinstance(w, CommandItemWidget):
                continue
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
        # 7. 分隔线列表（虚拟化：遍历已绑定槽中的 QFrame）
        for w in list(self._slot_widgets.values()):
            if not isinstance(w, QFrame):
                continue
            try:
                w.setStyleSheet(f"background: {Colors.DIVIDER_COLOR}; border: none;")
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
        self._detail_params_scroll = ScrollArea()
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
        self._detail_value_scroll = ScrollArea()
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
        """刷新悬浮描述 tooltip（气泡）的样式

        视觉规范：
        - 主题实底背景（CARD_BG_SOLID，卡片表面色），保证在任意聊天背景上都清晰可读
        - 圆角 + 细分隔边框，营造悬浮信息气泡质感
        - 文字保持 TEXT_PRIMARY，确保一眼可读
        - 气泡以独立顶层窗口（WA_TranslucentBackground）悬浮在卡片上方，
          由 _position_desc_tooltip 定位；圆角外的区域保持透明
        """
        Colors.refresh()
        if getattr(self, "_desc_tooltip_label", None) is None:
            return
        # 背景/边框由 _DescTooltipBubble.paintEvent 自绘（WA_TranslucentBackground 下
        # 样式表 background 不绘制），此处通过 setBrushColors 传入主题色。
        self._desc_tooltip_label.setBrushColors(Colors.CARD_BG_SOLID, Colors.DIVIDER_COLOR)
        self._desc_tooltip_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                {get_font_family_css()} {font_size_css(11)};
                border: none;
                border-radius: 6px;
                margin: 6px 8px 6px 8px;
                padding: 6px 10px;
            }}
        """)
        # 字体/主题变化可能改变换行高度，重新定位气泡
        if self._desc_tooltip_label.isVisible():
            self._position_desc_tooltip()

    # ── 悬浮描述气泡（顶层窗口，浮于卡片上方，不占卡片布局高度） ──

    def _ensure_desc_tooltip(self):
        """惰性创建悬浮描述气泡（需要顶层窗口作为父级）

        将气泡创建为独立顶层窗口（Frameless + ToolTip + 透明背景），
        浮在卡片上方覆盖聊天区，鼠标穿透，带阴影营造悬浮感。
        """
        if getattr(self, "_desc_tooltip_label", None) is not None:
            return
        top = self.window()
        if top is None:
            return
        lbl = _DescTooltipBubble(top)
        lbl.setWindowFlags(Qt.FramelessWindowHint | Qt.ToolTip | Qt.WindowDoesNotAcceptFocus)
        lbl.setAttribute(Qt.WA_TranslucentBackground, True)
        lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.NoTextInteraction)
        lbl.setAlignment(Qt.AlignLeft | Qt.AlignTop)  # 顶部对齐，避免 VCenter 平分多余空间导致上下 padding 不均
        # ⚠️ 注意：不在顶层 WA_TranslucentBackground 窗口上使用 QGraphicsDropShadowEffect。
        # Windows 分层窗口（Layered Window）下，QGraphicsDropShadowEffect 的 bounding rect
        # 会延伸到窗口边界之外，导致 Qt 传给 UpdateLayeredWindowIndirect 的脏区域包含负坐标，
        # Windows 拒绝该参数并报 "参数错误"（UpdateLayeredWindowIndirect failed）。
        # 气泡已通过 paintEvent 自绘圆角实底 + 边框，视觉上足够清晰。
        self._desc_tooltip_label = lbl
        self._apply_desc_tooltip_style()
        # 安装父控件 move 钩子，确保气泡跟随父控件移动
        self._ensure_parent_move_hook()
        # 气泡位置在每次显示/窗口变化时由 _position_desc_tooltip 计算

    def _position_desc_tooltip(self):
        """将悬浮气泡定位到卡片正上方（覆盖聊天区），并自适应宽度/高度

        气泡底部与卡片顶边保留 GAP 间距；宽度对齐卡片宽度。
        高度按当前宽度精确测量换行后高度（含样式 margin/padding），
        并受可用上方空间约束，避免极端长描述溢出屏幕顶部。
        """
        lbl = getattr(self, "_desc_tooltip_label", None)
        if lbl is None or not lbl.isVisible():
            return
        top = self.window()
        if top is None:
            return
        card_global = self.mapToGlobal(QPoint(0, 0))
        win_global = top.mapToGlobal(QPoint(0, 0))
        gap = 6
        # 对齐卡片宽度（气泡比卡片窄 2px 以贴合圆角边框）
        w = max(1, self.width() - 2)
        lbl.setFixedWidth(w)
        # 可用上方空间（窗口顶部到卡片顶边，减去间距）
        available_above = max(0, (card_global.y() - win_global.y()) - gap)
        if available_above > 60:
            line_h = lbl.fontMetrics().lineSpacing()
            allowed = max(2, (available_above - 22) // line_h)
            tip_h = self._compute_desc_tooltip_height(max_lines=min(16, allowed))
        else:
            tip_h = self._compute_desc_tooltip_height(max_lines=16)
        # 安全兜底：气泡高度不超过卡片上方可用空间，避免溢出屏幕顶部
        if available_above > 0:
            tip_h = min(tip_h, available_above)
        else:
            # 卡片上方无可用空间（卡片紧贴窗口顶边），隐藏气泡避免遮挡卡片
            tip_h = 0
        lbl.setFixedHeight(tip_h)
        if tip_h <= 0:
            lbl.setVisible(False)
            return
        gx = card_global.x() + 1
        gy = card_global.y() - tip_h - gap
        if gy < win_global.y():
            gy = win_global.y()
        lbl.move(gx, gy)

    def _compute_desc_tooltip_height(self, max_lines: int = 16) -> int:
        """计算描述 tooltip 的高度（像素）

        - 用 QTextDocument 以 QLabel 自身相同的字体/富文本引擎精确测量换行后高度，
          与最终渲染结果一致，避免旧实现用 QFontMetrics.boundingRect 估算时
          （未计入富文本行高、宽度取值偏差、HTML 高亮标签被当作正文等）导致高度偏小、
          文字被裁切（包括首行只显示半截）的问题。
        - 优先使用 label 的实际宽度（已布局）；卡片尚未布局时退回 card 宽度估算。
        - max_lines 仅作为极端长描述的兜底上限，正常描述会完整展示。
        """
        if not hasattr(self, "_desc_tooltip_label") or self._desc_tooltip_label is None:
            return 0
        text = self._desc_tooltip_label.text()
        if not text.strip():
            return 0
        # 实际可用宽度：优先 label 已布局宽度，否则退回卡片宽度 - 左右边框(2px)
        label_w = self._desc_tooltip_label.width()
        if label_w <= 0:
            label_w = self.width() - 2
        if label_w <= 0:
            return 0
        # 样式表：margin 6px 8px 6px 8px + padding 6px 10px
        # → 文本实际渲染宽度 = label 宽度 - 左右 margin(8*2) - 左右 padding(10*2)
        inner_w = max(1, label_w - 36)
        # 用 QTextDocument 复现 QLabel 的换行高度（documentMargin=0 表示只量文本本身，
        # 外层 margin/padding 由下方手动累加，数值完全可控）。
        doc = QTextDocument()
        doc.setDocumentMargin(0)
        doc.setDefaultFont(self._desc_tooltip_label.font())
        if self._desc_tooltip_label.textFormat() == Qt.RichText:
            doc.setHtml(text)
        else:
            doc.setPlainText(text)
        doc.setTextWidth(inner_w)
        text_h = math.ceil(doc.size().height())
        # 上下 margin(6+6) + 上下 padding(6+6) = 24px
        # 额外加 4px 容差：QTextDocument 无父级时使用默认 DPI 做字体度量，
        # 与 QLabel 实际屏幕渲染存在亚像素差异（尤其是 125%+ 缩放时），
        # 导致短文本最后一行底部被吞 1/3，加 4px 安全边距补偿。
        total = 28 + text_h
        if max_lines and max_lines > 0:
            fm = self._desc_tooltip_label.fontMetrics()
            line_h = fm.lineSpacing()
            total = min(total, 24 + int(max_lines * line_h))
        return total

    def _update_desc_tooltip(self, item: Optional[Dict[str, str]] = None):
        """根据当前选中项更新悬浮气泡文本与可见性

        Args:
            item: 可选的选中项数据；为 None 时从 _filtered_items 与 _selected_index 推导
        """
        # 延迟创建悬浮气泡（需要顶层窗口）
        self._ensure_desc_tooltip()
        lbl = getattr(self, "_desc_tooltip_label", None)
        if lbl is None:
            return
        # 值选择模式（枚举值列表）：最上方 tooltip 显示当前选中枚举值的描述
        if self._value_selection_mode:
            self._update_value_desc_tooltip()
            return
        # 其他 detail 模式（参数列表）自带描述区，无需重复 tooltip
        if self._detail_mode:
            lbl.setVisible(False)
            # detail 模式的高度由 _adjust_detail_height 控制，此处不刷新
            return
        if item is None:
            if 0 <= self._selected_index < len(self._filtered_items):
                item = self._filtered_items[self._selected_index]
            else:
                item = None
        desc = (item or {}).get("description", "") if item else ""
        if not desc.strip():
            # 空描述：隐藏（不显示空白气泡）；气泡独立窗口，无布局高度需刷新
            self._has_tooltip_text = False
            lbl.setVisible(False)
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
        # QLabel AutoText 只认字面 < 与 "& "，不认 &quot; 等 HTML 实体：
        # escape 后不含 < 的文本会被判定为 PlainText，实体字面显示（&quot; 问题）。
        # 显式强制 RichText，让实体在渲染时被解析为真实字符。
        lbl.setTextFormat(Qt.RichText)
        lbl.setText(safe)
        self._has_tooltip_text = True
        # 先定位再显示，避免 tooltip 在错误位置闪现
        # 若卡片尚未布局（width<=0），延迟到 layout 完成后再定位+显示
        if self.width() <= 0:
            lbl.setVisible(False)
            QTimer.singleShot(0, self._update_desc_tooltip)
            return
        self._position_desc_tooltip()
        lbl.setVisible(True)

    def _update_value_desc_tooltip(self):
        """值选择模式（枚举值列表）：最上方 tooltip 显示当前选中枚举值的描述

        复用列表模式的悬浮气泡系统（_desc_tooltip_label）：气泡独立顶层窗口，
        浮在卡片上方，不占布局高度；无描述（或空描述）时隐藏，与列表模式一致。
        """
        lbl = getattr(self, "_desc_tooltip_label", None)
        if lbl is None:
            return
        desc = ""
        if 0 <= self._selected_value_index < len(self._value_widgets):
            w = self._value_widgets[self._selected_value_index]
            desc = getattr(w, "description", "") or ""
        desc = (desc or "").strip()
        if not desc:
            self._has_tooltip_text = False
            lbl.setVisible(False)
            return
        safe = html.escape(desc)
        # 同 _update_desc_tooltip：强制 RichText，避免 AutoText 将纯文本实体
        # （&quot; 等）判定为 PlainText 而字面显示
        lbl.setTextFormat(Qt.RichText)
        lbl.setText(safe)
        self._has_tooltip_text = True
        # 先定位再显示，避免 tooltip 在错误位置闪现；卡片未布局时延迟
        if self.width() <= 0:
            lbl.setVisible(False)
            QTimer.singleShot(0, self._update_desc_tooltip)
            return
        self._position_desc_tooltip()
        lbl.setVisible(True)

    def _apply_list_height(self):
        """统一刷新卡片总高度：仅命令列表高度（含分区分隔线）

        由 _render / 选中变更 / detail→list 切换等多个入口复用。
        当卡片空列表时直接置 0（卡片可见性由 _filtered_items 控制，调用方负责）。

        顶部描述 tooltip 已改为「悬浮气泡」（独立顶层窗口，见 _desc_tooltip_label），
        不在卡片布局内，因此不占用卡片高度预算——无论描述多长，命令列表始终拥有
        完整可用空间。矮窗口自适应仅压缩列表可见项数量（剩余项在滚动区滚动）。
        detail 模式由 _adjust_detail_height 控制高度，此处不干预。
        """
        # 虚拟化后 _item_widgets 是固定大小的池，改用数据源计数
        # 高度口径必须与 _build_virtual_layout 一致：
        # 真实总高 = item_count * ITEM_HEIGHT + divider_count * 1
        # 若用 total_items（含 dividers）作 visible 基数，
        # 当 item_count < MAX_VISIBLE_ITEMS 时 visible = item_count + divider_count，
        # 卡片高度 = (item_count + divider_count) * ITEM_HEIGHT + divider_count
        # = 真实高度 + divider_count * (ITEM_HEIGHT - 1) + (visible - item_count) * ITEM_HEIGHT
        # 即多出空槽占位，导致列表底部出现大量空白。
        # 因此 visible 只能按 item_count 计算，dividers 单独加 1px 即可。
        item_count = len(self._filtered_items)
        divider_count = self._divider_count
        total_items = item_count + divider_count
        if total_items == 0:
            self.setFixedHeight(0)
            return
        if self._detail_mode:
            return

        visible = min(item_count, MAX_VISIBLE_ITEMS)
        natural = visible * ITEM_HEIGHT + divider_count * 1

        budget = self._available_card_budget()
        if natural <= budget:
            # 正常/高窗口：完整展示至多 MAX_VISIBLE_ITEMS 项
            self._card_target_height = natural
            self.setFixedHeight(natural)
            self._sync_desc_tooltip_position()
            self._sync_visible_slots()
            return

        # ── 矮窗口自适应压缩：仅压缩可见项数量 ──
        visible_fit = max(
            CARD_MIN_VISIBLE_ITEMS,
            min(item_count, MAX_VISIBLE_ITEMS, budget // ITEM_HEIGHT),
        )
        self._card_target_height = visible_fit * ITEM_HEIGHT + divider_count * 1
        self.setFixedHeight(self._card_target_height)
        self._sync_desc_tooltip_position()
        self._sync_visible_slots()

    def hasHeightForWidth(self):
        # follow_content 分支用 heightForWidth 精确锁定容器高度（避开 C++ 布局
        # sizeHint 受 QScrollArea 默认尺寸影响的不确定性）。
        return True

    def heightForWidth(self, w):
        """按宽度返回卡片目标高度（列表/矮窗口压缩/detail 三态统一）"""
        return getattr(self, "_card_target_height", self.height()) or super().heightForWidth(w)

    def _sync_desc_tooltip_position(self):
        """卡片几何变化（高度/位置）后，将悬浮气泡重新锚定到卡片上方

        气泡以卡片顶边为锚点，卡片底部固定、高度变化时顶边随之移动，
        因此需要同步重定位，避免气泡悬空或覆盖卡片。
        """
        lbl = getattr(self, "_desc_tooltip_label", None)
        if lbl is not None and lbl.isVisible():
            self._position_desc_tooltip()

    def _available_card_budget(self) -> int:
        """命令卡片在矮窗口下允许占用的最大高度（px）

        预算 = 顶层窗口高度 - 输入区实际高度 - 工具栏/最小聊天区预留。
        仅在自然高度超过预算时进入压缩（见 _apply_list_height）。
        无窗口环境（单元测试构造的无父 CommandCard）返回极大值，即不约束——
        保持与旧行为一致，确保现有高度测试不受影响。
        """
        top = self.window()
        if top is None or top.height() <= 0:
            return 16777215
        reserve = CARD_RESIZE_RESERVE
        # 输入区实际高度（_input_card 在 _bottom_input_container 内，可能随内容增高）
        p = self.parent()
        if p is not None:
            inp = p.findChild(QWidget, "_input_card")
            if inp is not None:
                reserve += inp.height()
        budget = top.height() - reserve
        return max(CARD_MIN_HEIGHT, budget)

    def _ensure_parent_move_hook(self):
        """给父控件安装一次性 move 事件过滤器

        父控件（BottomCardContainer）移动时，CommandCard 作为子控件
        其 screen 位置也会改变，但 card 自身的 moveEvent 不会触发
        （因为 card 在父控件内的相对位置未变），导致悬浮气泡留在错误位置。
        此处监听父控件的 move 事件，同步重定位气泡。
        """
        if self._parent_move_hooked:
            return
        parent = self.parentWidget()
        if parent is None:
            return
        parent.installEventFilter(self)
        self._parent_move_hooked = True

    def _ensure_window_resize_hook(self):
        """给顶层窗口安装一次性的 resize 事件过滤器，窗口高度变化时按预算重算卡片高度"""
        if self._window_resize_hooked:
            return
        top = self.window()
        if top is None:
            return
        top.installEventFilter(self)
        self._window_resize_hooked = True
        # 同时安装父控件 move 钩子（两者独立，互不干扰）
        self._ensure_parent_move_hook()

    def eventFilter(self, obj, event):
        """监听顶层窗口 resize / move 以及父控件 move：

        - window resize：高度/宽度变化（卡片自身收不到的窗口级 resize）时按最新预算
          重算卡片高度，并重定位悬浮气泡，保证矮窗口压缩/放宽实时生效。
        - window move：窗口被拖动时，悬浮气泡（独立顶层窗口）需同步跟随重定位。
        - parent move：父控件移动导致卡片 screen 位置变化时，重定位气泡。
        """
        # 父控件移动 → 只需重定位悬浮气泡（卡片高度/预算不受影响）
        parent = self.parentWidget()
        if obj is parent and event.type() == QEvent.Move:
            self._sync_desc_tooltip_position()
            return False
        if obj is self.window() and event.type() in (QEvent.Resize, QEvent.Move):
            if self._resize_recompute_timer is None:
                self._resize_recompute_timer = QTimer(self)
                self._resize_recompute_timer.setSingleShot(True)
                self._resize_recompute_timer.setInterval(0)
                self._resize_recompute_timer.timeout.connect(self._on_window_geom_changed)
            self._resize_recompute_timer.start()
            return False
        return super().eventFilter(obj, event)

    def _on_window_geom_changed(self):
        """窗口尺寸/位置变化后的统一处理：重算卡片高度 + 重定位悬浮气泡"""
        self._apply_list_height()
        lbl = getattr(self, "_desc_tooltip_label", None)
        if lbl is not None and lbl.isVisible():
            self._position_desc_tooltip()

    def moveEvent(self, event):
        """卡片因布局（如底部锚定、高度变化）移动时，将悬浮气泡重新锚定到卡片上方"""
        super().moveEvent(event)
        self._sync_desc_tooltip_position()

    def showEvent(self, event):
        """卡片显示时同步 _visible 标志位

        当 CardManager 或其他外部代码直接调用 setVisible(True) 时，
        _visible 应当反映 widget 的实际可见性。此方法确保两者一致。
        """
        super().showEvent(event)
        self._visible = True

    def hideEvent(self, event):
        """卡片隐藏时同步 _visible 标志位并隐藏悬浮气泡

        当 CardManager 或其他外部代码直接调用 setVisible(False) 时，
        _visible 会被正确置为 False，避免 refresh_if_visible 误以为
        卡片仍可见而去刷新数据但不显示卡片。
        """
        super().hideEvent(event)
        self._visible = False
        lbl = getattr(self, "_desc_tooltip_label", None)
        if lbl is not None:
            lbl.hide()

    def resizeEvent(self, event):
        """卡片宽度变化（或首帧布局）时，重算并定位悬浮描述气泡 + 刷新卡片总高度。

        悬浮气泡是独立顶层窗口、按宽度换行的 QLabel。若卡片宽度改变
        （如窗口缩放）而气泡高度未同步，换行行数会变但高度不变，
        导致文字被裁切（首行也可能只显示半截）。此处按宽度变化重算并定位。
        窗口纯高度变化（宽度不变，卡片自身收不到 resizeEvent）由
        eventFilter 监听顶层窗口 resize 处理。
        仅当宽度真正变化时才重入气泡重算，避免 setFixedHeight 触发的
        高度变化再次进入本方法形成自激。
        """
        super().resizeEvent(event)
        self._ensure_window_resize_hook()
        new_w = self.width()
        if new_w == getattr(self, "_last_tip_width", -1):
            return
        self._last_tip_width = new_w
        lbl = getattr(self, "_desc_tooltip_label", None)
        if lbl is not None and lbl.isVisible():
            # 宽度变化：重新测算气泡换行高度并定位（_position_desc_tooltip 内含重算）
            self._position_desc_tooltip()
        else:
            # 气泡未显示：仍按新宽度/新预算重算卡片高度
            self._apply_list_height()

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
        skill = None

        if use_type == "skill":
            # 技能类型：直接查技能，不查命令
            # 修复：同名命令存在时，若不优先查技能，技能参数（--enable/--disable）
            # 会被命令参数覆盖，因为下方兜底逻辑 `if not cmd and entries` 会抢走 cmd
            skill = get_skill_by_name(cmd_name)
        elif use_type:
            type_map = {"command": CommandType.FUNCTION, "prompt": CommandType.PROMPT, "agent": CommandType.AGENT}
            preferred = type_map.get(use_type)
            if preferred and preferred in entries:
                cmd = entries[preferred]

        # 兜底：未通过类型匹配到，退而尝试任意命令或技能
        if not cmd and not skill:
            if entries:
                cmd = next(iter(entries.values()))
            else:
                skill = get_skill_by_name(cmd_name)

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
        # escape 后强制 RichText：纯文本安全显示（< > & " 等字符不被误解析为
        # HTML 标签/实体，避免描述含尖括号时渲染破坏与潜在注入）
        self._detail_desc_label.setTextFormat(Qt.RichText)
        self._detail_desc_label.setText(html.escape(desc))

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
        # detail 模式下隐藏悬浮描述气泡（detail 自带描述区，无需重复）
        if getattr(self, "_desc_tooltip_label", None) is not None:
            self._desc_tooltip_label.setVisible(False)
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
        self._card_target_height = total_height
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

        # 获取可选值列表（统一查表：动态源 → data_provider；否则 → 静态 value_options）
        options = _get_value_options(param_name, self._data_provider)

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

        # 构建值列表（条目兼容 str / {"value","description"} 两种格式）
        for entry in filtered:
            val, desc = _split_value_entry(entry)
            item = ValueItemWidget(val, desc)
            item.clicked.connect(self._on_value_clicked)
            item.hovered.connect(self._on_value_hovered)
            self._detail_value_layout.addWidget(item)
            self._value_widgets.append(item)

        # 切换显示
        self._value_selection_mode = True
        self._value_selection_param = param_name
        self._detail_params_scroll.setVisible(False)
        self._detail_value_scroll.setVisible(True)

        # 先重算高度再更新选中：_update_value_selection → _update_desc_tooltip
        # 会按当前卡片几何定位悬浮描述气泡，若高度未更新，气泡锚定在旧顶边，
        # 卡片随后变矮时气泡悬在旧位置（枚举描述悬浮窗位置不更新 bug）
        self._adjust_detail_height()

        # 选中第一项
        self._selected_value_index = 0 if self._value_widgets else -1
        self._update_value_selection()

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
        #    动态源（_DYNAMIC_VALUE_PROVIDERS）或静态 value_options 任一即可
        candidate_params = []
        for w in self._param_widgets:
            if w.param_type != "value":
                continue
            if w.param_name in _DYNAMIC_VALUE_PROVIDERS or w._param.value_options:
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

        # 3.5 选择完成回声抑制：程序化选择退出值选择模式后的首次防抖同步
        # 会带完整值再次命中同一参数；用户 100ms 内不可能有物理编辑，
        # 视为回声直接跳过，不再弹回枚举列表。参数不同（用户已切到别的参数）
        # 则清除标记正常放行。
        echo_param = self._value_just_selected_param
        if echo_param:
            self._value_just_selected_param = ""
            if target_widget.param_name == echo_param:
                return

        # 4. 切到值选择模式
        self._switch_to_value_selection(target_widget, query=query)

    def _cursor_past_param_value(self, text: str, token_end: int, cursor_pos: int) -> bool:
        """判断光标是否已越过参数值后的第一个空格（即离开此参数）

        用于决定是否退出或跳过值选择模式。
        注意：行尾空格（空格后无实质内容，用户刚打完值）**不算已离开**，
        否则手打 `/subagent --model=gpt ` 会因末尾空格被误判为离开，
        导致枚举列表永不弹出（与 Tab 选择路径行为不一致）。
        只有空格后还有其它内容且光标越过该空格时才判定为离开。

        参数值在末尾（无空格）或紧跟另一参数名时，
        cursor_pos 不会 > space_after，不会误判为"已离开"。
        """
        if cursor_pos < 0:
            return False
        space_after = text.find(" ", token_end)
        if space_after < 0:
            return False
        # 行尾空格（空格后只剩空白，用户刚打完值）：不算已离开，
        # 与 Tab 路径（_execute_param_selection → _switch_to_value_selection）对齐
        if not text[space_after + 1 :].strip():
            return False
        return cursor_pos > space_after

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
        """按子串过滤选项（不区分大小写）；空 query 返回全部

        条目兼容 str / {"value","description"} 两种格式（按 value 匹配）。
        """
        if not query:
            return list(options)
        q = query.lower()
        result = []
        for opt in options:
            val, _ = _split_value_entry(opt)
            if q in val.lower():
                result.append(opt)
        return result

    def _refresh_value_list(self, query: str):
        """在不重建模式状态的前提下，仅刷新值列表 widget（用于实时搜索）"""
        param_name = self._value_selection_param
        if not param_name:
            return

        # 重新取源 options（统一查表）
        options = _get_value_options(param_name, self._data_provider, self._param_widgets)

        filtered = self._filter_value_options(options, query)

        # 重建 widget
        for w in self._value_widgets:
            try:
                self._detail_value_layout.removeWidget(w)
                w.deleteLater()
            except RuntimeError:
                pass
        self._value_widgets.clear()

        for entry in filtered:
            val, desc = _split_value_entry(entry)
            item = ValueItemWidget(val, desc)
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
        # 回退到参数列表模式（标记为选择完成，抑制防抖回声重入）
        self._exit_value_selection(mark_selected=True)

    def _exit_value_selection(self, mark_selected: bool = False):
        """退出值选择模式，回到参数列表

        Args:
            mark_selected: True 表示本次退出源于用户完成枚举选择（Tab/Enter/点击），
                记录参数名用于抑制防抖回声重入；False 表示参数被删/光标离开等
                情境退出，不得打标（否则用户删值重新输入时列表会被误抑制）。
        """
        if mark_selected:
            # 选择（Tab/Enter/点击）会同步插入完整值+空格并触发 textChanged →
            # 100ms 防抖 → _sync_detail_params → _auto_switch_to_value_selection。
            # 此时行尾空格不算"已离开"，--load= 仍会命中并重新弹回值选择模式，
            # 枚举描述气泡随之重新显示在旧几何位置（悬在聊天区中间）。
            # 用户 100ms 内不可能有物理编辑，该次重入纯属程序回声，见
            # _auto_switch_to_value_selection 的回声抑制分支。
            self._value_just_selected_param = self._value_selection_param
        self._value_selection_mode = False
        self._value_selection_param = ""
        self._detail_value_scroll.setVisible(False)
        self._detail_params_scroll.setVisible(True)
        self._adjust_detail_height()
        # 清理残留 tooltip：值选择模式退出后 _value_selection_mode=False，
        # _update_desc_tooltip 走 detail（参数列表）分支自动隐藏气泡，
        # 避免刚选中枚举值的描述残留显示到下次切换/关闭。
        self._update_desc_tooltip()

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

        # 自动检测枚举参数：进入/刷新值选择模式（实时搜索）
        # 注意：此方法只匹配完整参数名 + =（如 --model=），不做前缀匹配。
        # 不依赖参数列表可见性：手输参数时，前缀过滤可能暂时隐藏列表项，
        # 但完整的 value 参数仍应立即触发枚举列表。
        if full_text:
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

        # 刷新最上方 tooltip：显示当前选中枚举值的描述（_update_desc_tooltip
        # 内部检测 _value_selection_mode 分支，仅值选择模式更新）
        self._update_desc_tooltip()

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
        self._value_just_selected_param = ""
        self._selected_param_index = -1
        self._selected_value_index = -1
        self._last_selected_value_index = -1
        self._detail_positional_hint.setVisible(False)
        self._detail_container.setVisible(False)
        self._detail_params_scroll.setVisible(False)
        self._detail_value_scroll.setVisible(False)
        self._scroll_area.setVisible(True)
        # 列表模式恢复：按当前滚动位置重新绑定可见槽（detail 期间 viewport 尺寸变化可能已解绑）
        self._sync_visible_slots()
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
        """渲染当前筛选结果（虚拟化：只创建可见窗口内的池 widget）

        Args:
            incremental: 是否增量更新（保留匹配项，复用 widget）

        虚拟化策略：无论匹配项多少，只维护 VIRTUAL_POOL_SIZE 个池 widget，
        按滚动位置绑定到可见槽（含上下缓冲），滚动时仅移动/显隐 widget，
        不随内容总数增长，彻底消除内容多时全量重建卡顿。

        快速路径：新旧 items 完全相同（name+type+subtype 一致）时跳过
        虚拟布局重建，仅刷新已绑定 widget 的 query 高亮。
        """
        new_items = self._filtered_items
        cur_keys = [(it["name"], it["type"], it.get("subtype", "")) for it in new_items]

        # ---- 快速路径：新旧 items 完全一致，仅更新已绑定 widget 高亮 ----
        if cur_keys == self._last_render_keys:
            for slot, w in list(self._slot_widgets.items()):
                if not isinstance(w, CommandItemWidget):
                    continue
                kind, item_idx, _y = self._virtual_slots[slot]
                if kind == "item":
                    try:
                        w.reuse(new_items[item_idx], self._current_text_query)
                    except RuntimeError:
                        continue
            # 快速路径仍需重新设置固定高度：
            # _reset_detail_mode() 会清除卡片的 minH/maxH 约束，
            # 如果不重新 setFixedHeight，layout 会算出很小的 natural_h
            # 导致容器无法展开到正常高度（见插件卡片关闭后命令卡片高度异常 bug）
            self._apply_list_height()
            # 快速路径 selected_index 不会变，仍需刷新 tooltip（描述可能因 query 高亮而变）
            if not self._detail_mode:
                self._update_desc_tooltip()
            return

        # ---- 全量路径：解绑全部池 widget，重建虚拟布局 ----
        for slot, w in list(self._slot_widgets.items()):
            if isinstance(w, CommandItemWidget):
                self._free_item_widgets.append(w)
            else:
                self._divider_pool.append(w)
            w.hide()
        self._slot_widgets.clear()

        self._build_virtual_layout()
        self._sync_visible_slots()
        self._last_render_keys = cur_keys

        # 计算卡片高度（列表部分 + 顶部描述 tooltip）
        self._apply_list_height()
        # 重新渲染后 selected_index 可能已被重置为 0，更新 tooltip
        if not self._detail_mode:
            self._update_desc_tooltip()

    # ── 虚拟化渲染辅助 ──

    def _build_virtual_layout(self):
        """构建虚拟布局：items + 分隔线 → 槽列表（kind, item_idx, y）

        分隔线算作独立槽（1px 高），与 item 槽统一按 y 偏移排列，
        供 _sync_visible_slots 按滚动位置二分定位可见范围。
        """
        items = self._filtered_items

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
        for i in range(1, len(items)):
            if _section(items[i]) != _section(items[i - 1]):
                divider_positions.append(i)

        slots: List[tuple] = []
        ys: List[int] = []
        y = 0
        next_div_idx = 0
        for i in range(len(items)):
            if next_div_idx < len(divider_positions) and i == divider_positions[next_div_idx]:
                slots.append(("divider", None, y))
                ys.append(y)
                y += 1
                next_div_idx += 1
            slots.append(("item", i, y))
            ys.append(y)
            y += ITEM_HEIGHT

        self._virtual_slots = slots
        self._virtual_slot_ys = ys
        self._virtual_total_height = y
        self._divider_count = len(divider_positions)
        # 内容高度即虚拟总高度（QScrollArea 负责滚动）；空列表置 1 防 0 高度异常
        self._scroll_content.setFixedHeight(max(1, y))

    def _sync_visible_slots(self):
        """按当前滚动位置绑定/解绑可见窗口（含缓冲）内的池 widget

        绑定槽的 item 数据在虚拟布局构建时已固定，滚动只改变可见范围，
        因此此处仅做 widget 的移动/显隐，不更新文本，开销极小。
        """
        slots = self._virtual_slots
        if not slots:
            return
        sb = self._scroll_area.verticalScrollBar()
        view_h = max(1, self._scroll_area.viewport().height())
        y0 = sb.value()
        y1 = y0 + view_h
        ys = self._virtual_slot_ys
        # 二分定位可见槽范围，上下各留缓冲（滚动提前量）
        first = bisect.bisect_left(ys, y0) - VIRTUAL_BUFFER_SLOTS
        last = bisect.bisect_right(ys, y1) + VIRTUAL_BUFFER_SLOTS - 1
        first = max(0, first)
        last = min(len(slots) - 1, last)

        # 宽度同步：content 宽度变化时全量更新已绑定 widget 宽度。
        # 首次渲染发生在卡片布局完成前（viewport 高度 0 → 只绑定顶部几槽），
        # 此时 content 宽度尚未确定，绑定的 widget 会被 setFixedWidth(0) 挤压
        # （内部标签全部挤到左边，表现为 tag 显示在左侧）；布局完成后 content
        # 变宽，若不同步则已绑定 widget 保持错误宽度直到滚动重绑。
        content_w = self._scroll_content.width()
        if content_w != self._bound_content_width:
            self._bound_content_width = content_w
            for w in list(self._slot_widgets.values()):
                try:
                    w.setFixedWidth(content_w)
                except RuntimeError:
                    continue

        # 解绑离开可见范围的槽（widget 回池 + 隐藏）
        for slot in list(self._slot_widgets):
            if slot < first or slot > last:
                w = self._slot_widgets.pop(slot)
                if isinstance(w, CommandItemWidget):
                    self._free_item_widgets.append(w)
                else:
                    self._divider_pool.append(w)
                w.hide()

        # 绑定新进入可见范围的槽（content_w 已在上方宽度同步处取得）
        for slot in range(first, last + 1):
            if slot in self._slot_widgets:
                continue
            kind, item_idx, y = slots[slot]
            if kind == "divider":
                if self._divider_pool:
                    div = self._divider_pool.pop()
                else:
                    div = QFrame(self._scroll_content)
                    div.setFrameShape(QFrame.HLine)
                    div.setFixedHeight(1)
                    div.setStyleSheet(f"background: {Colors.DIVIDER_COLOR}; border: none;")
                    div.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                div._virtual_slot = slot
                div.move(0, y)
                div.setFixedWidth(content_w)
                self._slot_widgets[slot] = div
                div.show()
            else:
                if self._free_item_widgets:
                    w = self._free_item_widgets.pop()
                else:
                    # 池耗尽（理论上缓冲保证不会）：新建并记录到 _item_pool
                    # （调试/统计用，正常路径下数量恒 ≤ VIRTUAL_POOL_SIZE）
                    w = CommandItemWidget(self._dummy_item, self._current_text_query, self._scroll_content)
                    w.clicked.connect(self._on_item_clicked)
                    w.hovered.connect(self._on_item_hovered)
                    self._item_pool.append(w)
                w._virtual_slot = slot
                w.move(0, y)
                w.setFixedWidth(content_w)
                self._slot_widgets[slot] = w
                try:
                    w.reuse(self._filtered_items[item_idx], self._current_text_query)
                except RuntimeError:
                    continue
                w.show()

    def _scroll_to_item(self, item_idx: int):
        """滚动列表使指定 item 最小可见（对齐 ensureWidgetVisible(0,0) 语义）

        完全可见则不滚动；否则滚动最小量让目标 item 进入视口。
        """
        if item_idx < 0 or item_idx >= len(self._filtered_items):
            return
        target_y = None
        for kind, idx, y in self._virtual_slots:
            if kind == "item" and idx == item_idx:
                target_y = y
                break
        if target_y is None:
            return
        sb = self._scroll_area.verticalScrollBar()
        cur = sb.value()
        view_h = max(1, self._scroll_area.viewport().height())
        if target_y < cur:
            sb.setValue(target_y)
        elif target_y + ITEM_HEIGHT > cur + view_h:
            sb.setValue(target_y + ITEM_HEIGHT - view_h)

    def _on_item_clicked(self):
        """item 被鼠标点击"""
        sender = self.sender()
        item_idx = self._slot_to_item_index(sender)
        if item_idx is not None:
            self._selected_index = item_idx
            self._update_selection()
            self.select_current()

    def _slot_to_item_index(self, widget) -> Optional[int]:
        """从池 widget 反查其绑定的 filtered_items 索引（虚拟化映射）"""
        slot = getattr(widget, "_virtual_slot", -1)
        if 0 <= slot < len(self._virtual_slots):
            kind, item_idx, _y = self._virtual_slots[slot]
            if kind == "item":
                return item_idx
        return None

    def _on_item_hovered(self, widget):
        """鼠标悬停到 item → 同步选中索引

        实现 hover 即选中：鼠标悬停到哪个 item，键盘导航的起始位置就跟随到哪。
        tooltip 也会自动跟随（_update_selection 内部调用 _update_desc_tooltip）。
        """
        if self._detail_mode:
            return  # detail 模式不处理列表 hover
        item_idx = self._slot_to_item_index(widget)
        if item_idx is None:
            return
        # 索引相同则跳过，避免不必要的重绘
        if item_idx == self._selected_index:
            return
        self._selected_index = item_idx
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
        """更新选中高亮，并记录当前选中项类型供 detail 模式使用

        虚拟化下池 widget 与槽绑定：遍历已绑定槽，按槽对应的 item 索引
        统一设置选中状态（选中项仅一个，其余全部取消，幂等安全）。
        """
        old_idx = self._last_selected_index
        new_idx = self._selected_index

        # 遍历已绑定的 item 槽：命中选中索引的激活，其余取消
        for slot, w in list(self._slot_widgets.items()):
            if not isinstance(w, CommandItemWidget):
                continue
            try:
                _ = w.isVisible()
            except RuntimeError:
                continue
            kind, item_idx, _y = self._virtual_slots[slot]
            if kind == "item":
                w.set_selected(item_idx == new_idx)

        self._last_selected_index = new_idx

        # 记录当前选中项的 display_type（用于 detail 模式显示/执行）
        if 0 <= self._selected_index < len(self._filtered_items):
            self._current_selected_type = self._filtered_items[self._selected_index].get("type", "")
        else:
            self._current_selected_type = ""

        # 刷新顶部描述 tooltip（仅列表模式；detail 模式自带描述区）
        if not self._detail_mode:
            self._update_desc_tooltip()

        # 滚动到可见区域（虚拟化：按 item 的 y 偏移滚动，等效 ensureWidgetVisible）
        if old_idx != new_idx:
            self._scroll_to_item(new_idx)

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
        if self._filtered_items and self._selected_index < len(self._filtered_items) - 1:
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
        if self._filtered_items and self._selected_index > 0:
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
                    self._exit_value_selection(mark_selected=True)
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
        # 关闭时隐藏并清空悬浮气泡文本，避免下次 show_card 闪现旧描述
        if getattr(self, "_desc_tooltip_label", None) is not None:
            self._desc_tooltip_label.setVisible(False)
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
        # 延迟一帧到 layout 完成后刷新悬浮气泡位置，解决首次显示时
        # mapToGlobal 尚未反映卡片最终布局位置的问题
        QTimer.singleShot(0, self._sync_desc_tooltip_position)

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
        # 卡片可见：先标记缓存脏，确保 _refresh_data 跳过缓存重建
        self._cache_dirty = True
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
        # 热重载后确保卡片可见：load_items 不会自动调用 setVisible，
        # 若卡片因外部原因（如其他卡片互斥切换）被隐藏但 _visible 仍为 True，
        # 新数据渲染后需显式恢复可见性。
        if len(self._filtered_items) > 0:
            self.setVisible(True)

    def _refresh_detail_view(self):
        """重建 detail 模式的参数视图（命令元数据变更后调用）

        与 show_command_detail 不同：本方法强制重新渲染（即使 cmd_name 未变），
        以反映热重载后的命令/参数描述变更。
        保留 _current_query 和输入框上下文。
        """
        cmd_name = self._detail_cmd_name
        if not cmd_name:
            return
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
