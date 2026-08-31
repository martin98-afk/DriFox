# -*- coding: utf-8 -*-
"""
右侧工作台浮层（WorkbenchPanel）— 纯悬浮，不挤压窗口内部元素

形态：TabManagerWindow 的 child widget，几何贴主窗口右侧（标题栏下方），
不进任何 layout，与桌宠（PixelPetWidget）同模式。由标题栏「右侧边栏」
按钮 toggle 显隐，带滑入/滑出动画。

遮挡对抗：对话区消息卡片是 QWebEngineView（原生 HWND），普通 child widget
会被其文字盖住。本面板设 WA_NativeWindow + WA_DontCreateNativeAncestors
获得独立原生 HWND，与 WebEngine 同级，Z-order 由 Windows 管理，
raise/move 对其生效；背景用主题实色（content_bg）杜绝透字。
child widget（而非顶层窗口）保证与主窗口移动/缩放/最小化严格同步，
无跟随延迟、无残留。

布局（自上而下）：
- 头部：标题「工作台」+ 页签（产物/记忆）+ 刷新/关闭（紧凑间距）
- 任务坞（常驻置顶，可折叠）：todowrite 任务清单，类似独立任务列表，
  不随页签切换消失；无任务时整坞隐藏
- 页签内容：产物 / 记忆（嵌入 MemoryCardContent：条目/项目笔记/关键文档）

数据由外部驱动（TabManagerWindow.refresh_workbench / MainWidget 推送），
面板自身不持有 backend 引用，便于测试与解耦。
"""

from datetime import datetime
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import QPoint, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, TransparentToolButton

from app.utils.design_tokens import BorderRadius, Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import get_font_family_css, get_icon

# ── 尺寸常量 ──
PANEL_WIDTH_DEFAULT = 480  # 默认宽度
PANEL_WIDTH_MIN = 320  # 左缘拖拽最小宽
PANEL_WIDTH_MAX = 820  # 左缘拖拽最大宽
DRAG_HANDLE_WIDTH = 6  # 左缘拖拽热区宽
SLIDE_DURATION_MS = 220  # 展开/折叠滑动动画时长
SLIDE_FRAME_MS = 16  # 动画帧间隔（PreciseTimer，避免 Windows 默认 timer 精度抖动）
TASKS_DOCK_MAX_H = 170  # 任务坞内容区最大高（超出内滚）


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
            f" {get_font_family_css()} {font_size_css(12)}; padding: 24px 12px;"
        )


class _SectionHeader(QFrame):
    """小节头：图标 + 标题 + 折叠箭头 + 右侧统计；整行可点击（发射 clicked）"""

    clicked = pyqtSignal()

    def __init__(self, title: str, icon_name: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchSectionHeader")
        self.setCursor(Qt.PointingHandCursor)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(5)
        self._icon_label = QLabel(self)
        self._icon_label.setFixedSize(14, 14)
        self._icon_label.setScaledContents(True)
        self._icon_label.setVisible(bool(icon_name))
        if icon_name:
            self.set_icon_name(icon_name)
        self._title_label = QLabel(title, self)
        self._arrow_label = QLabel("▾", self)  # 折叠指示
        self._extra_label = QLabel("", self)  # 统计信息
        layout.addWidget(self._icon_label)
        layout.addWidget(self._title_label)
        layout.addWidget(self._arrow_label)
        layout.addStretch(1)
        layout.addWidget(self._extra_label)
        self.refresh_style()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def set_icon_name(self, icon_name: str) -> None:
        self._icon_label.setPixmap(get_icon(icon_name).pixmap(14, 14))

    def set_extra(self, text: str) -> None:
        self._extra_label.setText(text)

    def set_collapsed(self, collapsed: bool) -> None:
        self._arrow_label.setText("▸" if collapsed else "▾")

    def refresh_style(self) -> None:
        self.setStyleSheet(
            "QFrame#workbenchSectionHeader { background: transparent; border: none; }"
            f" QLabel {{ color: {Colors.TEXT_SECONDARY}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(12)}; }}"
        )


class TasksDock(QWidget):
    """任务坞（常驻置顶）：任务清单不进页签，可整行折叠，空任务时整坞隐藏

    类似 IDE 的 TODO 面板：header 显示进度（x/y 已完成），条目区固定高内滚。
    """

    _PRI_COLORS = {"high": "#ef4444", "medium": "#f59e0b", "low": "#3b82f6"}

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._header = _SectionHeader("任务", "todo", self)
        self._header.clicked.connect(self._toggle)
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
        self._list_layout.addStretch(1)
        layout.addWidget(self._scroll)
        self._collapsed = False
        self.hide()  # 空任务时整个坞隐藏

    # ── 数据 ──

    def update_todos(self, todos: List[Dict[str, Any]]) -> None:
        """刷新任务列表（签名对齐 MessageCard.update_todo_list 的数据约定）"""
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        todos = list(todos or [])
        done = sum(1 for t in todos if (t.get("status") or "pending") == "completed")
        self._header.set_extra(f"{done}/{len(todos)} 已完成" if todos else "")
        for t in todos:
            status = (t.get("status") or "pending") if isinstance(t, dict) else "pending"
            content = (t.get("content") or "") if isinstance(t, dict) else str(t)
            priority = ((t.get("priority") or "medium") if isinstance(t, dict) else "medium") or "medium"
            self._list_layout.insertWidget(self._list_layout.count() - 1, self._make_item(status, content, priority))
        self.setVisible(bool(todos))  # 无任务整个坞隐藏
        if not todos:
            self._collapsed = False
            self._scroll.setVisible(True)
            self._header.set_collapsed(False)

    def _make_item(self, status: str, content: str, priority: str) -> QFrame:
        frame = QFrame(self._list_wrap)
        frame.setObjectName("taskItem")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 4, 8, 4)
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
            f" border-radius: {BorderRadius.MD}; }}"
        )
        return frame

    # ── 折叠 ──

    def _toggle(self) -> None:
        self._collapsed = not self._collapsed
        self._scroll.setVisible(not self._collapsed)
        self._header.set_collapsed(self._collapsed)

    # ── 主题 ──

    def refresh_style(self) -> None:
        self._header.refresh_style()
        for i in range(self._list_layout.count()):
            w = self._list_layout.itemAt(i).widget()
            if isinstance(w, QFrame):
                w.setStyleSheet(
                    "QFrame#taskItem {"
                    f" background: {Colors.CARD_BG.format(alpha=120)};"
                    f" border: 1px solid {Colors.BORDER};"
                    f" border-radius: {BorderRadius.MD}; }}"
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
        for op in operations or []:
            fp = op.get("file_path") or ""
            if not fp:
                continue
            prev = latest.get(fp)
            if prev is None or str(op.get("created_at", "")) >= str(prev.get("created_at", "")):
                latest[fp] = op
        # 按 created_at 倒序（最新在前）；解析失败的排最后
        def _sort_key(op: Dict[str, Any]) -> datetime:
            try:
                return datetime.strptime(op.get("created_at", ""), "%Y-%m-%d %H:%M:%S")
            except (TypeError, ValueError):
                return datetime.min

        ordered = sorted(latest.values(), key=_sort_key, reverse=True)
        self._empty_hint.setVisible(not ordered)
        self._header.set_extra(f"{len(ordered)} 个文件" if ordered else "")
        # 🐛 必须插在末尾 stretch 之前：addWidget 会追加到 stretch 之后，
        # stretch 在顶部把所有条目压到面板底部（贴底 bug）
        for op in ordered:
            self._list_layout.insertWidget(self._list_layout.count() - 1, _ArtifactItem(op, self._list_wrap))

    def refresh_style(self) -> None:
        self._header.refresh_style()
        self._empty_hint.refresh_style()
        for i in range(self._list_layout.count()):
            w = self._list_layout.itemAt(i).widget()
            if isinstance(w, _ArtifactItem):
                w.refresh_style()


class _ArtifactItem(QFrame):
    """单条产物条目：文件名 + 工具/时间 + 路径；按钮打开文件/所在目录"""

    def __init__(self, op: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.setObjectName("artifactItem")
        self._file_path = op.get("file_path", "")
        self.setToolTip(self._file_path)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(6)

        text_col = QVBoxLayout()
        text_col.setSpacing(1)
        name = Path(self._file_path).name or self._file_path
        parent_dir = str(Path(self._file_path).parent)
        self._name_label = QLabel(name, self)
        self._meta_label = QLabel(f"{op.get('tool_name', '')} · {_relative_time(op.get('created_at', ''))}", self)
        self._meta_label.setToolTip(parent_dir)
        text_col.addWidget(self._name_label)
        text_col.addWidget(self._meta_label)
        layout.addLayout(text_col, 1)

        self._open_btn = TransparentToolButton(FluentIcon.LINK, self)
        self._open_btn.setToolTip("打开文件")
        self._open_btn.setFixedSize(22, 22)
        self._open_btn.clicked.connect(self._open_file)
        self._folder_btn = TransparentToolButton(FluentIcon.FOLDER, self)
        self._folder_btn.setToolTip("打开所在目录")
        self._folder_btn.setFixedSize(22, 22)
        self._folder_btn.clicked.connect(self._open_folder)
        layout.addWidget(self._open_btn)
        layout.addWidget(self._folder_btn)
        self.refresh_style()

    def refresh_style(self) -> None:
        self.setStyleSheet(
            "QFrame#artifactItem {"
            f" background: {Colors.CARD_BG.format(alpha=120)};"
            f" border: 1px solid {Colors.BORDER};"
            f" border-radius: {BorderRadius.MD}; }}"
            "QFrame#artifactItem:hover {"
            f" background: {Colors.HOVER_BG};"
            f" border-color: {Colors.BORDER_ACCENT}; }}"
        )
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
        try:
            if sys.platform == "win32":
                os.startfile(parent)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", parent])  # noqa: S603,S607
            else:
                subprocess.Popen(["xdg-open", parent])  # noqa: S603,S607
        except OSError:
            pass


class MemoryPage(QWidget):
    """记忆页：懒构建并嵌入 MemoryCardContent（条目/项目笔记/关键文档三页签）"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._content: Optional[QWidget] = None
        self._memory_manager = None
        self._project: str = ""
        self._workdir: Optional[str] = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._hint = _EmptyHint("长期记忆未就绪", self)
        self._layout.addWidget(self._hint)

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
            # 🐛 修复：MemoryCardContent 构造后条目列表是空的（数据只在
            # switch_tab/_add_entry 等路径加载），首次嵌入时手动拉一次
            if hasattr(self._content, "_load_entries"):
                self._content._load_entries()

    def set_project(self, project: str, workdir: Optional[str] = None) -> None:
        self._project = project or ""
        self._workdir = workdir
        if self._content is not None:
            self._content.set_project(self._project, self._workdir)

    def refresh_style(self) -> None:
        self._hint.refresh_style()
        if self._content is not None and hasattr(self._content, "refresh_style"):
            self._content.refresh_style()


class WorkbenchPanel(QWidget):
    """右侧工作台浮层容器（独立顶层无边框工具窗口）

    使用方式（TabManagerWindow）：
        panel = WorkbenchPanel(self)
        panel.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint)
        panel.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # 定位：宿主 reposition_workbench() 用屏幕坐标 move（贴主窗口右缘）
        panel.slide_in() / panel.slide_out()

    主题：theme_manager.register_refresh_target(panel) → refresh_style()
    """

    close_requested = pyqtSignal()
    refresh_requested = pyqtSignal()

    TAB_ARTIFACTS, TAB_MEMORY = 0, 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchPanel")
        # 实色背景（主题 content_bg）+ 自绘边框
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setFixedWidth(PANEL_WIDTH_DEFAULT)
        # 🛡️ 遮挡对抗：对话区消息卡片是 QWebEngineView（原生 HWND），普通
        # child widget 会被其文字盖住。独立原生 HWND 后与 WebEngine 同级，
        # Z-order 由 Windows 管理，raise/move 均生效；面板实色无圆角无
        # 半透明，native child window 不会出现黑角/透明失效。
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.setAttribute(Qt.WA_DontCreateNativeAncestors, True)

        self._dragging = False
        self._drag_start_x = 0
        self._drag_start_width = PANEL_WIDTH_DEFAULT
        self._anim: Optional[QTimer] = None  # 滑入/滑出动画 timer（非 None 表示动画中）

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 6, 6, 6)
        root.setSpacing(4)

        # ── 头部：标题 + 页签 + 刷新/关闭（紧凑间距） ──
        header = QHBoxLayout()
        header.setSpacing(2)
        self._title_label = QLabel("工作台", self)
        header.addWidget(self._title_label)
        header.addStretch(1)
        self._tab_buttons: List[QPushButton] = []
        for label, idx in (("产物", self.TAB_ARTIFACTS), ("记忆", self.TAB_MEMORY)):
            btn = QPushButton(label, self)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(22)
            btn.clicked.connect(lambda _=False, i=idx: self.set_current_tab(i))
            header.addWidget(btn)
            self._tab_buttons.append(btn)
        self._refresh_btn = TransparentToolButton(FluentIcon.SYNC, self)
        self._refresh_btn.setToolTip("刷新产物与任务")
        self._refresh_btn.setFixedSize(22, 22)
        self._refresh_btn.setIconSize(QSize(12, 12))
        self._refresh_btn.clicked.connect(self.refresh_requested.emit)
        header.addWidget(self._refresh_btn)
        self._close_btn = TransparentToolButton(FluentIcon.CLOSE, self)
        self._close_btn.setFixedSize(22, 22)
        self._close_btn.clicked.connect(self.close_requested.emit)
        header.addWidget(self._close_btn)
        root.addLayout(header)

        # ── 任务坞：常驻置顶（不进页签），空任务自动隐藏 ──
        self.tasks_dock = TasksDock(self)
        root.addWidget(self.tasks_dock)

        # ── 页签内容栈 ──
        self._stack = QStackedWidget(self)
        self.artifacts_page = ArtifactsPage(self._stack)
        self.memory_page = MemoryPage(self._stack)
        self._stack.addWidget(self.artifacts_page)
        self._stack.addWidget(self.memory_page)
        root.addWidget(self._stack, 1)

        self.set_current_tab(self.TAB_ARTIFACTS)
        self.refresh_style()

    # ── 页签 ──

    def set_current_tab(self, index: int) -> None:
        """切换页签；记忆页首次进入时由宿主调 ensure_memory 构建"""
        self._stack.setCurrentIndex(index)
        for i, btn in enumerate(self._tab_buttons):
            btn.setChecked(i == index)  # 选中态由 :checked 伪类 QSS 即时生效

    def current_tab(self) -> int:
        return self._stack.currentIndex()

    def ensure_memory(self, memory_manager: Any) -> None:
        """懒构建记忆页（切到记忆页签时由宿主调用）"""
        self.memory_page.ensure_built(memory_manager)

    # ── 数据入口（宿主驱动） ──

    def update_artifacts(self, operations: List[Dict[str, Any]]) -> None:
        self.artifacts_page.set_operations(operations)

    def update_todos(self, todos: List[Dict[str, Any]]) -> None:
        self.tasks_dock.update_todos(todos)

    def update_project(self, project: str, workdir: Optional[str] = None) -> None:
        self.memory_page.set_project(project, workdir)

    # ── 展开折叠动画（沿 x 轴滑入滑出，右缘固定贴主窗口边；屏幕坐标） ──

    @property
    def is_sliding(self) -> bool:
        """滑入/滑出动画进行中（宿主 reposition 需跳过，避免打架）"""
        return self._anim is not None

    def slide_in(self) -> None:
        """从主窗口右缘外滑入到位（宿主负责 show 前重定位与数据刷新）"""
        parent = self.parentWidget()
        if parent is None:
            self.show()
            return
        start = QPoint(parent.width(), self.y())
        end = QPoint(max(0, parent.width() - self.width()), self.y())
        self.move(start)
        self.show()
        self.raise_()
        self._start_slide(end.x(), self._on_slide_done)

    def slide_out(self) -> None:
        """滑出主窗口右缘外，动画结束自动 hide（实例保留，再次开启零重建）"""
        parent = self.parentWidget()
        if parent is None or not self.isVisible():
            self.hide()
            return
        end = QPoint(parent.width(), self.y())
        self._start_slide(end.x(), self._on_slide_out_done)

    def _on_slide_done(self) -> None:
        self._anim = None

    def _on_slide_out_done(self) -> None:
        self._anim = None
        self.hide()

    def _start_slide(self, end_x: int, on_done: Callable[[], None]) -> None:
        """滑动动画：PreciseTimer 16ms 手动 OutCubic 插值

        不用 QPropertyAnimation：其底层默认 timer 在 Windows 上精度差
        （~15.6ms 粒度 + 合并延迟），配合 native child window 的每帧
        位块传输容易跳帧；手动插值按真实耗时计算进度，帧间无漂移。
        """
        self._stop_slide()
        start_x = self.x()
        if start_x == end_x:
            on_done()
            return
        duration_s = SLIDE_DURATION_MS / 1000.0
        info = {"t0": time.monotonic(), "start_x": start_x}

        def _tick() -> None:
            t = min(1.0, (time.monotonic() - info["t0"]) / duration_s)
            eased = 1.0 - (1.0 - t) ** 3  # OutCubic
            self.move(round(info["start_x"] + (end_x - info["start_x"]) * eased), self.y())
            if t >= 1.0:
                self._stop_slide()
                on_done()

        timer = QTimer(self)
        timer.setTimerType(Qt.PreciseTimer)
        timer.setInterval(SLIDE_FRAME_MS)
        timer.timeout.connect(_tick)
        self._anim = timer  # 非 None 即 is_sliding（语义与旧实现一致）
        timer.start()

    def _stop_slide(self) -> None:
        """停掉进行中的动画（断开信号防误触 hide；toggle 快速切换时调用）"""
        if self._anim is not None:
            timer = self._anim
            self._anim = None
            try:
                timer.timeout.disconnect()
            except TypeError:
                pass
            timer.stop()
            timer.deleteLater()

    # ── 主题 ──

    def refresh_style(self) -> None:
        Colors.refresh()
        # 实色底（主题 content_bg）+ 左缘边线；native child window 无透明问题
        self.setStyleSheet(
            f"QWidget#workbenchPanel {{ background: {Colors.CONTENT_BG}; border-left: 1px solid {Colors.BORDER}; }}"
        )
        self._title_label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent;"
            f" {get_font_family_css()} {font_size_css(14)}; font-weight: 700;"
        )
        # 选中态用 :checked 伪类：setChecked 即时换肤，无需重建样式表
        for btn in self._tab_buttons:
            btn.setStyleSheet(
                "QPushButton {"
                f" color: {Colors.TEXT_SECONDARY};"
                " background: transparent;"
                " border: 1px solid transparent;"
                " border-radius: " + BorderRadius.SM + "; padding: 0 8px;"
                f" {get_font_family_css()} {font_size_css(12)}; }}"
                "QPushButton:checked {"
                " color: #ffffff;"
                f" background: {Colors.SELECTED_BG};"
                f" border: 1px solid {Colors.BORDER_ACCENT}; }}"
                "QPushButton:hover {"
                f" background: {Colors.HOVER_BG};"
                f" color: {Colors.TEXT_PRIMARY}; }}"
            )
        self.tasks_dock.refresh_style()
        self.artifacts_page.refresh_style()
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
            # 向左拖 → 变宽（面板贴主窗口右缘，宽度 = 起始宽 + 起始光标x - 当前光标x）
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

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        # 兑底：面板隐藏时若还有动画未收尾，强制停掉，
        # 避免下次 show 时动画状态残留（旧版本曾导致鼠标事件被屏蔽后拖拽失灵）
        self._stop_slide()

    def _emit_geometry_change(self) -> None:
        """宽度变化后通知宿主重定位（面板右缘固定贴主窗口右边）"""
        parent = self.parentWidget()
        if parent is not None and hasattr(parent, "reposition_workbench"):
            parent.reposition_workbench()
