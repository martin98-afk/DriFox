# -*- coding: utf-8 -*-
"""
历史会话卡片 - 包含当前会话列表和归档会话列表
"""
import datetime
from typing import List, Dict, Optional

from pypinyin import lazy_pinyin

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QDragEnterEvent
from PyQt5.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    TransparentToolButton,
    FluentIcon,
    SimpleCardWidget,
)

from app.utils.utils import get_icon, get_unified_font
from app.utils.design_tokens import (
    ItemStyles, Colors, get_font_family_css, font_size_css,
    get_ui_font_size, apply_font_size_to_widget, scale_font_size,
)


def format_relative_time(time_str: str) -> str:
    """将时间字符串转换为相对时间显示"""
    if not time_str or time_str == "未知":
        return "更早"
    try:
        session_time = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        now = datetime.datetime.now()
        diff = now - session_time

        if diff.total_seconds() < 60:
            return "刚刚"
        elif diff.total_seconds() < 3600:
            minutes = int(diff.total_seconds() / 60)
            return f"{minutes}分钟前"
        elif diff.total_seconds() < 86400:
            hours = int(diff.total_seconds() / 3600)
            return f"{hours}小时前"
        elif diff.days == 1:
            return "昨天"
        elif diff.days < 7:
            return f"{diff.days}天前"
        else:
            return time_str[5:10] if len(time_str) >= 10 else time_str
    except (ValueError, TypeError):
        return time_str[5:10] if time_str and len(time_str) >= 10 else "更早"


def get_message_preview(messages: List[Dict], max_len: int = 50) -> str:
    """从消息列表中提取预览文本"""
    if not messages:
        return ""
    for msg in reversed(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "user" and content:
            if isinstance(content, list):
                content = " ".join(
                    c.get("text", "") if isinstance(c, dict) else str(c)
                    for c in content
                )
            return content[:max_len].strip() + ("..." if len(content) > max_len else "")
    return ""


def _matches_search(session: Dict, search_text: str) -> bool:
    """检查会话是否匹配搜索文本（支持拼音搜索）"""
    if not search_text:
        return True
    search_lower = search_text.lower().replace(" ", "")
    if not search_lower:
        return True

    title = (session.get("title", "") or "")
    preview = (session.get("preview", "") or "")

    # 1. 直接子串匹配
    if search_lower in title.lower() or search_lower in preview.lower():
        return True

    # 2. 拼音匹配（标题和预览转拼音）
    try:
        title_pinyin = "".join(lazy_pinyin(title)).lower()
        preview_pinyin = "".join(lazy_pinyin(preview)).lower()
        if search_lower in title_pinyin or search_lower in preview_pinyin:
            return True

        # 3. 拼音首字母匹配
        title_initials = "".join(p[0] for p in lazy_pinyin(title) if p).lower()
        preview_initials = "".join(p[0] for p in lazy_pinyin(preview) if p).lower()
        if search_lower in title_initials or search_lower in preview_initials:
            return True
    except Exception:
        pass

    return False


class _HistoryItemCard(SimpleCardWidget):
    """历史会话项卡片"""

    sessionClicked = pyqtSignal(int)
    deleteRequested = pyqtSignal(int)
    renameRequested = pyqtSignal(int, str)

    def __init__(
        self,
        index: int,
        title: str,
        last_time: str,
        message_count: int,
        is_current: bool,
        preview: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._index = index
        self._is_current = is_current
        self._is_editing = False
        self.setCursor(Qt.PointingHandCursor)

        if is_current:
            self.setStyleSheet(
                """
                CardWidget {
                    background-color: rgba(102, 198, 255, 0.12);
                    border: 2px solid rgba(102, 198, 255, 0.6);
                    border-radius: 10px;
                }
                CardWidget:hover {
                    background-color: rgba(102, 198, 255, 0.18);
                    border: 2px solid rgba(102, 198, 255, 0.8);
                }
                """
            )
        else:
            self.setStyleSheet(
                """
                CardWidget {
                    background-color: rgba(255, 255, 255, 0.04);
                    border: 1px solid rgba(255, 255, 255, 0.08);
                    border-radius: 10px;
                }
                CardWidget:hover {
                    background-color: rgba(255, 255, 255, 0.08);
                    border: 1px solid rgba(102, 198, 255, 0.45);
                }
                """
            )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self.title_label = BodyLabel(title[:100], self)
        self.title_label.setWordWrap(True)
        font_size = scale_font_size(14)
        self.title_label.setStyleSheet(
            f"color: white; font-weight: bold; font-size: {font_size}px; {get_font_family_css()}" if is_current else f"color: white; font-size: {font_size}px; {get_font_family_css()}"
        )
        top_row.addWidget(self.title_label, 1)

        self.title_edit = QLineEdit(title[:100], self)
        self.title_edit.setStyleSheet(
            f"""
            QLineEdit {{
                background-color: rgba(0, 0, 0, 0.3);
                border: 1px solid rgba(102, 198, 255, 0.5);
                border-radius: 4px;
                color: white;
                padding: 2px 6px;
                {get_font_family_css()}
            }}
            """
        )
        self.title_edit.hide()
        self.title_edit.setMaximumWidth(250)
        self.title_edit.returnPressed.connect(self._finish_edit)
        self.title_edit.editingFinished.connect(self._finish_edit)
        top_row.addWidget(self.title_edit, 1, Qt.AlignLeft)

        self.current_indicator = CaptionLabel("🔥 活跃中", self)
        caption_size = scale_font_size(12)
        self.current_indicator.setStyleSheet(
            f"font-size: {caption_size}px; " + ItemStyles.tag() + get_font_family_css()
        )
        self.current_indicator.setVisible(is_current)
        top_row.addWidget(self.current_indicator, 0, Qt.AlignTop)

        btn_container = QHBoxLayout()
        btn_container.setSpacing(2)

        self.edit_btn = TransparentToolButton(get_icon("重命名"), self)
        self.edit_btn.setToolTip("重命名")
        self.edit_btn.setFixedSize(24, 24)
        self.edit_btn.clicked.connect(self._start_edit)
        btn_container.addWidget(self.edit_btn)

        self.delete_btn = TransparentToolButton(get_icon("归档"), self)
        self.delete_btn.setToolTip("归档")
        self.delete_btn.setFixedSize(24, 24)
        self.delete_btn.clicked.connect(lambda: self.deleteRequested.emit(self._index))
        btn_container.addWidget(self.delete_btn)

        top_row.addLayout(btn_container, 0)

        layout.addLayout(top_row)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)

        rel_time = format_relative_time(last_time)
        meta_text = f"{rel_time} · {message_count} 轮对话 · "
        self.meta_label = CaptionLabel(meta_text, self)
        caption_size = scale_font_size(12)
        self.meta_label.setStyleSheet(
            f"color: #ffb65c; font-size: {caption_size}px; {get_font_family_css()}" if is_current else f"color: rgba(255, 255, 255, 0.5); font-size: {caption_size}px; {get_font_family_css()}"
        )
        bottom_row.addWidget(self.meta_label)

        bottom_row.addStretch()

        if preview:
            self.preview_label = CaptionLabel(preview, self)
            caption_size = scale_font_size(12)
            self.preview_label.setStyleSheet(
                f"color: rgba(255, 255, 255, 0.4); font-style: italic; font-size: {caption_size}px; {get_font_family_css()}"
            )
            self.preview_label.setWordWrap(True)
            bottom_row.addSpacing(25)
            bottom_row.addWidget(self.preview_label, 1)

        layout.addLayout(bottom_row)

    def _start_edit(self):
        self._is_editing = True
        self.title_label.hide()
        self.title_edit.show()
        self.title_edit.setText(self.title_label.text())
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def _finish_edit(self):
        if not self._is_editing:
            return
        new_title = self.title_edit.text().strip()
        if new_title and new_title != self.title_label.text():
            self.renameRequested.emit(self._index, new_title)
        self._is_editing = False
        self.title_edit.hide()
        self.title_label.show()

    def update_title(self, new_title: str):
        self.title_label.setText(new_title[:100])

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._is_editing:
            self.sessionClicked.emit(self._index)
        super().mousePressEvent(event)


class _ArchivedItemCard(CardWidget):
    """归档会话项卡片 - 用于归档列表"""

    restored = pyqtSignal(str)  # 文件路径
    permanentlyDeleted = pyqtSignal(str)  # 文件路径
    renameRequested = pyqtSignal(str, str)  # 旧路径, 新标题

    def __init__(
        self,
        file_path: str,
        title: str,
        session_id: str,
        last_time: str,
        message_count: int = 0,
        preview: str = "",
        project: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._file_path = file_path
        self._title = title
        self._session_id = session_id
        self._is_editing = False
        self._message_count = message_count
        self._project = project
        self.setCursor(Qt.PointingHandCursor)

        # 归档卡片样式 - 使用不同的背景色区分
        self.setStyleSheet(
            """
            CardWidget {
                background-color: rgba(255, 180, 100, 0.08);
                border: 1px solid rgba(255, 150, 80, 0.2);
                border-radius: 10px;
            }
            CardWidget:hover {
                background-color: rgba(255, 180, 100, 0.15);
                border: 1px solid rgba(255, 150, 80, 0.4);
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        # 归档图标
        archive_icon = QLabel("📦", self)
        archive_icon.setStyleSheet("font-size: 14px;")
        top_row.addWidget(archive_icon)

        self.title_label = BodyLabel(title[:100], self)
        self.title_label.setWordWrap(True)
        body_size = scale_font_size(14)
        self.title_label.setStyleSheet(f"color: white; font-size: {body_size}px; {get_font_family_css()}")
        top_row.addWidget(self.title_label, 1)

        self.title_edit = QLineEdit(title[:100], self)
        self.title_edit.setStyleSheet(
            f"""
            QLineEdit {{
                background-color: rgba(0, 0, 0, 0.3);
                border: 1px solid rgba(255, 180, 100, 0.5);
                border-radius: 4px;
                color: white;
                padding: 2px 6px;
                {get_font_family_css()}
            }}
            """
        )
        self.title_edit.hide()
        self.title_edit.setMaximumWidth(250)
        self.title_edit.returnPressed.connect(self._finish_edit)
        self.title_edit.editingFinished.connect(self._finish_edit)
        top_row.addWidget(self.title_edit, 1, Qt.AlignLeft)

        layout.addLayout(top_row)

        # 项目标签（归档会话显示原项目）
        if project:
            project_label = QLabel(f"📁 {project}", self)
            caption_size = scale_font_size(11)
            project_label.setStyleSheet(f"""
                color: rgba(245, 158, 11, 0.7);
                {get_font_family_css()} font-size: {caption_size}px;
                padding: 2px 0px 2px 0px;
            """)
            layout.addWidget(project_label)

        btn_container = QHBoxLayout()
        btn_container.setSpacing(2)

        # 重命名按钮
        self.edit_btn = TransparentToolButton(get_icon("重命名"), self)
        self.edit_btn.setToolTip("重命名")
        self.edit_btn.setFixedSize(24, 24)
        self.edit_btn.clicked.connect(self._start_edit)
        btn_container.addWidget(self.edit_btn)

        # 彻底删除按钮
        self.delete_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.delete_btn.setToolTip("彻底删除")
        self.delete_btn.setFixedSize(24, 24)
        self.delete_btn.clicked.connect(lambda: self.permanentlyDeleted.emit(self._file_path))
        btn_container.addWidget(self.delete_btn)

        top_row.addLayout(btn_container, 0)

        layout.addLayout(top_row)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)

        rel_time = format_relative_time(last_time)
        meta_text = f"{rel_time}"
        if message_count > 0:
            meta_text += f" · {message_count} 轮对话"
        self.meta_label = CaptionLabel(meta_text, self)
        caption_size = scale_font_size(12)
        self.meta_label.setStyleSheet(f"color: rgba(255, 255, 255, 0.5); font-size: {caption_size}px; {get_font_family_css()}")
        bottom_row.addWidget(self.meta_label)

        bottom_row.addStretch()

        if preview:
            self.preview_label = CaptionLabel(preview, self)
            caption_size = scale_font_size(12)
            self.preview_label.setStyleSheet(
                f"color: rgba(255, 255, 255, 0.4); font-style: italic; font-size: {caption_size}px; {get_font_family_css()}"
            )
            self.preview_label.setWordWrap(True)
            bottom_row.addSpacing(25)
            bottom_row.addWidget(self.preview_label, 1)

        layout.addLayout(bottom_row)

    def _start_edit(self):
        self._is_editing = True
        self.title_label.hide()
        self.title_edit.show()
        self.title_edit.setText(self.title_label.text())
        self.title_edit.setFocus()
        self.title_edit.selectAll()

    def _finish_edit(self):
        if not self._is_editing:
            return
        new_title = self.title_edit.text().strip()
        if new_title and new_title != self.title_label.text():
            self.renameRequested.emit(self._file_path, new_title)
        self._is_editing = False
        self.title_edit.hide()
        self.title_label.show()

    def update_title(self, new_title: str):
        self.title_label.setText(new_title[:100])

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._is_editing:
            # 单击也可以恢复会话
            self.restored.emit(self._file_path)
        super().mousePressEvent(event)


class _SectionHeader(QLabel):
    def __init__(self, text: str, count: int = 0, parent=None):
        super().__init__(parent)
        display_text = text if count == 0 else f"{text} ({count})"
        self.setText(display_text)
        caption_size = scale_font_size(12)
        self.setStyleSheet(
            f"""
            color: rgba(255, 255, 255, 0.45);
            {get_font_family_css()} font-size: {caption_size}px;
            font-weight: bold;
            padding: 4px 2px;
            """
        )


class HistoryCard(QWidget):
    """历史会话卡片内容 - 支持历史会话和归档会话切换"""

    sessionSelected = pyqtSignal(int)
    sessionArchived = pyqtSignal(int)
    sessionRenamed = pyqtSignal(int, str)
    refreshRequested = pyqtSignal()
    sessionImported = pyqtSignal(dict)  # 导入会话时发出
    sessionRestored = pyqtSignal(str)  # 恢复归档会话
    sessionPermanentlyDeleted = pyqtSignal(str)  # 彻底删除归档会话
    archivedSessionRenamed = pyqtSignal(str, str)  # 归档会话重命名

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_history: List[Dict] = []
        self._current_index: Optional[int] = None
        self._archived_sessions: List[Dict] = []
        self._item_cards = []
        self._current_tab = "history"  # "history" or "archived"
        self._current_project: Optional[str] = None  # 当前过滤的项目
        self._search_filter: str = ""  # 搜索过滤文本
        self._setup_ui()
        # 启用拖放支持
        self.setAcceptDrops(True)
        
        # 初始化时应用配置中的字体大小
        from PyQt5.QtCore import QTimer
        QTimer.singleShot(0, self._refresh_font_size)

    def _refresh_font_size(self):
        """刷新字体大小"""
        actual_size = get_ui_font_size()
        apply_font_size_to_widget(self, actual_size)

    def _setup_ui(self):
        """不需要创建自己的布局，直接使用父控件的 scroll_area"""
        pass

    def set_current_project(self, project: str):
        """设置当前过滤项目"""
        self._current_project = project

    def set_search_filter(self, text: str):
        """设置搜索过滤文本（同时支持历史会话和归档）"""
        self._search_filter = text.strip()
        self._update_display()

    def get_content_layout(self) -> QVBoxLayout:
        """返回内容布局，供外部使用"""
        # 找到 BaseSettingsCard 的 content_layout
        parent = self.parent()
        while parent:
            if hasattr(parent, 'content_layout'):
                return parent.content_layout
            parent = parent.parent()
        # 如果没找到，返回自己的默认布局
        if self.layout() is None:
            layout = QVBoxLayout(self)
            layout.setContentsMargins(4, 4, 4, 4)
            layout.setSpacing(6)
        return self.layout()

    def _get_date_category(self, last_time_str: str) -> str:
        if not last_time_str or last_time_str == "未知":
            return "更早"
        try:
            session_date = datetime.datetime.strptime(
                last_time_str[:10], "%Y-%m-%d"
            ).date()
            today = datetime.datetime.now().date()
            yesterday = today - datetime.timedelta(days=1)
            week_start = today - datetime.timedelta(days=today.weekday())
            last_week_start = week_start - datetime.timedelta(days=7)
            month_start = today.replace(day=1)

            if session_date == today:
                return "今天"
            elif session_date == yesterday:
                return "昨天"
            elif week_start <= session_date <= today:
                return "本周"
            elif last_week_start <= session_date < week_start:
                return "上周"
            elif session_date >= month_start:
                return "本月"
            elif session_date.year == today.year:
                month_names = ["一月", "二月", "三月", "四月", "五月", "六月",
                               "七月", "八月", "九月", "十月", "十一月", "十二月"]
                return month_names[session_date.month - 1]
            else:
                return f"{session_date.year}年"
        except (ValueError, TypeError):
            return "更早"

    def _clear_content(self):
        """清理内容区域"""
        layout = self.get_content_layout()
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() and item.widget() != self:
                item.widget().deleteLater()
        self._item_cards.clear()

    def set_history(self, history_list: List[Dict], current_index=None):
        """设置历史会话列表"""
        self._all_history = history_list
        self._current_index = current_index
        if self._current_tab == "history":
            self._update_display()

    def set_archived_sessions(self, archived_list: List[Dict]):
        """设置归档会话列表"""
        self._archived_sessions = archived_list
        if self._current_tab == "archived":
            self._update_display()

    def switch_tab(self, tab: str):
        """切换标签页"""
        if self._current_tab != tab:
            self._current_tab = tab
            self._update_display()

    def _update_display(self):
        """更新显示内容"""
        layout = self.get_content_layout()

        # 性能优化：批量更新，避免每加一个 widget 就重绘一次
        content_widget = layout.parentWidget() if layout else None
        if content_widget:
            content_widget.setUpdatesEnabled(False)

        self._clear_content()

        if self._current_tab == "history":
            self._update_history_display(layout)
        else:
            self._update_archived_display(layout)

        layout.addStretch(1)

        if content_widget:
            content_widget.setUpdatesEnabled(True)
            content_widget.repaint()

        # 刷新字体大小（会话卡片创建后应用）
        self._refresh_font_size()

    def _update_history_display(self, layout: QVBoxLayout):
        """更新历史会话显示"""
        if not self._all_history:
            empty_label = QLabel("暂无历史对话记录")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("color: rgba(255, 255, 255, 0.6); padding: 16px;")
            layout.addWidget(empty_label)
            return

        # 先显示当前会话
        current_session_widget = None
        current_matches_search = True
        if self._current_index is not None and 0 <= self._current_index < len(self._all_history):
            current_session = self._all_history[self._current_index]
            current_matches_search = not self._search_filter or _matches_search(current_session, self._search_filter)
            if current_matches_search:
                current_preview = get_message_preview(current_session.get("messages", []))
                current_session_widget = _HistoryItemCard(
                    index=self._current_index,
                    title=current_session.get("title", "当前对话"),
                    last_time=current_session.get("last_time", "未知"),
                    message_count=current_session.get("message_count", 0),
                    is_current=True,
                    preview=current_preview,
                )
                current_session_widget.sessionClicked.connect(self._on_card_clicked)
                current_session_widget.deleteRequested.connect(self._on_card_deleted)
                current_session_widget.renameRequested.connect(self._on_card_renamed)

        # 分离当前会话和其他会话（保存原始索引，避免后续O(n²)查找）
        other_sessions = [(i, s) for i, s in enumerate(self._all_history) if i != self._current_index]
        # 搜索过滤
        if self._search_filter:
            other_sessions = [(i, s) for i, s in other_sessions if _matches_search(s, self._search_filter)]
        grouped = {}
        for original_index, session in other_sessions:
            category = self._get_date_category(session.get("last_time", ""))
            if category not in grouped:
                grouped[category] = []
            grouped[category].append((original_index, session))

        order = ["今天", "昨天", "本周", "上周", "本月"]
        month_names = ["一月", "二月", "三月", "四月", "五月", "六月",
                       "七月", "八月", "九月", "十月", "十一月", "十二月"]

        extra_sections = []
        for key in grouped.keys():
            if key not in order and key != "更早":
                extra_sections.append((key, grouped[key]))

        year_groups = {}
        month_groups = []
        for key, sessions in extra_sections:
            if key.endswith("年"):
                year_groups[key] = sessions
            else:
                month_groups.append((key, sessions))

        final_order = []
        for section in order:
            if section in grouped:
                final_order.append((section, grouped[section]))
        for section, sessions in month_groups:
            final_order.append((section, sessions))
        for year in sorted(year_groups.keys(), reverse=True):
            final_order.append((year, year_groups[year]))

        has_items = False

        # 渲染当前会话
        if current_session_widget:
            has_items = True
            current_header = _SectionHeader("当前会话", 0)
            layout.addWidget(current_header)
            layout.addWidget(current_session_widget)
            self._item_cards.append(current_session_widget)

            spacer = QWidget()
            spacer.setFixedHeight(8)
            layout.addWidget(spacer)

        # 渲染其他历史会话
        for section, sessions in final_order:
            if not sessions:
                continue
            has_items = True

            header = _SectionHeader(section, len(sessions))
            layout.addWidget(header)

            for original_index, session in sessions:
                messages = session.get("messages", [])
                preview = get_message_preview(messages)
                card = _HistoryItemCard(
                    index=original_index,
                    title=session.get("title", "新对话"),
                    last_time=session.get("last_time", "未知"),
                    message_count=session.get("message_count", 0),
                    is_current=False,
                    preview=preview,
                )
                card.sessionClicked.connect(self._on_card_clicked)
                card.deleteRequested.connect(self._on_card_deleted)
                card.renameRequested.connect(self._on_card_renamed)
                layout.addWidget(card)
                self._item_cards.append(card)

            spacer = QWidget()
            spacer.setFixedHeight(8)
            layout.addWidget(spacer)

        if not has_items:
            if self._search_filter:
                empty_label = QLabel(f"没有找到匹配「{self._search_filter}」的会话")
            else:
                empty_label = QLabel("暂无历史对话记录")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("color: rgba(255, 255, 255, 0.6); padding: 16px;")
            layout.addWidget(empty_label)

    def _update_archived_display(self, layout: QVBoxLayout):
        """更新归档会话显示"""
        if not self._archived_sessions:
            empty_label = QLabel("暂无归档会话")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("color: rgba(255, 255, 255, 0.6); padding: 16px;")
            layout.addWidget(empty_label)
            return

        # 搜索过滤
        sessions_to_show = self._archived_sessions
        if self._search_filter:
            sessions_to_show = [
                s for s in self._archived_sessions
                if _matches_search(s, self._search_filter)
            ]

        if not sessions_to_show:
            empty_label = QLabel(f"没有找到匹配「{self._search_filter}」的会话")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("color: rgba(255, 255, 255, 0.6); padding: 16px;")
            layout.addWidget(empty_label)
            return

        # 按日期分组
        grouped = {}
        for session in sessions_to_show:
            last_time = session.get("last_time", session.get("saved_at", ""))
            category = self._get_date_category(last_time)
            if category not in grouped:
                grouped[category] = []
            grouped[category].append(session)

        order = ["今天", "昨天", "本周", "上周", "本月"]

        final_order = []
        for section in order:
            if section in grouped:
                final_order.append((section, grouped[section]))

        # 添加其他分组
        for category, sessions in grouped.items():
            if category not in order:
                final_order.append((category, sessions))

        has_items = False

        for section, sessions in final_order:
            if not sessions:
                continue
            has_items = True

            header = _SectionHeader(section, len(sessions))
            layout.addWidget(header)

            for session in sessions:
                file_path = session.get("path", "")
                title = session.get("title", "归档会话")
                session_id = session.get("session_id", "")
                last_time = session.get("last_time", session.get("saved_at", "未知"))

                # 尝试从文件获取更多信息
                message_count = session.get("message_count", 0)
                preview = session.get("preview", "")

                card = _ArchivedItemCard(
                    file_path=file_path,
                    title=title,
                    session_id=session_id,
                    last_time=last_time,
                    message_count=message_count,
                    preview=preview,
                    project=session.get("project", ""),
                )
                card.restored.connect(self._on_archived_restored)
                card.permanentlyDeleted.connect(self._on_archived_deleted)
                card.renameRequested.connect(self._on_archived_renamed)
                layout.addWidget(card)
                self._item_cards.append(card)

            spacer = QWidget()
            spacer.setFixedHeight(8)
            layout.addWidget(spacer)

        if not has_items:
            empty_label = QLabel("暂无归档会话")
            empty_label.setAlignment(Qt.AlignCenter)
            empty_label.setStyleSheet("color: rgba(255, 255, 255, 0.6); padding: 16px;")
            layout.addWidget(empty_label)

    def _on_card_clicked(self, index: int):
        self.sessionSelected.emit(index)

    def _on_card_deleted(self, index: int):
        self.sessionArchived.emit(index)

    def _on_card_renamed(self, index: int, new_title: str):
        self.sessionRenamed.emit(index, new_title)

    def _on_archived_restored(self, file_path: str):
        """恢复归档会话"""
        self.sessionRestored.emit(file_path)

    def _on_archived_deleted(self, file_path: str):
        """彻底删除归档会话"""
        self.sessionPermanentlyDeleted.emit(file_path)

    def _on_archived_renamed(self, file_path: str, new_title: str):
        """重命名归档会话"""
        self.archivedSessionRenamed.emit(file_path, new_title)

    # ==================== 拖放和导入功能 ====================

    def dragEnterEvent(self, event: QDragEnterEvent):
        """处理拖入事件"""
        if event.mimeData().hasUrls():
            # 检查是否包含 JSON 文件
            urls = event.mimeData().urls()
            for url in urls:
                if url.isLocalFile() and url.toLocalFile().endswith('.json'):
                    event.acceptProposedAction()
                    return
        super().dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        """处理拖离事件"""
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        """处理文件放下事件"""
        if event.mimeData().hasUrls():
            json_files = []
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    file_path = url.toLocalFile()
                    if file_path.endswith('.json'):
                        json_files.append(file_path)

            if json_files:
                self._handle_import_files(json_files)
                event.acceptProposedAction()
                return

        super().dropEvent(event)

    def _handle_import_files(self, file_paths: List[str]):
        """处理导入的文件列表"""
        for file_path in file_paths:
            self.sessionImported.emit({"file_path": file_path})

    def get_import_button_handler(self):
        """返回一个可调用的导入处理函数，供外部设置"""
        def handle_import():
            from PyQt5.QtWidgets import QFileDialog
            files, _ = QFileDialog.getOpenFileNames(
                self,
                "导入会话",
                "",
                "JSON 文件 (*.json)"
            )
            if files:
                self._handle_import_files(files)
        return handle_import