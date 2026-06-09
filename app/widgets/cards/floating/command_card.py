# -*- coding: utf-8 -*-
"""
斜杠命令卡片 - 输入框上方展开，显示命令和技能列表

触发方式：在输入框输入 / 后，卡片自动展开
数据来源：CommandManager 内置命令 + get_local_skills()
交互方式：↑/↓ 导航，Enter 选中，Esc 关闭
"""
from typing import List, Dict

import html
from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QMouseEvent
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QScrollArea, QFrame, QSizePolicy,
)

from app.utils.utils import get_font_family_css, get_local_skills, get_skill_by_name
from app.utils.design_tokens import Colors, font_size_css
from app.core.command_manager import CommandManager, CommandType, CommandParameter
from app.widgets.elided_label import _ElidedLabel




ITEM_HEIGHT = 36       # 每个 item 高度
MAX_VISIBLE_ITEMS = 8  # 最多同时显示 item 数


class CommandItemWidget(QWidget):
    """命令/技能列表单项"""

    clicked = pyqtSignal()

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

        # 类型标签（技能显示【技能】，智能体显示【智能体】，提示词显示【提示词】）
        item_type = self._data["type"]
        if item_type == "skill":
            self._tag_label = QLabel("【技能】")
            self._tag_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._tag_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
            layout.addWidget(self._tag_label)
        elif item_type == "agent":
            self._tag_label = QLabel("【智能体】")
            self._tag_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._tag_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
            layout.addWidget(self._tag_label)
        elif item_type == "prompt":
            self._tag_label = QLabel("【提示词】")
            self._tag_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._tag_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
            layout.addWidget(self._tag_label)

        # 快捷键文本（仅 command 类型且有快捷键时显示）
        shortcut = self._data.get("shortcut", "")
        if item_type == "command" and shortcut:
            self._shortcut_label.setText(shortcut)
            self._shortcut_label.setVisible(True)
        else:
            self._shortcut_label.setVisible(False)

        # 设置快捷键静态样式（只在创建时设置一次，避免每次导航都触发 setStyleSheet）
        # 注意：_name_label 和 _desc_label 的样式在 _apply_style 中动态更新
        self._shortcut_label.setStyleSheet(f"""
            QLabel {{
                color: {Colors.TEXT_MUTED};
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

        # 标签样式：技能蓝色，智能体紫色，提示词橙色
        item_type = self._data["type"]
        if item_type == "skill":
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

        # 快捷键标签样式：类键盘键帽风格，加粗
        shortcut = self._data.get("shortcut", "")
        if item_type == "command" and shortcut:
            shortcut_fg = Colors.TEXT_MUTED
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
        for or_term in query.split('|'):
            or_term = or_term.strip()
            if not or_term:
                continue
            for and_part in or_term.split('&'):
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
                            f'{safe_text[start:end]}</span>'
                        )
                        pos = end
                    if pos < len(safe_text):
                        parts.append(safe_text[pos:])
                    self._name_label.setText(''.join(parts))
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

    def set_selected(self, selected: bool):
        """设置选中状态"""
        self._selected = selected
        if selected:
            self._hovered = False
        self._apply_style()

    def enterEvent(self, event):
        self._hovered = True
        if not self._selected:
            self._apply_style()
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

    def __init__(self, param: CommandParameter, parent=None):
        super().__init__(parent)
        self._param = param
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

        # 必填/选填标签（放在最前面）
        if self._param.param_type != "positional":
            req_tag = QLabel("必填" if self._param.required else "可选")
            req_tag.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            req_color = Colors.TEXT_ACCENT if self._param.required else Colors.TEXT_MUTED
            req_tag.setStyleSheet(f"""
                color: {req_color};
                background: rgba(128,128,128,0.06);
                border-radius: 3px;
                padding: 1px 6px;
                font-weight: bold;
            """)
            layout.addWidget(req_tag)

        # 参数名（加粗）
        self._name_label = QLabel(self._param.name)
        self._name_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._name_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        self._name_label.setStyleSheet(f"color: {Colors.SEND_BTN_START}; background: transparent; font-weight: bold;")
        layout.addWidget(self._name_label)

        # 参数说明
        if self._param.description:
            desc_label = QLabel(self._param.description)
            desc_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            desc_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            desc_label.setMinimumWidth(0)
            desc_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; background: transparent;")
            layout.addWidget(desc_label, 1)

        # 类型标签
        type_map = {"flag": "标志", "value": "值", "positional": "参数"}
        type_tag = QLabel(type_map.get(self._param.param_type, ""))
        type_tag.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        type_tag.setStyleSheet(f"""
            color: {Colors.TEXT_MUTED};
            background: rgba(128,128,128,0.1);
            border-radius: 3px;
            padding: 1px 6px;
        """)
        layout.addWidget(type_tag)



    @property
    def param_name(self) -> str:
        return self._param.name

    @property
    def param_type(self) -> str:
        return self._param.param_type

    def set_selected(self, selected: bool):
        self._selected = selected
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
        if not self._selected:
            self._apply_style()
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


class CommandCard(QWidget):
    """斜杠命令卡片"""

    commandSelected = pyqtSignal(str, str)  # name, display_type（"command"/"prompt"/"agent"/"skill"/""）
    dismissed = pyqtSignal()                # 卡片被关闭
    parameterSelected = pyqtSignal(str, str)  # param_name, param_type — 参数项被点击
    parameterValueSelected = pyqtSignal(str)  # value — --model= 的值被选中

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_items: List[Dict[str, str]] = []
        self._all_items_cache: List[Dict[str, str]] = []  # 缓存，避免每次敲击都读磁盘
        self._cache_dirty: bool = True                     # 缓存脏标记，热重载后置 True
        self._filtered_items: List[Dict[str, str]] = []
        self._selected_index = 0
        self._last_selected_index = -1  # 上次选中索引，用于增量更新
        self._item_widgets: List[CommandItemWidget] = []
        self._divider = None  # 缓存分隔线 QFrame，避免积累
        self._visible = False
        self._current_query = ""
        self._last_query = ""  # 上次过滤的 query，用于增量剪枝
        self._current_selected_type: str = ""  # 当前选中项的 display_type（用于 detail 模式）

        # Detail mode：匹配到完整命令 + 空格后显示参数提示
        self._detail_mode = False
        self._detail_cmd_name = ""
        self._detail_selected_type: str = ""  # detail 模式下的选中类型
        self._detail_has_params: bool = False  # 当前命令是否有可交互参数列表
        self._param_widgets: List["ParameterItemWidget"] = []  # 参数列表项
        self._selected_param_index: int = -1   # 参数列表选中索引
        self._value_selection_mode: bool = False  # 是否处于值选择模式
        self._value_selection_param: str = ""     # 值选择对应的参数名（如 "--model="）
        self._value_widgets: List[QWidget] = []   # 值选择列表项
        self._selected_value_index: int = -1      # 值列表选中索引
        self._last_selected_value_index: int = -1  # 上次值列表选中索引，用于增量更新
        self._data_provider: dict = {}            # 外部数据源（如 model_options）
        self.setVisible(False)
        self._setup_ui()
        self._setup_detail_widget()

    def _setup_ui(self):
        # 自身填充父容器宽度
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # 自身样式：使用系统实时卡片背景色，底部直角与输入框融合
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 滚动区域
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QScrollArea.NoFrame)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll_area.setStyleSheet(f"""
            QScrollArea, QScrollArea * {{
                background: transparent;
                border: none;
                padding: 0;
                margin: 0;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 12px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {Colors.SCROLLBAR_HANDLE_BG};
                border-radius: 6px;
                min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {Colors.SCROLLBAR_HANDLE_HOVER_BG};
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0px;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: none;
            }}
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

    def _setup_detail_widget(self):
        """构建 detail 模式下的交互式参数 UI"""
        detail_layout = QVBoxLayout(self._detail_container)
        detail_layout.setContentsMargins(12, 1, 12, 2)
        detail_layout.setSpacing(2)

        # 第一行：命令说明（始终显示）
        self._detail_desc_label = QLabel()
        self._detail_desc_label.setStyleSheet(f"""
            QLabel {{ color: {Colors.TEXT_PRIMARY}; {get_font_family_css()} {font_size_css(12)}; background: transparent; margin: 0; padding: 0; }}
        """)
        self._detail_desc_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._detail_desc_label.setWordWrap(True)
        detail_layout.addWidget(self._detail_desc_label)

        # 参数列表滚动区（有 parameters 时显示）
        self._detail_params_scroll = QScrollArea()
        self._detail_params_scroll.setWidgetResizable(True)
        self._detail_params_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._detail_params_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._detail_params_scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ background: transparent; width: 4px; margin: 0; }}
            QScrollBar::handle:vertical {{ background: {Colors.SCROLLBAR_HANDLE_BG}; border-radius: 2px; min-height: 20px; }}
            QScrollBar::handle:vertical:hover {{ background: {Colors.SCROLLBAR_HANDLE_HOVER_BG}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """)
        self._detail_params_scroll.viewport().setStyleSheet("background: transparent; border: none;")
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
        self._detail_value_scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{ background: transparent; width: 4px; margin: 0; }}
            QScrollBar::handle:vertical {{ background: {Colors.SCROLLBAR_HANDLE_BG}; border-radius: 2px; min-height: 20px; }}
            QScrollBar::handle:vertical:hover {{ background: {Colors.SCROLLBAR_HANDLE_HOVER_BG}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """)
        self._detail_value_scroll.viewport().setStyleSheet("background: transparent; border: none;")
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
        self._detail_hint_label.setStyleSheet(f"""
            QLabel {{ color: {Colors.SEND_BTN_START}; {get_font_family_css()} {font_size_css(12)}; background: transparent; margin: 0; padding: 0; }}
        """)
        self._detail_hint_label.setWordWrap(True)
        detail_layout.addWidget(self._detail_hint_label)

        # 点击整块等同于选中当前命令并发送
        self._detail_container.setCursor(Qt.PointingHandCursor)
        self._detail_container.mousePressEvent = self._on_detail_clicked

    # ---- Detail 模式 ----

    @property
    def is_detail_mode(self) -> bool:
        """是否处于 detail 模式（显示参数提示）"""
        return self._detail_mode

    @property
    def detail_cmd_name(self) -> str:
        """detail 模式匹配的命令名"""
        return self._detail_cmd_name

    def show_command_detail(self, cmd_name: str, selected_type: str = "",
                            data_provider: dict = None):
        """切换到 detail 模式：显示指定命令/技能的参数提示

        Args:
            cmd_name: 已匹配的命令名或技能名
            selected_type: 选中项的 display_type（"command"/"prompt"/"agent"）
                          为空时使用当前选中项类型（通过 _current_selected_type）
            data_provider: 外部数据源，如 {"model_options": ["OpenAI:gpt-4o", ...]}
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

        # 决定显示交互参数列表 或 回退静态 hint
        has_params = bool(cmd and cmd.parameters)
        self._detail_has_params = has_params

        if has_params:
            # 交互式参数列表
            self._detail_hint_label.setVisible(False)
            self._detail_value_scroll.setVisible(False)
            self._build_param_widgets(cmd.parameters)
            self._detail_params_scroll.setVisible(True)
            # 初始选中第一项
            self._selected_param_index = 0 if self._param_widgets else -1
            self._update_param_selection()
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
        self._visible = True
        self.setVisible(True)

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

        # 计算描述文本高度
        fm = self._detail_desc_label.fontMetrics()
        line_height = fm.lineSpacing()
        desc_text = self._detail_desc_label.text()
        if desc_text.strip():
            label_width = self._detail_desc_label.width() or 1
            if label_width <= 0:
                label_width = self.width() - 24
            text_width = fm.horizontalAdvance(desc_text)
            line_count = max(1, (text_width + label_width - 1) // label_width)
            desc_height = line_height * line_count
        else:
            desc_height = line_height

        # 计算参数列表/值列表/提示文本高度
        if self._detail_has_params and not self._value_selection_mode:
            # 交互参数列表高度
            param_count = len(self._param_widgets)
            visible_params = sum(1 for w in self._param_widgets if w.isVisible())
            content_height = visible_params * ITEM_HEIGHT
            self._detail_params_scroll.setFixedHeight(min(content_height, 7 * ITEM_HEIGHT))
            content_height = min(content_height, 7 * ITEM_HEIGHT)
            hint_height = 0
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
                hint_width = fm_hint.horizontalAdvance(hint_text)
                label_width = self._detail_hint_label.width() or 1
                if label_width <= 0:
                    label_width = self.width() - 24
                hint_line_count = max(1, (hint_width + label_width - 1) // label_width)
                hint_height = hint_line_height * hint_line_count
                self._detail_hint_label.setVisible(True)
            else:
                hint_height = 0
            content_height = 0

        total_height = v_margin + desc_height + spacing + hint_height + content_height
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
            self._detail_params_layout.addWidget(w)
            self._param_widgets.append(w)

    def _on_param_clicked(self):
        """参数项被点击"""
        sender = self.sender()
        if sender in self._param_widgets:
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
            item = QLabel(val)
            item.setFixedHeight(ITEM_HEIGHT)
            item.setCursor(Qt.PointingHandCursor)
            item.setStyleSheet(f"""
                QLabel {{
                    color: {Colors.TEXT_PRIMARY}; background: transparent;
                    padding: 0 12px; {get_font_family_css()} {font_size_css(12)};
                }}
            """)
            # 用 lambda 捕获值
            item.mousePressEvent = lambda e, v=val: self._on_value_clicked(v)
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
        """检测 --model 前缀输入，自动进入/刷新值选择模式

        行为：
        - 用户在 detail 命令范围内输入 `--m`/`--mo`/`--mod`/.../`--model=` 时自动弹出模型列表
        - 已在值选择模式时，根据光标前内容实时过滤
        - cursor_pos=-1 时按"到下一个空格/末尾"取搜索关键字
        """
        import re

        # 1. 找 --model 前缀位置（--m, --mo, --model, --model= 都匹配）
        #    使用 lookahead 限制最长匹配到 = 之前/或 整个 token
        match = re.search(r'--model[a-z-]*', text)
        if not match:
            return

        token_start = match.start()
        token_end = match.end()  # 包含 = 时指向 = 之后

        # 2. 限定在 detail 命令范围内（避免误识别）
        #    detail 模式的输入形如 "/agent --model=xxx"
        cmd_prefix_len = len(self._detail_cmd_name) + 2  # "/<cmd_name> "
        if token_start < cmd_prefix_len:
            return

        # 3. 找到对应的参数 widget（必须在 _param_widgets 中且仍可见）
        target_widget = None
        for w in self._param_widgets:
            if w.param_name == "--model=" and w.param_type == "value":
                if w.isVisible():
                    target_widget = w
                break
        if target_widget is None:
            return  # 参数已激活（被显隐为不可见），不做自动触发

        # 4. 提取搜索 query：match 之后到光标/下一个空格的内容
        query = self._extract_model_query(text, token_end, cursor_pos)

        # 5. 已在值选择模式：仅刷新过滤
        if self._value_selection_mode and self._value_selection_param == "--model=":
            self._refresh_value_list(query)
            return

        # 6. 否则切到值选择模式
        self._switch_to_value_selection(target_widget, query=query)

    def _extract_model_query(self, text: str, after_token_end: int, cursor_pos: int) -> str:
        """提取 --model= 之后到光标前/下一个空格前的子串作为搜索关键字"""
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
            item = QLabel(val)
            item.setFixedHeight(ITEM_HEIGHT)
            item.setCursor(Qt.PointingHandCursor)
            item.setStyleSheet(f"""
                QLabel {{
                    color: {Colors.TEXT_PRIMARY}; background: transparent;
                    padding: 0 12px; {get_font_family_css()} {font_size_css(12)};
                }}
            """)
            item.mousePressEvent = lambda e, v=val: self._on_value_clicked(v)
            self._detail_value_layout.addWidget(item)
            self._value_widgets.append(item)

        # 保持选中索引在有效范围
        if self._value_widgets:
            self._selected_value_index = min(
                max(self._selected_value_index, 0), len(self._value_widgets) - 1
            )
        else:
            self._selected_value_index = -1
        self._update_value_selection()
        self._adjust_detail_height()

    def _on_value_clicked(self, value: str):
        """值选择项被点击"""
        self.parameterValueSelected.emit(value)
        # 回退到参数列表模式
        self._exit_value_selection()

    def _exit_value_selection(self):
        """退出值选择模式，回到参数列表"""
        self._value_selection_mode = False
        self._value_selection_param = ""
        self._detail_value_scroll.setVisible(False)
        self._detail_params_scroll.setVisible(True)
        self._adjust_detail_height()

    def update_active_params(self, active: set, full_text: str = "", cursor_pos: int = -1):
        """根据输入中已存在的参数名列表，显隐参数项

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

        # 值选择模式：检查对应的参数是否还在输入中
        if self._value_selection_mode and self._value_selection_param:
            param_clean = self._value_selection_param.rstrip("=")
            # value 参数必须有 = 才算激活（防止 --xxx 裸名也被算作激活）
            still_active = any(
                "=" in a and a.rstrip("=") == param_clean
                for a in active
            )
            if not still_active:
                # 参数已被删掉 → 退出值选择模式，回到参数列表
                self._exit_value_selection()

        any_visible = False
        for w in self._param_widgets:
            param_key = w.param_name
            param_clean = param_key.rstrip("=")
            if w.param_type == "value" and param_key.endswith("="):
                is_active = any("=" in a and a.rstrip("=") == param_clean for a in active)
            else:
                is_active = any(a.rstrip("=") == param_clean for a in active)
            w.setVisible(not is_active)
            if w.isVisible():
                any_visible = True

        # 显示/隐藏参数滚动区
        self._detail_params_scroll.setVisible(any_visible)
        if any_visible:
            self._detail_params_content.setVisible(True)

        # 自动检测 --model 前缀：进入/刷新值选择模式（实时搜索）
        if full_text and any_visible:
            self._auto_switch_to_value_selection(full_text, cursor_pos)

        # 重算高度
        self._adjust_detail_height()
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
            self._detail_params_scroll.ensureWidgetVisible(
                self._param_widgets[self._selected_param_index], 0, 0
            )

    def _update_value_selection(self):
        """更新值列表选中高亮，滚动到可见"""
        # Colors.refresh() 不在导航路径中调用——颜色在 show_card 时刷新一次即可
        old_idx = self._last_selected_value_index if hasattr(self, '_last_selected_value_index') else -1
        new_idx = self._selected_value_index
        self._last_selected_value_index = new_idx

        # 只更新变化的项
        if old_idx != new_idx:
            if 0 <= old_idx < len(self._value_widgets):
                self._value_widgets[old_idx].setStyleSheet(f"""
                    QLabel {{ color: {Colors.TEXT_PRIMARY}; background: transparent;
                             padding: 0 12px; {get_font_family_css()} {font_size_css(12)}; }}
                """)
            if 0 <= new_idx < len(self._value_widgets):
                self._value_widgets[new_idx].setStyleSheet(f"""
                    QLabel {{ color: {Colors.TEXT_PRIMARY}; background: {Colors.REALTIME_TAG_BG};
                             padding: 0 12px; {get_font_family_css()} {font_size_css(12)}; }}
                """)
        elif 0 <= new_idx < len(self._value_widgets):
            self._value_widgets[new_idx].setStyleSheet(f"""
                QLabel {{ color: {Colors.TEXT_PRIMARY}; background: {Colors.REALTIME_TAG_BG};
                         padding: 0 12px; {get_font_family_css()} {font_size_css(12)}; }}
            """)

        # 滚动到可见
        if 0 <= self._selected_value_index < len(self._value_widgets):
            self._detail_value_scroll.ensureWidgetVisible(
                self._value_widgets[self._selected_value_index], 0, 0
            )

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
        self._detail_container.setVisible(False)
        self._detail_params_scroll.setVisible(False)
        self._detail_value_scroll.setVisible(False)
        self._scroll_area.setVisible(True)
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
        skills = [
            {"name": s["name"], "description": s.get("description", ""), "type": "skill"}
            for s in get_local_skills()
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
        text = (
            item["name"] + " "
            + item.get("display_name", item["name"]) + " "
            + item["description"]
        ).lower()

        for or_term in query.split('|'):
            or_term = or_term.strip()
            if not or_term:
                continue
            # 过滤空 AND 部分：让 "find&" 等价于 "find"（用户还在打字中）
            and_parts = [p.strip() for p in or_term.split('&') if p.strip()]
            if not and_parts:
                continue
            if all(part in text for part in and_parts):
                return True
        return False

    @staticmethod
    def _parse_type_filter(query: str):
        """从 query 中提取 type:xxx 或 #xxx 过滤器

        例如：
          "type:skill tdd"      → ({"skill"}, "tdd")
          "#agent"               → ({"agent"}, "")
          "#cmd find"            → ({"command"}, "find")
          "type:skill|type:agent" → ({"skill","agent"}, "")
          "find"                 → (None, "find")

        支持简写：cmd→command
        支持 OR：type:skill|type:agent → {"skill","agent"}
        """
        if not query:
            return None, query

        type_set = set()
        clean_tokens = []
        type_map = {'cmd': 'command', 'skill': 'skill', 'agent': 'agent', 'prompt': 'prompt'}

        for token in query.split():
            if token.startswith('type:'):
                tf = token[5:].strip()
                for t in tf.split('|'):
                    t = t.strip()
                    if t in type_map:
                        type_set.add(type_map[t])
            elif token.startswith('#') and token[1:] in type_map:
                # #skill, #agent, #prompt, #cmd 简写
                type_set.add(type_map[token[1:]])
            else:
                clean_tokens.append(token)

        return type_set if type_set else None, ' '.join(clean_tokens)

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
                self._filtered_items = [item for item in self._all_items if item["type"] in type_filter]
            else:
                self._filtered_items = list(self._all_items)
            self._last_query = ""
        else:
            # 增量剪枝：仅当新 query 是上次的扩展且不含 |
            can_prune = (
                self._last_query
                and query.startswith(self._last_query)
                and '|' not in query
            )
            source = self._filtered_items if can_prune else self._all_items
            self._filtered_items = [
                item for item in source
                if self._matches_multi(item, text_query)
            ]
            # 文本匹配后再按类别过滤
            if type_filter:
                self._filtered_items = [item for item in self._filtered_items if item["type"] in type_filter]

        # 排序：命令/技能在前，智能体在后，同类型按名称
        sort_order = {"command": 0, "skill": 1, "agent": 2}
        self._filtered_items.sort(key=lambda x: (sort_order.get(x["type"], 99), x["name"]))

        self._last_query = query

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
        """
        new_items = self._filtered_items
        old_widgets = list(self._item_widgets)  # 复制一份

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

        # 检查是否需要分隔线（命令/技能 与 智能体/提示词之间）
        has_commands_or_skills = any(item["type"] in ("command", "skill") for item in new_items)
        has_agents_or_prompts = any(item["type"] in ("agent", "prompt") for item in new_items)
        insert_divider = has_commands_or_skills and has_agents_or_prompts

        # 增量模式：需要分隔线但还没有时，退化到全量（简化逻辑）
        if incremental and insert_divider and self._divider is None:
            self._render(incremental=False)
            return

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
                # 更新 widget 数据和显示（热重载后 shortcut 等字段可能变化）
                w._data = item
                w._query = self._current_query
                w._update_display()
                # 刷新快捷键标签
                shortcut = item.get("shortcut", "")
                if item["type"] == "command" and shortcut:
                    w._shortcut_label.setText(shortcut)
                    w._shortcut_label.setVisible(True)
                else:
                    w._shortcut_label.setVisible(False)
                new_widgets.append(w)
            else:
                # 创建新 widget
                w = CommandItemWidget(item, self._current_query, self._scroll_content)
                w.clicked.connect(self._on_item_clicked)
                new_widgets.append(w)

        self._item_widgets = new_widgets

        # 删除不再需要的旧 widget（未被复用的）
        for w in old_by_key_copy.values():
            try:
                self._scroll_layout.removeWidget(w)
                w.deleteLater()
            except RuntimeError:
                continue

        # 处理分隔线（仅非增量模式）
        if not incremental:
            if self._divider is not None:
                try:
                    self._scroll_layout.removeWidget(self._divider)
                    self._divider.deleteLater()
                except RuntimeError:
                    pass
                self._divider = None

        # 清空 layout，重新按正确顺序添加 widget
        while self._scroll_layout.count():
            child = self._scroll_layout.takeAt(0)
            if child.widget():
                pass  # 仅移除，不删除（widget 在 _item_widgets 中）

        # 添加 widget，按顺序
        divider_inserted = False
        for i, widget in enumerate(self._item_widgets):
            # 在第一个智能体或提示词前插入分隔线（非增量模式）
            if not incremental and insert_divider and not divider_inserted:
                item = new_items[i]
                if i > 0 and item["type"] in ("agent", "prompt") and new_items[i - 1]["type"] in ("command", "skill"):
                    divider = QFrame()
                    divider.setFrameShape(QFrame.HLine)
                    divider.setFixedHeight(1)
                    divider.setStyleSheet(f"background: {Colors.DIVIDER_COLOR}; border: none;")
                    divider.setAttribute(Qt.WA_TransparentForMouseEvents, True)
                    self._scroll_layout.addWidget(divider)
                    self._divider = divider
                    divider_inserted = True
            self._scroll_layout.addWidget(widget)

        # 非增量模式下添加分隔线
        if not incremental and insert_divider and self._divider is None:
            divider = QFrame()
            divider.setFrameShape(QFrame.HLine)
            divider.setFixedHeight(1)
            divider.setStyleSheet(f"background: {Colors.DIVIDER_COLOR}; border: none;")
            divider.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            self._scroll_layout.addWidget(divider)
            self._divider = divider

        # 计算卡片高度
        item_count = len(new_items)
        divider_count = 1 if (divider_inserted and not incremental) else 0
        total_items = item_count + divider_count

        if total_items == 0:
            self.setFixedHeight(0)
        else:
            visible = min(total_items, MAX_VISIBLE_ITEMS)
            height = visible * ITEM_HEIGHT + divider_count * 1
            self.setFixedHeight(height)

    def _on_item_clicked(self):
        """item 被鼠标点击"""
        sender = self.sender()
        if sender in self._item_widgets:
            idx = self._item_widgets.index(sender)
            self._selected_index = idx
            self._update_selection()
            self.select_current()

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

        # 滚动到可见区域
        if 0 <= self._selected_index < len(self._item_widgets):
            self._scroll_area.ensureWidgetVisible(
                self._item_widgets[self._selected_index], 0, 0
            )

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
            if not visible:
                return True
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
            if not visible:
                return True
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
                text = widget.text() if hasattr(widget, 'text') else ""
                if text:
                    self.parameterValueSelected.emit(text)
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

    @property
    def is_card_visible(self) -> bool:
        return self._visible

    @property
    def filtered_count(self) -> int:
        return len(self._filtered_items)
