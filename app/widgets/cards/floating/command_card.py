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

from app.utils.utils import get_font_family_css, get_local_skills
from app.utils.design_tokens import Colors, font_size_css
from app.core.command_manager import CommandManager


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

    @property
    def item_data(self) -> Dict[str, str]:
        """返回项数据（用于增量更新时的比较）"""
        return self._data


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
        fg = "#ffffff" if self._selected else Colors.TEXT_PRIMARY
        self._name_label.setStyleSheet(f"""
            QLabel {{
                color: {fg};
                {get_font_family_css()} {font_size_css(13)};
                background: transparent;
            }}
        """)

        # 描述样式
        desc_fg = "#ffffff" if self._selected else "rgba(255,255,255,0.45)"
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
            tag_fg = "#66c6ff" if not self._selected else "#aae0ff"
            self._tag_label.setStyleSheet(f"""
                QLabel {{
                    color: {tag_fg};
                    {get_font_family_css()} {font_size_css(11)};
                    font-weight: bold;
                    background: transparent;
                }}
            """)
        elif item_type == "agent":
            tag_fg = "#b388ff" if not self._selected else "#d1b3ff"
            self._tag_label.setStyleSheet(f"""
                QLabel {{
                    color: {tag_fg};
                    {get_font_family_css()} {font_size_css(11)};
                    font-weight: bold;
                    background: transparent;
                }}
            """)
        elif item_type == "prompt":
            tag_fg = "#ffb366" if not self._selected else "#ffc999"
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
                    html += f'<span style="color: #C9A85C; font-weight: bold;">{display_name[idx]}</span>'
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


class CommandCard(QWidget):
    """斜杠命令卡片"""

    commandSelected = pyqtSignal(str)  # 选中命令/技能名称
    dismissed = pyqtSignal()           # 卡片被关闭

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

        self.setVisible(False)
        self._setup_ui()

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
                width: 4px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: rgba(255, 255, 255, 0.15);
                border-radius: 2px;
                min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba(255, 255, 255, 0.25);
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
                    divider.setStyleSheet("background: rgba(255, 255, 255, 0.08); border: none;")
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
            divider.setStyleSheet("background: rgba(255, 255, 255, 0.08); border: none;")
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

    def _update_selection(self):
        """更新选中高亮"""
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

        # 滚动到可见区域
        if 0 <= self._selected_index < len(self._item_widgets):
            self._scroll_area.ensureWidgetVisible(
                self._item_widgets[self._selected_index], 0, 0
            )

    def select_next(self):
        """选择下一项"""
        if self._item_widgets and self._selected_index < len(self._item_widgets) - 1:
            self._selected_index += 1
            self._update_selection()

    def select_prev(self):
        """选择上一项"""
        if self._item_widgets and self._selected_index > 0:
            self._selected_index -= 1
            self._update_selection()

    def select_current(self):
        """确认选中当前项"""
        if 0 <= self._selected_index < len(self._filtered_items):
            item = self._filtered_items[self._selected_index]
            self.commandSelected.emit(item["name"])
            self.dismiss()

    def dismiss(self):
        """关闭卡片（清理状态并隐藏自身）"""
        self._visible = False
        self.setVisible(False)
        self.dismissed.emit()

    def show_card(self, query: str = "", incremental: bool = True):
        """加载数据并显示（显示由 CardManager 控制，此方法只准备数据）

        Args:
            query: 搜索查询
            incremental: 是否增量更新（默认开启，可提升流畅性）
        """
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
