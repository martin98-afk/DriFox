# -*- coding: utf-8 -*-
"""
斜杠命令卡片 - 输入框上方展开，显示命令和技能列表

触发方式：在输入框输入 / 后，卡片自动展开
数据来源：CommandManager 内置命令 + get_local_skills()
交互方式：↑/↓ 导航，Enter 选中，Esc 关闭
"""
from typing import List, Dict

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QMouseEvent, QFontMetrics
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QScrollArea, QFrame, QSizePolicy,
)

from app.utils.utils import get_font_family_css, get_local_skills, get_skill_by_name
from app.utils.design_tokens import Colors, font_size_css
from app.core.command_manager import CommandManager, CommandType, CommandParameter


class _ElidedLabel(QLabel):
    """自动根据可用宽度省略文本的 QLabel"""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self._full_text = text
        self._was_elided = False

    def setText(self, text: str):
        self._full_text = text
        self._update_elided()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided()

    def _update_elided(self):
        fm = self.fontMetrics()
        elided = fm.elidedText(self._full_text, Qt.ElideRight, self.width())
        super().setText(elided)
        self._was_elided = elided != self._full_text

    def update_full_text(self, text: str):
        """更新完整文本并重新省略"""
        self._full_text = text
        self._update_elided()

    @property
    def was_elided(self) -> bool:
        return self._was_elided




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

        self._apply_style()
        self._update_display()

    def _apply_style(self):
        """应用当前状态的样式"""
        Colors.refresh()

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
                border-radius: 0px;
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

        # 描述样式
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

    def _update_display(self):
        """更新名称显示（含查询高亮）"""
        name = self._data["name"]
        # 命令需要加 / 前缀，技能和智能体直接显示名称
        item_type = self._data["type"]
        display_name = f"/{name}" if item_type == "command" else name
        query = self._query

        if query:
            html = ""
            lower_name = display_name.lower()
            lower_query = query.lower()
            last_end = 0
            for ch in lower_query:
                idx = lower_name.find(ch, last_end)
                if idx >= 0:
                    html += display_name[last_end:idx]
                    html += f'<span style="color: {Colors.SEND_BTN_START}; font-weight: bold;">{display_name[idx]}</span>'
                    last_end = idx + 1
                else:
                    break
            html += display_name[last_end:]
            self._name_label.setText(html)
        else:
            self._name_label.setText(display_name)

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

        # 参数名
        self._name_label = QLabel(self._param.name)
        self._name_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._name_label.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Preferred)
        self._name_label.setStyleSheet(f"color: {Colors.SEND_BTN_START}; background: transparent;")
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

        # 必填/选填标签
        if self._param.param_type != "positional":
            req_tag = QLabel("必填" if self._param.required else "可选")
            req_tag.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            req_color = Colors.TEXT_ACCENT if self._param.required else Colors.TEXT_MUTED
            req_tag.setStyleSheet(f"""
                color: {req_color};
                background: rgba(128,128,128,0.06);
                border-radius: 3px;
                padding: 1px 6px;
            """)
            layout.addWidget(req_tag)

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
        self._item_widgets: List[CommandItemWidget] = []
        self._divider = None  # 缓存分隔线 QFrame，避免积累
        self._visible = False
        self._current_query = ""
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

        # 已在此命令的 detail 模式，无需刷新
        if self._detail_mode and self._detail_cmd_name == cmd_name:
            # 但需要更新 data_provider（可能异步加载）
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
        QTimer.singleShot(0, self._adjust_detail_height)

    def _adjust_detail_height(self):
        """根据内容动态调整 detail 容器高度"""
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
            self._detail_params_scroll.setFixedHeight(min(content_height, 4 * ITEM_HEIGHT))
            content_height = min(content_height, 4 * ITEM_HEIGHT)
            hint_height = 0
        elif self._value_selection_mode:
            # 值选择列表高度
            value_count = len(self._value_widgets)
            content_height = min(value_count * ITEM_HEIGHT, 4 * ITEM_HEIGHT)
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

    def _switch_to_value_selection(self, widget: "ParameterItemWidget"):
        """切换到值选择模式：显示当前参数的可选值"""
        param_name = widget.param_name
        param = widget._param

        # 获取可选值列表
        options = []
        if param_name == "--model=":
            options = self._data_provider.get("model_options", [])
        else:
            options = param.value_options or []

        if not options:
            # 无可选值，退化为 flag 插入
            self.parameterSelected.emit(param_name, "flag")
            return

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
        for val in options:
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
        QTimer.singleShot(0, self._adjust_detail_height)

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
        QTimer.singleShot(0, self._adjust_detail_height)

    def update_active_params(self, active: set):
        """根据输入中已存在的参数名列表，显隐参数项

        Args:
            active: 输入文本中已存在的参数名集合，如 {"--with-context", "--model="}
        """
        if not self._detail_mode or not self._detail_has_params:
            return

        # 值选择模式：检查对应的参数是否还在输入中
        if self._value_selection_mode and self._value_selection_param:
            param_clean = self._value_selection_param.rstrip("=")
            still_active = any(a.rstrip("=") == param_clean for a in active)
            if not still_active:
                # 参数已被删掉 → 退出值选择模式，回到参数列表
                self._exit_value_selection()

        any_visible = False
        for w in self._param_widgets:
            param_key = w.param_name
            param_clean = param_key.rstrip("=")
            # 检查 active 集合中是否有同名参数
            is_active = any(a.rstrip("=") == param_clean for a in active)
            w.setVisible(not is_active)
            if w.isVisible():
                any_visible = True

        # 无可见参数时隐藏整个滚动区
        self._detail_params_scroll.setVisible(any_visible)

        # 重算高度
        QTimer.singleShot(0, self._adjust_detail_height)

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
        Colors.refresh()
        for i, w in enumerate(self._value_widgets):
            if i == self._selected_value_index:
                w.setStyleSheet(f"""
                    QLabel {{ color: {Colors.TEXT_PRIMARY}; background: {Colors.REALTIME_TAG_BG};
                             padding: 0 12px; {get_font_family_css()} {font_size_css(12)}; }}
                """)
            else:
                w.setStyleSheet(f"""
                    QLabel {{ color: {Colors.TEXT_PRIMARY}; background: transparent;
                             padding: 0 12px; {get_font_family_css()} {font_size_css(12)}; }}
                """)
        # 滚动到可见
        if 0 <= self._selected_value_index < len(self._value_widgets):
            self._detail_value_scroll.ensureWidgetVisible(
                self._value_widgets[self._selected_value_index], 0, 0
            )

    def _reset_detail_mode(self):
        """退出 detail 模式，回到列表模式"""
        if not self._detail_mode:
            return
        self._detail_mode = False
        self._detail_cmd_name = ""
        self._detail_has_params = False
        self._value_selection_mode = False
        self._value_selection_param = ""
        self._selected_param_index = -1
        self._selected_value_index = -1
        self._detail_container.setVisible(False)
        self._detail_params_scroll.setVisible(False)
        self._detail_value_scroll.setVisible(False)
        self._scroll_area.setVisible(True)

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
        self._all_items_cache = list(self._all_items)
        self._cache_dirty = False

    def load_items(self, query: str = "", incremental: bool = False):
        """根据 query 筛选并渲染列表

        Args:
            query: 搜索查询
            incremental: 是否增量更新（保留已有 widget，重用匹配项）
        """
        query = query.strip().lower()

        if not query:
            self._filtered_items = list(self._all_items)
        else:
            self._filtered_items = [
                item for item in self._all_items
                if query in item["name"].lower()
                or query in item["description"].lower()
            ]

        # 排序：命令和技能在前，智能体在后，同类型按名称排序
        sort_order = {"command": 0, "skill": 1, "agent": 2}
        self._filtered_items.sort(key=lambda x: (sort_order.get(x["type"], 99), x["name"]))

        self._render(incremental=incremental)

        if len(self._filtered_items) > 0:
            self._selected_index = 0
            self._update_selection()
            # 延迟滚动到顶部：等待布局完成后强制归零，避免初始渲染时 scroll 位置偏移
            QTimer.singleShot(0, lambda: self._scroll_area.verticalScrollBar().setValue(0))

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
                # 更新高亮查询（query 变化时重新渲染名称）
                w._query = self._current_query
                w._update_display()
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

        for i, widget in enumerate(self._item_widgets):
            widget.set_selected(i == self._selected_index)

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

    def select_next(self):
        """选择下一项"""
        if self._value_selection_mode:
            if self._value_widgets and self._selected_value_index < len(self._value_widgets) - 1:
                self._selected_value_index += 1
                self._update_value_selection()
            return
        if self._detail_mode and self._detail_has_params:
            # 只对可见参数导航
            visible = [i for i, w in enumerate(self._param_widgets) if w.isVisible()]
            if not visible:
                return
            if self._selected_param_index < visible[-1]:
                # 找到下一个可见的
                current_pos = visible.index(self._selected_param_index) if self._selected_param_index in visible else -1
                if current_pos < len(visible) - 1:
                    self._selected_param_index = visible[current_pos + 1]
                    self._update_param_selection()
            return
        if self._item_widgets and self._selected_index < len(self._item_widgets) - 1:
            self._selected_index += 1
            self._update_selection()

    def select_prev(self):
        """选择上一项"""
        if self._value_selection_mode:
            if self._value_widgets and self._selected_value_index > 0:
                self._selected_value_index -= 1
                self._update_value_selection()
            return
        if self._detail_mode and self._detail_has_params:
            visible = [i for i, w in enumerate(self._param_widgets) if w.isVisible()]
            if not visible:
                return
            if self._selected_param_index > visible[0]:
                current_pos = visible.index(self._selected_param_index) if self._selected_param_index in visible else -1
                if current_pos > 0:
                    self._selected_param_index = visible[current_pos - 1]
                    self._update_param_selection()
            return
        if self._item_widgets and self._selected_index > 0:
            self._selected_index -= 1
            self._update_selection()

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
            self.commandSelected.emit(item["name"], item["type"])
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
        self._reset_detail_mode()  # 回到列表模式（如果之前在 detail 模式）
        self._current_query = query
        self._refresh_data()
        self.load_items(query, incremental=incremental)
        has_items = len(self._filtered_items) > 0
        self._visible = has_items
        self.setVisible(has_items)

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
