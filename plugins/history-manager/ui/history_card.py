# -*- coding: utf-8 -*-
"""历史会话卡片 - 当前会话列表 + 归档会话列表

归属 ``history-manager`` 插件（原 ``app/widgets/cards/settings/history_card.py``）。
通用辅助 ``format_relative_time`` / ``get_message_preview`` 已上移到
``app.utils.session_preview``（消息卡片等非历史模块也复用），此处仅转引，
保持 ``from .history_card import get_message_preview`` 的旧导入路径可用。
"""

import datetime
import json
import os
from typing import Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QThread, pyqtSlot
from PyQt5.QtGui import QColor, QDragEnterEvent
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
    QFrame,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    MaskDialogBase,
    PrimaryPushButton,
    PushButton,
    SimpleCardWidget,
    TransparentToolButton,
)

from app.widgets.elided_label import _ElidedLabel

# ── 分页常量 ──
_PAGE_SIZE = 30  # 每页显示的会话数（含当前会话）

# 项目过滤器哨兵值（HistoryPage 项目切换器语义）
_PROJECT_ALL = "__all__"  # 全部项目混合视图


def split_pinned_entries(entries: List[tuple], exclude_index: Optional[int] = None) -> tuple:
    """拆分置顶/非置顶条目（纯函数，供置顶分组与单测使用）

    Args:
        entries: [(original_index, session)]；session 需含 pinned 字段（缺省 False）
        exclude_index: 剔除的 original_index（当前会话已在顶部独立区显示，需排除防重复）

    Returns:
        (pinned, rest)：pinned 组内按 last_time 降序；rest 保持入参相对顺序
    """
    pinned = [(i, s) for i, s in entries if i != exclude_index and s.get("pinned")]
    rest = [(i, s) for i, s in entries if i == exclude_index or not s.get("pinned")]
    pinned.sort(key=lambda x: x[1].get("last_time", ""), reverse=True)
    return pinned, rest


from app.utils.design_tokens import (
    Colors,
    apply_font_size_to_widget,
    font_size_css,
    get_ui_font_size,
    scale_font_size,
)
from app.utils.utils import get_font_family_css, get_icon
from app.utils.session_preview import format_relative_time, get_message_preview  # noqa: F401  (get_message_preview 转引)


class _UrlImportThread(QThread):
    """后台线程：从 URL 拉取并校验导入内容，避免最长 30s 的同步请求冻结 UI。

    finished 信号携带 (content, error)，二者有且仅有一个非空。
    """

    finished = pyqtSignal(str, str)

    def __init__(self, url: str):
        super().__init__()
        self._url = url

    def run(self):
        try:
            import requests

            resp = requests.get(self._url, timeout=30)
            resp.raise_for_status()
            content = resp.text
            # 验证是否是有效的 JSON
            json.loads(content)
            self.finished.emit(content, "")
        except json.JSONDecodeError:
            self.finished.emit("", "URL内容不是有效的JSON格式")
        except Exception as e:
            self.finished.emit("", f"无法从URL获取数据: {e}")


def _matches_search(session: Dict, search_text: str, pinyin_cache: dict = None) -> bool:
    """检查会话是否匹配搜索文本（支持拼音搜索）

    Args:
        session: 会话数据
        search_text: 搜索文本
        pinyin_cache: 拼音缓存字典 {session_id: {"pinyin": str, "initials": str}}，
                      传入后可避免重复计算
    """
    # 内存治理(#21-B)：pypinyin 惰性导入，避免模块加载即常驻拼音词典内存
    from pypinyin import lazy_pinyin

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


class _HistoryItemCard(QFrame):
    """历史会话条目（行式）：双行文本 + 右侧时间，hover 时时间换操作按钮"""

    sessionClicked = pyqtSignal(int)
    deleteRequested = pyqtSignal(int)
    pinToggleRequested = pyqtSignal(int, bool)  # (index, 目标状态)
    moveToProjectRequested = pyqtSignal(int, str)  # (index, 目标项目)

    def __init__(
        self,
        index: int,
        title: str,
        last_time: str,
        message_count: int,
        is_current: bool,
        preview: str = "",
        worktree_branch: str = "",
        pinned: bool = False,
        project: str = "",
        show_project: bool = False,
        menu_provider=None,
        parent=None,
    ):
        super().__init__(parent)
        self._index = index
        self._is_current = is_current
        self._is_editing = False
        self._session_id = None  # 用于缓存匹配
        self._worktree_branch = worktree_branch
        self._pinned = pinned
        self._project = project
        self._menu_provider = menu_provider
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("historyItemCard")

        # 批量读取颜色 token 和字体尺寸
        Colors.refresh()
        self._font_family = get_font_family_css()
        self._font_size = scale_font_size(13)
        self._caption_size = scale_font_size(11)

        h = QHBoxLayout(self)
        h.setContentsMargins(8, 4, 6, 4)
        h.setSpacing(6)

        body = QVBoxLayout()
        body.setSpacing(1)
        self._body = body

        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        prefix = "📌 " if pinned else ""
        self.title_label = _ElidedLabel(f"{prefix}{title}", self)
        self.title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-weight: bold; font-size: {self._font_size}px;"
            f" background: transparent; {self._font_family}"
            if is_current
            else f"color: {Colors.TEXT_PRIMARY}; font-size: {self._font_size}px;"
            f" background: transparent; {self._font_family}"
        )
        title_row.addWidget(self.title_label, 1)

        # worktree 分支标记（沿用既有语义：仅非主分支显示）
        self._branch_label = CaptionLabel("", self)
        self._branch_label.setStyleSheet(
            f"color: {Colors.ACCENT_WARM}; background-color: {Colors.TAB_ACTIVE_BG};"
            f" border-radius: 3px; padding: 0px 4px; font-size: {self._caption_size - 1}px; {self._font_family}"
        )
        self._branch_label.setVisible(bool(worktree_branch))
        if worktree_branch:
            self._branch_label.setText(f"🌿 {worktree_branch}")
        title_row.addWidget(self._branch_label, 0)

        # 项目标签（仅「全部项目」视图显示）
        self._project_label = CaptionLabel("", self)
        self._project_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background-color: {Colors.HOVER_BG};"
            f" border-radius: 3px; padding: 0px 4px; font-size: {self._caption_size - 1}px; {self._font_family}"
        )
        title_row.addWidget(self._project_label, 0)
        body.addLayout(title_row)

        # 预览行
        self._preview_label: Optional[_ElidedLabel] = None
        if preview:
            self._ensure_preview_label(preview)

        h.addLayout(body, 1)

        # 右侧相对时间（hover 时隐藏、换操作按钮）
        self.meta_label = CaptionLabel(format_relative_time(last_time), self)
        self.meta_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: {self._caption_size}px; {self._font_family}"
        )
        h.addWidget(self.meta_label, 0, Qt.AlignVCenter)

        # hover 浮现的操作按钮（与 meta_label 同位置互斥显隐）
        self._btns = QWidget(self)
        btns_layout = QHBoxLayout(self._btns)
        btns_layout.setContentsMargins(0, 0, 0, 0)
        btns_layout.setSpacing(0)
        self.pin_btn = TransparentToolButton(get_icon("置顶"), self._btns)
        self.pin_btn.setToolTip("取消置顶" if pinned else "置顶")
        self.pin_btn.setFixedSize(22, 22)
        self.pin_btn.clicked.connect(lambda: self.pinToggleRequested.emit(self._index, not self._pinned))
        btns_layout.addWidget(self.pin_btn)
        self.delete_btn = TransparentToolButton(get_icon("归档"), self._btns)
        self.delete_btn.setToolTip("归档")
        self.delete_btn.setFixedSize(22, 22)
        self.delete_btn.clicked.connect(lambda: self.deleteRequested.emit(self._index))
        btns_layout.addWidget(self.delete_btn)
        self._btns.hide()
        h.addWidget(self._btns, 0, Qt.AlignVCenter)

        self._apply_style()
        self._update_project_label(show_project)

    # ── 样式 ──

    def _apply_style(self):
        """行式样式：当前会话左色条 + 淡底；普通行透明，hover 微底色"""
        Colors.refresh()
        if self._is_current:
            self.setStyleSheet(
                f"QFrame#historyItemCard {{ background-color: {Colors.SELECTED_BG};"
                f" border-left: 3px solid {Colors.TEXT_ACCENT}; border-radius: 4px; }}"
                f"QFrame#historyItemCard:hover {{ background-color: {Colors.TAB_ACTIVE_BG}; }}"
            )
        else:
            self.setStyleSheet(
                "QFrame#historyItemCard { background-color: transparent; border: none; border-radius: 4px; }"
                f"QFrame#historyItemCard:hover {{ background-color: {Colors.HOVER_BG}; }}"
            )

    def _update_project_label(self, show_project: bool):
        """项目小标签显隐（仅全部项目视图且有项目名时显示）"""
        text = f"📁 {self._project}" if show_project and self._project else ""
        self._project_label.setText(text)
        self._project_label.setVisible(bool(text))

    def _ensure_preview_label(self, text: str):
        """预览行（标题行下方独立一行，空文本时隐藏）"""
        if self._preview_label is None:
            self._preview_label = _ElidedLabel("", self)
            self._preview_label.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; font-size: {self._caption_size}px; {self._font_family}"
            )
            self._body.addWidget(self._preview_label)
        self._preview_label.setText(text)
        self._preview_label.setVisible(bool(text))

    # ── hover：时间 ↔ 操作按钮互斥 ──

    def enterEvent(self, event):  # noqa: N802 (Qt 命名)
        if not self._is_editing:
            self.meta_label.hide()
            self._btns.show()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 (Qt 命名)
        self._btns.hide()
        self.meta_label.show()
        super().leaveEvent(event)

    # ── 右键菜单（菜单构建委托 HistoryCard 注入的 provider，项目列表集中持有） ──

    def contextMenuEvent(self, event):
        if self._menu_provider is not None:
            self._menu_provider(self, event.globalPos())
            event.accept()
        else:
            super().contextMenuEvent(event)

    # ── 数据更新 ──

    def update_data(
        self,
        index: int,
        title: str,
        last_time: str,
        message_count: int,
        is_current: bool,
        preview: str = "",
        worktree_branch: str = "",
        pinned: bool = False,
        project: str = "",
        show_project: bool = False,
    ):
        """原地更新卡片数据（增量复用关键路径，字段级 diff 避免 QSS 重设）"""
        self._index = index

        # 标题变化（含置顶前缀变化）
        prefix_changed = self._pinned != pinned
        self._pinned = pinned
        if getattr(self.title_label, "_full_text", "") != title or prefix_changed:
            self.title_label.setText(f"{'📌 ' if pinned else ''}{title}")

        # 活跃状态变化 → 重设样式
        if self._is_current != is_current:
            self._is_current = is_current
            self._apply_style()
            if is_current:
                self.title_label.setStyleSheet(
                    f"color: {Colors.TEXT_PRIMARY}; font-weight: bold; font-size: {self._font_size}px;"
                    f" {self._font_family}"
                )
            else:
                self.title_label.setStyleSheet(
                    f"color: {Colors.TEXT_PRIMARY}; font-size: {self._font_size}px; {self._font_family}"
                )

        # 元信息变化
        self.meta_label.setText(format_relative_time(last_time))

        # worktree 分支变化
        self._worktree_branch = worktree_branch
        self._branch_label.setText(f"🌿 {worktree_branch}" if worktree_branch else "")
        self._branch_label.setVisible(bool(worktree_branch))

        # 项目标签变化
        self._project = project
        self._update_project_label(show_project)

        # 置顶按钮提示跟随状态
        self.pin_btn.setToolTip("取消置顶" if pinned else "置顶")

        # 预览变化
        self._ensure_preview_label(preview)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._is_editing:
            self.sessionClicked.emit(self._index)
        super().mousePressEvent(event)


class _ArchivedItemCard(QFrame):
    """归档会话条目（行式，与 _HistoryItemCard 同构）：恢复 / 彻底删除"""

    restored = pyqtSignal(str)  # 文件路径
    permanentlyDeleted = pyqtSignal(str)  # 文件路径

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
        self.setObjectName("archivedItemCard")

        h = QHBoxLayout(self)
        h.setContentsMargins(8, 4, 6, 4)
        h.setSpacing(6)

        body = QVBoxLayout()
        body.setSpacing(1)
        self._body = body

        title_row = QHBoxLayout()
        title_row.setSpacing(4)
        self.title_label = _ElidedLabel(f"📦 {title}", self)
        self.title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; font-size: {scale_font_size(13)}px;"
            f" background: transparent; {get_font_family_css()}"
        )
        title_row.addWidget(self.title_label, 1)

        # 项目标签（归档会话显示原项目）
        self._project_label = CaptionLabel("", self)
        self._project_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background-color: {Colors.HOVER_BG};"
            f" border-radius: 3px; padding: 0px 4px; font-size: {scale_font_size(11)}px; {get_font_family_css()}"
        )
        title_row.addWidget(self._project_label, 0)
        body.addLayout(title_row)

        # 预览行
        self._preview_label: Optional[_ElidedLabel] = None
        if preview:
            self._init_preview_label(preview)

        h.addLayout(body, 1)

        # 右侧元信息（相对时间 [+ 轮次]），hover 时隐藏换操作按钮
        caption_size = scale_font_size(11)
        rel_time = format_relative_time(last_time)
        meta_text = rel_time
        if message_count > 0:
            meta_text += f" · {message_count} 轮"
        self.meta_label = CaptionLabel(meta_text, self)
        self.meta_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: {caption_size}px; {get_font_family_css()}"
        )
        h.addWidget(self.meta_label, 0, Qt.AlignVCenter)

        # hover 浮现的操作按钮
        self._btns = QWidget(self)
        btns_layout = QHBoxLayout(self._btns)
        btns_layout.setContentsMargins(0, 0, 0, 0)
        btns_layout.setSpacing(0)
        self.delete_btn = TransparentToolButton(FluentIcon.DELETE, self._btns)
        self.delete_btn.setToolTip("彻底删除")
        self.delete_btn.setFixedSize(22, 22)
        self.delete_btn.clicked.connect(lambda: self.permanentlyDeleted.emit(self._file_path))
        btns_layout.addWidget(self.delete_btn)
        self._btns.hide()
        h.addWidget(self._btns, 0, Qt.AlignVCenter)

        self._apply_style()
        self._update_project_label()

    # ── 样式 ──

    def _apply_style(self):
        """行式样式：普通行透明 + hover 微底色"""
        Colors.refresh()
        self.setStyleSheet(
            "QFrame#archivedItemCard { background-color: transparent; border: none; border-radius: 4px; }"
            f"QFrame#archivedItemCard:hover {{ background-color: {Colors.HOVER_BG}; }}"
        )

    def _update_project_label(self):
        """项目标签显隐（有项目名才显示）"""
        text = f"📁 {self._project}" if self._project else ""
        self._project_label.setText(text)
        self._project_label.setVisible(bool(text))

    def _init_preview_label(self, text: str):
        """预览行（标题行下方独立一行）"""
        self._preview_label = _ElidedLabel(text, self)
        self._preview_label.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: {scale_font_size(11)}px; {get_font_family_css()}"
        )
        self._body.addWidget(self._preview_label)

    # ── hover：时间 ↔ 操作按钮互斥 ──

    def enterEvent(self, event):  # noqa: N802 (Qt 命名)
        if not self._is_editing:
            self.meta_label.hide()
            self._btns.show()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 (Qt 命名)
        self._btns.hide()
        self.meta_label.show()
        super().leaveEvent(event)

    # ── 数据更新 ──

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
        """原地更新归档条目数据"""
        self._file_path = file_path
        self._session_id = session_id

        if getattr(self.title_label, "_full_text", "") != title:
            self.title_label.setText(f"📦 {title}")

        rel_time = format_relative_time(last_time)
        meta_text = rel_time
        if message_count > 0:
            meta_text += f" · {message_count} 轮"
        self.meta_label.setText(meta_text)

        # 预览更新
        if self._preview_label is None:
            if preview:
                self._init_preview_label(preview)
        else:
            self._preview_label.setText(preview)
            self._preview_label.setVisible(bool(preview))

        # 项目标签更新
        self._project = project
        self._update_project_label()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self._is_editing:
            # 单击也可以恢复会话
            self.restored.emit(self._file_path)
        super().mousePressEvent(event)


class _HistorySectionHeader(QLabel):
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


class _TeamGroupCard(CardWidget):
    """团队对话合并条目卡片 - 显示团队名 + 成员数 + 首问预览 + 恢复/归档按钮 + 成员展开

    方案 A（M4）：取消顶部团队分组区，团队会话在普通列表内按 run_id 合并为
    单一条目（数据层 merge_team=True 提供），此处渲染该条目：
    - 顶行：👥 团队名 + 恢复团队 + 归档按钮
    - 元信息行：N 位成员 · M 轮 · 相对时间
    - 预览行：团队首问（数据层 get_team_first_question 提供）
    - 展开区：点击卡片仅切换展开/收起（不再触发恢复）；展开后渲染成员行
      （角色胶囊 + 标题 + 相对时间），点击成员行 → memberSelected(session_record)
    """

    restoreRequested = pyqtSignal(str)  # run_id
    archiveRequested = pyqtSignal(str)  # run_id
    memberSelected = pyqtSignal(dict)  # 成员 session_record

    def __init__(self, group: Dict, parent=None):
        super().__init__(parent)
        # 兼容两种来源：set_team_groups 老格式（run_id）/ 合并条目新格式（team_run_id）
        self._run_id = group.get("run_id") or group.get("team_run_id") or ""
        self._members: List[Dict] = []
        self._members_visible = False
        self.setCursor(Qt.PointingHandCursor)

        Colors.refresh()
        _card_bg = Colors.CARD_BG
        _border = Colors.BORDER
        _text_primary = Colors.TEXT_PRIMARY
        _text_secondary = Colors.TEXT_SECONDARY
        _text_muted = Colors.TEXT_MUTED
        _accent = Colors.TEXT_ACCENT
        _tag_bg = Colors.TAB_ACTIVE_BG
        _ff = get_font_family_css()
        _body = scale_font_size(13)
        _caption = scale_font_size(11)

        self.setStyleSheet(f"""
            CardWidget {{
                background-color: {_card_bg.format(alpha=140)};
                border: 1px solid {_border};
                border-radius: 10px;
            }}
            CardWidget:hover {{
                border: 1px solid {_accent};
            }}
        """)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 8, 8, 8)
        self._layout.setSpacing(6)

        # 顶行：团队名 + 恢复 + 归档按钮
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        self.title_label = QLabel(f"👥 {group.get('team_name') or '团队对话'}", self)
        self.title_label.setStyleSheet(
            f"color: {_text_primary}; font-weight: bold; font-size: {_body}px; background: transparent; {_ff}"
        )
        top_row.addWidget(self.title_label, 1)

        self._last_time = group.get("last_time", "")

        archive_btn = TransparentToolButton(get_icon("归档"), self)
        archive_btn.setToolTip("归档该团队")
        archive_btn.setFixedSize(24, 24)
        archive_btn.clicked.connect(lambda: self.archiveRequested.emit(self._run_id))
        top_row.addWidget(archive_btn, 0)

        restore_btn = PrimaryPushButton("恢复团队", self)
        restore_btn.setFixedHeight(26)
        restore_btn.setStyleSheet(f"""
            PrimaryPushButton {{
                color: white;
                background-color: {Colors.INFO};
                border: none;
                border-radius: 6px;
                padding: 2px 14px;
                font-size: {_caption}px;
                {_ff}
            }}
            PrimaryPushButton:hover {{
                background-color: {Colors.SEND_BTN_END};
            }}
        """)
        restore_btn.setCursor(Qt.PointingHandCursor)
        restore_btn.clicked.connect(lambda: self.restoreRequested.emit(self._run_id))
        top_row.addWidget(restore_btn, 0)

        self._layout.addLayout(top_row)

        # 元信息行：N 位成员 · M 轮
        self.meta_label = CaptionLabel("", self)
        self.meta_label.setStyleSheet(
            f"color: {_text_secondary}; font-size: {_caption}px; background: transparent; {_ff}"
        )
        self._layout.addWidget(self.meta_label)

        # 预览行（首问预览，复用 _HistoryItemCard 的预览样式）
        self._preview_label: Optional[_ElidedLabel] = None
        self._ensure_preview_label(group.get("preview", "") or "")

        # 展开区容器：成员行列表（懒创建）
        self._members_container: Optional[QWidget] = None
        self._members_layout: Optional[QVBoxLayout] = None

        self.update_group(group)

    def _ensure_preview_label(self, text: str):
        """确保存在预览标签（独立一行，样式与 _HistoryItemCard 一致）"""
        if self._preview_label is None:
            self._preview_label = _ElidedLabel("", self)
            self._preview_label.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; font-style: italic; font-size: {scale_font_size(11)}px; "
                f"{get_font_family_css()}"
            )
            self._layout.addWidget(self._preview_label)
        self._preview_label.setText(text)
        self._preview_label.setVisible(bool(text))

    def _ensure_members_container(self):
        """懒创建成员展开容器"""
        if self._members_container is not None:
            return
        self._members_container = QWidget(self)
        self._members_layout = QVBoxLayout(self._members_container)
        self._members_layout.setContentsMargins(0, 0, 0, 0)
        self._members_layout.setSpacing(4)
        self._layout.addWidget(self._members_container)

    def _toggle_members(self):
        """切换成员展开/收起"""
        self._members_visible = not self._members_visible
        if not self._members_visible:
            if self._members_container is not None:
                self._members_container.hide()
            return
        self._ensure_members_container()
        self._rebuild_member_rows()
        self._members_container.show()

    def _rebuild_member_rows(self):
        """重建成员行（展开时渲染；成员列表变化时增量刷新）"""
        if self._members_layout is None:
            return
        # 清空旧成员行（先 setParent(None) 摘除，确保 findChildren 立即不再命中）
        while self._members_layout.count():
            item = self._members_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                try:
                    w.setParent(None)
                except Exception:
                    pass
                w.deleteLater()
        if not self._members:
            empty = CaptionLabel("（无成员会话）", self._members_container)
            empty.setStyleSheet(
                f"color: {Colors.TEXT_MUTED}; font-size: {scale_font_size(11)}px; background: transparent; "
                f"{get_font_family_css()}"
            )
            self._members_layout.addWidget(empty)
            return
        _ff = get_font_family_css()
        _caption = scale_font_size(11)
        _accent = Colors.TEXT_ACCENT
        _tag_bg = Colors.TAB_ACTIVE_BG
        _text_secondary = Colors.TEXT_SECONDARY
        _text_primary = Colors.TEXT_PRIMARY
        for member in self._members:
            row = QWidget(self._members_container)
            row.setCursor(Qt.PointingHandCursor)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 2, 2, 2)
            row_layout.setSpacing(6)

            agent = member.get("agent_name") or ""
            # T3：同角色多成员（两个 build 异 window_id）→ 胶囊加 window_id 后缀
            # 区分（如 build·w01，window_id 前缀 win_ 简写为 w）
            _wid = member.get("window_id") or ""
            capsule_text = f"{agent}·{_wid.replace('win_', 'w')}" if _wid else (agent or "?")
            capsule = QLabel(capsule_text, row)
            capsule.setStyleSheet(f"""
                QLabel {{
                    color: {_accent};
                    background-color: {_tag_bg};
                    border-radius: 8px;
                    padding: 1px 8px;
                    font-size: {_caption}px;
                    {_ff}
                }}
            """)
            row_layout.addWidget(capsule, 0)

            title = member.get("title") or member.get("name") or "团队对话"
            title_label = QLabel(title, row)
            title_label.setStyleSheet(
                f"color: {_text_primary}; font-size: {_caption}px; background: transparent; {_ff}"
            )
            row_layout.addWidget(title_label, 1)

            mt = member.get("last_time") or member.get("saved_at") or ""
            if mt:
                rel = format_relative_time(mt)
                time_label = QLabel(rel, row)
                time_label.setStyleSheet(
                    f"color: {_text_secondary}; font-size: {_caption}px; background: transparent; {_ff}"
                )
                row_layout.addWidget(time_label, 0)

            member_record = dict(member)
            row.mousePressEvent = lambda event, rec=member_record, w=row: self._on_member_row_clicked(event, rec, w)
            self._members_layout.addWidget(row)

    def _on_member_row_clicked(self, event, member_record: Dict, row: QWidget):
        """成员行点击 → 发射 memberSelected（携带成员 session_record）"""
        if event.button() == Qt.LeftButton:
            self.memberSelected.emit(member_record)

    def update_group(self, group: Dict):
        """增量刷新团队合并条目（团队名/时间/元信息/预览/成员列表）"""
        self._run_id = group.get("run_id", self._run_id)
        team_name = group.get("team_name") or "团队对话"
        self.title_label.setText(f"👥 {team_name}")

        self._last_time = group.get("last_time", "") or ""

        member_count = group.get("member_count", len(group.get("agent_names") or []))
        message_count = group.get("message_count", 0)
        meta_text = f"{member_count} 位成员 · {message_count} 轮"
        if self._last_time:
            meta_text += f" · {format_relative_time(self._last_time)}"
        self.meta_label.setText(meta_text)

        preview = group.get("preview", "") or ""
        self._ensure_preview_label(preview)

        # 成员列表：展开区数据刷新
        # 🛡️ 兼容旧格式（set_team_groups 传 run_id/agent_names，无 members）：
        # 从 agent_names 兜底构造最小成员记录，保证展开区至少显示成员名
        members = group.get("members") or []
        if not members:
            members = [{"agent_name": a} for a in (group.get("agent_names") or []) if a]
        self._members = [dict(m) for m in members if isinstance(m, dict)]
        if self._members_visible:
            self._rebuild_member_rows()

    def mousePressEvent(self, event):
        # 点击卡片空白区域仅切换成员展开/收起（不再触发恢复；恢复走按钮）
        if event.button() == Qt.LeftButton:
            self._toggle_members()
        super().mousePressEvent(event)


class HistoryCard(QWidget):
    """历史会话卡片内容 - 支持历史会话和归档会话切换"""

    sessionSelected = pyqtSignal(int)
    sessionArchived = pyqtSignal(int)
    refreshRequested = pyqtSignal()
    sessionImported = pyqtSignal(dict)  # 导入会话时发出
    sessionRestored = pyqtSignal(str)  # 恢复归档会话
    sessionPermanentlyDeleted = pyqtSignal(str)  # 彻底删除归档会话
    teamRestoreRequested = pyqtSignal(str)  # 恢复团队会话（参数 = run_id）
    teamArchiveRequested = pyqtSignal(str)  # 归档团队会话（参数 = run_id）
    memberSelected = pyqtSignal(dict)  # 团队成员 session_record 被选中进入会话
    pinToggled = pyqtSignal(int, bool)  # 右键置顶/取消置顶
    moveToProjectRequested = pyqtSignal(int, str)  # 右键移动到项目
    dataChanged = pyqtSignal()  # 列表渲染完成（页面刷新项目下拉等联动）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_history: List[Dict] = []
        self._current_index: Optional[int] = None
        self._archived_sessions: List[Dict] = []
        self._current_tab = "history"  # "history" or "archived"
        self._current_project: Optional[str] = None  # 当前过滤的项目
        self._search_filter: str = ""  # 搜索过滤文本

        # === 团队对话分组（方案 A：按 run_id 分组的历史会话） ===
        # 每项: {"run_id": str, "team_name": str, "agent_names": [str], "last_time": str}
        # 由 main_widget 从 history_list 的团队字段组装后注入
        self._team_groups: List[Dict] = []
        # run_id → 团队分组卡片缓存（避免重复创建 widget）
        self._cached_team_cards: Dict[str, QWidget] = {}

        # === 分页控制 ===
        self._page_size = _PAGE_SIZE
        self._show_limit = _PAGE_SIZE  # 当前最多显示的会话数
        self._remaining_count = 0  # 未显示的会话数
        self._total_session_count = 0  # 当前列表总会话数
        self._load_more_btn = None  # 加载更多按钮引用
        # 「加载更多」前的滚动位置（渲染完成后恢复；其他刷新路径保持 None）
        self._pending_scroll_restore: Optional[int] = None

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

        # 项目列表（右键「移动到项目」子菜单数据源，页面从 HistoryManager 拉取注入）
        self._project_list: List[str] = []
        # 行内项目标签显隐（仅「全部项目」视图为 True）
        self._show_project_labels = False
        # 条目右键菜单构建器（项目列表由本卡片集中持有）
        self._item_menu_provider = self._make_item_menu_provider()

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
        # [PERF] 原为 0ms：0 间隔的定时器在事件循环空闲时会连续抢占，
        # 历史列表上千条（30/批 → 数十批）期间主线程被批处理独占，
        # 输入法/滚动/流式渲染全部饿死（表现为打开历史面板时界面卡住）。
        # 改为 8ms：每批之间让出事件循环，整体仍在一两百毫秒内完成，
        # 但不再阻塞其它交互。
        self._render_timer.setInterval(8)
        self._render_timer.timeout.connect(self._process_render_batch)
        self._batch_size = 30  # 每批渲染 30 个 widget（增大批次减少事件循环次数）

        # 分组标题 + 间隔线缓存（避免重复创建/销毁）
        self._cached_headers: Dict[str, _HistorySectionHeader] = {}
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
        for header in self.findChildren(_HistorySectionHeader):
            header._apply_style()

    def _setup_ui(self):
        """不需要创建自己的布局，直接使用父控件的 scroll_area"""
        pass

    def set_current_project(self, project: str):
        """设置当前过滤项目"""
        self._current_project = project

    # ── 置顶 / 项目标签 / 右键菜单 ──

    def set_project_list(self, projects: List[str]):
        """注入项目列表（右键菜单「移动到项目」数据源，页面从 HistoryManager 拉取）"""
        self._project_list = list(projects or [])

    def get_project_list(self) -> List[str]:
        """从当前列表数据聚合 distinct 项目名（页面下拉兑底数据源）"""
        projects = {(s.get("project") or "默认项目").strip() or "默认项目" for s in self._all_history}
        return sorted(projects)

    def set_show_project_labels(self, show: bool):
        """设置行内项目标签显隐（仅全部项目视图为 True；变化时全量重渲染）"""
        if self._show_project_labels != show:
            self._show_project_labels = show
            self._update_display()

    def _make_item_menu_provider(self):
        """会话条目右键菜单构建器（置顶 / 移动到项目 / 归档）

        样式与 TabPanel.contextMenuEvent 一致（主题色插值，深浅自适配）。
        """

        def provider(card: "_HistoryItemCard", global_pos):
            from PyQt5.QtWidgets import QMenu

            menu_style = f"""
                QMenu {{
                    background: {Colors.CARD_BG};
                    border: 1px solid {Colors.BORDER};
                    border-radius: 6px;
                    padding: 4px;
                }}
                QMenu::item {{
                    padding: 6px 20px;
                    border-radius: 4px;
                    color: {Colors.TEXT_PRIMARY};
                    {get_font_family_css()} {font_size_css(13)}
                }}
                QMenu::item:selected {{
                    background: {Colors.HOVER_BG};
                }}
            """
            menu = QMenu(card)
            menu.setStyleSheet(menu_style)
            pin_text = "取消置顶" if card._pinned else "置顶"
            act_pin = menu.addAction(pin_text)
            move_menu = menu.addMenu("移动到项目")
            move_menu.setStyleSheet(menu_style)
            for proj in self._project_list:
                if proj and proj != card._project:
                    move_menu.addAction(proj, lambda p=proj: self.moveToProjectRequested.emit(card._index, p))
            menu.addSeparator()
            act_archive = menu.addAction("归档")
            chosen = menu.exec(global_pos)
            if chosen is act_pin:
                self.pinToggled.emit(card._index, not card._pinned)
            elif chosen is act_archive:
                card.deleteRequested.emit(card._index)

        return provider

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
        # 搜索框清空时恢复分页首屏（避免用户看到展开后的全部列表）
        if not self._search_filter:
            self._show_limit = self._page_size
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
        except ValueError, TypeError:
            return "更早"

    def _clear_content(self):
        """清理内容区域（保留所有缓存的 widget，包括分组标题和间隔线）"""
        layout = self.get_content_layout()
        # 收集所有需要保留下来的 widget
        cached_set = set(id(w) for w in self._cached_cards.values())
        cached_set.update(id(w) for w in self._cached_archived.values())
        cached_set.update(id(w) for w in self._cached_headers.values())
        cached_set.update(id(w) for w in self._cached_spacers)
        cached_set.update(id(w) for w in self._cached_team_cards.values())

        # 清理「加载更多」按钮引用（它不在缓存中，会被 deleteLater 清理）
        self._load_more_btn = None

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

    def set_team_groups(self, teams: List[Dict]):
        """设置团队对话分组数据（方案 A）

        # TODO: deprecated, remove with TestBuildTeamGroups
        # M4 混排后无业务调用方（main_widget 改用 get_history_list(merge_team=True)
        # 混排渲染），仅保留供旧测试引用。

        Args:
            teams: 按 run_id 分组的团队信息列表，每项:
                {"run_id": str, "team_name": str, "agent_names": [str],
                 "last_time": str, "session_count": int}
            由 main_widget 从 history_list 的团队字段组装后传入。
        """
        self._team_groups = list(teams or [])
        # 🛡️ 清理已不在新分组列表中的缓存 key：避免团队解散/分组消失后
        # 旧卡片仍残留缓存，下次渲染时复用过期成员胶囊。
        new_run_ids = {g.get("run_id", "") for g in self._team_groups}
        stale_keys = [k for k in self._cached_team_cards if k not in new_run_ids]
        for k in stale_keys:
            card = self._cached_team_cards.pop(k, None)
            if card is not None:
                try:
                    card.deleteLater()
                except Exception:
                    pass
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
        self._pinyin_cache.pop(session_id, None)

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

    def get_history_at_index(self, index: int) -> Optional[Dict]:
        """安全获取历史会话缓存在 index 位置的记录

        返回 _all_history 中缓存的轻量记录（不含 messages），
        供 main_widget 通过 session_id 加载完整数据。
        避免外部直接访问私有属性 _all_history。
        """
        if index < 0 or index >= len(self._all_history):
            return None
        return self._all_history[index]

    def set_archived_sessions(self, archived_list: List[Dict]):
        """设置归档会话列表"""
        self._archived_sessions = archived_list
        if self._current_tab == "archived":
            self._update_display()

    def switch_tab(self, tab: str):
        """切换标签页"""
        if self._current_tab != tab:
            self._current_tab = tab
            self._show_limit = self._page_size
            self._update_display()

    def _update_display(self):
        """逐步渲染：先准备数据队列，再分批创建 widget（避免一次创建全部导致 UI 冻结）"""
        self._render_timer.stop()
        self._render_queue.clear()
        self._remaining_count = 0

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
            # [PERF] 原此处在 setUpdatesEnabled(True) 后又调 repaint()：
            # setUpdatesEnabled(True) 本身已会调度一次 update()，额外的
            # repaint() 强制同步重绘整棵子树，本次渲染的内容还是空的
            # （widget 尚未创建），纯粹是白开销。
            content_widget.setUpdatesEnabled(True)

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
                    header = _HistorySectionHeader(section_name, count, self)
                    self._cached_headers[section_name] = header
                else:
                    header.setText(f"{section_name} ({count})" if count else section_name)
                layout.insertWidget(layout.count() - 1, header)
                header.show()

            elif item_type == "team_group":
                group = item[1]
                # 兼容两种来源：set_team_groups 老格式（run_id）/ 合并条目新格式（team_run_id）
                run_id = group.get("run_id") or group.get("team_run_id") or ""
                card = self._cached_team_cards.get(run_id)
                if card is None:
                    card = _TeamGroupCard(group, self)
                    card.restoreRequested.connect(self.teamRestoreRequested)
                    card.archiveRequested.connect(self.teamArchiveRequested)
                    card.memberSelected.connect(self._on_team_member_selected)
                    self._cached_team_cards[run_id] = card
                else:
                    # 🛡️ 缓存命中：增量刷新成员列表/元信息/预览（agent_names
                    # 可能已变化，如新成员加入/成员清理），避免 UI 与实际不一致
                    card.update_group(group)
                layout.insertWidget(layout.count() - 1, card)
                card.show()

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
                empty_label.setStyleSheet(
                    f"color: {Colors.TEXT_MUTED}; padding: 16px; {font_size_css(14)} {get_font_family_css()}"
                )
                layout.insertWidget(layout.count() - 1, empty_label)

        if suspend_repaint:
            # [PERF] 同上：去掉每批一次的强制同步 repaint()，交给事件循环合并重绘。
            parent_widget.setUpdatesEnabled(True)

        self._render_batch_index = end

        if self._render_batch_index < len(queue):
            self._render_timer.start()
        else:
            # 全部渲染完成
            self._prune_cached_spacers()
            self._add_load_more_if_needed(layout)
            self._refresh_font_size()
            self._restore_scroll_if_pending()
            # 通知页面（列表数据已就绪，页面据此刷新项目下拉等联动）
            self.dataChanged.emit()

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
                pinned=bool(session.get("pinned", False)),
                project=session.get("project", ""),
                show_project=self._show_project_labels,
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
                card.pinToggleRequested.disconnect()
            except TypeError:
                pass
            card.sessionClicked.connect(self._on_card_clicked)
            card.deleteRequested.connect(self._on_card_deleted)
            card.pinToggleRequested.connect(self.pinToggled)
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
                pinned=bool(session.get("pinned", False)),
                project=session.get("project", ""),
                show_project=self._show_project_labels,
                menu_provider=self._item_menu_provider,
                parent=self,
            )
            card.sessionClicked.connect(self._on_card_clicked)
            card.deleteRequested.connect(self._on_card_deleted)
            card.pinToggleRequested.connect(self.pinToggled)
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
            card.restored.connect(self._on_archived_restored)
            card.permanentlyDeleted.connect(self._on_archived_deleted)
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
            self._cached_archived[file_path] = card

        return card

    def _prepare_history_render_queue(self):
        """准备历史会话渲染队列（只做数据分组，不创建 widget）

        M4 混排：取消顶部团队分组区，团队合并条目（team_merged）与普通会话
        条目按 last_time 天然混排（数据层 merge_team=True 已合并，此处按
        session 条目统一渲染即可）。
        """
        queue = self._render_queue

        if not self._all_history:
            if self._search_filter:
                queue.append(("empty", f"没有找到匹配「{self._search_filter}」的会话"))
            else:
                queue.append(("empty", "暂无历史对话记录"))
            self._cleanup_orphan_history_cards(set())
            self._cleanup_orphan_team_cards(set())
            return

        visible_ids = set()
        active_run_ids = set()
        current_session_widget = False
        current_matches_search = True

        if self._current_index is not None and 0 <= self._current_index < len(self._all_history):
            current_session = self._all_history[self._current_index]
            current_matches_search = not self._search_filter or _matches_search(
                current_session, self._search_filter, self._pinyin_cache
            )
            if current_matches_search:
                if current_session.get("team_merged"):
                    active_run_ids.add(current_session.get("team_run_id", ""))
                else:
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

        # ── 置顶拆分：置顶组优先渲染（组内 last_time 降序），不占分页 limit ──
        pinned_entries, other_sessions = split_pinned_entries(other_sessions)

        grouped = {}
        for original_index, session in other_sessions:
            category = self._get_date_category(session.get("last_time", ""))
            if category not in grouped:
                grouped[category] = []
            grouped[category].append((original_index, session))

        order = ["今天", "昨天", "本周", "上周", "本月"]

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

        # ── 计算分页 ──
        total_other = sum(len(sessions) for _, sessions in final_order)
        self._total_session_count = len(self._all_history)
        limit = self._show_limit if not self._search_filter else total_other
        session_count = 0
        self._remaining_count = max(0, total_other - limit)

        has_items = current_session_widget

        # ── 置顶分组（当前会话区之后、日期分组之前；不受分页限制） ──
        if pinned_entries:
            has_items = True
            queue.append(("header", "置顶", len(pinned_entries)))
            for original_index, session in pinned_entries:
                if session.get("team_merged"):
                    continue  # 团队合并条目无置顶语义，防御跳过
                sid = session.get("session_id", "")
                visible_ids.add(sid)
                queue.append(("session", session, original_index, False))
            queue.append(("spacer",))

        for section, sessions in final_order:
            if not sessions:
                continue
            # 分页截断：已达到限制则跳过剩余分组
            if session_count >= limit:
                continue
            total_in_section = len(sessions)
            to_add = sessions[: limit - session_count] if session_count + total_in_section > limit else sessions
            if not to_add:
                continue
            has_items = True
            # header 显示该分组总会话数（而非仅可见数），让用户了解完整规模
            queue.append(("header", section, total_in_section))
            for original_index, session in to_add:
                if session.get("team_merged"):
                    # 团队合并条目：按 run_id 渲染团队卡（与普通条目同位置混排）
                    run_id = session.get("team_run_id", "")
                    if run_id:
                        active_run_ids.add(run_id)
                        queue.append(("team_group", session, original_index))
                else:
                    sid = session.get("session_id", "")
                    visible_ids.add(sid)
                    queue.append(("session", session, original_index, False))
            session_count += len(to_add)
            queue.append(("spacer",))

        if not has_items:
            if self._search_filter:
                queue.append(("empty", f"没有找到匹配「{self._search_filter}」的会话"))
            else:
                queue.append(("empty", "暂无历史对话记录"))

        self._cleanup_orphan_history_cards(visible_ids)
        self._cleanup_orphan_team_cards(active_run_ids)

    def _cleanup_orphan_team_cards(self, active_run_ids: set):
        """清理不再显示的团队合并条目缓存卡片（搜索过滤时不清理，保留缓存）"""
        if self._search_filter:
            return
        orphan_ids = set(self._cached_team_cards.keys()) - active_run_ids
        for run_id in orphan_ids:
            card = self._cached_team_cards.pop(run_id, None)
            if card is not None:
                try:
                    card.deleteLater()
                except Exception:
                    pass

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

        # ── 计算分页 ──
        total_archived = sum(len(sessions) for _, sessions in final_order)
        self._total_session_count = total_archived
        limit = self._show_limit if not self._search_filter else total_archived
        session_count = 0
        self._remaining_count = max(0, total_archived - limit)

        has_items = False
        active_paths = set()
        for section, sessions in final_order:
            if not sessions:
                continue
            # 分页截断
            if session_count >= limit:
                continue
            total_in_section = len(sessions)
            to_add = sessions[: limit - session_count] if session_count + total_in_section > limit else sessions
            if not to_add:
                continue
            has_items = True
            # header 显示该分组总会话数（而非仅可见数），让用户了解完整规模
            queue.append(("header", section, total_in_section))
            for session in to_add:
                file_path = session.get("path", "")
                active_paths.add(file_path)
                queue.append(("archived", session))
            session_count += len(to_add)
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

    # ── 加载更多 ──────────────────────────────────────────────

    def _add_load_more_if_needed(self, layout):
        """在列表底部添加「加载更多」按钮（stretch 之前）"""
        if self._remaining_count <= 0:
            self._remove_load_more_button()
            return
        if self._search_filter:
            return  # 搜索模式不显示加载更多（已显示全部结果）

        self._remove_load_more_button()

        total = self._total_session_count
        shown = total - self._remaining_count
        btn = PushButton(f"共 {total} 个会话，点击加载更多（已显示 {shown} 个）", self)
        btn.setStyleSheet(
            "PushButton { background: rgba(128,128,128,0.06); border: 1px solid rgba(128,128,128,0.15);"
            " border-radius: 8px; padding: 10px; color: rgba(128,128,128,0.6); }"
            "PushButton:hover { background: rgba(128,128,128,0.15); border-color: rgba(128,128,128,0.3);"
            " color: rgba(255,255,255,0.85); }"
        )
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(self._on_load_more)
        self._load_more_btn = btn
        # 插入到 stretch 之前
        count = layout.count()
        last_item = layout.itemAt(count - 1) if count > 0 else None
        if last_item and last_item.widget() is None:
            layout.insertWidget(count - 1, btn)
        else:
            layout.addWidget(btn)

    def _remove_load_more_button(self):
        """移除现有的「加载更多」按钮"""
        btn = self._load_more_btn
        if btn is None:
            return
        self._load_more_btn = None
        try:
            btn.clicked.disconnect()
        except TypeError:
            pass
        layout = self.get_content_layout()
        if layout is None:
            btn.deleteLater()
            return
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item and item.widget() is btn:
                layout.takeAt(i)
                break
        btn.deleteLater()

    def _on_load_more(self):
        """加载下一批会话"""
        # 🛡️ 保留滚动位置：_update_display 全清重建时内容瞬间清空，
        # widgetResizable 模式下 scrollbar range 收缩会把 value clamp 到 0，
        # 渲染完成后视口跳回顶部（此前每次点加载更多都弹回列表头）。
        # 加载更多只在尾部追加、头部内容不变，恢复原 value 即可保持视口。
        self._pending_scroll_restore = self._parent_scroll_vbar_value()
        self._show_limit += self._page_size
        self._update_display()

    def _parent_scroll_vbar_value(self) -> Optional[int]:
        """沿父链找宿主滚动区，返回当前垂直滚动值（找不到返回 None）"""
        parent = self.parent()
        while parent:
            scroll_area = getattr(parent, "_scroll_area", None)
            if scroll_area is not None:
                return scroll_area.verticalScrollBar().value()
            parent = parent.parent()
        return None

    def _restore_scroll_if_pending(self):
        """渲染完成后恢复「加载更多」前记录的滚动位置（延迟一拍等布局生效）"""
        if self._pending_scroll_restore is None:
            return
        target = self._pending_scroll_restore
        self._pending_scroll_restore = None

        def _apply():
            parent = self.parent()
            while parent:
                scroll_area = getattr(parent, "_scroll_area", None)
                if scroll_area is not None:
                    scroll_area.verticalScrollBar().setValue(target)
                    return
                parent = parent.parent()

        QTimer.singleShot(0, _apply)

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

    def _on_team_member_selected(self, member_record: dict):
        """团队合并条目成员行被点击 → 转发给 main_widget 进入该会话"""
        self.memberSelected.emit(member_record)

    def _on_card_deleted(self, index: int):
        self.sessionArchived.emit(index)

    def _on_archived_restored(self, file_path: str):
        """恢复归档会话"""
        self.sessionRestored.emit(file_path)

    def _on_archived_deleted(self, file_path: str):
        """彻底删除归档会话"""
        self.sessionPermanentlyDeleted.emit(file_path)

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
            dialog = ImportOptionDialog(parent=self.window())
            dialog.fileImportRequested.connect(self._on_import_from_file)
            dialog.urlImportRequested.connect(self._on_import_from_url)
            dialog.exec_()

        return handle_import

    def _on_import_from_file(self):
        """从文件导入"""
        from PyQt5.QtWidgets import QFileDialog

        files, _ = QFileDialog.getOpenFileNames(self, "导入会话", "", "JSON 文件 (*.json)")
        if files:
            self._handle_import_files(files)

    def _on_import_from_url(self):
        """从URL导入会话JSON"""
        from app.widgets.common_dialogs import SingleInputDialog

        dialog = SingleInputDialog(
            title="🔗 从URL导入会话",
            hint="请输入会话 JSON 文件的分享链接",
            placeholder="https://gitee.com/.../xxx.json",
            default_text="https://",
            confirm_text="导入",
            cancel_text="取消",
            parent=self.window(),
        )
        dialog.confirmed.connect(self._on_url_import_confirmed)
        dialog.exec_()

    def _on_url_import_confirmed(self, url: str):
        """URL确认后的导入处理（请求在后台线程执行，不阻塞 UI）"""
        url = url.strip()
        if not url:
            return

        # 补全协议前缀
        if not (url.startswith("http://") or url.startswith("https://")):
            url = "https://" + url

        # 在后台线程拉取并校验，避免最长 30s 同步请求冻结界面
        self._url_import_thread = _UrlImportThread(url)
        self._url_import_thread.finished.connect(self._on_url_import_result)
        self._url_import_thread.finished.connect(self._url_import_thread.deleteLater)
        self._url_import_thread.start()

    @pyqtSlot(str, str)
    def _on_url_import_result(self, content: str, error: str):
        """后台导入线程完成后的回调（主线程执行）"""
        if error:
            from qfluentwidgets import InfoBar, InfoBarPosition
            from app.widgets.tab_manager_window import TabManagerWindow

            # 挂到 tab 管理器顶层窗口（未就绪时兜底卡片所在窗口）
            bar_parent = TabManagerWindow.get_instance() or self.window()
            InfoBar.error(
                title="导入失败",
                content=error,
                duration=3000,
                position=InfoBarPosition.BOTTOM,
                parent=bar_parent,
            )
            return

        # 保存到临时文件，复用现有导入流程
        import tempfile

        tmp = tempfile.NamedTemporaryFile(
            suffix=".json", prefix="drifox_import_url_", delete=False, mode="w", encoding="utf-8"
        )
        tmp.write(content)
        tmp_path = tmp.name
        tmp.close()

        self._handle_import_files([tmp_path])


class ImportOptionDialog(MaskDialogBase):
    """导入选项弹框：从文件导入 / 从URL导入，与 SingleInputDialog 同款样式"""

    fileImportRequested = pyqtSignal()
    urlImportRequested = pyqtSignal()

    DEFAULT_WIDTH = 400
    DEFAULT_HEIGHT = 300

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        Colors.refresh()
        self.setShadowEffect(60, (0, 10), QColor(0, 0, 0, 100))
        self.setClosableOnMaskClicked(True)
        self.setDraggable(True)
        self.setMaskColor(QColor(0, 0, 0, 76))

        self.widget.setObjectName("importOptionDialogWidget")
        self.widget.setStyleSheet(f"""
            #importOptionDialogWidget {{
                background-color: {Colors.CONTENT_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self.widget)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        # 标题
        title_label = BodyLabel("📥 导入会话", self.widget)
        title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; {get_font_family_css()} {font_size_css(16)}"
        )
        layout.addWidget(title_label)

        # 从文件导入
        file_btn = QPushButton("📁  从文件导入", self.widget)
        file_btn.setCursor(Qt.PointingHandCursor)
        file_btn.setFixedHeight(56)
        file_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 8px 16px;
                text-align: left;
                {get_font_family_css()} {font_size_css(14)}
            }}
            QPushButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.INFO};
            }}
        """)
        file_btn.clicked.connect(lambda: self._on_choose("file"))
        layout.addWidget(file_btn)

        # 文件导入说明
        file_hint = CaptionLabel("选择本地 JSON 会话文件导入", self.widget)
        file_hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(10)}; padding-left: 4px;"
        )
        layout.addWidget(file_hint)

        # 从URL导入
        url_btn = QPushButton("🔗  从URL导入", self.widget)
        url_btn.setCursor(Qt.PointingHandCursor)
        url_btn.setFixedHeight(56)
        url_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 8px 16px;
                text-align: left;
                {get_font_family_css()} {font_size_css(14)}
            }}
            QPushButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.INFO};
            }}
        """)
        url_btn.clicked.connect(lambda: self._on_choose("url"))
        layout.addWidget(url_btn)

        # URL导入说明
        url_hint = CaptionLabel("输入会话 JSON 文件的分享链接", self.widget)
        url_hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(10)}; padding-left: 4px;"
        )
        layout.addWidget(url_hint)

        layout.addStretch()

        # 取消按钮
        cancel_btn = PushButton("取消", self.widget)
        cancel_btn.setStyleSheet(f"""
            PushButton {{
                background-color: {Colors.CARD_BG.format(alpha=180)};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px 20px;
                {font_size_css(12)}
            }}
            PushButton:hover {{
                background-color: {Colors.HOVER_BG};
                border-color: {Colors.BORDER_ACCENT};
            }}
        """)
        cancel_btn.clicked.connect(self.close)
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        self.widget.setFixedSize(self.DEFAULT_WIDTH, self.DEFAULT_HEIGHT)
        self._center_widget()

    def _center_widget(self):
        x = max(0, (self.width() - self.widget.width()) // 2)
        y = max(0, (self.height() - self.widget.height()) // 2)
        self.widget.move(x, y)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._center_widget()

    def _on_choose(self, choice: str):
        self.close()
        if choice == "file":
            self.fileImportRequested.emit()
        else:
            self.urlImportRequested.emit()
