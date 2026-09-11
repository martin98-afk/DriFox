# -*- coding: utf-8 -*-
"""
右侧工作台浮层（WorkbenchPanel）— 纯悬浮，不挤压窗口内部元素

形态：TabManagerWindow 的 child widget，几何贴主窗口右侧（标题栏下方），
不进任何 layout，与桌宠（PixelPetWidget）同模式。由标题栏「右侧边栏」
按钮 toggle 显隐。

内置区域：
- 任务：todowrite 工具回传的待办列表（窗口级）—— 置顶常驻，不进页签

★ 页面已**完全插件化 · 零保留槽位**：面板不内置任何页实现、不识任何
page_id 语义，全部页由插件通过
``UIPluginRegistry.register_workbench_tab(plugin_name, page_id, label, widget_class, metadata=...)``
注册：
- ``page_id="worktree-manager"``  → ``plugins/worktree-manager/ui/worktree_page.py``（order_hint=0，默认落点）
- ``page_id="artifacts-manager"`` → ``plugins/artifacts-manager/ui/artifacts_page.py``（order_hint=10）
- ``page_id="history-manager"``   → ``plugins/history-manager/ui/history_page.py``（order_hint=20）
页序 = ``(metadata["order_hint"], 注册序)``；默认落点页 = ``metadata["default_landing"]``
标记页，缺省为顺序第一页；一个页都没注册时显示空态页。页签一律按 **tab_id**
定位（``set_current_tab_by_id`` / ``current_tab_id``），宿主不得假设 index。

数据由页面**自拉**：面板只提供无参 ``refresh_current_page_data()``（切页 /
refresh_workbench 时调用），页面自行实现可选协议 ``refresh_data()`` 从
``context`` / 活跃窗口取数 —— 面板不承载任何页面专属逻辑、不持有 backend 引用。
"""

import json
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import ScrollArea, TransparentToolButton

from app.core.project_changed import dispatch_project_changed, is_active_window
from app.utils.design_tokens import BorderRadius, Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import _is_current_theme_light, get_font_family_css, get_icon
from loguru import logger
from app.widgets._workbench_helpers import _EmptyHint, _SectionHeader
from app.widgets.custom_title_bar import CustomTabButton
from app.widgets.flow_layout import FlowLayout
from app.widgets.cards.floating.sub_agent_compact_widget import _RotatingIcon

# ── 尺寸常量 ──
PANEL_WIDTH_DEFAULT = 480  # 默认宽度（splitter 初始分配用）
PANEL_WIDTH_MIN = 320  # 拖拽最小宽
PANEL_WIDTH_MAX = 820  # 拖拽最大宽
TASKS_MIN_HEIGHT = 0  # 任务区最小高度（无任务时折叠到此）
TASKS_MAX_HEIGHT = 360  # 任务区最大高度（splitter 上限）
TASKS_DEFAULT_HEIGHT = 180  # 任务区默认高度


# 注：_EmptyHint / _SectionHeader 已迁移到 app.widgets._workbench_helpers 共享模块，
# 被 TasksPage 和 plugins/artifacts-manager/ui/artifacts_page.py 共用。


class _PagePlaceholder(QWidget):
    """页签占位页：插件页未注册 / 已卸载时的兜底内容

    产物页（page_id="artifacts-manager"）已完全插件化——面板不再内置实现，
    由 plugins/artifacts-manager/ui/artifacts_page.py 的 SystemArtifactsPage 提供。
    插件未加载时显示本占位，避免出现空白页。
    """

    def __init__(self, text: str = "页面未加载\n\n插件未注册或已卸载", parent=None):
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
    靠左侧状态符号的颜色区分状态：pending 实心圆点按优先级着色（高=红、中=黄、低=绿），
    右侧不放文字标签，进一步降噪。
    """

    _PRI_COLORS = {"high": "#ef4444", "medium": "#f59e0b", "low": "#3fb950"}
    # 状态 → (符号, 颜色, 右侧文字)；pending 颜色仅兑底，实际按优先级用 _PRI_COLORS 着色
    _STATUS_META = {
        "completed": ("✓", "#3fb950", ""),
        "in_progress": ("◐", "#f59e0b", ""),
        "pending": ("●", "#6b7280", ""),
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
        self._scroll = ScrollArea(self)
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
        # 任务列表内容签名（数据未变时 update_todos 跳过全量重建）
        self._last_todos_sig: Optional[str] = None

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
        """刷新任务列表（无任务时整区 hide，由宿主 splitter 收敛；非空时显示并默认展开）

        ★ 数据未变时跳过重建（内容签名比对）：切对话标签等高频路径每次都会
        全量推送，任务多时逐条重建 widget 成本可观；签名相同 → 渲染结果
        相同，直接跳过。
        """
        todos = list(todos or [])
        sig = json.dumps(todos, ensure_ascii=False, sort_keys=True, default=str)
        if sig == self._last_todos_sig:
            return
        self._last_todos_sig = sig
        self._clear_items()
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
        active_item: QWidget | None = None
        for t in todos:
            status = (t.get("status") or "pending") if isinstance(t, dict) else "pending"
            raw = (t.get("content") if isinstance(t, dict) else t) or ""
            # 兜存量脏数据：content 非 str 时 dict/list 转 JSON 文本（QLabel 只收 str）
            content = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
            priority = ((t.get("priority") or "medium") if isinstance(t, dict) else "medium") or "medium"
            item = self._make_item(status, content, priority)
            self._list_layout.addWidget(item)
            if status == "in_progress" and active_item is None:
                active_item = item
        self._list_layout.addStretch(1)

        self._notify_collapse_changed()

        # 任务更新后始终把当前正在执行的任务滚进可视区
        # （全量重建会丢滚动位置；layout 需一帧生效，用 singleShot(0) 延迟滚动）
        if active_item is not None:
            QTimer.singleShot(0, lambda: self._scroll_to_item(active_item))

    def _scroll_to_item(self, item: QWidget) -> None:
        """把指定任务条目滚进可视区（条目已被销毁时静默跳过）"""
        try:
            item.windowTitle()  # sip 探活：已销毁的 C++ 对象会抛 RuntimeError
            self._scroll.ensureWidgetVisible(item, 0, 8)
        except RuntimeError:
            pass

    def _make_item(self, status: str, content: str, priority: str) -> QFrame:
        """单条任务：左状态符号 + 中内容（自动换行），右侧不放任何标签

        视觉降噪：pending 用实心圆点按优先级着色（高=红、中=黄、低=绿），
        in_progress 用旋转图标，completed 用绿勾；不出现右侧文字标签。
        """
        frame = QFrame(self._list_wrap)
        frame.setObjectName("taskItem")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        mark, _color, _status_text = self._STATUS_META.get(status, self._STATUS_META["pending"])
        if status == "in_progress":
            # 进行中：子智能体运行中同款旋转 SVG 图标（QPainter 原地旋转，无抖动）
            mark_widget: QWidget = _RotatingIcon(":/icons/执行中.svg", size=16, parent=frame)
            # 浅色主题叠加半透明黑色，避免亮背景下图标不可见（与子智能体悬浮框一致）
            mark_widget.set_tint("#88000000" if _is_current_theme_light() else None)
            # _RotatingIcon 无自驱动定时器，条目自带 QTimer 驱动
            # （60ms/24° 与子智能体悬浮框旋转参数一致；定时器挂 frame，条目销毁自动停）
            spin_timer = QTimer(mark_widget)
            _angle = 0

            def _spin_tick() -> None:
                nonlocal _angle
                _angle = (_angle + 24) % 360
                mark_widget.set_angle(_angle)

            spin_timer.timeout.connect(_spin_tick)
            spin_timer.start(60)
            layout.addWidget(mark_widget)
        else:
            mark_label = QLabel(mark, frame)
            mark_label.setObjectName("taskMark")
            mark_label.setFixedWidth(16)
            mark_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(mark_label)

        content_label = QLabel(content, frame)
        content_label.setObjectName("taskContent")
        content_label.setWordWrap(True)
        layout.addWidget(content_label, 1)

        frame.setProperty("status", status)
        frame.setProperty("priority", priority)
        self._apply_item_style(frame)
        return frame

    def _apply_item_style(self, frame: QFrame) -> None:
        """应用条目样式（行式：默认透明，hover 淡背景；靠状态符号着色）"""
        status = frame.property("status") or "pending"
        _mark, color, _text = self._STATUS_META.get(status, self._STATUS_META["pending"])
        if status == "pending":
            # 实心圆点按优先级着色：高=红、中=黄、低=绿
            color = self._PRI_COLORS.get(frame.property("priority") or "medium", self._PRI_COLORS["medium"])

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
    # 本页面板内任一页请求「切换工作目录」（页面自带 workingDirChanged 信号时由
    # 面板通用转发，见 _wire_page_signals）→ 宿主转发给活跃窗口
    workingDirChanged = pyqtSignal(str)
    # 差异请求：file_paths 为 None 表示「查看所有产物差异」
    diff_requested = pyqtSignal(object)  # Optional[List[str]]
    # 卡片 tab × 关闭钮点击（registry 连接本信号，同步清理卡片归属状态并摘 tab）
    card_tab_close_requested = pyqtSignal(str)  # card_id
    # 当前页签变化（含程序化切换）：宿主用于按对话窗口独立记忆页签
    current_tab_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchPanel")
        # 嵌入式：宽度交给外层 splitter 拖拽，这里只给 min/max 约束
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumWidth(PANEL_WIDTH_MIN)
        self.setMaximumWidth(PANEL_WIDTH_MAX)

        # 当前任务区高度（splitter 拖拽持久化用）
        self._tasks_height = TASKS_DEFAULT_HEIGHT
        # 页面账本（由插件注册表 reconcile，见 sync_plugin_pages）
        # ★ 面板不认任何 page_id 语义、不保留槽位：worktree / artifacts / history
        #   与其它插件页一视同仁，顺序由 metadata["order_hint"] 决定。
        self._page_order: List[str] = []
        self._default_page_id: Optional[str] = None
        # 空态页（未注册任何插件页时占位，无对应页签）
        self._empty_page: QWidget = _PagePlaceholder(parent=None)
        # 宿主窗口缓存（见 _host_window：hover 预览期间 window() 会取到浮层）
        self._host_window_ref: Optional[QWidget] = None
        # 页签记忆：None = 面板尚未打开过（首次打开默认第一个页签，之后恢复上次关闭时页签）
        self._last_tab_index: Optional[int] = None
        self._last_artifacts_sig: Optional[tuple] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 8, 8)
        root.setSpacing(6)

        # ── 顶部：页签条（右对齐 + 自动换行）──
        # 旧头部（「工作台」标题 + 刷新/关闭按钮）已按需求移除：
        # 显隐由标题栏「右侧边栏」开关负责，数据刷新由 refresh_workbench 驱动。
        tab_bar_host = QWidget(self)
        tab_bar_host.setObjectName("workbenchTabBarHost")
        tab_bar_host.setStyleSheet("background: transparent;")
        # FlowLayout：tab 多时自动折行；AlignRight 整体右对齐（每行独立计算）
        self._tab_bar_layout = FlowLayout(tab_bar_host, spacing=2, alignment=Qt.AlignRight, margins=0)
        root.addWidget(tab_bar_host)

        # ── 主体：QSplitter(垂直) 切分内容栈 / 任务区 ──
        # 上：tab 条已上移，这里只剩 QStackedWidget（工作树 / 记忆 / 产物 / 插件页 / 卡片页）
        # 下：TasksPage（底部独立任务区，无任务时整区 hide）
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

        # 上半：内容栈容器（tab 条已上移至面板顶部，这里只剩 stack）
        self._bottom = QWidget(self)
        self._bottom_layout = QVBoxLayout(self._bottom)
        self._bottom_layout.setContentsMargins(0, 0, 0, 0)
        self._bottom_layout.setSpacing(4)
        self._tab_buttons: List[CustomTabButton] = []
        self._tab_ids: List[str] = []
        self._tab_labels: List[str] = []  # 与 _tab_ids 平行，供 _rebuild_tab_bar 标签变更判定

        # 内容栈：索引 = _page_order 顺序（全部由插件注册），卡片 tab 追加在后
        self._stack = QStackedWidget(self._bottom)
        self._bottom_layout.addWidget(self._stack, 1)
        # 初始无任何插件页 → 先挂空态页（首帧不出现空白无提示）
        self._sync_empty_page()

        self._body_splitter.addWidget(self._bottom)

        # 下半：任务区（底部独立区域）
        self.tasks_page = TasksPage(self)
        self.tasks_page.setMinimumHeight(TASKS_MIN_HEIGHT)
        self.tasks_page.setMaximumHeight(TASKS_MAX_HEIGHT)
        # 折叠状态变化 → 收敛 splitter 下半高度（否则折叠后留大片空白）
        self.tasks_page.set_collapse_callback(self._on_tasks_collapsed)
        self._body_splitter.addWidget(self.tasks_page)

        # 内容区吃掉剩余空间（stretch 1），任务区固定高度（stretch 0）。
        # ★ 不能给下半区 setSizes 传 0：QSplitter 会把它压到 0 高度，
        #   任务区内容显示不出来。无任务时由 apply_tasks_visible 走 [1, 0]。
        self._body_splitter.setStretchFactor(0, 1)
        self._body_splitter.setStretchFactor(1, 0)
        self._body_splitter.setSizes([1, 0])
        # splitter 拖拽结束时把下半尺寸持久化（无任务时 tasks_page hide 高度=0）
        self._body_splitter.splitterMoved.connect(self._on_splitter_moved)

        root.addWidget(self._body_splitter, 1)

        # 插件页签：{page_id: widget}，按注册表 reconcile（见 sync_plugin_pages）
        self._plugin_widgets: Dict[str, QWidget] = {}
        # {page_id: 归属插件名}。热重载精准重建用：面板销毁页时需要知道「这页
        # 属于谁」，而卸载后该页已从注册表消失，届时查不到归属——必须就地记账。
        self._page_owner: Dict[str, str] = {}
        self._plugin_infos: Dict[str, Any] = {}
        self._plugin_sig: Optional[tuple] = None
        # 动态卡片 tab（right 容器 UI 插件卡片，见 open_card_tab）
        # {card_id: {"label": str, "widget": QWidget}}；widget 生命周期归 registry 管
        self._card_tabs: Dict[str, Dict[str, Any]] = {}

        self._rebuild_tab_bar()
        # 初始默认选中第一个页签（非「默认工作树」特判；当前首个页签恰为工作树）
        self.set_current_tab(0)
        self.refresh_style()
        self._subscribe_project_changed()

    # ── 显隐（直接 show/hide，无折叠动画） ──

    def set_panel_visible(self, visible: bool) -> None:
        """显示/隐藏面板（用户要求：不做折叠动画，直接隐藏/显示）"""
        self.setVisible(bool(visible))
        if visible:
            self.raise_()

    def remember_closed_tab(self) -> None:
        """关闭右侧边栏时记录当前页签（下次打开恢复，不强制重置为工作树）"""
        self._last_tab_index = self.current_tab()

    def restore_last_tab(self) -> None:
        """打开右侧边栏时的页签恢复

        首次打开默认第一个页签，之后完全按用户上次选择的页签恢复；
        页签越界（如卡片/插件页已卸载）时回落第一个页签。
        """
        idx = self._last_tab_index
        if idx is None or not 0 <= idx < self._stack.count():
            idx = 0
        self.set_current_tab(idx)

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
        """splitter 拖拽结束：把下半（任务区）高度持久化（折叠态与无任务态不记忆）"""
        sizes = self._body_splitter.sizes()
        if sizes and sizes[1] > 0 and not self.tasks_page._collapsed:
            self._tasks_height = max(TASKS_MIN_HEIGHT, min(TASKS_MAX_HEIGHT, sizes[1]))

    def _on_tasks_collapsed(self, collapsed: bool) -> None:
        """任务区折叠/展开：收敛 splitter 下半高度

        折叠时不能保持展开高度——否则任务区只剩 header 却仍占 ~180px。
        这里把下半收敛到 header 高度（约 50px），展开时恢复记忆高度。
        """
        if not self.tasks_page.isVisible():
            self._body_splitter.setSizes([1, 0])
            return
        if collapsed:
            h = max(TASKS_MIN_HEIGHT, self.tasks_page.header_height())
            self._body_splitter.setSizes([1, h])
        else:
            self._body_splitter.setSizes([1, self._tasks_height])

    def apply_tasks_visible(self) -> None:
        """根据 tasks_page 可见性收敛 splitter 下半高度

        无任务时 tasks_page 已 hide → 下半置 0，内容区（stretch 1）占满。
        有任务时按记忆高度恢复（折叠态则用 header 高度）。

        ★ 下半区永远给 >=1 的初值：setSizes 传 0 会让任务区塌成 0 高度。
        """
        if not self.tasks_page.isVisible():
            self._body_splitter.setSizes([1, 0])
        elif self.tasks_page._collapsed:
            h = max(TASKS_MIN_HEIGHT, self.tasks_page.header_height())
            self._body_splitter.setSizes([1, h])
        else:
            h = max(TASKS_MIN_HEIGHT, min(TASKS_MAX_HEIGHT, self._tasks_height))
            self._body_splitter.setSizes([1, h])

    # ── 动态卡片 tab（right 容器 UI 插件卡片） ──

    def open_card_tab(
        self, card_id: str, label: str, widget: QWidget, *, activate: bool = True
    ) -> None:
        """打开/激活一张卡片 tab（已存在则仅激活；同 id 换新实例则替换页内容）

        卡片页追加在 stack 末尾（内置 3 页 + 插件页之后），与 _tab_specs
        的追加顺序严格一致。widget 生命周期归调用方（registry）管理，
        本面板只负责挂载/摘除（摘除不销毁，重复打开零重建）。

        Args:
            activate: True（默认）= 挂载并激活为当前页，且走用户路径写入
                per-window 页签记忆（用户主动打开卡片 = 切页，切走再切回应
                停留在此）。False = 仅挂载/重建页签条，不改变当前页签也不
                写记忆（对话标签页投影恢复用——恢复卡片 tab 不应把用户当前
                停留的工作台页签抢走）。
        """
        entry = self._card_tabs.get(card_id)
        if entry is not None:
            if entry["widget"] is not widget:
                old = entry["widget"]
                pos = self._stack.indexOf(old)
                if pos >= 0:
                    self._stack.removeWidget(old)
                    old.setParent(None)
                    # ★ 原位替换而非 remove+append：append 会把卡片移到栈尾，
                    # 多卡片时栈顺序与页签顺序（按注册序）错位 → tab 与内容对不上
                    self._stack.insertWidget(pos, widget)
                else:
                    self._stack.addWidget(widget)
                entry["widget"] = widget
            entry["label"] = label
        else:
            self._card_tabs[card_id] = {"label": label, "widget": widget}
            self._stack.addWidget(widget)
        self._rebuild_tab_bar()
        if not activate:
            return
        idx = self._stack.indexOf(widget)
        if idx >= 0:
            self.set_current_tab(idx, user=True)

    def close_card_tab(self, card_id: str) -> bool:
        """关闭卡片 tab：从页签条与内容栈摘除（widget 不销毁，交还调用方）

        Returns:
            True 表示确实移除了一个 tab；False 表示该 tab 不存在（幂等）
        """
        entry = self._card_tabs.pop(card_id, None)
        if entry is None:
            return False
        widget = entry["widget"]
        if self._stack.indexOf(widget) >= 0:
            self._stack.removeWidget(widget)
            widget.setParent(None)
        self._rebuild_tab_bar()
        # 当前页被移除时 QStackedWidget 已自动切到邻近页，这里同步按钮高亮；
        # 极端情况（空栈）回落产物页
        self.set_current_tab(max(0, self._stack.currentIndex()))
        return True

    def has_card_tab(self, card_id: str) -> bool:
        return card_id in self._card_tabs

    # ── 页签条构建（内置 + 插件，顺序与 stack 一致） ──

    def _tab_specs(self) -> List[tuple]:
        """页签规格：插件页（按 _page_order）+ 卡片页（顺序与 QStackedWidget 一致）

        ★ 面板**不认任何 page_id 语义**：页与其标签均来自插件注册
        （``register_workbench_tab``），顺序由 ``metadata["order_hint"]`` 决定。
        """
        infos = getattr(self, "_plugin_infos", {})
        specs: List[tuple] = [
            (pid, infos[pid].label if pid in infos and infos[pid].label else pid)
            for pid in getattr(self, "_page_order", [])
        ]
        # 动态卡片 tab 追加在插件页之后（与 stack 追加顺序一致）
        for card_id, entry in self._card_tabs.items():
            specs.append((card_id, entry["label"]))
        return specs

    def _rebuild_tab_bar(self) -> None:
        """按当前插件注册表重建页签条（内置页签在前，插件按注册序在后）

        布局：FlowLayout（右对齐 + 自动换行），无需 stretch 占位。

        ★ specs 未变时跳过重建：页签按钮是 hover/选中动画的载体，切窗换挂
        历史页等高频路径若每次都销毁重建，按钮上的 hover/动画状态全丢，
        且延迟删除的旧按钮在事件循环繁忙期间可能留下残影（用户实测 hover
        混乱残留的主因）。"""
        specs = self._tab_specs()
        spec_ids = [t for t, _ in specs]
        spec_labels = [label for _, label in specs]
        if spec_ids == self._tab_ids and spec_labels == self._tab_labels:
            # 页签集合与标签都未变：仅重算 hover 仲裁（光标下的高亮跟随真实位置）
            self._schedule_tab_hover_sync()
            return
        while self._tab_bar_layout.count():
            item = self._tab_bar_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                # ★ 先断开父子关系再预约删除：deleteLater 只是延迟销毁，
                # 不 setParent(None) 时旧按钮仍挂 children 链上，事件循环
                # 繁忙期间可能残影（对齐 TasksPage._clear_items 同款教训）
                w.setParent(None)
                w.hide()
                w.deleteLater()
        self._tab_buttons = []
        self._tab_ids = []
        self._tab_labels = []
        for tab_id, label in specs:
            closable = tab_id in self._card_tabs
            btn = CustomTabButton(tab_id, label, self, closable=closable)
            btn.clicked.connect(self._on_tab_clicked)
            if closable:
                btn.close_clicked.connect(self.card_tab_close_requested.emit)
            self._tab_bar_layout.addWidget(btn)
            self._tab_buttons.append(btn)
            self._tab_ids.append(tab_id)
            self._tab_labels.append(label)
        # 重建后恢复选中态：历史页换挂等触发的重建不能让当前页高亮丢失
        # （按钮全部新建，默认非激活；此前仅 was_current 路径会经
        # set_current_tab 补高亮，其余场景高亮直接丢失）
        cur = self._stack.currentIndex()
        for i, btn in enumerate(self._tab_buttons):
            btn.set_active(i == cur)
        # 增删 tab 后布局会平移旧 tab：光标静止时 Qt 不补发 enter/leave，需重算
        self._schedule_tab_hover_sync()
        # 布局重排在下一帧：0ms 仲裁可能拿旧 geometry 漏判，60ms 后补一次兜底
        QTimer.singleShot(60, self._delayed_hover_resync)

    def _delayed_hover_resync(self) -> None:
        """重建后布局落定再仲裁一次 hover（sip 探活防面板已销毁）"""
        try:
            self._tab_buttons  # noqa: B018 - 访问即探活，已销毁抛 RuntimeError
        except RuntimeError:
            return
        self._schedule_tab_hover_sync()

    # ── tab hover 仲裁（对齐 CustomTitleBar 的残留修复） ──

    def sync_tab_hover(self) -> None:
        """按光标真实位置重新仲裁 tab hover（自愈入口，可安全重复调用）

        ★ 为什么不能只靠 enter/leave：工作台宽度动画 / splitter 拖拽会让
        tab 在**光标静止**时平移（FlowLayout 右对齐），而 Qt 既不为「被移到
        光标下」的 widget 补发 enterEvent、也不为「被移走」的 widget 补发
        leaveEvent → hover 高亮残留（与标题栏居中 tab 同源，修复方式同款）。"""
        if not self._tab_buttons or not self.isVisible():
            return
        global_pos = QCursor.pos()
        hit_btn = None
        for btn in self._tab_buttons:
            if btn.isVisible() and btn.rect().contains(btn.mapFromGlobal(global_pos)):
                hit_btn = btn
                break
        for btn in self._tab_buttons:
            btn.set_hover(btn is hit_btn)

    def _schedule_tab_hover_sync(self) -> None:
        """合并同一事件循环内的多次重算请求（宽度动画每帧都 resize）"""
        if getattr(self, "_hover_sync_pending", False):
            return
        self._hover_sync_pending = True
        QTimer.singleShot(0, self._run_tab_hover_sync)

    def _run_tab_hover_sync(self) -> None:
        self._hover_sync_pending = False
        self.sync_tab_hover()

    def leaveEvent(self, event) -> None:
        # 鼠标离开面板时兜底清空（动画/其它窗口抢焦点时子 widget 的
        # leaveEvent 不保证到达）
        for btn in self._tab_buttons:
            btn.set_hover(False)
        super().leaveEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # 面板宽度变化 → FlowLayout 右对齐平移 tab → 光标静止时 Qt 不补发
        # enter/leave，必须重算 hover（对齐标题栏 tab 残留修复）
        self._schedule_tab_hover_sync()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # show 之前无有效几何，命中测试无意义；首帧布局后补一次
        self._schedule_tab_hover_sync()
        # ★ 残缺 context 自愈补触发：启动早期挂载的插件页可能只拿到残缺
        #   UI context（宿主窗口尚未就绪，如文件树页 project_root 恒空、
        #   工作树页提示未设置工作目录）。sync_plugin_pages 的自愈只在
        #   宿主 refresh_workbench 时驱动；单窗口场景用户不开工作台就
        #   不会触发。面板 show 时补检一次，检出坏页即定向重建。
        try:
            broken = [w for w in self._plugin_widgets.values() if self._page_context_incomplete(w)]
            if not broken:
                return
            try:
                probe_ctx = self._host_window()._build_ui_context()
            except Exception:
                probe_ctx = {}
            if (probe_ctx or {}).get("backend"):
                for widget in broken:
                    try:
                        if hasattr(widget, "set_context"):
                            widget.set_context(dict(probe_ctx))
                    except RuntimeError:
                        continue  # C++ 对象已销毁
        except Exception:
            pass

    def _on_tab_clicked(self, tab_id: str) -> None:
        """页签点击：按 tab_id 定位 stack 索引（与 _tab_ids 顺序一致）"""
        idx = self._tab_id_index(tab_id)
        if idx is not None:
            # user=True：用户主动切换才发射 current_tab_changed → 宿主写页签记忆
            self.set_current_tab(idx, user=True)

    def _tab_id_index(self, tab_id: str) -> Optional[int]:
        # 注意：tab 顺序与 _tab_buttons 一致；与 _stack 顺序也一致（同步添加）
        for i, t in enumerate(self._tab_ids):
            if t == tab_id:
                return i
        return None

    def set_current_tab_by_id(self, tab_id: str, *, user: bool = False) -> bool:
        """按页签 id 切换（通用直达入口：插件命令 / 宿主定向跳转）

        与 ``_on_tab_clicked`` 同一套索引定位逻辑，供工作台页命令
        （``/{page_id}``）与宿主跨模块调用复用。

        Returns:
            True 表示页签存在并已切换；False 表示当前页签集合中无此 id
            （典型场景：插件页尚未注册 / 已被卸载）
        """
        idx = self._tab_id_index(tab_id)
        if idx is None:
            return False
        self.set_current_tab(idx, user=user)
        return True

    def _tab_id_at(self, index: int) -> Optional[str]:
        """按当前页签顺序取 index 对应的 tab_id（越界返回 None）"""
        if 0 <= index < len(self._tab_ids):
            return self._tab_ids[index]
        return None

    # ── 插件页签 reconcile（宿主在 refresh_workbench 时调用） ──

    def sync_plugin_pages(
        self,
        tabs: List[Any],
        force: bool = False,
        force_plugin: str = "",
    ) -> None:
        """按插件注册表 reconcile 工作台页（签名不变则跳过重建）

        ★ 面板对页面**零语义**：不识 page_id、不保留槽位、不内置任何页实现。
        - 顺序：``(metadata["order_hint"], 注册序)``
        - 默认落点：``metadata["default_landing"]`` 标记页，缺省为顺序第一页
        - 未注册任何页时显示空态页（无页签）

        force=True（热重载）：签名不变但实现已变 → 销毁全部插件页强制重建
        （数据由页面自身 ``refresh_data()`` / ``showEvent`` 重新拉取）。

        ★ 精准重建：``force_plugin="<插件名>"`` 只销毁重建**归属该插件**的页，
        其余插件页原样保留。用于「热重载插件 A 时不连带销毁重建 B/C/D 的页」——
        旧行为 force=True 会无差别销毁全部插件页，导致无关插件的页状态
        （滚动位置/展开项/输入草稿）丢失、并造成明显卡顿。

        ★ 判定完全**无状态**：只比对「此刻注册表传入的 tabs」与「此刻已挂载页
        及其记录的归属」，不依赖任何跨调用的历史快照。热重载是异步广播 + 多
        入口 + 可重入的，任何「上一次记录 → 这一次消费」的快照都会与真实时序
        错位（曾导致「有时刷新、有时不刷、卸载后残留旧实例」）。

        Args:
            force_plugin: 需定向重建的**插件名**（不是 page_id）。插件卸载后其
                页已从 tabs 中消失，靠 ``_page_owner`` 就地记账才能定位并销毁。
        """
        raw = list(tabs or [])
        ordered = sorted(
            enumerate(raw),
            key=lambda pair: (int((getattr(pair[1], "metadata", None) or {}).get("order_hint", 0)), pair[0]),
        )
        infos: Dict[str, Any] = {info.page_id: info for _i, info in ordered}
        order: List[str] = [info.page_id for _i, info in ordered]
        sig = tuple((pid, infos[pid].label) for pid in order)

        self._plugin_infos = infos
        self._page_order = order
        self._default_page_id = self._resolve_default_page_id(order, infos)

        # ★ 残缺 context 自愈（插件页通用）：工作台 frame 在 hover 预览期间会被
        #   setParent 到浮层，此时构建出的插件页可能只拿到残缺 UI context
        #   （缺 backend）—— 症状：工作树页空列表、添加/删除/切换全部静默失效。
        #   检出这种坏页时，即便 (page_id,label) 签名未变也强制重建；仅在
        #   「宿主此刻确实能提供 backend」时触发，避免无 backend 的测试/降级
        #   环境下反复销毁重建。
        context_broken = False
        if not force and self._plugin_widgets:
            if any(self._page_context_incomplete(w) for w in self._plugin_widgets.values()):
                try:
                    probe_ctx = self._host_window()._build_ui_context()
                except Exception:
                    probe_ctx = {}
                context_broken = bool((probe_ctx or {}).get("backend"))

        if force_plugin:
            # 定向重建：销毁归属该插件的全部页（含已从注册表注销的页）。
            # 置空签名使其落到下方常规 reconcile，由 _mount_plugin_page 按新
            # widget_class 重建；被卸载的页则因不在 infos 中而不再重建。
            # 无目标页（首次安装 / 该插件本就没有工作台页）时交给下方 reconcile。
            targets = [pid for pid, owner in self._page_owner.items() if owner == force_plugin]
            if targets:
                self._plugin_sig = None
                for page_id in targets:
                    self._destroy_plugin_page(page_id)
        if force or context_broken:
            self._plugin_sig = None
            for page_id in list(self._plugin_widgets.keys()):
                self._destroy_plugin_page(page_id)
        elif sig == self._plugin_sig and set(order) == set(self._plugin_widgets.keys()):
            # 页集合与 (page_id,label) 签名均未变：只补一次页签条 reconcile
            # （label 未入旧签名时可能残留旧标签；_rebuild_tab_bar 内部再做
            #  id+label 比对，集合未变时仅重算 hover，成本为零）
            self._rebuild_tab_bar()
            return

        # 空态页先对齐（非空时先摘除，避免占用 index 0 干扰下面的重排）
        self._sync_empty_page()
        # 卸载已注销页
        for page_id in list(self._plugin_widgets.keys()):
            if page_id not in infos:
                self._destroy_plugin_page(page_id)
        # 挂载新页 + 按 order 重排 stack
        for index, page_id in enumerate(order):
            if page_id not in self._plugin_widgets:
                self._mount_plugin_page(infos[page_id])
            widget = self._plugin_widgets.get(page_id)
            if widget is None:
                continue
            cur_idx = self._stack.indexOf(widget)
            if cur_idx != index:
                self._stack.removeWidget(widget)
                self._stack.insertWidget(index, widget)
        self._plugin_sig = sig
        current = self._stack.currentIndex()
        self._rebuild_tab_bar()
        if current >= self._stack.count() or current < 0:
            current = 0
        self.set_current_tab(current)

    def _resolve_default_page_id(self, order: List[str], infos: Dict[str, Any]) -> Optional[str]:
        """默认落点页：显式 ``metadata["default_landing"]`` 优先，否则顺序第一页"""
        for pid in order:
            meta = getattr(infos[pid], "metadata", None) or {}
            if meta.get("default_landing"):
                return pid
        return order[0] if order else None

    def default_page_id(self) -> Optional[str]:
        """默认落点页 id（工作台首次打开 / 无页签记忆时使用）"""
        return getattr(self, "_default_page_id", None)

    def _sync_empty_page(self) -> None:
        """页集合为空时挂上空态页（无页签），非空时移除"""
        empty = self._empty_page
        has_pages = bool(self._page_order)
        if not has_pages:
            if self._stack.indexOf(empty) < 0:
                self._stack.addWidget(empty)
        elif self._stack.indexOf(empty) >= 0:
            self._stack.removeWidget(empty)

    def refresh_current_page_data(self) -> None:
        """让当前页自拉数据（插件页可选协议：``refresh_data()``）

        宿主不再为具体页面推送数据（原 ``update_artifacts`` / ``update_project``）：
        页自己从 ``context`` / 活跃窗口取数，宿主只发「刷新」这一个无参指令，
        从而不承载任何页面专属逻辑。
        """
        page = self._stack.currentWidget()
        fn = getattr(page, "refresh_data", None)
        if callable(fn):
            try:
                fn()
            except Exception:  # noqa: BLE001 — 诊断日志见 except 内
                logger.exception(f"[WorkbenchPanel] 页面数据刷新失败: {type(page).__name__}")

    def _wire_page_signals(self, widget: QWidget) -> None:
        """通用页信号接线：页面若定义 ``workingDirChanged`` 则转发为面板同名信号

        （工作树页切 worktree / 恢复主仓库 / 清除根目录等场景由页面发信号，
        面板只做无差别转发，不识别页面语义。）
        """
        sig = getattr(widget, "workingDirChanged", None)
        if sig is not None and hasattr(sig, "connect"):
            try:
                sig.connect(self.workingDirChanged.emit)
            except Exception:
                logger.exception(f"[WorkbenchPanel] workingDirChanged 接线失败: {type(widget).__name__}")

    def _host_window(self):
        """返回宿主主窗口（TabManagerWindow）

        嵌入式下 parentWidget() 可能只是中间容器（如 #workbenchFrame），
        因此统一上溯到顶层窗口取 UI context。

        ★ 不能用裸 window()：hover 悬浮预览期间工作台 frame 会被 setParent
        到 HoverPreviewOverlay（Qt.Tool 顶层 owned 窗口），此时 panel.window()
        返回的是**浮层**而不是主窗口。浮层没有 _build_ui_context → 插件页拿到
        的 context 只剩 diff_requested_callback（backend 缺失 → 工作树页空列表、
        添加/删除/切换全部静默失效）。因此这里按"能构建 UI context 的顶层窗口"
        解析：先看缓存宿主，再按 window() → 全部顶层窗口的顺序找第一个具备
        _build_ui_context 的窗口。
        """
        cached = getattr(self, "_host_window_ref", None)
        if cached is not None:
            try:
                if hasattr(cached, "_build_ui_context"):
                    return cached
            except RuntimeError:
                pass  # C++ 对象已销毁
            self._host_window_ref = None

        candidates = []
        try:
            candidates.append(self.window())
        except Exception:
            pass
        try:
            from PyQt5.QtWidgets import QApplication

            candidates.extend(QApplication.topLevelWidgets())
        except Exception:
            pass
        for w in candidates:
            try:
                if w is not None and hasattr(w, "_build_ui_context"):
                    self._host_window_ref = w
                    return w
            except RuntimeError:
                continue  # C++ 对象已销毁
        return candidates[0] if candidates else self.parentWidget()

    def _page_context_incomplete(self, widget: Optional[QWidget]) -> bool:
        """插件页构建时是否只拿到了残缺 UI context（无 backend → 页面无数据源）

        判据看**值**不看键：``_make_page_widget`` 恒以 ``_build_ui_context()``
        的整体结果构造 context，启动早期 backend 尚未就绪时该键存在但值为
        None，只查 ``"backend" in ctx`` 会把「早期坏页」误判为完整，自愈不再
        触发（文件树页 project_root 恒空、工作树页提示未设置工作目录即此分支）。
        """
        ctx = getattr(widget, "_context", None)
        # 老签名插件页没有 _context，不做干预
        if not isinstance(ctx, dict):
            return False
        return not ctx.get("backend")

    # ── 项目 / 工作目录联动（UI 插件可选协议 on_project_changed） ──

    def _subscribe_project_changed(self) -> None:
        """订阅项目 / 工作目录变更：向当前插件页派发

        退订要点：UIEventBus 用 ``is`` 比较回调对象，bound method 每次取值都是
        新对象 —— 必须先把 ``self._on_project_changed_event`` 存进局部变量再
        同时用于 subscribe 与 unsubscribe，否则退订静默失效、留下悬挂回调。
        """
        try:
            from app.core.ui_event_bus import EV_PROJECT_CHANGED, UIEventBus

            bus = UIEventBus.get_instance()
            handler = self._on_project_changed_event
            bus.subscribe(EV_PROJECT_CHANGED, handler)
            self.destroyed.connect(lambda: bus.unsubscribe(EV_PROJECT_CHANGED, handler))
        except Exception as e:
            logger.warning(f"[WorkbenchPanel] 项目变更订阅失败: {e}")

    def _on_project_changed_event(self, payload: dict) -> None:
        """项目 / 工作目录变更：只对当前页派发

        非当前页不派发：用户切到该页时 ``set_current_tab`` →
        ``refresh_current_page_data()`` 会调页面 ``refresh_data()`` 补刷。
        """
        if not is_active_window(payload.get("window_id", "")):
            return
        dispatch_project_changed(
            self._stack.currentWidget(),
            project=payload.get("project", ""),
            workdir=payload.get("workdir", ""),
            window_id=payload.get("window_id", ""),
        )

    def _make_page_widget(self, info: Any) -> Optional[QWidget]:
        """构建插件页 widget（构造 parent + context，兼容无 context 的老签名）"""
        context: Dict[str, Any] = {}
        parent_win = None
        try:
            parent_win = self._host_window()
            if parent_win is not None and hasattr(parent_win, "_build_ui_context"):
                context = parent_win._build_ui_context()
        except Exception:
            context = {}
        # 差异回调注入 context，供插件版产物页触发
        context.setdefault("diff_requested_callback", self._emit_diff)
        # 插件页拉取最新宿主上下文的入口：工作台页的 context 是构造时快照，
        # 项目切换后 project_root 会过期（文件树页曾因此显示旧项目目录）。
        # 老页面不读该键，行为完全不变。
        if parent_win is not None and hasattr(parent_win, "_build_ui_context"):
            context["context_provider"] = lambda _w=parent_win: _w._build_ui_context()
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
        self._page_owner[info.page_id] = getattr(info, "plugin_name", "") or ""
        # 挂载点补刷：页面构造可能早于主题应用（启动期插件加载先于 Colors 就绪），
        # 构造期固化的 QSS 是旧主题色；此后无人再通知它。
        if hasattr(widget, "refresh_style"):
            widget.refresh_style()
        # 通用信号接线（页面若定义 workingDirChanged 则转发为面板同名信号）
        self._wire_page_signals(widget)

    def _destroy_plugin_page(self, page_id: str) -> None:
        """销毁插件页 widget（显式隐藏 + 移除布局，避免残影）"""
        widget = self._plugin_widgets.pop(page_id, None)
        self._page_owner.pop(page_id, None)
        if widget is not None:
            widget.hide()
            self._stack.removeWidget(widget)
            widget.deleteLater()

    # ── 页签 ──

    def set_current_tab(self, index: int, *, user: bool = False) -> None:
        """切换页签

        user=True 表示用户主动切换（页签点击 / 定向入口），仅此路径发射
        current_tab_changed 驱动宿主写入 per-window 页签记忆；程序化切换
        （切窗恢复 saved）不发射，避免污染活跃窗口记忆。

        ★ 越界保护：按窗口记忆恢复的页签可能已被卸载（卡片 tab 关闭 / 插件
        卸载）。此时 setCurrentIndex 是空操作而下方按钮循环会把全部按钮置
        非激活 —— 「tab 选中丢失」根因。越界时不强行跳页，仅把按钮高亮与
        stack 当前页重新对齐。

        ★ 切页后统一触发当前页 ``refresh_data()``（通用自拉，见
        ``refresh_current_page_data``）：宿主不识别页面语义，页面自己决定
        要不要重建（如产物页按数据签名跳过、历史页按子页签分流）。
        """
        if index < 0 or index >= self._stack.count():
            index = self._stack.currentIndex()
        changed = index != self._stack.currentIndex()
        self._stack.setCurrentIndex(index)
        for i, btn in enumerate(self._tab_buttons):
            btn.set_active(i == index)
        # 通知宿主记录（当前页签按对话窗口独立记忆，见 TabManagerWindow 回调）
        if user:
            self.current_tab_changed.emit(index)
        if changed:
            self.refresh_current_page_data()

    def current_tab(self) -> int:
        return self._stack.currentIndex()

    def current_tab_id(self) -> Optional[str]:
        """当前页签 id（越界/空页签返回 None）

        历史会话页等**插件页**的位置由注册序决定，宿主不应假设 index，
        一律用本方法 + ``set_current_tab_by_id`` 按 id 定位。
        """
        return self._tab_id_at(self._stack.currentIndex())

    # ── 数据入口（宿主驱动；仅任务区，页面数据由页面自拉） ──

    def update_todos(self, todos: List[Dict[str, Any]]) -> None:
        """刷新任务列表

        高度收敛由 TasksPage 的折叠回调（``_on_tasks_collapsed``）驱动，
        它已覆盖「无任务 / 折叠 / 展开」三种情形，此处不再重复 setSizes。
        """
        self.tasks_page.update_todos(todos)

    # ── 主题 ──

    def refresh_style(self) -> None:
        Colors.refresh()
        # 嵌入式：背景透明（由外层 #workbenchFrame 圆角矩形容器提供背景）
        self.setStyleSheet("QWidget#workbenchPanel { background: transparent; border: none; }")
        # splitter handle 边框色随主题（构造时用旧 Colors 固化）
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
        for btn in self._tab_buttons:
            btn.refresh_style()
        # ★ 逐页隔离分发：任何一页 refresh_style 抛异常都会中断后续分发
        #   （历史实锤：真机上 worktree 页残留 azure 旧色而 artifacts 正常，
        #   dispatch 外层 except 吞掉异常只留一行 warning）。每页独立捕获，
        #   异常带页类名落日志，保证一页炸不掉整面板。
        for _label, _page in (("tasks", self.tasks_page),):
            if _page is None or not hasattr(_page, "refresh_style"):
                continue
            try:
                _page.refresh_style()
            except Exception:  # noqa: BLE001 — 诊断日志见 except 内
                logger.exception(f"[WorkbenchPanel] 页面主题刷新失败: {_label} ({type(_page).__name__})")
        # 插件页签 / 卡片 tab（right 容器 UI 插件卡片）：外部不广播主题事件，
        # 面板统一分发；无 refresh_style 的插件页跳过
        for widget in self._plugin_widgets.values():
            if hasattr(widget, "refresh_style"):
                try:
                    widget.refresh_style()
                except Exception:  # noqa: BLE001
                    logger.exception(f"[WorkbenchPanel] 插件页主题刷新失败: {type(widget).__name__}")
        for entry in self._card_tabs.values():
            widget = entry.get("widget")
            if widget is not None and hasattr(widget, "refresh_style"):
                try:
                    widget.refresh_style()
                except Exception:  # noqa: BLE001
                    logger.exception(f"[WorkbenchPanel] 卡片页主题刷新失败: {type(widget).__name__}")

    def refresh_theme(self) -> None:
        """ThemeManager 协议入口（dispatch_refresh 只认 refresh_theme）

        ★ 此前只实现 refresh_style：register_refresh_target 注册了本面板，
        但 dispatch 的 hasattr(widget, "refresh_theme") 探测落空 → 静默跳过，
        主题切换时整面板（含工作树/产物插件页）都不刷新。
        """
        self.refresh_style()
