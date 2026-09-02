# -*- coding: utf-8 -*-
"""
右侧工作台浮层（WorkbenchPanel）— 纯悬浮，不挤压窗口内部元素

形态：TabManagerWindow 的 child widget，几何贴主窗口右侧（标题栏下方），
不进任何 layout，与桌宠（PixelPetWidget）同模式。由标题栏「右侧边栏」
按钮 toggle 显隐。

内置区域：
- 任务：todowrite 工具回传的待办列表（窗口级）—— 置顶常驻，不进页签
- 工作树（排第一，默认落点）：git 工作树切换 + 关键文档（原「记忆」页
  的 docs 子页原样上移，从二级子页签升为一级页签）
- 记忆：条目记忆 / 项目笔记（原 MemoryCardContent 共享单实例拆挂）
- 历史会话：宿主挂载当前活跃窗口的历史卡片（原对话区底部卡片迁移至此，
  懒挂载——窗口侧 _ensure_history_card / open_workbench_history 首次触发）

★ 产物页已完全插件化：面板**不再内置**产物实现，改由插件通过
``UIPluginRegistry.register_workbench_tab(plugin_name, page_id="artifacts", ...)``
注册（系统插件见 ``plugins/system/ui/_artifacts_page.py``）。
插件未注册时 index 2 显示 ``_PagePlaceholder`` 占位。

其它 page_id 的插件页追加在「工作树 / 记忆 / 产物」之后（见 sync_plugin_pages）。

数据由外部驱动（TabManagerWindow.refresh_workbench / MainWidget 推送），
面板自身不持有 backend 引用，便于测试与解耦。
"""

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QCursor
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
from app.utils.utils import _is_current_theme_light, get_font_family_css, get_icon
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
# 被 TasksPage 和 plugins/system/ui/_artifacts_page.py 共用。


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
        active_item: QWidget | None = None
        for t in todos:
            status = (t.get("status") or "pending") if isinstance(t, dict) else "pending"
            content = (t.get("content") or "") if isinstance(t, dict) else str(t)
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


class HistoryPage(QWidget):
    """历史会话页（一级页签）：历史会话 / 归档 子页签 + 列表上方搜索框

    由对话区底部历史卡片迁移而来，对齐记忆页的统一 tab 形态：去卡片框架，
    内容直接平铺在工作台页签下。HistoryCard 内容（会话列表本体）由宿主
    窗口 ``attach`` 挂载（每窗口单实例，显示内容跟随当前活跃窗口投影）。

    接口兼容：保留原 SystemCardFrame 的 ``tabChanged`` / ``set_current_tab`` /
    ``set_search_handler`` / ``set_extra_button_handler`` / ``_search_input`` /
    ``_current_tab`` / ``set_opacity`` 契约，宿主侧逻辑零改动。
    """

    closed = pyqtSignal()  # 兼容契约：页内无关闭钮，保留信号位（宿主连接不失效）
    tabChanged = pyqtSignal(str)  # 子页签切换（history / archived）

    SUB_TABS = (("history", "历史会话"), ("archived", "归档"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self._content: Optional[QWidget] = None
        self._current_tab = "history"
        self._search_input: Optional[QLineEdit] = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # ── 行1：子页签（居中，与记忆页统一）+ 右端导入按钮 ──
        tabs_row = QHBoxLayout()
        tabs_row.setSpacing(2)
        tabs_row.addStretch(1)
        self._sub_buttons: List[CustomTabButton] = []
        for tab_id, label in self.SUB_TABS:
            btn = CustomTabButton(tab_id, label, self)
            btn.clicked.connect(self._on_sub_tab_clicked)
            tabs_row.addWidget(btn)
            self._sub_buttons.append(btn)
        tabs_row.addStretch(1)
        self._import_btn = TransparentToolButton(get_icon("导入"), self)
        self._import_btn.setFixedSize(26, 26)
        self._import_btn.setToolTip("导入会话")
        self._import_btn.hide()  # handler 注入前隐藏（懒挂载期无导入能力）
        tabs_row.addWidget(self._import_btn)
        layout.addLayout(tabs_row)

        # ── 行2：搜索框（列表之上，整行） ──
        self._search_input = QLineEdit(self)
        self._search_input.setPlaceholderText("🔍 搜索会话...")
        self._search_input.setFixedHeight(24)
        self._apply_search_style()
        layout.addWidget(self._search_input)

        # ── 内容占位（attach 后隐藏） ──
        self._hint = _EmptyHint("历史会话未加载", self)
        layout.addWidget(self._hint, 1)

        # ── 内容滚动区：复刻原 SystemCardFrame 的 scroll_area > content_widget
        #    > content_layout 结构。★ HistoryCard 自己无布局，条目经
        #    get_content_layout() 沿父链上溯找 content_layout 属性后直接插入，
        #    去掉滚动容器会被压缩成一条条（2026-09-01 用户实测回归）。
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        Colors.refresh()
        self._scroll_area.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            + get_unified_scrollbar_style(8)
        )
        self._content_widget = QWidget()
        self._content_widget.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content_widget)
        self._content_layout.setContentsMargins(4, 2, 4, 2)
        self._content_layout.setSpacing(4)
        self._scroll_area.setWidget(self._content_widget)
        self._scroll_area.hide()  # attach 前隐藏（空滚动区会闪白底）
        layout.addWidget(self._scroll_area, 1)
        self._set_sub_tab_active(0)

    @property
    def content_layout(self):
        """内容布局（HistoryCard.get_content_layout() 沿父链上溯命中本属性）"""
        return self._content_layout

    # ── 子页签 ──

    def _on_sub_tab_clicked(self, tab_id: str) -> None:
        self.set_current_tab(tab_id)

    def set_current_tab(self, tab_id: str) -> None:
        """程序化切换子页签（变化时发射 tabChanged，由宿主驱动列表刷新）"""
        if tab_id not in {t for t, _ in self.SUB_TABS} or tab_id == self._current_tab:
            return
        self._current_tab = tab_id
        for btn in self._sub_buttons:
            btn.set_active(btn.tab_id == tab_id)
        self.tabChanged.emit(tab_id)

    def _set_sub_tab_active(self, index: int) -> None:
        for i, btn in enumerate(self._sub_buttons):
            btn.set_active(i == index)

    # ── 宿主注入（兼容原 SystemCardFrame 接口） ──

    def attach(self, content: Any) -> None:
        """挂载 HistoryCard 内容（原 content_layout.addWidget 等价：放进滚动区内容布局）"""
        if self._content is not None:
            return
        self._content = content
        self._hint.hide()
        self._content_layout.addWidget(content)
        content.show()
        self._scroll_area.show()

    def set_search_handler(self, placeholder: str, callback) -> None:
        """设置搜索框占位文本 + 文本变化回调（原卡片头部搜索框 → 列表上方）"""
        if self._search_input is None:
            return
        self._search_input.setPlaceholderText(placeholder)
        self._search_input.textChanged.connect(callback)

    def set_extra_button_handler(self, handler, icon=None, tooltip="") -> None:
        """注入导入按钮回调（原卡片标题栏额外按钮 → 子页签行右端）"""
        if icon is not None:
            self._import_btn.setIcon(icon)
        self._import_btn.setToolTip(tooltip or "导入会话")
        self._import_btn.clicked.connect(handler)
        self._import_btn.show()

    def set_opacity(self, opacity: float) -> None:
        """透明度联动契约（原 SystemCardFrame 为空实现，此处同语义）"""

    def _apply_search_style(self) -> None:
        Colors.refresh()
        self._search_input.setStyleSheet(
            f"""
            QLineEdit {{
                background: {Colors.HOVER_BG};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                color: {Colors.TEXT_PRIMARY};
                padding: 2px 8px;
                {font_size_css(11)}
                {get_font_family_css()}
            }}
            QLineEdit:focus {{
                border: 1px solid {Colors.TEXT_ACCENT};
            }}
            QLineEdit::placeholder {{
                color: {Colors.INPUT_PLACEHOLDER};
            }}
        """
        )

    def refresh_style(self) -> None:
        self._hint.refresh_style()
        for btn in self._sub_buttons:
            btn.refresh_style()
        if self._search_input is not None:
            self._apply_search_style()


class WorktreePage(QWidget):
    """工作树页（一级页签，始终排第一）：git 工作树切换 + 关键文档

    内容 = 共享 MemoryCardContent 的 docs 子页**原样上移**（关键文档从
    「记忆」页的二级子页签升为一级页签；git 工作树组件本就插在关键文档
    列表内，随子页一起上移，内容零改动）。默认页签：打开右侧边栏时
    非插件定向入口都落在本页。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._content: Optional[QWidget] = None
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._hint = _EmptyHint("长期记忆未就绪", self)
        self._layout.addWidget(self._hint)

    def attach(self, content: Any) -> None:
        """挂载共享 MemoryCardContent 的 docs 子页（reparent 到本页）"""
        if self._content is not None:
            return
        self._content = content
        self._hint.hide()
        docs = content.detach_tab("docs")
        if docs is not None:
            docs.setParent(self)
            self._layout.addWidget(docs, 1)
            docs.show()
        # 工作树页为默认落点：同步内部激活子页状态（不重复刷数据，
        # 数据由宿主随后的 update_project 驱动）
        content.set_active_tab("docs", refresh=False)

    def set_project(self, project: str, workdir: Optional[str] = None) -> None:
        if self._content is not None:
            self._content.set_project(project, workdir)

    def refresh_style(self) -> None:
        self._hint.refresh_style()


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
    # 工作树页内工作目录变更（切 worktree/恢复主仓库/清除根目录）→ 宿主转发给活跃窗口
    workingDirChanged = pyqtSignal(str)
    # 差异请求：file_paths 为 None 表示「查看所有产物差异」
    diff_requested = pyqtSignal(object)  # Optional[List[str]]
    # 卡片 tab × 关闭钮点击（registry 连接本信号，同步清理卡片归属状态并摘 tab）
    card_tab_close_requested = pyqtSignal(str)  # card_id
    # 切到「历史会话」页时发射：宿主窗口此时刷新历史列表数据（面板隐藏期间
    # isVisible()=False 会被 refresh_history_card_if_visible 跳过，靠本信号补刷）
    history_tab_shown = pyqtSignal()
    # 当前页签变化（含程序化切换）：宿主用于按对话窗口独立记忆页签
    current_tab_changed = pyqtSignal(int)

    TAB_WORKTREE, TAB_ARTIFACTS, TAB_HISTORY = 0, 1, 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchPanel")
        # 嵌入式：宽度交给外层 splitter 拖拽，这里只给 min/max 约束
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumWidth(PANEL_WIDTH_MIN)
        self.setMaximumWidth(PANEL_WIDTH_MAX)

        # 当前任务区高度（splitter 拖拽持久化用）
        self._tasks_height = TASKS_DEFAULT_HEIGHT
        # 记忆内容单实例（工作树/记忆两页共用，见 ensure_memory）；None = 未构建
        self._memory_content: Optional[QWidget] = None
        # 构建前收到的项目信息（ensure_memory 后补投递）
        self._pending_project: Optional[tuple] = None
        # 页签记忆：None = 面板尚未打开过（首次打开默认第一个页签，之后恢复上次关闭时页签）
        self._last_tab_index: Optional[int] = None
        # 历史会话页内容（宿主当前活跃窗口的历史卡片框架）；None = 未挂载（页签不出现）
        self._history_page: Optional[QWidget] = None
        # 产物页：插件注册时的来源签名（None = 显示占位页）
        self._artifacts_plugin_sig: Optional[tuple] = None
        self._plugin_artifacts_widget: Optional[QWidget] = None
        self._artifacts_plugin_info: Optional[Any] = None

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

        # 内容栈
        # index 0 = 工作树页（git 工作树 + 关键文档，默认落点）
        # index 1 = 产物页槽位（插件提供；未注册时为 _PagePlaceholder 占位）
        # index 2 = 历史会话页；index 3+ = 其它插件页
        self._stack = QStackedWidget(self._bottom)
        self._artifacts_placeholder = _PagePlaceholder(parent=self._stack)
        self.artifacts_page: QWidget = self._artifacts_placeholder
        self.worktree_page = WorktreePage(self._stack)
        self._stack.addWidget(self.worktree_page)
        self._stack.addWidget(self._artifacts_placeholder)
        self._bottom_layout.addWidget(self._stack, 1)

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
        self._plugin_infos: Dict[str, Any] = {}
        self._plugin_sig: Optional[tuple] = None
        # 动态卡片 tab（right 容器 UI 插件卡片，见 open_card_tab）
        # {card_id: {"label": str, "widget": QWidget}}；widget 生命周期归 registry 管
        self._card_tabs: Dict[str, Dict[str, Any]] = {}

        self._rebuild_tab_bar()
        # 初始默认选中第一个页签（非「默认工作树」特判；当前首个页签恰为工作树）
        self.set_current_tab(0)
        self.refresh_style()

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

    # ── 历史会话页（宿主挂载当前活跃窗口的历史卡片） ──

    def attach_history_page(self, widget: Optional[QWidget]) -> None:
        """挂载「历史会话」页内容（宿主当前活跃窗口的历史卡片框架）

        工作台是宿主级单例，历史页与其他页同一投影语义：内容始终来自
        「当前活跃对话窗口」，窗口切换时由 refresh_workbench → 本方法换挂。
        换挂时旧页不销毁（setParent(None) 还给它所属窗口，其 MainWidget
        仍持有引用与信号连接，切回时再挂回来）。

        Args:
            widget: 窗口的历史卡片框架（SystemCardFrame）；None 跳过（幂等）
        """
        if widget is None or self._history_page is widget:
            return
        # ★ 换挂/首次挂载后页签集合变化：后续页签 index 整体平移（insert 前插
        # 时 Qt 递增 currentIndex、remove 前删时递减，保持同一 current widget），
        # 依赖 Qt 的 index 记账容易在组合路径下错位。这里统一按「此前所在页签的
        # tab_id」重新定位：正显示历史页则延续新窗口的历史页（跨窗口不跳页），
        # 停在插件/卡片页则按 id 找回新 index，页面与高亮都不丢。
        prev_id = self._tab_id_at(self._stack.currentIndex())
        if self._history_page is not None:
            old = self._history_page
            idx = self._stack.indexOf(old)
            self._history_page = None
            if idx >= 0:
                self._stack.removeWidget(old)
                old.setParent(None)
        self._history_page = widget
        # 固定插在 index 3（工作树/记忆/产物之后、插件页之前），页签顺序稳定
        self._stack.insertWidget(self.TAB_HISTORY, widget)
        self._rebuild_tab_bar()
        # 还原用户此前所在页签：tab 集合变化后按 id 重新定位；此前正显示历史页
        # 则延续显示新窗口的历史页（跨窗口切换不跳页的既有语义）
        if prev_id is not None:
            restore_idx = self._tab_id_index(prev_id)
            if restore_idx is not None and restore_idx != self._stack.currentIndex():
                self.set_current_tab(restore_idx)

    def detach_history_page(self, widget: QWidget) -> None:
        """摘除「历史会话」页（窗口关闭时由宿主窗口调用，防 stack 悬空引用）

        仅当当前挂载的就是该 widget 时生效（幂等）。摘除后页签条同步重建，
        当前正显示历史页时 QStackedWidget 已自动切到邻近页，这里同步按钮高亮。
        """
        if self._history_page is not widget:
            return
        self._history_page = None
        idx = self._stack.indexOf(widget)
        if idx >= 0:
            self._stack.removeWidget(widget)
            widget.setParent(None)
        self._rebuild_tab_bar()
        self.set_current_tab(max(0, self._stack.currentIndex()))

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
        specs: List[tuple] = [
            ("worktree", "工作树"),
            ("artifacts", art_label),
        ]
        # 历史会话页：宿主挂载历史卡片后常驻（懒挂载，见 attach_history_page），
        # 顺序与 stack.insertWidget(TAB_HISTORY) 严格一致
        if self._history_page is not None:
            specs.append(("history", "历史会话"))
        # _plugin_infos 已排除 artifacts（见 sync_plugin_pages），此处无需再过滤
        for page_id, info in self._plugin_infos.items():
            specs.append((page_id, info.label))
        # 动态卡片 tab 追加在内置 + 插件页之后（与 stack 追加顺序一致）
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

    def _tab_id_at(self, index: int) -> Optional[str]:
        """按当前页签顺序取 index 对应的 tab_id（越界返回 None）"""
        if 0 <= index < len(self._tab_ids):
            return self._tab_ids[index]
        return None

    # ── 插件页签 reconcile（宿主在 refresh_workbench 时调用） ──

    def sync_plugin_pages(self, tabs: List[Any]) -> None:
        """按 UIPluginRegistry 的 workbench_tabs 增删插件页（签名不变则跳过重建）

        产物页特例：``page_id="artifacts"`` 是**保留 id**，插件注册它即填充
        index 2 的产物页槽位（面板本身不提供产物实现）。未注册时显示占位页。
        其余 page_id 按注册序追加在「工作树 / 记忆 / 产物」之后。
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
            # 集合未变也补一次页签条 reconcile：产物页 label 未入 sig，
            # 插件注册/改名后标签可能残留旧值（_rebuild_tab_bar 内部再做
            # id+label 比对，集合未变时仅重算 hover，成本为零）
            self._rebuild_tab_bar()
            return
        self._plugin_infos = infos
        # 当前页是否是被卸载的插件页（卸载后 Qt 会自动切到邻近页，需回落第一个页签）
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
        # 当前页被移除（插件页）或越界时回落第一个页签（默认落点，非工作树特判）
        if was_plugin_current or current >= self._stack.count() or current < 0:
            current = 0
        self.set_current_tab(current)

    # ── 产物页槽位（完全插件化，index 2 恒定） ──

    def _use_plugin_artifacts(self, info: Any) -> None:
        """用插件版产物页**替换** index 2 的当前内容（占位页或旧插件页）

        ★ 必须是"替换"而非 insertWidget：QStackedWidget.insertWidget(TAB_ARTIFACTS, w)
        会把原 index 2 及其后所有页**整体后移**，导致后续插件页与 tab 错位
        （表现为插件页显示出产物内容）。因此这里先移除旧页再插入，
        保证 stack 索引与 tab 顺序严格一致：
        0=工作树 / 1=记忆 / 2=产物 / 3+=其它插件页。
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

    def set_current_tab(self, index: int, *, user: bool = False) -> None:
        """切换页签；记忆内容首次进入前由宿主调 ensure_memory 构建

        user=True 表示用户主动切换（页签点击 / 定向入口），仅此路径发射
        current_tab_changed 驱动宿主写入 per-window 页签记忆；程序化切换
        （历史页换挂延续、切窗恢复 saved）不发射，避免污染活跃窗口记忆。

        ★ 越界保护：按窗口记忆恢复的页签可能已被卸载（卡片 tab 关闭 / 插件
        卸载 / 历史页摘除）。此时 setCurrentIndex 是空操作而下方按钮循环会把
        全部按钮置非激活 —— 「tab 选中丢失」根因。越界时不强行跳页，仅把
        按钮高亮与 stack 当前页重新对齐。
        """
        if index < 0 or index >= self._stack.count():
            index = self._stack.currentIndex()
        self._stack.setCurrentIndex(index)
        for i, btn in enumerate(self._tab_buttons):
            btn.set_active(i == index)
        # 停在工作树页时同步内部激活子页状态
        # （MemoryCardContent._current_tab 驱动 set_search_filter 等分发）
        if index == self.TAB_WORKTREE and self._memory_content is not None:
            self._memory_content.set_active_tab("docs", refresh=False)
        if index == self.TAB_HISTORY:
            self.history_tab_shown.emit()
        # 通知宿主记录（当前页签按对话窗口独立记忆，见 TabManagerWindow 回调）
        if user:
            self.current_tab_changed.emit(index)

    def current_tab(self) -> int:
        return self._stack.currentIndex()

    def ensure_memory(self, memory_manager: Any) -> None:
        """懒构建记忆内容（宿主 refresh_workbench 调用；避免初始化期拉起存储层）

        单实例 MemoryCardContent：docs 子页挂到 WorktreePage（关键文档+工作树）。
        工作目录变更经 workingDirChanged 信号转发宿主（每窗口实例缓存/分支标签刷新）。
        """
        if memory_manager is None or self._memory_content is not None:
            return
        from app.widgets.cards.settings.memory_card import MemoryCardContent

        content = MemoryCardContent(memory_manager, self)
        content.hide()  # 容器自身不显示，只有拆出的子页显示
        content.workingDirChanged.connect(self.workingDirChanged.emit)
        self._memory_content = content
        self.worktree_page.attach(content)
        if self._pending_project is not None:
            project, workdir = self._pending_project
            self._pending_project = None
            content.set_project(project, workdir)
        # 当前一级页签若停在工作树页，同步内部激活子页状态（分发依赖）
        if self._stack.currentIndex() == self.TAB_WORKTREE:
            content.set_active_tab("docs", refresh=False)

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
        """同步当前项目到记忆内容（未构建时缓存，ensure_memory 后补投递）"""
        self._pending_project = (project or "", workdir)
        if self._memory_content is not None:
            self._memory_content.set_project(project, workdir)

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
        self.artifacts_page.refresh_style()
        self.tasks_page.refresh_style()
        self.worktree_page.refresh_style()
        if self._memory_content is not None and hasattr(self._memory_content, "refresh_style"):
            self._memory_content.refresh_style()
        if self._history_page is not None and hasattr(self._history_page, "refresh_style"):
            self._history_page.refresh_style()
        # 插件页签 / 卡片 tab（right 容器 UI 插件卡片）：外部不广播主题事件，
        # 面板统一分发；无 refresh_style 的插件页跳过
        for widget in self._plugin_widgets.values():
            if hasattr(widget, "refresh_style"):
                widget.refresh_style()
        for entry in self._card_tabs.values():
            widget = entry.get("widget")
            if widget is not None and hasattr(widget, "refresh_style"):
                widget.refresh_style()
