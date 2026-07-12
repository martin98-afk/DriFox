# -*- coding: utf-8 -*-
"""
历史会话卡片 - 包含当前会话列表和归档会话列表
"""

import datetime
import os
from typing import Dict, List, Optional

from pypinyin import lazy_pinyin
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QDragEnterEvent
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    SimpleCardWidget,
    TransparentToolButton,
)

from app.utils.design_tokens import (
    Colors,
    apply_font_size_to_widget,
    font_size_css,
    get_font_family_css,
    get_ui_font_size,
    scale_font_size,
)
from app.utils.utils import get_icon


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
        # 跳过 hook 消息（role=user 但带 _hook_event 标记），避免预览显示 hook 内容
        if msg.get("_hook_event"):
            continue
        if role == "user" and content:
            if isinstance(content, list):
                content = " ".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
            return content[:max_len].strip() + ("..." if len(content) > max_len else "")
    return ""


def _matches_search(session: Dict, search_text: str, pinyin_cache: dict = None) -> bool:
    """检查会话是否匹配搜索文本（支持拼音搜索）

    Args:
        session: 会话数据
        search_text: 搜索文本
        pinyin_cache: 拼音缓存字典 {session_id: {"pinyin": str, "initials": str}}，
                      传入后可避免重复计算
    """
    if not search_text:
        return True
    search_lower = search_text.lower().replace(" ", "")
    if not search_lower:
        return True

    title = session.get("title", "") or ""
    preview = session.get("preview", "") or ""

    # 1. 直接子串匹配（快速路径，不走拼音）
    if search_lower in title.lower() or search_lower in preview.lower():
        return True

    # 2. 拼音匹配（尝试从缓存读取，避免重复计算）
    session_id = session.get("session_id", "")
    try:
        if pinyin_cache is not None and session_id:
            cached = pinyin_cache.get(session_id)
            if cached:
                title_pinyin = cached.get("title_pinyin", "")
                preview_pinyin = cached.get("preview_pinyin", "")
                title_initials = cached.get("title_initials", "")
                preview_initials = cached.get("preview_initials", "")
            else:
                title_pinyin = "".join(lazy_pinyin(title)).lower()
                preview_pinyin = "".join(lazy_pinyin(preview)).lower()
                title_initials = "".join(p[0] for p in lazy_pinyin(title) if p).lower()
                preview_initials = "".join(p[0] for p in lazy_pinyin(preview) if p).lower()
                pinyin_cache[session_id] = {
                    "title_pinyin": title_pinyin,
                    "preview_pinyin": preview_pinyin,
                    "title_initials": title_initials,
                    "preview_initials": preview_initials,
                }
        else:
            title_pinyin = "".join(lazy_pinyin(title)).lower()
            preview_pinyin = "".join(lazy_pinyin(preview)).lower()
            title_initials = "".join(p[0] for p in lazy_pinyin(title) if p).lower()
            preview_initials = "".join(p[0] for p in lazy_pinyin(preview) if p).lower()

        if search_lower in title_pinyin or search_lower in preview_pinyin:
            return True
        if search_lower in title_initials or search_lower in preview_initials:
            return True
    except Exception:
        pass

    # 3. worktree 分支名匹配（目录名作为分支名）
    worktree_path = session.get("worktree_path", "") or ""
    if worktree_path:
        branch_name = os.path.basename(worktree_path.rstrip("/\\"))
        if search_lower in branch_name.lower():
            return True

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
        worktree_branch: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._index = index
        self._is_current = is_current
        self._is_editing = False
        self._session_id = None  # 用于缓存匹配
        self._worktree_branch = worktree_branch  # 保留用于后续更新
        self.setCursor(Qt.PointingHandCursor)

        # 批量读取颜色 token 和字体尺寸（避免多次 refresh/scale_font_size 的累积开销）
        Colors.refresh()
        self._font_family = get_font_family_css()
        self._font_size = scale_font_size(14)
        self._caption_size = scale_font_size(12)
        _font_family = self._font_family
        _font_size = self._font_size
        _caption_size = self._caption_size
        _selected_bg = Colors.SELECTED_BG
        _border_accent = Colors.BORDER_ACCENT
        _tab_active_bg = Colors.TAB_ACTIVE_BG
        _text_accent = Colors.TEXT_ACCENT
        _card_bg_dim = Colors.CARD_BG_DIM
        _border = Colors.BORDER
        _hover_bg = Colors.HOVER_BG
        _text_primary = Colors.TEXT_PRIMARY
        _accent_warm = Colors.ACCENT_WARM
        _text_secondary = Colors.TEXT_SECONDARY
        _tag_bg = Colors.TAB_ACTIVE_BG
        _tag_text = Colors.ACCENT_WARM

        if is_current:
            self.setStyleSheet(f"""
                CardWidget {{
                    background-color: {_selected_bg};
                    border: 2px solid {_border_accent};
                    border-radius: 10px;
                }}
                CardWidget:hover {{
                    background-color: {_tab_active_bg};
                    border: 2px solid {_text_accent};
                }}
            """)
        else:
            self.setStyleSheet(f"""
                CardWidget {{
                    background-color: {_card_bg_dim};
                    border: 1px solid {_border};
                    border-radius: 10px;
                }}
                CardWidget:hover {{
                    background-color: {_hover_bg};
                    border: 1px solid {_border_accent};
                }}
            """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 8, 8)
        layout.setSpacing(4)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self.title_label = BodyLabel(title[:100], self)
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet(
            f"color: {_text_primary}; font-weight: bold; font-size: {_font_size}px; {_font_family}"
            if is_current
            else f"color: {_text_primary}; font-size: {_font_size}px; {_font_family}"
        )
        top_row.addWidget(self.title_label, 1)

        self.title_edit = QLineEdit(title[:100], self)
        self.title_edit.setStyleSheet(
            f"""
            QLineEdit {{
                background-color: rgba(0, 0, 0, 0.3);
                border: 1px solid {_border_accent};
                border-radius: 4px;
                color: {_text_primary};
                padding: 2px 6px;
                {_font_family}
            }}
            """
        )
        self.title_edit.hide()
        self.title_edit.setMaximumWidth(250)
        self.title_edit.returnPressed.connect(self._finish_edit)
        self.title_edit.editingFinished.connect(self._finish_edit)
        top_row.addWidget(self.title_edit, 1, Qt.AlignLeft)

        # worktree 分支标记（仅非主分支显示）
        self._branch_label = CaptionLabel("", self)
        self._branch_label.setStyleSheet(f"""
            CaptionLabel {{
                color: {_tag_text};
                background-color: {_tag_bg};
                border-radius: 3px;
                padding: 1px 5px;
                font-size: {_caption_size - 1}px;
                {_font_family}
            }}
        """)
        self._branch_label.setVisible(bool(worktree_branch))
        if worktree_branch:
            self._branch_label.setText(f"🌿 {worktree_branch}")
        top_row.addWidget(self._branch_label, 0, Qt.AlignTop)

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
        meta_text = f"{rel_time} · {message_count} 轮对话"
        self.meta_label = CaptionLabel(meta_text, self)
        self.meta_label.setStyleSheet(
            f"color: {_accent_warm}; font-size: {_caption_size}px; {_font_family}"
            if is_current
            else f"color: {_text_secondary}; font-size: {_caption_size}px; {_font_family}"
        )
        bottom_row.addWidget(self.meta_label)

        bottom_row.addStretch()

        layout.addLayout(bottom_row)

        # 预览标签独立一行（不放在 bottom_row 中，避免与右侧按钮竞争水平空间）
        self._preview_label = None  # 懒创建，便于更新
        if preview:
            self._ensure_preview_label(preview)

    def _ensure_preview_label(self, text: str):
        """确保存在预览标签（独立一行，不挤占右侧按钮空间）"""
        if self._preview_label is None:
            self._preview_label = CaptionLabel("", self)
            self._preview_label.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; font-style: italic; font-size: {self._caption_size}px; {self._font_family}"
            )
            self._preview_label.setWordWrap(True)
            # 添加到主布局底部（bottom_row 下方），占满整行宽度
            self.layout().addWidget(self._preview_label)
        self._preview_label.setText(text)
        self._preview_label.setVisible(bool(text))

    def update_data(
        self,
        index: int,
        title: str,
        last_time: str,
        message_count: int,
        is_current: bool,
        preview: str = "",
        worktree_branch: str = "",
    ):
        """原地更新卡片数据，避免重建widget"""
        self._index = index

        # 标题变化
        if self.title_label.text() != title[:100]:
            self.title_label.setText(title[:100])
            self.title_edit.setText(title[:100])

        # 活跃状态变化 → 需重设样式
        if self._is_current != is_current:
            self._is_current = is_current
            Colors.refresh()
            if is_current:
                self.setStyleSheet(f"""
                    CardWidget {{
                        background-color: {Colors.SELECTED_BG};
                        border: 2px solid {Colors.BORDER_ACCENT};
                        border-radius: 10px;
                    }}
                    CardWidget:hover {{
                        background-color: {Colors.TAB_ACTIVE_BG};
                        border: 2px solid {Colors.TEXT_ACCENT};
                    }}
                """)
                self.title_label.setStyleSheet(
                    f"color: {Colors.TEXT_PRIMARY}; font-weight: bold; font-size: {self._font_size}px; {self._font_family}"
                )
                self.meta_label.setStyleSheet(
                    f"color: {Colors.ACCENT_WARM}; font-size: {self._caption_size}px; {self._font_family}"
                )
            else:
                self.setStyleSheet(f"""
                    CardWidget {{
                        background-color: {Colors.CARD_BG_DIM};
                        border: 1px solid {Colors.BORDER};
                        border-radius: 10px;
                    }}
                    CardWidget:hover {{
                        background-color: {Colors.HOVER_BG};
                        border: 1px solid {Colors.BORDER_ACCENT};
                    }}
                """)
                self.title_label.setStyleSheet(
                    f"color: {Colors.TEXT_PRIMARY}; font-size: {self._font_size}px; {self._font_family}"
                )
                self.meta_label.setStyleSheet(
                    f"color: {Colors.TEXT_SECONDARY}; font-size: {self._caption_size}px; {self._font_family}"
                )

        # 元信息变化
        rel_time = format_relative_time(last_time)
        meta_text = f"{rel_time} · {message_count} 轮对话"
        self.meta_label.setText(meta_text)

        # worktree 分支变化
        self._worktree_branch = worktree_branch
        if worktree_branch:
            self._branch_label.setText(f"🌿 {worktree_branch}")
            self._branch_label.setVisible(True)
        else:
            self._branch_label.setVisible(False)

        # 预览变化
        self._ensure_preview_label(preview)

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
        archive_icon.setStyleSheet(f"font-size: {font_size_css(14)};")
        top_row.addWidget(archive_icon)

        self.title_label = BodyLabel(title[:100], self)
        self.title_label.setWordWrap(True)
        body_size = scale_font_size(14)
        self.title_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-size: {body_size}px; {get_font_family_css()}")
        top_row.addWidget(self.title_label, 1)

        self.title_edit = QLineEdit(title[:100], self)
        self.title_edit.setStyleSheet(
            f"""
            QLineEdit {{
                background-color: rgba(0, 0, 0, 0.3);
                border: 1px solid rgba(255, 180, 100, 0.5);
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
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

        # 项目标签（归档会话显示原项目）- 懒创建，支持 update_data 复用
        self._project_label = None
        if project:
            self._init_project_label(project)

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
        Colors.refresh()
        self.meta_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: {caption_size}px; {get_font_family_css()}"
        )
        bottom_row.addWidget(self.meta_label)

        bottom_row.addStretch()

        layout.addLayout(bottom_row)

        # 预览标签独立一行（不放在 bottom_row 中，避免与右侧按钮竞争水平空间）
        self._preview_label = None  # 懒创建
        if preview:
            self._init_preview_label(preview)

    def _init_preview_label(self, text: str):
        """初始化预览标签（独立一行，不挤占右侧按钮空间）"""
        caption_size = scale_font_size(12)
        self._preview_label = CaptionLabel(text, self)
        self._preview_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-style: italic; font-size: {caption_size}px; {get_font_family_css()}"
        )
        self._preview_label.setWordWrap(True)
        # 添加到主布局底部（bottom_row 下方），占满整行宽度
        self.layout().addWidget(self._preview_label)

    def _init_project_label(self, project: str):
        """初始化项目标签"""
        caption_size = scale_font_size(11)
        self._project_label = QLabel(f"📁 {project}", self)
        self._project_label.setStyleSheet(f"""
            color: rgba(245, 158, 11, 0.7);
            {get_font_family_css()} font-size: {caption_size}px;
            padding: 2px 0px 2px 0px;
        """)
        # 插入到布局第二个位置（top_row 之后）
        self.layout().insertWidget(1, self._project_label)

    def update_data(
        self,
        file_path: str,
        title: str,
        session_id: str,
        last_time: str,
        message_count: int = 0,
        preview: str = "",
        project: str = "",
    ):
        """原地更新归档卡片数据"""
        self._file_path = file_path
        self._session_id = session_id

        if self.title_label.text() != title[:100]:
            self.title_label.setText(title[:100])
            self.title_edit.setText(title[:100])

        rel_time = format_relative_time(last_time)
        meta_text = f"{rel_time}"
        if message_count > 0:
            meta_text += f" · {message_count} 轮对话"
        self.meta_label.setText(meta_text)

        # 预览更新
        if self._preview_label is None:
            self._init_preview_label(preview or "")
        else:
            self._preview_label.setText(preview)
            self._preview_label.setVisible(bool(preview))

        # 项目标签更新
        if self._project_label is None:
            self._init_project_label(project)
        else:
            self._project_label.setText(f"📁 {project}")
            self._project_label.setVisible(bool(project))

        # 重连信号以传递新路径
        try:
            self.delete_btn.clicked.disconnect()
        except TypeError:
            pass
        self.delete_btn.clicked.connect(lambda: self.permanentlyDeleted.emit(self._file_path))

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
        self._apply_style()

    def _apply_style(self):
        """应用/刷新样式（支持主题切换时重刷）"""
        Colors.refresh()
        caption_size = scale_font_size(12)
        self.setStyleSheet(
            f"""
            color: {Colors.TEXT_SECONDARY};
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
        self._current_tab = "history"  # "history" or "archived"
        self._current_project: Optional[str] = None  # 当前过滤的项目
        self._search_filter: str = ""  # 搜索过滤文本

        # === 增量更新缓存 ===
        # session_id → _HistoryItemCard 缓存（避免重复创建 widget）
        self._cached_cards: Dict[str, _HistoryItemCard] = {}
        # file_path → _ArchivedItemCard 缓存
        self._cached_archived: Dict[str, _ArchivedItemCard] = {}
        # 最近一次显示的历史会话 ID 集合（用于检测变化）
        self._last_displayed_ids: set = set()

        # 拼音缓存：session_id → {"pinyin": str, "initials": str}
        self._pinyin_cache: Dict[str, Dict[str, str]] = {}

        # worktree 分支名缓存：worktree_path → branch_name（避免重复调 git）
        self._worktree_branch_cache: Dict[str, str] = {}

        # === 搜索防抖 ===
        from PyQt5.QtCore import QTimer

        self._search_debounce_timer = QTimer(self)
        self._search_debounce_timer.setSingleShot(True)
        self._search_debounce_timer.setInterval(200)  # 200ms 防抖
        self._search_debounce_timer.timeout.connect(self._do_search)

        # === 分批渲染 ===
        self._render_queue: List[tuple] = []
        self._render_batch_index = 0
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(0)  # 下一个事件循环立即执行
        self._render_timer.timeout.connect(self._process_render_batch)
        self._batch_size = 30  # 每批渲染 30 个 widget（增大批次减少事件循环次数）

        # 分组标题 + 间隔线缓存（避免重复创建/销毁）
        self._cached_headers: Dict[str, _SectionHeader] = {}
        self._cached_spacers: List[QWidget] = []

        self._setup_ui()
        # 启用拖放支持
        self.setAcceptDrops(True)

        # 初始化时应用配置中的字体大小
        QTimer.singleShot(0, self._refresh_font_size)

    def _refresh_font_size(self):
        """刷新字体大小"""
        actual_size = get_ui_font_size()
        apply_font_size_to_widget(self, actual_size)

    def refresh_style(self):
        """刷新主题样式：更新所有分组标题的颜色"""
        Colors.refresh()
        for header in self.findChildren(_SectionHeader):
            header._apply_style()

    def _setup_ui(self):
        """不需要创建自己的布局，直接使用父控件的 scroll_area"""
        pass

    def set_current_project(self, project: str):
        """设置当前过滤项目"""
        self._current_project = project

    def _resolve_worktree_branch(self, worktree_path: str) -> str:
        """从 worktree 路径解析分支名（带缓存）"""
        if not worktree_path:
            return ""
        # 缓存命中
        cached = self._worktree_branch_cache.get(worktree_path)
        if cached is not None:
            return cached
        # 调用 git 获取分支名
        try:
            from app.utils.git_worktree import GitWorktreeDetector

            branch = GitWorktreeDetector.get_current_branch(worktree_path)
            if branch:
                self._worktree_branch_cache[worktree_path] = branch
                return branch
        except Exception:
            pass
        # 兜底：用目录名作为显示
        fallback = os.path.basename(worktree_path.rstrip("/\\"))
        self._worktree_branch_cache[worktree_path] = fallback
        return fallback

    def set_search_filter(self, text: str):
        """设置搜索过滤文本（带防抖 200ms）"""
        self._search_filter = text.strip()
        # 防抖：每次输入重启定时器，停止输入 200ms 后才触发刷新
        self._search_debounce_timer.stop()
        self._search_debounce_timer.start()

    def _do_search(self):
        """防抖超时后执行实际搜索刷新"""
        # 注意：不再清空 _pinyin_cache。会话标题/预览的拼音与 session_id 绑定，
        # 只要 session 存在，拼音结果就不变。删除会话时其缓存条目自然失效。
        # 这样每次搜索避免 O(n) 次 lazy_pinyin 重复计算。
        self._update_display()

    def get_content_layout(self) -> QVBoxLayout:
        """返回内容布局，供外部使用"""
        # 找到 BaseSettingsCard 的 content_layout
        parent = self.parent()
        while parent:
            if hasattr(parent, "content_layout"):
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
            session_date = datetime.datetime.strptime(last_time_str[:10], "%Y-%m-%d").date()
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
                month_names = [
                    "一月",
                    "二月",
                    "三月",
                    "四月",
                    "五月",
                    "六月",
                    "七月",
                    "八月",
                    "九月",
                    "十月",
                    "十一月",
                    "十二月",
                ]
                return month_names[session_date.month - 1]
            else:
                return f"{session_date.year}年"
        except (ValueError, TypeError):
            return "更早"

    def _clear_content(self):
        """清理内容区域（保留所有缓存的 widget，包括分组标题和间隔线）"""
        layout = self.get_content_layout()
        # 收集所有需要保留下来的 widget
        cached_set = set(id(w) for w in self._cached_cards.values())
        cached_set.update(id(w) for w in self._cached_archived.values())
        cached_set.update(id(w) for w in self._cached_headers.values())
        cached_set.update(id(w) for w in self._cached_spacers)

        while layout.count():
            item = layout.takeAt(0)
            if item.widget() and item.widget() != self:
                if id(item.widget()) in cached_set:
                    item.widget().hide()
                else:
                    item.widget().deleteLater()

    def set_history(self, history_list: List[Dict], current_index=None, clear_archived=False):
        """设置历史会话列表

        Args:
            history_list: 历史会话列表
            current_index: 当前会话索引
            clear_archived: 是否清理缓存的归档卡片（归档操作后调用，
                            下次切到归档标签时重新加载）
        """
        self._all_history = history_list
        self._current_index = current_index
        if clear_archived:
            self._cached_archived.clear()
        if self._current_tab == "history":
            self._update_display()

    def remove_session_card(self, session_id: str) -> bool:
        """手术式删除单个历史会话卡片，避免全量刷新。

        直接从布局和缓存中移除指定 session_id 的卡片，
        同时更新 _all_history 数据。
        如果当前在归档标签页或该 session 不在显示列表中，则回退到全量刷新。

        Returns:
            True 表示成功手术式删除；False 表示需要调用方回退到全量刷新
        """
        if self._current_tab != "history":
            return False
        if self._search_filter:
            # 搜索模式下缓存/布局不一致，回退全量刷新
            return False

        # 从缓存中查找卡片
        card = self._cached_cards.get(session_id)
        if card is None:
            return False

        # 记录被删除会话的原始索引（用于后续修正 _current_index）
        removed_index = None
        for idx, s in enumerate(self._all_history):
            if s.get("session_id") == session_id:
                removed_index = idx
                break

        # 从布局中移除该卡片
        layout = self.get_content_layout()
        if layout is None:
            return False

        # 找到卡片在布局中的位置并移除
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item and item.widget() is card:
                layout.takeAt(i)
                break

        # 删除卡片 widget
        card.deleteLater()
        self._cached_cards.pop(session_id, None)

        # 从 _all_history 中移除该会话
        self._all_history = [s for s in self._all_history if s.get("session_id") != session_id]

        # 更新 _current_index：如果被删除的是当前会话，index 置 None；
        # 如果删除位置在当前会话之前，当前会话索引减 1
        if self._current_index is not None and removed_index is not None:
            if removed_index == self._current_index:
                self._current_index = None
            elif removed_index < self._current_index:
                self._current_index -= 1

        # 【关键修复】同步更新剩余缓存卡片的 _index，使其与 _all_history 中的新位置一致
        for new_idx, s in enumerate(self._all_history):
            sid = s.get("session_id", "")
            cached_card = self._cached_cards.get(sid)
            if cached_card is not None and cached_card._index != new_idx:
                cached_card._index = new_idx

        return True

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
        """逐步渲染：先准备数据队列，再分批创建 widget（避免一次创建全部导致 UI 冻结）"""
        self._render_timer.stop()
        self._render_queue.clear()

        layout = self.get_content_layout()
        content_widget = layout.parentWidget() if layout else None
        if content_widget:
            content_widget.setUpdatesEnabled(False)

        self._clear_content()

        # 快速阶段：只做数据分组/过滤，不创建任何 widget
        if self._current_tab == "history":
            self._prepare_history_render_queue()
        else:
            self._prepare_archived_render_queue()

        layout.addStretch(1)

        if content_widget:
            content_widget.setUpdatesEnabled(True)
            content_widget.repaint()

        # 分批渲染 widget
        # 关键修复：第一批也延迟到下一个事件循环执行，确保 _on_system_card_opened
        # 完成的输入区收缩布局已生效后再开始创建 widget，避免工具栏抖动。
        self._render_batch_index = 0
        QTimer.singleShot(0, self._process_render_batch)

    def _process_render_batch(self):
        """处理下一批渲染任务"""
        layout = self.get_content_layout()
        if layout is None:
            return

        queue = self._render_queue
        start = self._render_batch_index
        end = min(start + self._batch_size, len(queue))

        # content_widget 是 layout 的 parent（即 BaseSettingsCard 的 content_widget）
        parent_widget = layout.parentWidget() if layout else None
        suspend_repaint = bool(parent_widget) and (end - start) > 1

        if suspend_repaint:
            parent_widget.setUpdatesEnabled(False)

        for i in range(start, end):
            item = queue[i]
            item_type = item[0]

            if item_type == "header":
                section_name, count = item[1], item[2]
                # 复用或创建分组标题
                header = self._cached_headers.get(section_name)
                if header is None:
                    header = _SectionHeader(section_name, count, self)
                    self._cached_headers[section_name] = header
                else:
                    header.setText(f"{section_name} ({count})" if count else section_name)
                layout.insertWidget(layout.count() - 1, header)
                header.show()

            elif item_type == "spacer":
                # 从缓存池复用间隔线
                spacer = self._cached_spacers.pop() if self._cached_spacers else QWidget()
                spacer.setFixedHeight(8)
                layout.insertWidget(layout.count() - 1, spacer)
                spacer.show()

            elif item_type == "session":
                session, original_index, is_current = item[1], item[2], item[3]
                preview = session.get("preview", "")
                card = self._get_or_create_history_card(session, original_index, is_current, preview)
                layout.insertWidget(layout.count() - 1, card)
                card.show()

            elif item_type == "archived":
                session = item[1]
                card = self._get_or_create_archived_card(session)
                layout.insertWidget(layout.count() - 1, card)
                card.show()

            elif item_type == "empty":
                text = item[1]
                empty_label = QLabel(text)
                empty_label.setAlignment(Qt.AlignCenter)
                empty_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; padding: 16px;")
                layout.insertWidget(layout.count() - 1, empty_label)

        if suspend_repaint:
            parent_widget.setUpdatesEnabled(True)
            parent_widget.repaint()

        self._render_batch_index = end

        if self._render_batch_index < len(queue):
            self._render_timer.start()
        else:
            # 全部渲染完成
            self._prune_cached_spacers()
            self._refresh_font_size()

    def _get_or_create_history_card(
        self, session: Dict, index: int, is_current: bool, preview: str
    ) -> _HistoryItemCard:
        """获取或创建缓存的 _HistoryItemCard（增量复用关键）"""
        session_id = session.get("session_id", "")
        worktree_path = session.get("worktree_path", "") or ""
        worktree_branch = self._resolve_worktree_branch(worktree_path) if worktree_path else ""
        card = self._cached_cards.get(session_id)

        if card is not None:
            # 缓存命中 → 原地更新数据
            card.update_data(
                index=index,
                title=session.get("title", "新对话"),
                last_time=session.get("last_time", "未知"),
                message_count=session.get("message_count", 0),
                is_current=is_current,
                preview=preview,
                worktree_branch=worktree_branch,
            )
            # 确保信号连接正确（用新 index）
            try:
                card.sessionClicked.disconnect()
            except TypeError:
                pass
            try:
                card.deleteRequested.disconnect()
            except TypeError:
                pass
            try:
                card.renameRequested.disconnect()
            except TypeError:
                pass
            card.sessionClicked.connect(self._on_card_clicked)
            card.deleteRequested.connect(self._on_card_deleted)
            card.renameRequested.connect(self._on_card_renamed)
        else:
            # 缓存未命中 → 创建新卡片并缓存
            card = _HistoryItemCard(
                index=index,
                title=session.get("title", "新对话"),
                last_time=session.get("last_time", "未知"),
                message_count=session.get("message_count", 0),
                is_current=is_current,
                preview=preview,
                worktree_branch=worktree_branch,
                parent=self,
            )
            card.sessionClicked.connect(self._on_card_clicked)
            card.deleteRequested.connect(self._on_card_deleted)
            card.renameRequested.connect(self._on_card_renamed)
            card._session_id = session_id
            self._cached_cards[session_id] = card

        return card

    def _get_or_create_archived_card(self, session: Dict) -> _ArchivedItemCard:
        """获取或创建缓存的 _ArchivedItemCard"""
        file_path = session.get("path", "")
        card = self._cached_archived.get(file_path)

        if card is not None:
            card.update_data(
                file_path=file_path,
                title=session.get("title", "归档会话"),
                session_id=session.get("session_id", ""),
                last_time=session.get("last_time", session.get("saved_at", "未知")),
                message_count=session.get("message_count", 0),
                preview=session.get("preview", ""),
                project=session.get("project", ""),
            )
            # 重连信号
            try:
                card.restored.disconnect()
            except TypeError:
                pass
            try:
                card.permanentlyDeleted.disconnect()
            except TypeError:
                pass
            try:
                card.renameRequested.disconnect()
            except TypeError:
                pass
            card.restored.connect(self._on_archived_restored)
            card.permanentlyDeleted.connect(self._on_archived_deleted)
            card.renameRequested.connect(self._on_archived_renamed)
        else:
            card = _ArchivedItemCard(
                file_path=file_path,
                title=session.get("title", "归档会话"),
                session_id=session.get("session_id", ""),
                last_time=session.get("last_time", session.get("saved_at", "未知")),
                message_count=session.get("message_count", 0),
                preview=session.get("preview", ""),
                project=session.get("project", ""),
                parent=self,
            )
            card.restored.connect(self._on_archived_restored)
            card.permanentlyDeleted.connect(self._on_archived_deleted)
            card.renameRequested.connect(self._on_archived_renamed)
            self._cached_archived[file_path] = card

        return card

    def _prepare_history_render_queue(self):
        """准备历史会话渲染队列（只做数据分组，不创建 widget）"""
        queue = self._render_queue

        if not self._all_history:
            if self._search_filter:
                queue.append(("empty", f"没有找到匹配「{self._search_filter}」的会话"))
            else:
                queue.append(("empty", "暂无历史对话记录"))
            self._cleanup_orphan_history_cards(set())
            return

        visible_ids = set()
        current_session_widget = False
        current_matches_search = True

        if self._current_index is not None and 0 <= self._current_index < len(self._all_history):
            current_session = self._all_history[self._current_index]
            current_matches_search = not self._search_filter or _matches_search(
                current_session, self._search_filter, self._pinyin_cache
            )
            if current_matches_search:
                visible_ids.add(current_session.get("session_id", ""))
                current_session_widget = True
                queue.append(("header", "当前会话", 0))
                queue.append(("session", current_session, self._current_index, True))
                queue.append(("spacer",))

        other_sessions = [(i, s) for i, s in enumerate(self._all_history) if i != self._current_index]
        if self._search_filter:
            other_sessions = [
                (i, s) for i, s in other_sessions if _matches_search(s, self._search_filter, self._pinyin_cache)
            ]

        grouped = {}
        for original_index, session in other_sessions:
            category = self._get_date_category(session.get("last_time", ""))
            if category not in grouped:
                grouped[category] = []
            grouped[category].append((original_index, session))

        order = ["今天", "昨天", "本周", "上周", "本月"]
        month_names = [
            "一月",
            "二月",
            "三月",
            "四月",
            "五月",
            "六月",
            "七月",
            "八月",
            "九月",
            "十月",
            "十一月",
            "十二月",
        ]

        extra_sections = [k for k in grouped if k not in order and k != "更早"]
        year_groups = {}
        month_groups = []
        for key in extra_sections:
            (year_groups if key.endswith("年") else month_groups).append((key, grouped[key]))

        final_order = []
        for section in order:
            if section in grouped:
                final_order.append((section, grouped[section]))
        for section, sessions in month_groups:
            final_order.append((section, sessions))
        for year in sorted(year_groups.keys(), reverse=True):
            final_order.append((year, year_groups[year]))

        has_items = current_session_widget
        for section, sessions in final_order:
            if not sessions:
                continue
            has_items = True
            queue.append(("header", section, len(sessions)))
            for original_index, session in sessions:
                sid = session.get("session_id", "")
                visible_ids.add(sid)
                queue.append(("session", session, original_index, False))
            queue.append(("spacer",))

        if not has_items:
            if self._search_filter:
                queue.append(("empty", f"没有找到匹配「{self._search_filter}」的会话"))
            else:
                queue.append(("empty", "暂无历史对话记录"))

        self._cleanup_orphan_history_cards(visible_ids)

    def _cleanup_orphan_history_cards(self, active_ids: set):
        """清理不再显示的会话缓存卡片（搜索过滤时不清理，保留缓存）"""
        if self._search_filter:
            # 搜索过滤模式下，不清理未匹配的缓存卡片
            # 这样用户清除搜索时无需重建 widget
            return
        orphan_ids = set(self._cached_cards.keys()) - active_ids
        for sid in orphan_ids:
            card = self._cached_cards.pop(sid, None)
            if card:
                card.deleteLater()

    def _prepare_archived_render_queue(self):
        """准备归档会话渲染队列（只做数据分组，不创建 widget）"""
        queue = self._render_queue

        if not self._archived_sessions:
            queue.append(("empty", "暂无归档会话"))
            self._cleanup_orphan_archived_cards(set())
            return

        sessions_to_show = self._archived_sessions
        if self._search_filter:
            sessions_to_show = [
                s for s in self._archived_sessions if _matches_search(s, self._search_filter, self._pinyin_cache)
            ]

        if not sessions_to_show:
            queue.append(("empty", f"没有找到匹配「{self._search_filter}」的会话"))
            self._cleanup_orphan_archived_cards(set())
            return

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
        for category, sessions in grouped.items():
            if category not in order:
                final_order.append((category, sessions))

        has_items = False
        active_paths = set()
        for section, sessions in final_order:
            if not sessions:
                continue
            has_items = True
            queue.append(("header", section, len(sessions)))
            for session in sessions:
                file_path = session.get("path", "")
                active_paths.add(file_path)
                queue.append(("archived", session))
            queue.append(("spacer",))

        if not has_items:
            queue.append(("empty", "暂无归档会话"))

        self._cleanup_orphan_archived_cards(active_paths)

    def _prune_cached_spacers(self):
        """回收多余的间隔线缓存"""
        max_spacers = 20
        while len(self._cached_spacers) > max_spacers:
            spacer = self._cached_spacers.pop()
            spacer.deleteLater()

    def _cleanup_orphan_archived_cards(self, active_paths: set):
        """清理不再显示的归档缓存卡片（搜索过滤时不清理）"""
        if self._search_filter:
            return
        orphan_paths = set(self._cached_archived.keys()) - active_paths
        for fp in orphan_paths:
            card = self._cached_archived.pop(fp, None)
            if card:
                card.deleteLater()

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
                if url.isLocalFile() and url.toLocalFile().endswith(".json"):
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
                    if file_path.endswith(".json"):
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

            files, _ = QFileDialog.getOpenFileNames(self, "导入会话", "", "JSON 文件 (*.json)")
            if files:
                self._handle_import_files(files)

        return handle_import
