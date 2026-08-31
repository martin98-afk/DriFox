# -*- coding: utf-8 -*-
"""
右侧工作台浮层（WorkbenchPanel）— 纯悬浮，不挤压窗口内部元素

形态：TabManagerWindow 的 child widget，几何贴主窗口右侧（标题栏下方），
不进任何 layout，与桌宠（PixelPetWidget）同模式。由标题栏「右侧边栏」
按钮 toggle 显隐。

内置区域：
- 任务：todowrite 工具回传的待办列表（窗口级）—— 置顶常驻，不进页签
- 记忆：嵌入 MemoryCardContent 完整长期记忆面板（条目/项目笔记/关键文档）

★ 产物页已完全插件化：面板**不再内置**产物实现，改由插件通过
``UIPluginRegistry.register_workbench_tab(plugin_name, page_id="artifacts", ...)``
注册（系统插件见 ``plugins/system/ui/_artifacts_page.py``）。
插件未注册时 index 0 显示 ``_PagePlaceholder`` 占位。

其它 page_id 的插件页追加在「产物 / 记忆」之后（见 sync_plugin_pages）。

数据由外部驱动（TabManagerWindow.refresh_workbench / MainWidget 推送），
面板自身不持有 backend 引用，便于测试与解耦。
"""

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import TransparentToolButton
from qfluentwidgets import FluentIcon

from app.utils.design_tokens import BorderRadius, Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import get_font_family_css, get_icon
from app.widgets._workbench_helpers import _EmptyHint, _SectionHeader
from app.widgets.custom_title_bar import CustomTabButton

# ── 尺寸常量 ──
PANEL_WIDTH_DEFAULT = 480  # 默认宽度（splitter 初始分配用）
PANEL_WIDTH_MIN = 320  # 拖拽最小宽
PANEL_WIDTH_MAX = 820  # 拖拽最大宽
TASKS_MIN_HEIGHT = 0  # 任务区最小高度（无任务时折叠到此）
TASKS_MAX_HEIGHT = 360  # 任务区最大高度（splitter 上限）
TASKS_DEFAULT_HEIGHT = 180  # 任务区默认高度


# 注：_EmptyHint / _SectionHeader 已迁移到 app.widgets._workbench_helpers 共享模块，
# 被 TasksPage / MemoryPage 和 plugins/system/ui/_artifacts_page.py 共用。


class _PagePlaceholder(QWidget):
    """页签占位页：插件页未注册 / 已卸载时的兜底内容

    产物页（page_id="artifacts"）已完全插件化——面板不再内置实现，
    由 plugins/system/ui/_artifacts_page.py 的 SystemArtifactsPage 提供。
    插件未加载时显示本占位，避免出现空白页。
    """

    def __init__(self, text: str = "产物页未加载\n\n插件未注册或已卸载", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._hint = _EmptyHint(text, self)
        layout.addWidget(self._hint)

    def set_operations(self, operations) -> None:
        """宿主数据入口的空实现（插件版会覆盖；占位页无数据可渲染）"""

    def set_diff_all_callback(self, callback) -> None:
        """差异回调注入的空实现（占位页无差异入口）"""

    def refresh_style(self) -> None:
        self._hint.refresh_style()


class TasksPage(QWidget):
    """任务区：todowrite 待办列表（置顶常驻，不进内容栈；与下方 tab+stack 用 splitter 隔开）

    结构：头部（图标 + 标题 + 进度统计 + 折叠按钮）→ 细进度条 → 行式任务清单。

    视觉取舍：条目用**行式（无边框）**而非卡片堆叠——任务清单通常条目多，
    每条目加边框会形成密集的"框中框"，视觉噪声重。改为默认透明、hover 淡背景，
    靠左侧状态符号的颜色区分状态；右侧标签只在 in_progress / high 优先级时出现，
    进一步降噪。
    """

    _PRI_COLORS = {"high": "#ef4444", "medium": "#f59e0b", "low": "#3b82f6"}
    # 状态 → (符号, 颜色, 右侧文字)
    _STATUS_META = {
        "completed": ("✓", "#3fb950", ""),
        "in_progress": ("◐", "#f59e0b", "进行中"),
        "pending": ("○", "#6b7280", ""),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._collapsed = False
        self._on_collapse_changed = None  # 折叠状态变化回调（宿主收敛 splitter 高度）
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # ── 头部：图标 + 标题 + 统计 + 折叠按钮 ──
        self._header = _SectionHeader("任务", "todo", self)
        self._collapse_btn = TransparentToolButton(get_icon("折叠"), self)
        self._collapse_btn.setFixedSize(22, 22)
        self._collapse_btn.setToolTip("折叠任务区")
        self._collapse_btn.clicked.connect(self._on_collapse_clicked)
        # 折叠按钮插入到 header 末尾（统计之后）
        hdr_layout = self._header.layout()
        hdr_layout.insertWidget(hdr_layout.count(), self._collapse_btn)
        layout.addWidget(self._header)

        # ── 进度条：细横条，显示完成比例 ──
        self._progress = QProgressBar(self)
        self._progress.setObjectName("taskProgressBar")
        self._progress.setFixedHeight(3)
        self._progress.setTextVisible(False)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        layout.addWidget(self._progress)

        # ── 任务列表 ──
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
        self._list_layout.setSpacing(1)  # 行式条目：紧凑行距
        self._empty_hint = _EmptyHint("暂无任务\n\nAI 使用 todowrite 建立的任务列表会显示在这里", self._list_wrap)
        self._list_layout.addWidget(self._empty_hint)
        self._list_layout.addStretch(1)
        layout.addWidget(self._scroll, 1)

        # 初始无任务：整区隐藏（不占高度），折叠按钮一并隐藏
        self._collapse_btn.hide()
        self.hide()
        self.refresh_style()

    # ── 折叠 ──

    def set_collapse_callback(self, callback) -> None:
        """宿主注入折叠状态变化回调（用于收敛 splitter 上半高度）"""
        self._on_collapse_changed = callback

    def header_height(self) -> int:
        """折叠态所需高度：头部 + 进度条 + 间距

        折叠后不能保持展开时的高度，否则只剩 header 的任务区会留一大片空白
        （用户看到「任务 title 跑中间」）。
        """
        lay = self.layout()
        m = lay.contentsMargins()
        return self._header.sizeHint().height() + self._progress.height() + lay.spacing() + m.top() + m.bottom()

    def _set_collapsed(self, collapsed: bool) -> None:
        """内部：设置折叠态（不触发回调，避免递归）"""
        self._collapsed = collapsed
        self._scroll.setVisible(not collapsed)
        icon = "展开" if collapsed else "折叠"
        self._collapse_btn.setIcon(get_icon(icon))
        self._collapse_btn.setToolTip("展开任务区" if collapsed else "折叠任务区")

    def _on_collapse_clicked(self) -> None:
        """折叠按钮：切换折叠态并通知宿主收敛高度"""
        self._set_collapsed(not self._collapsed)
        self._notify_collapse_changed()

    def _notify_collapse_changed(self) -> None:
        if callable(self._on_collapse_changed):
            try:
                self._on_collapse_changed(self._collapsed)
            except Exception:
                pass

    # ── 数据 ──

    def _clear_items(self) -> None:
        """清空列表项（保留 empty_hint 实例，其余彻底销毁）

        ★ ``deleteLater()`` 只是"预约删除"——在事件循环真正处理前，widget 仍挂在
        parent 的 children 链上并继续绘制。此前只 deleteLater 不 setParent(None)，
        旧条目残影会盖在新列表第一项上（现象：第一项重复显示上一次的最后一项）。
        """
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is None or w is self._empty_hint:
                continue
            w.setParent(None)  # ★ 先断开父子关系，立即停止绘制
            w.deleteLater()

    def update_todos(self, todos: List[Dict[str, Any]]) -> None:
        """刷新任务列表（无任务时整区 hide，由宿主 splitter 收敛；非空时显示并默认展开）"""
        self._clear_items()
        todos = list(todos or [])
        done = sum(1 for t in todos if (t.get("status") or "pending") == "completed")
        total = len(todos)
        pct = int(round(done * 100 / total)) if total else 0

        # 头部统计 + 进度条 + 折叠按钮
        self._header.set_extra(f"{done}/{total}" if todos else "")
        self._progress.setValue(pct)
        self._progress.setVisible(bool(todos))
        self._collapse_btn.setVisible(bool(todos))
        # 有新任务时若处于折叠态则自动展开（避免"有任务却看不见"）
        if todos and self._collapsed:
            self._set_collapsed(False)
        # 整区可见性：无任务时 hide
        self.setVisible(bool(todos))

        # 重建列表：empty_hint（按需可见）→ 任务项 → 底部 stretch
        self._empty_hint.setVisible(not todos)
        self._list_layout.addWidget(self._empty_hint)
        for t in todos:
            status = (t.get("status") or "pending") if isinstance(t, dict) else "pending"
            content = (t.get("content") or "") if isinstance(t, dict) else str(t)
            priority = ((t.get("priority") or "medium") if isinstance(t, dict) else "medium") or "medium"
            self._list_layout.addWidget(self._make_item(status, content, priority))
        self._list_layout.addStretch(1)

        self._notify_collapse_changed()

    def _make_item(self, status: str, content: str, priority: str) -> QFrame:
        """单条任务：左状态符号 + 中内容（自动换行）+ 右标签（按需）

        右侧标签只在两种噪声最值得暴露的情况出现：in_progress（"进行中"）、
        pending 且 high 优先级（"高"）。medium/low 与 completed 一律不显示。
        """
        frame = QFrame(self._list_wrap)
        frame.setObjectName("taskItem")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        mark, _color, status_text = self._STATUS_META.get(status, self._STATUS_META["pending"])
        mark_label = QLabel(mark, frame)
        mark_label.setObjectName("taskMark")
        mark_label.setFixedWidth(16)
        mark_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(mark_label)

        content_label = QLabel(content, frame)
        content_label.setObjectName("taskContent")
        content_label.setWordWrap(True)
        layout.addWidget(content_label, 1)

        tag_text, tag_kind = "", ""
        if status == "in_progress":
            tag_text, tag_kind = status_text, "status"
        elif status == "pending" and priority == "high":
            tag_text, tag_kind = "高", "prio_high"
        if tag_text:
            tag = QLabel(tag_text, frame)
            tag.setObjectName("taskTag")
            tag.setProperty("tagKind", tag_kind)
            tag.setAlignment(Qt.AlignCenter)
            layout.addWidget(tag)

        frame.setProperty("status", status)
        self._apply_item_style(frame)
        return frame

    def _apply_item_style(self, frame: QFrame) -> None:
        """应用条目样式（行式：默认透明，hover 淡背景；靠状态符号着色）"""
        status = frame.property("status") or "pending"
        _mark, color, _text = self._STATUS_META.get(status, self._STATUS_META["pending"])

        frame.setStyleSheet(
            "QFrame#taskItem { background: transparent; border: none;"
            f" border-radius: {BorderRadius.SM}; }}"
            "QFrame#taskItem:hover {"
            f" background: {Colors.HOVER_BG}; }}"
        )

        mark_label = frame.findChild(QLabel, "taskMark")
        if mark_label is not None:
            mark_label.setStyleSheet(
                f"color: {color}; background: transparent; font-weight: 700;"
                f" {get_font_family_css()} {font_size_css(13)};"
            )

        content_label = frame.findChild(QLabel, "taskContent")
        if content_label is not None:
            line = "text-decoration: line-through;" if status == "completed" else ""
            c = Colors.TEXT_MUTED if status == "completed" else Colors.TEXT_PRIMARY
            weight = "normal" if status == "completed" else "500"
            content_label.setStyleSheet(
                f"color: {c}; background: transparent; {line}"
                f" font-weight: {weight};"
                f" {get_font_family_css()} {font_size_css(12)};"
            )

        tag = frame.findChild(QLabel, "taskTag")
        if tag is not None:
            if tag.property("tagKind") == "prio_high":
                tc, bg = self._PRI_COLORS["high"], "rgba(239, 68, 68, 0.16)"
            else:
                tc, bg = self._STATUS_META["in_progress"][1], "rgba(245, 158, 11, 0.16)"
            tag.setStyleSheet(
                f"color: {tc}; background: {bg}; border: none;"
                f" border-radius: {BorderRadius.XS}; padding: 1px 6px;"
                f" {get_font_family_css()} {font_size_css(10)};"
                " font-weight: 600;"
            )

    def refresh_style(self) -> None:
        self._header.refresh_style()
        self._empty_hint.refresh_style()
        # 进度条：轨道 = BORDER 色，已完成块 = completed 绿
        self._progress.setStyleSheet(
            "QProgressBar#taskProgressBar {"
            f" background: {Colors.BORDER};"
            " border: none;"
            " border-radius: 2px;"
            " }"
            "QProgressBar#taskProgressBar::chunk {"
            f" background: {self._STATUS_META['completed'][1]};"
            " border-radius: 2px;"
            " }"
        )
        for i in range(self._list_layout.count()):
            w = self._list_layout.itemAt(i).widget()
            if isinstance(w, QFrame) and w.objectName() == "taskItem":
                self._apply_item_style(w)


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
        self._pending_sub_tab: Optional[str] = None  # 内容未构建时的待切页签
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
        self.switch_sub_tab(tab_id)

    def switch_sub_tab(self, tab_id: str) -> None:
        """切换到指定子页签（外部入口，供宿主"一键直达"用）

        Args:
            tab_id: entries=条目记忆 / notes=项目笔记 / docs=关键文档
                    （工作树的增删与切换 UI 挂在 docs 页签里）
        """
        # 内容未构建时先记录目标，ensure_built 后再补切
        if self._content is None:
            self._pending_sub_tab = tab_id
            return
        try:
            self._content.switch_tab(tab_id)
        except Exception:
            return
        for btn in self._sub_buttons:
            btn.set_active(btn.tab_id == tab_id)
        self._pending_sub_tab = None

    def _set_sub_tab_active(self, index: int) -> None:
        for i, btn in enumerate(self._sub_buttons):
            btn.set_active(i == index)

    def ensure_built(self, memory_manager: Any) -> None:
        """注入 memory_manager 并懒构建内容（避免初始化期拉起存储层）

        构建完成后立即触发一次当前子页签刷新（默认 entries），避免「刚进去没有内容」。
        """
        self._memory_manager = memory_manager
        if self._content is None and memory_manager is not None:
            from app.widgets.cards.settings.memory_card import MemoryCardContent

            self._hint.hide()
            self._content = MemoryCardContent(memory_manager, self)
            self._layout.addWidget(self._content, 1)
            if self._project:
                self._content.set_project(self._project, self._workdir)
            # 有"一键直达"目标页签则切过去，否则停在条目记忆
            target = self._pending_sub_tab or "entries"
            self.switch_sub_tab(target)
            try:
                self._content._refresh_current_tab()
            except Exception:
                pass

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
    """右侧工作台面板（**嵌入式**：对话区右侧第三窗格，与左侧 TabPanel 对称）

    形态：作为主窗口 splitter 的第三个窗格（左 #tabFrame | 中 #chatFrame |
    右 #workbenchFrame），几何由 layout 管理，外层套同款圆角矩形容器。

    ★ 为什么放弃悬浮：QWebEngineView 使用原生 HWND，Qt 中**原生 widget 永远
    绘制在 alien（非原生）widget 之上**，与 Qt 内部 z-order 无关。悬浮面板必须
    覆盖在对话区之上浮出，于是必然和 WebEngine 争 z-order，三条路全有硬伤：

    - 普通 child widget → 盖不住 WebEngine：消息正文穿透面板（用户实测现象）
    - ``WA_NativeWindow`` → 能盖住，但原生 HWND 会吞掉主窗口边缘的
      ``WM_NCHITTEST``，**主窗口边框无法 resize**，且面板内点击命中异常
    - 顶层 Tool 窗口 → 脱离父窗口几何管理，move/resize 跟随有延迟（用户否决）

    嵌入式从根上绕开：工作台与对话区**并列不重叠**，WebEngine 只在自己的
    窗格内绘制，既不会遮挡工作台，也不需要任何 HWND / raise 定时器 / 屏幕
    坐标同步。副作用全部消失。

    显隐：``set_panel_visible(bool)`` 直接 show/hide（用户要求无折叠动画）。
    宽度：由 splitter handle 拖拽，本类只给 min/max 约束。

    主题：theme_manager.register_refresh_target(panel) → refresh_style()
    """

    close_requested = pyqtSignal()
    refresh_requested = pyqtSignal()
    # 差异请求：file_paths 为 None 表示「查看所有产物差异」
    diff_requested = pyqtSignal(object)  # Optional[List[str]]

    TAB_ARTIFACTS, TAB_MEMORY = 0, 1

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchPanel")
        # 嵌入式：宽度交给外层 splitter 拖拽，这里只给 min/max 约束
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumWidth(PANEL_WIDTH_MIN)
        self.setMaximumWidth(PANEL_WIDTH_MAX)

        # 当前任务区高度（splitter 拖拽持久化用）
        self._tasks_height = TASKS_DEFAULT_HEIGHT
        # 产物页：插件注册时的来源签名（None = 显示占位页）
        self._artifacts_plugin_sig: Optional[tuple] = None
        self._plugin_artifacts_widget: Optional[QWidget] = None
        self._artifacts_plugin_info: Optional[Any] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 8, 8)
        root.setSpacing(6)

        # ── 头部：标题 + 刷新 + 关闭（关闭 = 隐藏面板）──
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
        self._close_btn.setToolTip("隐藏工作台")
        self._close_btn.clicked.connect(self.close_requested.emit)
        header.addWidget(self._close_btn)
        root.addLayout(header)

        # ── 主体：QSplitter(垂直) 切分任务区 / tabs+stack ──
        # 上：TasksPage（置顶常驻，无任务时整区 hide）
        # 下：tab 条 + QStackedWidget（产物 / 记忆 / 插件页）
        self._body_splitter = QSplitter(Qt.Vertical, self)
        self._body_splitter.setObjectName("workbenchBodySplitter")
        self._body_splitter.setChildrenCollapsible(False)
        self._body_splitter.setHandleWidth(4)
        self._body_splitter.setStyleSheet(
            "QSplitter#workbenchBodySplitter { background: transparent; border: none; }"
            "QSplitter#workbenchBodySplitter::handle:vertical {"
            f" background: transparent;"
            f" border-top: 1px solid {Colors.BORDER};"
            " margin: 0 4px;"
            " }"
            "QSplitter#workbenchBodySplitter::handle:vertical:hover {"
            f" border-top: 1px solid {Colors.BORDER_ACCENT};"
            " }"
        )

        # 上半：任务区
        self.tasks_page = TasksPage(self)
        self.tasks_page.setMinimumHeight(TASKS_MIN_HEIGHT)
        self.tasks_page.setMaximumHeight(TASKS_MAX_HEIGHT)
        # 折叠状态变化 → 收敛 splitter 上半高度（否则折叠后留大片空白）
        self.tasks_page.set_collapse_callback(self._on_tasks_collapsed)
        self._body_splitter.addWidget(self.tasks_page)

        # 下半：tabs + stack 容器
        self._bottom = QWidget(self)
        self._bottom_layout = QVBoxLayout(self._bottom)
        self._bottom_layout.setContentsMargins(0, 0, 0, 0)
        self._bottom_layout.setSpacing(4)

        # 页签条：复用顶栏 CustomTabButton 自绘风格
        self._tab_bar_layout = QHBoxLayout()
        self._tab_bar_layout.setSpacing(2)
        self._bottom_layout.addLayout(self._tab_bar_layout)
        self._tab_buttons: List[CustomTabButton] = []
        self._tab_ids: List[str] = []

        # 内容栈
        # index 0 = 产物页槽位（插件提供；未注册时为 _PagePlaceholder 占位）
        # index 1 = 记忆页（内置）
        # index 2+ = 其它插件页
        self._stack = QStackedWidget(self._bottom)
        self._artifacts_placeholder = _PagePlaceholder(parent=self._stack)
        self.artifacts_page: QWidget = self._artifacts_placeholder
        self.memory_page = MemoryPage(self._stack)
        self._stack.addWidget(self._artifacts_placeholder)
        self._stack.addWidget(self.memory_page)
        self._bottom_layout.addWidget(self._stack, 1)

        self._body_splitter.addWidget(self._bottom)
        # 任务区固定高度（stretch 0），内容区吃掉剩余空间（stretch 1）。
        # ★ 不能给下半区 setSizes 传 0：QSplitter 会把它压到 0 高度，
        #   内容区（页签 + stack）就完全显示不出来。这里给最小值 1，
        #   剩余空间由 stretch factor 分配给内容区。
        self._body_splitter.setStretchFactor(0, 0)
        self._body_splitter.setStretchFactor(1, 1)
        self._body_splitter.setSizes([0, 1])
        # splitter 拖拽结束时把上半尺寸持久化（无任务时 tasks_page hide 高度=0）
        self._body_splitter.splitterMoved.connect(self._on_splitter_moved)

        root.addWidget(self._body_splitter, 1)

        # 插件页签：{page_id: widget}，按注册表 reconcile（见 sync_plugin_pages）
        self._plugin_widgets: Dict[str, QWidget] = {}
        self._plugin_infos: Dict[str, Any] = {}
        self._plugin_sig: Optional[tuple] = None

        self._rebuild_tab_bar()
        self.set_current_tab(self.TAB_ARTIFACTS)
        self.refresh_style()

    # ── 显隐（直接 show/hide，无折叠动画） ──

    def set_panel_visible(self, visible: bool) -> None:
        """显示/隐藏面板（用户要求：不做折叠动画，直接隐藏/显示）"""
        self.setVisible(bool(visible))
        if visible:
            self.raise_()

    def is_panel_visible(self) -> bool:
        return self.isVisible()

    # ── 差异信号（产物页 → 宿主） ──

    def _emit_diff(self, file_paths: Optional[List[str]] = None) -> None:
        """产物页差异按钮回调：转发 file_paths 到宿主的 diff_requested 信号

        None 表示「查看所有产物差异」（由宿主从当前 ops 重新计算）。
        """
        self.diff_requested.emit(file_paths)

    # ── splitter 拖拽 ──

    def _on_splitter_moved(self, pos: int, index: int) -> None:
        """splitter 拖拽结束：把上半高度持久化（折叠态与无任务态不记忆）"""
        sizes = self._body_splitter.sizes()
        if sizes and sizes[0] > 0 and not self.tasks_page._collapsed:
            self._tasks_height = max(TASKS_MIN_HEIGHT, min(TASKS_MAX_HEIGHT, sizes[0]))

    def _on_tasks_collapsed(self, collapsed: bool) -> None:
        """任务区折叠/展开：收敛 splitter 上半高度

        折叠时不能保持展开高度——否则任务区只剩 header 却仍占 ~180px，
        表现为「折叠后高度没变、任务 title 跑中间」。这里把上半收敛到
        header 高度（约 50px），展开时恢复记忆高度。
        """
        if not self.tasks_page.isVisible():
            self._body_splitter.setSizes([0, 1])
            return
        if collapsed:
            h = max(TASKS_MIN_HEIGHT, self.tasks_page.header_height())
            self._body_splitter.setSizes([h, 1])
        else:
            self._body_splitter.setSizes([self._tasks_height, 1])

    def apply_tasks_visible(self) -> None:
        """根据 tasks_page 可见性收敛 splitter 上半高度

        无任务时 tasks_page 已 hide → 上半置 0，内容区（stretch 1）占满。
        有任务时按记忆高度恢复（折叠态则用 header 高度），剩余仍归内容区。

        ★ 下半区永远给 >=1 的初值：setSizes 传 0 会让内容区塌成 0 高度
          （表现为「工作台出来了但里面没有内容」）。
        """
        if not self.tasks_page.isVisible():
            self._body_splitter.setSizes([0, 1])
        elif self.tasks_page._collapsed:
            h = max(TASKS_MIN_HEIGHT, self.tasks_page.header_height())
            self._body_splitter.setSizes([h, 1])
        else:
            h = max(TASKS_MIN_HEIGHT, min(TASKS_MAX_HEIGHT, self._tasks_height))
            self._body_splitter.setSizes([h, 1])

    # ── 页签条构建（内置 + 插件，顺序与 stack 一致） ──

    def _tab_specs(self) -> List[tuple]:
        """内置页签 + 插件页签（顺序与 QStackedWidget 保持一致）

        产物页标签：插件用 ``page_id="artifacts"`` 填充槽位时用插件的 label，
        否则用 ``"产物"``（不会出现两个产物 tab）。
        """
        art_label = "产物"
        art_info = getattr(self, "_artifacts_plugin_info", None)
        if art_info is not None:
            art_label = art_info.label or "产物"
        specs: List[tuple] = [("artifacts", art_label), ("memory", "记忆")]
        # _plugin_infos 已排除 artifacts（见 sync_plugin_pages），此处无需再过滤
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
        """页签点击：按 tab_id 定位 stack 索引（与 _tab_ids 顺序一致）"""
        idx = self._tab_id_index(tab_id)
        if idx is not None:
            self.set_current_tab(idx)

    def _tab_id_index(self, tab_id: str) -> Optional[int]:
        # 注意：tab 顺序与 _tab_buttons 一致；与 _stack 顺序也一致（同步添加）
        for i, t in enumerate(self._tab_ids):
            if t == tab_id:
                return i
        return None

    # ── 插件页签 reconcile（宿主在 refresh_workbench 时调用） ──

    def sync_plugin_pages(self, tabs: List[Any]) -> None:
        """按 UIPluginRegistry 的 workbench_tabs 增删插件页（签名不变则跳过重建）

        产物页特例：``page_id="artifacts"`` 是**保留 id**，插件注册它即填充
        index 0 的产物页槽位（面板本身不提供产物实现）。未注册时显示占位页。
        其余 page_id 按注册序追加在「产物 / 记忆」之后。
        """
        all_infos = {t.page_id: t for t in (tabs or [])}
        # ── 产物页槽位：page_id="artifacts" 被保留，不进普通插件页列表 ──
        art_info = all_infos.get("artifacts")
        if art_info is not None:
            self._use_plugin_artifacts(art_info)
        else:
            self._use_placeholder_artifacts()

        infos = {pid: info for pid, info in all_infos.items() if pid != "artifacts"}
        sig = tuple((t.page_id, t.label) for t in (tabs or []) if t.page_id != "artifacts")
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
        if was_plugin_current or current >= self._stack.count() or current < 0:
            current = self.TAB_ARTIFACTS
        self.set_current_tab(current)

    # ── 产物页槽位（完全插件化，index 0 恒定） ──

    def _use_plugin_artifacts(self, info: Any) -> None:
        """用插件版产物页**替换** index 0 的当前内容（占位页或旧插件页）

        ★ 必须是"替换"而非 insertWidget：QStackedWidget.insertWidget(0, w) 会把
        原 index 0 及其后所有页**整体后移**，导致 TAB_MEMORY(=1) 落到产物页上
        （表现为"记忆"页显示出产物内容）。因此这里先移除旧页再插入，
        保证 stack 索引与 tab 顺序严格一致：0=产物 / 1=记忆 / 2+=其它插件页。
        """
        sig = (info.page_id, info.label)
        if sig == self._artifacts_plugin_sig and self._plugin_artifacts_widget is not None:
            return
        widget = self._make_page_widget(info)
        if widget is None:
            return
        # 先卸掉 index 0 上的旧页（占位页或上一个插件版），再插入新页
        self._remove_artifacts_slot_widget()
        self._stack.insertWidget(self.TAB_ARTIFACTS, widget)
        self._plugin_artifacts_widget = widget
        self.artifacts_page = widget
        self._artifacts_plugin_sig = sig
        self._artifacts_plugin_info = info
        # 差异入口接到 panel 的 diff_requested（插件版若有 set_diff_all_callback）
        self._wire_artifacts_diff(widget)
        if self._stack.currentIndex() == self.TAB_ARTIFACTS:
            self._stack.setCurrentIndex(self.TAB_ARTIFACTS)

    def _use_placeholder_artifacts(self) -> None:
        """插件未注册产物页 → 回落到占位页（产物功能完全插件化，无内置实现）"""
        if self._artifacts_plugin_sig is None and self._plugin_artifacts_widget is None:
            return  # 已是占位，跳过
        self._remove_artifacts_slot_widget()
        self._stack.insertWidget(self.TAB_ARTIFACTS, self._artifacts_placeholder)
        self._artifacts_placeholder.show()
        self.artifacts_page = self._artifacts_placeholder
        self._artifacts_plugin_sig = None
        self._artifacts_plugin_info = None
        if self._stack.currentIndex() == self.TAB_ARTIFACTS:
            self._stack.setCurrentIndex(self.TAB_ARTIFACTS)

    def _remove_artifacts_slot_widget(self) -> None:
        """移除产物页槽位（index 0）上的当前 widget

        占位页不销毁（留作后续回落复用），插件版则销毁回收。
        """
        current = self._stack.widget(self.TAB_ARTIFACTS)
        if current is None:
            return
        self._stack.removeWidget(current)
        if current is self._artifacts_placeholder:
            current.hide()  # 占位页保留实例
        elif current is self._plugin_artifacts_widget:
            current.hide()
            current.deleteLater()
            self._plugin_artifacts_widget = None
        else:
            current.hide()

    def _dispose_plugin_artifacts(self) -> None:
        """销毁当前插件版产物页 widget（保留占位页）"""
        widget = self._plugin_artifacts_widget
        if widget is not None:
            widget.hide()
            self._stack.removeWidget(widget)
            widget.deleteLater()
        self._plugin_artifacts_widget = None

    def _wire_artifacts_diff(self, widget: QWidget) -> None:
        """把产物页的差异入口接到 panel.diff_requested"""
        setter = getattr(widget, "set_diff_all_callback", None)
        if callable(setter):
            try:
                setter(self._emit_diff)
            except Exception:
                pass

    def _host_window(self):
        """返回宿主主窗口（TabManagerWindow）

        嵌入式下 parentWidget() 可能只是中间容器（如 #workbenchFrame），
        因此统一用 window() 上溯到顶层窗口取 UI context。
        """
        try:
            return self.window()
        except Exception:
            return self.parentWidget()

    def _make_page_widget(self, info: Any) -> Optional[QWidget]:
        """构建插件页 widget（构造 parent + context，兼容无 context 的老签名）"""
        context: Dict[str, Any] = {}
        try:
            parent_win = self._host_window()
            if parent_win is not None and hasattr(parent_win, "_build_ui_context"):
                context = parent_win._build_ui_context()
        except Exception:
            context = {}
        # 差异回调注入 context，供插件版产物页触发
        context.setdefault("diff_requested_callback", self._emit_diff)
        try:
            widget = info.widget_class(parent=self._stack, context=context)
        except TypeError:
            widget = info.widget_class(parent=self._stack)
        return widget if isinstance(widget, QWidget) else None

    def _mount_plugin_page(self, info: Any) -> None:
        """构建并挂载插件页 widget（构造 parent + context，兼容无 context 的老签名）"""
        widget = self._make_page_widget(info)
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
        """刷新任务列表

        高度收敛由 TasksPage 的折叠回调（``_on_tasks_collapsed``）驱动，
        它已覆盖「无任务 / 折叠 / 展开」三种情形，此处不再重复 setSizes。
        """
        self.tasks_page.update_todos(todos)

    def update_project(self, project: str, workdir: Optional[str] = None) -> None:
        self.memory_page.set_project(project, workdir)

    # ── 主题 ──

    def refresh_style(self) -> None:
        Colors.refresh()
        # 嵌入式：背景透明（由外层 #workbenchFrame 圆角矩形容器提供背景）
        self.setStyleSheet(
            "QWidget#workbenchPanel { background: transparent; border: none; }"
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
