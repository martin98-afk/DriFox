# -*- coding: utf-8 -*-
"""
右侧工作台浮层（WorkbenchPanel）— 纯悬浮，不挤压窗口内部元素

形态：TabManagerWindow 的 child widget，几何贴主窗口右侧（标题栏下方），
不进任何 layout，与桌宠（PixelPetWidget）同模式。由标题栏「右侧边栏」
按钮 toggle 显隐。

三个内置区域：
- 任务：todowrite 工具回传的待办列表（窗口级），进度 + 状态条目 —— 置顶常驻，不进页签
- 产物：本会话 AI 写过的文件（file_recorder 会话级文件写入记录，按文件去重倒序）
- 记忆：嵌入 MemoryCardContent 完整长期记忆面板（条目/项目笔记/关键文档）

页签条复用顶栏 CustomTabButton 自绘风格；插件可通过
UIPluginRegistry.register_workbench_tab 注册新页签（见 sync_plugin_pages）。

数据由外部驱动（TabManagerWindow.refresh_workbench / MainWidget 推送），
面板自身不持有 backend 引用，便于测试与解耦。
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QEasingCurve, QPoint, QPropertyAnimation, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import TransparentToolButton
from qfluentwidgets import FluentIcon

from app.utils.design_tokens import BorderRadius, Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import get_font_family_css, get_icon
from app.widgets.custom_title_bar import CustomTabButton

# ── 尺寸常量 ──
PANEL_WIDTH_DEFAULT = 480  # 默认宽度
PANEL_WIDTH_MIN = 320  # 左缘拖拽最小宽
PANEL_WIDTH_MAX = 820  # 左缘拖拽最大宽
DRAG_HANDLE_WIDTH = 6  # 左缘拖拽热区宽
SLIDE_DURATION_MS = 220  # 展开/折叠滑动动画时长


def _relative_time(created_at: str) -> str:
    """把 "YYYY-MM-DD HH:MM:SS" 转成友好相对时间（解析失败返回原文）"""
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except TypeError, ValueError:
        return created_at or ""
    delta = datetime.now() - dt
    seconds = delta.total_seconds()
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    if seconds < 86400 * 7:
        return f"{int(seconds // 86400)} 天前"
    return dt.strftime("%m-%d %H:%M")


class _EmptyHint(QLabel):
    """页面空态提示"""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setWordWrap(True)
        self.refresh_style()

    def refresh_style(self) -> None:
        self.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(12)}; padding: 32px 16px;"
        )


class _SectionHeader(QFrame):
    """页签内容小节头：图标 + 标题 + 右侧统计/操作"""

    def __init__(self, title: str, icon_name: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchSectionHeader")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(6)
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(16, 16)
        self._icon_label.setScaledContents(True)
        self._icon_label.setVisible(bool(icon_name))
        if icon_name:
            self.set_icon_name(icon_name)
        self._title_label = QLabel(title, self)
        self._extra_label = QLabel("", self)  # 统计信息（右侧）
        layout.addWidget(self._icon_label)
        layout.addWidget(self._title_label)
        layout.addStretch(1)
        layout.addWidget(self._extra_label)
        self.refresh_style()

    def set_icon_name(self, icon_name: str) -> None:
        self._icon_label.setPixmap(get_icon(icon_name).pixmap(16, 16))

    def set_extra(self, text: str) -> None:
        self._extra_label.setText(text)

    def refresh_style(self) -> None:
        self.setStyleSheet(
            "QFrame#workbenchSectionHeader { background: transparent; border: none; }"
            f" QLabel {{ color: {Colors.TEXT_SECONDARY}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(12)}; }}"
        )


class ArtifactsPage(QWidget):
    """产物页：本会话 AI 写过的文件（按文件去重，最近操作在前）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._header = _SectionHeader("产物", "根目录", self)
        layout.addWidget(self._header)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }\n" + get_unified_scrollbar_style(6)
        )
        self._list_wrap = QWidget()
        self._list_wrap.setStyleSheet("background: transparent;")
        self._scroll.setWidget(self._list_wrap)
        self._list_layout = QVBoxLayout(self._list_wrap)
        self._list_layout.setContentsMargins(0, 0, 2, 0)
        self._list_layout.setSpacing(2)
        self._empty_hint = _EmptyHint("本次会话暂无产物\n\nAI 写入或编辑过的文件会出现在这里", self._list_wrap)
        self._list_layout.addWidget(self._empty_hint)
        self._list_layout.addStretch(1)
        layout.addWidget(self._scroll, 1)
        self.refresh_style()

    # ── 数据 ──

    def set_operations(self, operations: List[Dict[str, Any]]) -> None:
        """渲染文件操作记录（按 file_path 去重，保留最新一次；倒序展示）"""
        while self._list_layout.count() > 1:  # 末尾是 stretch
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None and w is not self._empty_hint:
                w.deleteLater()
        # 去重：file_path → 最近一次操作
        latest: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for op in operations or []:
            fp = op.get("file_path") or ""
            if not fp:
                continue
            if fp not in latest:
                order.append(fp)
            latest[fp] = op
        ordered = [latest[fp] for fp in reversed(order)]  # 最新在前
        self._empty_hint.setVisible(not ordered)
        self._header.set_extra(f"{len(ordered)} 个文件" if ordered else "")
        for op in ordered:
            self._list_layout.addWidget(_ArtifactItem(op, self._list_wrap))

    def refresh_style(self) -> None:
        self._header.refresh_style()
        self._empty_hint.refresh_style()
        for i in range(self._list_layout.count()):
            w = self._list_layout.itemAt(i).widget()
            if isinstance(w, _ArtifactItem):
                w.refresh_style()


class _ArtifactItem(QFrame):
    """单条产物条目：文件名 + 工具/时间 + 路径；hover 显示打开按钮"""

    def __init__(self, op: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.setObjectName("artifactItem")
        self.setCursor(Qt.PointingHandCursor)
        self._file_path = op.get("file_path", "")
        self.setToolTip(self._file_path)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 5, 6, 5)
        layout.setSpacing(8)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        name = Path(self._file_path).name or self._file_path
        parent_dir = str(Path(self._file_path).parent)
        self._name_label = QLabel(name, self)
        self._name_label.setStyleSheet("font-weight: 600;")
        self._meta_label = QLabel(f"{op.get('tool_name', '')} · {_relative_time(op.get('created_at', ''))}", self)
        self._meta_label.setToolTip(parent_dir)
        text_col.addWidget(self._name_label)
        text_col.addWidget(self._meta_label)
        layout.addLayout(text_col, 1)

        self._open_btn = TransparentToolButton(FluentIcon.LINK, self)
        self._open_btn.setToolTip("打开文件")
        self._open_btn.setFixedSize(24, 24)
        self._open_btn.clicked.connect(self._open_file)
        self._folder_btn = TransparentToolButton(FluentIcon.FOLDER, self)
        self._folder_btn.setToolTip("打开所在目录")
        self._folder_btn.setFixedSize(24, 24)
        self._folder_btn.clicked.connect(self._open_folder)
        layout.addWidget(self._open_btn)
        layout.addWidget(self._folder_btn)
        self.refresh_style()

    def refresh_style(self) -> None:
        self.setStyleSheet(
            "QFrame#artifactItem {"
            f" background: {Colors.CARD_BG.format(alpha=120)};"
            f" border: 1px solid {Colors.BORDER};"
            " border-radius: 6px; }"
            "QFrame#artifactItem:hover {"
            f" background: {Colors.HOVER_BG};"
            f" border-color: {Colors.BORDER_ACCENT}; }}"
            f" QLabel {{ color: {Colors.TEXT_PRIMARY}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(12)}; }}"
        )
        # meta 行用次级色（setStyleSheet 会覆盖 name 的 font-weight，重设）
        self._name_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; font-weight: 600;"
            f" {get_font_family_css()} {font_size_css(12)};"
        )
        self._meta_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; background: transparent; {get_font_family_css()} {font_size_css(11)};"
        )

    def _open_file(self) -> None:
        """用系统默认程序打开文件（内联实现，对齐 diff_viewer_card 先例）"""
        import os
        import subprocess
        import sys

        if not self._file_path or not Path(self._file_path).exists():
            return
        try:
            if sys.platform == "win32":
                os.startfile(self._file_path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", self._file_path])  # noqa: S603,S607
            else:
                subprocess.Popen(["xdg-open", self._file_path])  # noqa: S603,S607
        except OSError:
            pass

    def _open_folder(self) -> None:
        import os
        import subprocess
        import sys

        parent = str(Path(self._file_path).parent)
        if not parent or not Path(parent).exists():
            return
        if sys.platform == "win32":
            os.startfile(parent)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", parent])  # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", parent])  # noqa: S603,S607


class TasksPage(QWidget):
    """任务页：todowrite 待办列表（进度 + 状态条目）"""

    _PRI_COLORS = {"high": "#ef4444", "medium": "#f59e0b", "low": "#3b82f6"}

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._header = _SectionHeader("任务", "todo", self)
        layout.addWidget(self._header)

        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFocusPolicy(Qt.NoFocus)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }\n" + get_unified_scrollbar_style(6)
        )
        self._list_wrap = QWidget()
        self._list_wrap.setStyleSheet("background: transparent;")
        self._scroll.setWidget(self._list_wrap)
        self._list_layout = QVBoxLayout(self._list_wrap)
        self._list_layout.setContentsMargins(0, 0, 2, 0)
        self._list_layout.setSpacing(2)
        self._empty_hint = _EmptyHint("暂无任务\n\nAI 使用 todowrite 建立的任务列表会显示在这里", self._list_wrap)
        self._list_layout.addWidget(self._empty_hint)
        self._list_layout.addStretch(1)
        layout.addWidget(self._scroll, 1)
        self.refresh_style()

    def update_todos(self, todos: List[Dict[str, Any]]) -> None:
        """刷新任务列表（签名对齐 MessageCard.update_todo_list 的数据约定）"""
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None and w is not self._empty_hint:
                w.deleteLater()
        todos = list(todos or [])
        done = sum(1 for t in todos if (t.get("status") or "pending") == "completed")
        self._empty_hint.setVisible(not todos)
        self._header.set_extra(f"{done}/{len(todos)} 已完成" if todos else "")
        for t in todos:
            status = (t.get("status") or "pending") if isinstance(t, dict) else "pending"
            content = (t.get("content") or "") if isinstance(t, dict) else str(t)
            priority = ((t.get("priority") or "medium") if isinstance(t, dict) else "medium") or "medium"
            self._list_layout.addWidget(self._make_item(status, content, priority))

    def _make_item(self, status: str, content: str, priority: str) -> QFrame:
        frame = QFrame(self._list_wrap)
        frame.setObjectName("taskItem")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(8)
        if status == "completed":
            mark, color, weight = "✓", "#3fb950", "normal"
        elif status == "in_progress":
            mark, color, weight = "⟳", "#f59e0b", "700"
        else:
            mark = "○"
            color = self._PRI_COLORS.get(priority, self._PRI_COLORS["medium"])
            weight = "normal"
        mark_label = QLabel(mark, frame)
        mark_label.setFixedWidth(14)
        mark_label.setAlignment(Qt.AlignCenter)
        mark_label.setStyleSheet(f"color: {color}; font-weight: 700; background: transparent;")
        content_label = QLabel(content, frame)
        content_label.setWordWrap(True)
        line = "text-decoration: line-through;" if status == "completed" else ""
        content_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; {line}"
            f" font-weight: {weight}; {get_font_family_css()} {font_size_css(12)};"
        )
        layout.addWidget(mark_label)
        layout.addWidget(content_label, 1)
        frame.setStyleSheet(
            "QFrame#taskItem {"
            f" background: {Colors.CARD_BG.format(alpha=120)};"
            f" border: 1px solid {Colors.BORDER};"
            " border-radius: 6px; }"
        )
        return frame

    def refresh_style(self) -> None:
        self._header.refresh_style()
        self._empty_hint.refresh_style()
        # 条目为自绘局部样式（状态色），主题切换时整体重建由下次 update_todos 承担
        for i in range(self._list_layout.count()):
            w = self._list_layout.itemAt(i).widget()
            if isinstance(w, QFrame):
                w.setStyleSheet(
                    "QFrame#taskItem {"
                    f" background: {Colors.CARD_BG.format(alpha=120)};"
                    f" border: 1px solid {Colors.BORDER};"
                    " border-radius: 6px; }"
                )


class MemoryPage(QWidget):
    """记忆页：懒构建并嵌入 MemoryCardContent（完整长期记忆面板）

    内置「条目记忆 / 项目笔记 / 关键文档」子页签（复用顶栏 CustomTabButton
    风格）。原 MemoryCardContent 本身没有页签 UI（页签在弹窗头部），
    嵌入工作台时必须自带，否则无法切到笔记/文档。
    """

    SUB_TABS = (("entries", "条目记忆"), ("notes", "项目笔记"), ("docs", "关键文档"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self._content: Optional[QWidget] = None
        self._memory_manager = None
        self._project: str = ""
        self._workdir: Optional[str] = None
        self._sub_buttons: List[CustomTabButton] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)

        # 子页签条（CustomTabButton 风格，与顶栏统一）
        self._tabs_layout = QHBoxLayout()
        self._tabs_layout.setSpacing(2)
        for tab_id, label in self.SUB_TABS:
            btn = CustomTabButton(tab_id, label, self)
            btn.clicked.connect(self._on_sub_tab_clicked)
            self._tabs_layout.addWidget(btn)
            self._sub_buttons.append(btn)
        self._tabs_layout.addStretch(1)
        self._layout.addLayout(self._tabs_layout)

        self._hint = _EmptyHint("长期记忆未就绪", self)
        self._layout.addWidget(self._hint)
        self._set_sub_tab_active(0)

    def _on_sub_tab_clicked(self, tab_id: str) -> None:
        """子页签点击：切换到 MemoryCardContent 对应页"""
        if self._content is None:
            return
        try:
            self._content.switch_tab(tab_id)
        except Exception:
            return
        for i, btn in enumerate(self._sub_buttons):
            btn.set_active(btn.tab_id == tab_id)

    def _set_sub_tab_active(self, index: int) -> None:
        for i, btn in enumerate(self._sub_buttons):
            btn.set_active(i == index)

    def ensure_built(self, memory_manager: Any) -> None:
        """注入 memory_manager 并懒构建内容（避免初始化期拉起存储层）"""
        self._memory_manager = memory_manager
        if self._content is None and memory_manager is not None:
            from app.widgets.cards.settings.memory_card import MemoryCardContent

            self._hint.hide()
            self._content = MemoryCardContent(memory_manager, self)
            self._layout.addWidget(self._content, 1)
            if self._project:
                self._content.set_project(self._project, self._workdir)
            # 初始停留在条目记忆页签
            self._set_sub_tab_active(0)

    def set_project(self, project: str, workdir: Optional[str] = None) -> None:
        self._project = project or ""
        self._workdir = workdir
        if self._content is not None:
            self._content.set_project(self._project, self._workdir)

    def refresh_style(self) -> None:
        self._hint.refresh_style()
        for btn in self._sub_buttons:
            btn.refresh_style()
        if self._content is not None and hasattr(self._content, "refresh_style"):
            self._content.refresh_style()


class WorkbenchPanel(QWidget):
    """右侧工作台浮层容器

    使用方式（TabManagerWindow）：
        panel = WorkbenchPanel(self)
        panel.resize(PANEL_WIDTH_DEFAULT, height)
        panel.move(width - panel.width, titleBar.height())
        panel.show() / panel.hide()

    主题：theme_manager.register_refresh_target(panel) → refresh_style()
    """

    close_requested = pyqtSignal()
    refresh_requested = pyqtSignal()

    TAB_ARTIFACTS, TAB_MEMORY = 0, 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchPanel")
        # 自行管理背景（圆角卡片），不继承父窗口透明
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setFixedWidth(PANEL_WIDTH_DEFAULT)

        self._dragging = False
        self._drag_start_x = 0
        self._drag_start_width = PANEL_WIDTH_DEFAULT
        self._anim: Optional[QPropertyAnimation] = None  # 滑入/滑出动画（非 None 表示动画中）

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 8, 8)
        root.setSpacing(6)

        # ── 头部：标题 + 刷新 + 关闭 ──
        header = QHBoxLayout()
        header.setSpacing(4)
        self._title_label = QLabel("工作台", self)
        self._title_label.setStyleSheet("font-weight: 700;")
        header.addWidget(self._title_label)
        header.addStretch(1)
        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC, self)
        self._refresh_btn.setToolTip("刷新产物与任务")
        self._refresh_btn.setFixedSize(24, 24)
        self._refresh_btn.clicked.connect(self.refresh_requested.emit)
        header.addWidget(self._refresh_btn)
        self._close_btn = TransparentToolButton(FluentIcon.CLOSE, self)
        self._close_btn.setFixedSize(24, 24)
        self._close_btn.clicked.connect(self.close_requested.emit)
        header.addWidget(self._close_btn)
        root.addLayout(header)

        # ── 任务区：置顶常驻（不进页签，类任务清单）──
        self.tasks_page = TasksPage(self)
        self.tasks_page.setMaximumHeight(200)
        root.addWidget(self.tasks_page)

        # ── 页签条：复用顶栏 CustomTabButton 自绘风格 ──
        self._tab_bar_layout = QHBoxLayout()
        self._tab_bar_layout.setSpacing(2)
        root.addLayout(self._tab_bar_layout)
        self._tab_buttons: List[CustomTabButton] = []
        self._tab_ids: List[str] = []

        # ── 内容栈 ──
        self._stack = QStackedWidget(self)
        self.artifacts_page = ArtifactsPage(self._stack)
        self.memory_page = MemoryPage(self._stack)
        self._stack.addWidget(self.artifacts_page)
        self._stack.addWidget(self.memory_page)
        root.addWidget(self._stack, 1)

        # 插件页签：{page_id: widget}，按注册表 reconcile（见 sync_plugin_pages）
        self._plugin_widgets: Dict[str, QWidget] = {}
        self._plugin_infos: Dict[str, Any] = {}
        self._plugin_sig: Optional[tuple] = None

        self._rebuild_tab_bar()
        self.set_current_tab(self.TAB_ARTIFACTS)
        self.refresh_style()

    # ── 页签条构建（内置 + 插件，顺序与 stack 一致） ──

    def _tab_specs(self) -> List[tuple]:
        """内置页签 + 插件页签（顺序与 QStackedWidget 保持一致）"""
        specs: List[tuple] = [("artifacts", "产物"), ("memory", "记忆")]
        for page_id, info in self._plugin_infos.items():
            specs.append((page_id, info.label))
        return specs

    def _rebuild_tab_bar(self) -> None:
        """按当前插件注册表重建页签条（内置页签在前，插件按注册序在后）"""
        for btn in self._tab_buttons:
            self._tab_bar_layout.removeWidget(btn)
            btn.hide()
            btn.deleteLater()
        self._tab_buttons = []
        self._tab_ids = []
        for tab_id, label in self._tab_specs():
            btn = CustomTabButton(tab_id, label, self)
            btn.clicked.connect(self._on_tab_clicked)
            self._tab_bar_layout.addWidget(btn)
            self._tab_buttons.append(btn)
            self._tab_ids.append(tab_id)
        self._tab_bar_layout.addStretch(1)

    def _on_tab_clicked(self, tab_id: str) -> None:
        """页签点击：按 tab_id 定位 stack 索引（内置 0/1，插件 2+）"""
        idx = self._tab_id_index(tab_id)
        if idx is not None:
            self.set_current_tab(idx)

    def _tab_id_index(self, tab_id: str) -> Optional[int]:
        if tab_id == "artifacts":
            return self.TAB_ARTIFACTS
        if tab_id == "memory":
            return self.TAB_MEMORY
        try:
            return self.TAB_MEMORY + 1 + list(self._plugin_infos.keys()).index(tab_id)
        except ValueError:
            return None

    # ── 插件页签 reconcile（宿主在 refresh_workbench 时调用） ──

    def sync_plugin_pages(self, tabs: List[Any]) -> None:
        """按 UIPluginRegistry 的 workbench_tabs 增删插件页（签名不变则跳过重建）"""
        infos = {t.page_id: t for t in (tabs or [])}
        sig = tuple((t.page_id, t.label) for t in (tabs or []))
        if sig == self._plugin_sig and set(infos.keys()) == set(self._plugin_widgets.keys()):
            return
        self._plugin_infos = infos
        # 当前页是否是被卸载的插件页（卸载后 Qt 会自动切到邻近页，需回落产物页）
        cur = self._stack.currentWidget()
        was_plugin_current = cur is not None and any(cur is w for w in self._plugin_widgets.values())
        # 卸载已注销页
        for page_id in list(self._plugin_widgets.keys()):
            if page_id not in infos:
                self._destroy_plugin_page(page_id)
        # 挂载新页
        for page_id, info in infos.items():
            if page_id not in self._plugin_widgets:
                self._mount_plugin_page(info)
        self._plugin_sig = sig
        current = self._stack.currentIndex()
        self._rebuild_tab_bar()
        # 当前页被移除（插件页）或越界时回落到产物页
        if was_plugin_current or current >= self._stack.count():
            current = self.TAB_ARTIFACTS
        self.set_current_tab(current)

    def _mount_plugin_page(self, info: Any) -> None:
        """构建并挂载插件页 widget（构造 parent + context，兼容无 context 的老签名）"""
        context = {}
        try:
            parent_win = self.parentWidget()
            if parent_win is not None and hasattr(parent_win, "_build_ui_context"):
                context = parent_win._build_ui_context()
        except Exception:
            context = {}
        try:
            widget = info.widget_class(parent=self._stack, context=context)
        except TypeError:
            widget = info.widget_class(parent=self._stack)
        if widget is None:
            return
        self._stack.addWidget(widget)
        self._plugin_widgets[info.page_id] = widget

    def _destroy_plugin_page(self, page_id: str) -> None:
        """销毁插件页 widget（显式隐藏 + 移除布局，避免残影）"""
        widget = self._plugin_widgets.pop(page_id, None)
        if widget is not None:
            widget.hide()
            self._stack.removeWidget(widget)
            widget.deleteLater()

    # ── 展开折叠动画（沿 x 轴滑入滑出，右缘固定贴窗口边） ──

    @property
    def is_sliding(self) -> bool:
        """滑入/滑出动画进行中（宿主 reposition 需跳过，避免打架）"""
        return self._anim is not None

    def slide_in(self) -> None:
        """从窗口右缘外滑入到位（宿主负责 show 前重定位与数据刷新）"""
        parent = self.parentWidget()
        if parent is None:
            self.show()
            return
        self._stop_slide()
        start = QPoint(parent.width(), self.y())
        end = QPoint(max(0, parent.width() - self.width()), self.y())
        self.move(start)
        self.show()
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(SLIDE_DURATION_MS)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.finished.connect(self._on_slide_done)
        self._anim.start()

    def slide_out(self) -> None:
        """滑出窗口右缘外，动画结束自动 hide（实例保留，再次开启零重建）"""
        parent = self.parentWidget()
        if parent is None or not self.isVisible():
            self.hide()
            return
        self._stop_slide()
        start = self.pos()
        end = QPoint(parent.width(), self.y())
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(SLIDE_DURATION_MS)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.setEasingCurve(QEasingCurve.InCubic)
        self._anim.finished.connect(self._on_slide_out_done)
        self._anim.start()

    def _on_slide_done(self) -> None:
        self._anim = None

    def _on_slide_out_done(self) -> None:
        self._anim = None
        self.hide()

    def _stop_slide(self) -> None:
        """停掉进行中的动画（断开信号防误触 hide；toggle 快速切换时调用）"""
        if self._anim is not None:
            try:
                self._anim.finished.disconnect()
            except TypeError:
                pass
            self._anim.stop()
            self._anim.deleteLater()
            self._anim = None

    # ── 页签 ──

    def set_current_tab(self, index: int) -> None:
        """切换页签；记忆页首次进入时构建（由宿主先调 ensure_memory）"""
        self._stack.setCurrentIndex(index)
        for i, btn in enumerate(self._tab_buttons):
            btn.set_active(i == index)

    def current_tab(self) -> int:
        return self._stack.currentIndex()

    def ensure_memory(self, memory_manager: Any) -> None:
        """懒构建记忆页（切到记忆页签时由宿主调用）"""
        self.memory_page.ensure_built(memory_manager)

    # ── 数据入口（宿主驱动） ──

    def update_artifacts(self, operations: List[Dict[str, Any]]) -> None:
        self.artifacts_page.set_operations(operations)

    def update_todos(self, todos: List[Dict[str, Any]]) -> None:
        self.tasks_page.update_todos(todos)

    def update_project(self, project: str, workdir: Optional[str] = None) -> None:
        self.memory_page.set_project(project, workdir)

    # ── 主题 ──

    def refresh_style(self) -> None:
        Colors.refresh()
        self.setStyleSheet(
            f"QWidget#workbenchPanel {{ background: {Colors.CARD_BG_SOLID};"
            f" border-left: 1px solid {Colors.BORDER};"
            f" border-top-left-radius: {BorderRadius.LG};"
            f" border-bottom-left-radius: {BorderRadius.LG}; }}"
        )
        self._title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(14)}; font-weight: 700;"
        )
        for btn in self._tab_buttons:
            btn.refresh_style()
        self.artifacts_page.refresh_style()
        self.tasks_page.refresh_style()
        self.memory_page.refresh_style()

    # ── 左缘拖拽调宽 ──

    def _in_drag_zone(self, x: int) -> bool:
        """左缘 DRAG_HANDLE_WIDTH 内为拖拽热区（面板自身 contentsMargins 含 10px 缓冲）"""
        return x <= self.contentsMargins().left() + DRAG_HANDLE_WIDTH

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._in_drag_zone(event.pos().x()):
            self._stop_slide()  # 拖拽与动画互斥
            self._dragging = True
            self._drag_start_x = event.globalX()
            self._drag_start_width = self.width()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            # 向左拖 → 变宽（面板贴右缘，宽度 = 起始宽 + 起始光标x - 当前光标x）
            new_width = self._drag_start_width + (self._drag_start_x - event.globalX())
            new_width = max(PANEL_WIDTH_MIN, min(PANEL_WIDTH_MAX, new_width))
            if new_width != self.width():
                self.setFixedWidth(new_width)
                self._emit_geometry_change()
            event.accept()
            return
        # hover 热区时显示水平调整光标
        self.setCursor(Qt.SizeHorCursor if self._in_drag_zone(event.pos().x()) else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _emit_geometry_change(self) -> None:
        """宽度变化后通知宿主重定位（面板右缘固定贴窗口右边）"""
        parent = self.parentWidget()
        if parent is not None and hasattr(parent, "reposition_workbench"):
            parent.reposition_workbench()
