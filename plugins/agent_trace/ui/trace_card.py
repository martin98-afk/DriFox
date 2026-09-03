# -*- coding: utf-8 -*-
"""agent_trace.TraceCardWidget — 主容器卡片（full 容器，v4）。

布局对齐 Chrome DevTools **Network** 面板（顶部工具条 / 概览条各自独立一行，
下方「列表 + 详情」并排）：

    ┌──────────────────────────────────────────────────────────────┐
    │ ● 记录中 23 条  [全部][系统][用户][钩子][助手][工具]   🔍 搜索 │ 工具条 44px
    │                                      [Duration|Turns|Calls] ⌫ │
    ├──────────────────────────────────────────────────────────────┤
    │  0s      2.5s      5.0s      7.5s     10.2s                   │ 时间线（全宽）
    │  Input ▓▓▓▓░░░░▓▓▓▓▓▓▓▓░░░░░░░░                              │
    │  Model    ░░░▓▓▓▓▓▓▓░░░░▓▓▓▓▓░░                               │
    │  Tools          ░░▓▓░░░▓▓░░░▓▓░                               │
    ├───────────────────────────────────┬──────────────────────────┤
    │ Name │ Type │ Size │ Time │ Water │  详情（按类型给 tab）     │
    │ ▓▓▓▓ │ TOOL │ 1.2kB│ 830ms│ ▬▬▬   │  Request/Response/…      │
    ├───────────────────────────────────┴──────────────────────────┤
    │ 23 条 · 已筛选 8  │ LLM 4.2s │ 工具 8.1s │ 总计 12.3s        │ 汇总栏
    └──────────────────────────────────────────────────────────────┘

v4 相对 v3 的变化：
- **工具条独占一行**（过滤 chips + 搜索 + 视图切换）。
- **时间线独占一行且横跨全宽**（v3 挤在左栏顶部，宽度只有一半）。
- **列表与详情同一行**（水平 splitter）。
- 列表升级为 Network 风格多列表格（Name/Type/Size/Time/Waterfall + 排序）。
- 详情按条目类型动态给出 tab（System Prompt / Tools Schema / Request /
  Response / Timing …），不再是所有条目都套 Summary/Preview/Raw/Source。
- 所有颜色走 :class:`ThemePalette`：修「深色主题下 Input/Model/Tools 与刻度
  是黑字」（主题色是 rgba 字符串，旧代码直接 ``QColor(...)`` 解析失败返回黑）。

v3 保留：**多标签页隔离** —— 每个对话标签页有独立 backend；
:class:`TraceCollectorHub` 为每个窗口持有一个常驻 collector 后台持续收集
（切走也在记 timing），卡片 showEvent / ``EV_TAB_SWITCHED`` 时切换到对应
collector。
"""

from __future__ import annotations

import time
import weakref
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import SearchLineEdit

from .detail_panel import DetailPanel
from .timeline_panel import TimelinePanel
from .trace_collector import TraceCollector, TraceCollectorHub
from .trace_models import (
    EntryKind,
    ThemePalette,
    TraceRecord,
    format_duration,
    time_bounds,
    with_alpha,
)
from .turn_list_widget import TurnListWidget

TOP_BAR_H = 46
BOTTOM_BAR_H = 30

_PLUGIN_NAME = "agent_trace"

# 类型过滤 chips 已下放到 TurnListWidget（时间线下方、列表上方），顶栏不再承载

# tools schema 拉取节流（秒）— 每次都拉会把 AgentManager 拉起来，卡 UI
_SCHEMA_TTL = 60.0


class _StatusDot(QWidget):
    """状态指示点：绿=空闲/完成，金=有 in-flight，红=最近有失败。"""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setFixedSize(8, 8)
        self._pal = ThemePalette()
        self._state = "idle"

    def set_palette(self, pal: ThemePalette) -> None:
        self._pal = pal
        self.update()

    def set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            c = QColor(
                self._pal.danger
                if self._state == "error"
                else (self._pal.warning if self._state == "busy" else self._pal.success)
            )
            p.setPen(Qt.NoPen)
            p.setBrush(with_alpha(c, 90))
            p.drawEllipse(self.rect().adjusted(-2, -2, 2, 2))
            p.setBrush(c)
            p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        finally:
            p.end()


class TraceCardWidget(QWidget):
    """轨迹主组件（浮动卡片，container='full'）。"""

    # ⚠️ 必须是 Signal：UIPluginRegistry._show_floating_card 会对 closed.connect(...)
    closed = pyqtSignal(str)

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._ctx: Dict[str, Any] = {}
        self._ctx_provider: Optional[Any] = None
        self._hub = TraceCollectorHub(self)
        self._collector: Optional[TraceCollector] = None
        self._active_wid: str = ""
        # 当前展示的 session_id（同窗口幂等判据 —— 见 _switch_collector）
        self._active_sid: str = ""
        self._context_tokens: int = 0
        self._context_limit: int = 0
        self._pal = ThemePalette()
        self._fs = 13
        self._schema_ts = 0.0
        self._build_ui()
        self._build_timer()

    # ──────────────────── 搭建 ────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ① 工具条（独占一行）
        outer.addWidget(self._build_top_bar())

        # ② 时间线（独占一行，全宽）
        self._timeline = TimelinePanel(self)
        outer.addWidget(self._timeline)

        # ③ 列表 + 详情（同一行）
        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setHandleWidth(6)
        splitter.setChildrenCollapsible(False)
        self._turn_list = TurnListWidget(splitter)
        self._turn_list.setMinimumWidth(420)
        self._detail = DetailPanel(splitter)
        self._detail.setMinimumWidth(320)
        splitter.addWidget(self._turn_list)
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([880, 520])
        outer.addWidget(splitter, 1)
        self._splitter = splitter
        self._detail_sizes: Optional[List[int]] = None  # 详情收起前的宽度（展开时恢复）
        self._detail.hide()  # 初始无选中 → 详情收起，点选条目再展开

        # ④ 汇总栏
        outer.addWidget(self._build_bottom_bar())

        # 信号
        self._turn_list.recordSelected.connect(self._on_record_selected)
        self._timeline.recordClicked.connect(self._on_record_clicked)
        self._timeline.rangeSelected.connect(self._turn_list.set_time_range)
        self._timeline.rangeCleared.connect(self._turn_list.clear_time_range)
        self._turn_list.timeRangeCleared.connect(self._timeline.clear_range)
        self._detail.dismissRequested.connect(self._on_detail_dismissed)
        self._search_box.textChanged.connect(self._turn_list.set_search)

    def _build_top_bar(self) -> QWidget:
        bar = QFrame(self)
        bar.setFixedHeight(TOP_BAR_H)
        bar.setObjectName("agentTraceTopBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 0, 12, 0)
        layout.setSpacing(8)

        # 顶栏只放状态 + 操作；类型过滤 chips 挪到「时间线下方、列表上方」，
        # 否则一行塞 11 个控件在窄窗口下会互相挤压。
        self._dot = _StatusDot(bar)
        layout.addWidget(self._dot, 0, Qt.AlignVCenter)
        self._status_label = QLabel("记录中", bar)
        layout.addWidget(self._status_label)

        layout.addStretch(1)

        # 三个**独立开关**（不是互斥 tab）：
        #   Duration 开=按真实时间比例画条带，关=每条固定宽度
        #   Turns    开=先按轮次等分整条轴
        #   Calls    开=只画 Tools 泳道
        self._flag_btns: Dict[str, QPushButton] = {}
        for key, label, tip in (
            ("duration", "Duration", "开：条带宽度按真实时间比例；关：每条等宽"),
            ("turns", "Turns", "开：按对话轮次等分时间轴"),
            ("calls", "Calls", "开：只显示 Tools 泳道"),
        ):
            btn = QPushButton(label, bar)
            btn.setCheckable(True)
            # 默认全关：Duration 关 = 每条等宽（固定长度），开启才按真实时间比例
            btn.setChecked(False)
            btn.setFixedHeight(26)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(tip)
            btn.toggled.connect(lambda _c=False, k=key: self._on_flag_toggled(k))
            self._flag_btns[key] = btn
            layout.addWidget(btn)

        self._search_box = SearchLineEdit(bar)
        self._search_box.setPlaceholderText("搜索内容 / 工具名…")
        self._search_box.setFixedWidth(220)
        self._search_box.setFixedHeight(28)
        self._search_box.setClearButtonEnabled(True)
        layout.addWidget(self._search_box)
        return bar

    def _build_bottom_bar(self) -> QWidget:
        bar = QFrame(self)
        bar.setFixedHeight(BOTTOM_BAR_H)
        bar.setObjectName("agentTraceBottomBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 0, 14, 0)
        layout.setSpacing(0)
        self._stats_turns = QLabel("0 条", bar)
        self._stats_time = QLabel("LLM - · 工具 -", bar)
        self._stats_ctx = QLabel("上下文 -", bar)
        self._stats_total = QLabel("", bar)
        widgets = (self._stats_turns, self._stats_time, self._stats_ctx)
        for i, lbl in enumerate(widgets):
            layout.addWidget(lbl)
            if i < len(widgets) - 1:
                sep = QLabel("   |   ", bar)
                sep.setObjectName("agentTraceStatSep")
                layout.addWidget(sep)
        layout.addStretch(1)
        layout.addWidget(self._stats_total)
        self._bottom_bar = bar
        return bar

    def _build_timer(self) -> None:
        """1s 心跳：in-flight 时长走动（只重绘，不重建）。"""
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()

    # ──────────────────── 上下文注入 ────────────────────

    def set_context_provider(self, provider: Any) -> None:
        """拉模型接口（主程序优先调用）：保存 provider 并按需拉取最新上下文。"""
        self._ctx_provider = provider
        self._subscribe_events()
        self._refresh_context()

    def set_context(self, ctx: Dict[str, Any]) -> None:
        """推模型兼容接口（旧主程序 / 冒烟测试）。"""
        self._ctx = dict(ctx or {})
        self._apply_latest_theme()
        main_widget = self._ctx.get("main_widget")
        if main_widget is not None:
            self._switch_collector(main_widget)
        else:
            self._pull_records()

    def _subscribe_events(self) -> None:
        """订阅 EV_TAB_SWITCHED（幂等；weakref 防悬挂回调引用已销毁卡片）。"""
        if getattr(self, "_events_subscribed", False):
            return
        try:
            from app.core.ui_event_bus import EV_TAB_SWITCHED, UIEventBus

            weak_self = weakref.ref(self)

            def _on_tab_switched(payload: Dict[str, Any], _ref=weak_self) -> None:
                card = _ref()
                if card is not None:
                    card._on_tab_switched(payload)

            UIEventBus.get_instance().subscribe(EV_TAB_SWITCHED, _on_tab_switched, plugin_name=_PLUGIN_NAME)
            self._events_subscribed = True
        except Exception as e:
            logger.warning(f"[agent_trace] 订阅 EV_TAB_SWITCHED 失败: {e}")

    def _on_tab_switched(self, payload: Dict[str, Any]) -> None:
        if not self.isVisible():
            return
        # 🚀 同窗口切换零动作：collector 常驻且由 backend 信号驱动同步（切走
        # 也在记 timing），数据不会过期；主题/字体未变，重刷样式与全量投影
        # 纯冗余（长会话 _project_messages 全量遍历是切标签卡顿主源之一）。
        wid = str(payload.get("window_id") or "")
        if wid and wid == self._active_wid:
            return
        self._refresh_context()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._ctx_provider is not None:
            self._refresh_context()

    def _refresh_context(self) -> None:
        if self._ctx_provider is None:
            return
        try:
            ctx = self._ctx_provider()
        except Exception as e:
            logger.warning(f"[agent_trace] ctx_provider() 失败: {e}")
            return
        if not ctx:
            return
        self._ctx = dict(ctx)
        # 🚀 主题/字体指纹未变时跳过全量样式重刷：QSS 重设会触发全面板重绘，
        # 切标签/重复 showEvent 的高频路径纯冗余；主题切换时指纹变化才真刷。
        fp = (
            tuple(sorted((k, str(v)) for k, v in (ctx.get("colors") or {}).items())),
            bool(ctx.get("is_dark", True)),
            int(ctx.get("font_size") or 13),
            str(ctx.get("font_family") or ""),
        )
        theme_changed = fp != getattr(self, "_theme_fp", None)
        self._theme_fp = fp
        if theme_changed:
            self._apply_latest_theme()
        main_widget = self._ctx.get("main_widget")
        if main_widget is not None:
            self._switch_collector(main_widget)

    # ──────────────────── 主题 ────────────────────

    def _apply_latest_theme(self) -> None:
        colors = dict(self._ctx.get("colors") or {})
        is_dark = bool(self._ctx.get("is_dark", True))
        # ctx.font_size 是「已应用缩放的像素值」→ 必须 setPixelSize（勿当磅值用）
        self._fs = max(10, int(self._ctx.get("font_size") or 13))
        self._pal = ThemePalette.from_theme(colors, is_dark, font_px=self._fs)
        # ⚠️ UI 字体一律取 ctx.font_family（系统字体），只有代码区用 mono_family
        self._pal.font_family = self._ctx.get("font_family") or self._pal.font_family

        self._timeline.set_palette(self._pal)
        self._turn_list.set_palette(self._pal)
        self._detail.set_palette(self._pal)
        self._dot.set_palette(self._pal)
        self._style_bars()

        f = QFont(self._pal.font_family)
        f.setPixelSize(self._fs)
        self.setFont(f)
        self._timeline._apply_font(f)
        self._turn_list._apply_font(f)
        self._detail._apply_font(f)

    def _style_bars(self) -> None:
        pal = self._pal
        fs = self._fs
        small = max(10, fs - 2)
        top = self.findChild(QFrame, "agentTraceTopBar")
        if top is not None:
            top.setStyleSheet(
                f"QFrame#agentTraceTopBar {{ background: transparent;"
                f" border-bottom: 1px solid {pal.q('border')}; }}"
                f"QFrame#agentTraceTopBar QPushButton {{"
                f"  background: transparent; color: {pal.q('text_secondary')};"
                f"  border: 1px solid transparent; border-radius: 5px; padding: 0 10px;"
                f"  font-family: '{pal.font_family}'; font-size: {max(10, fs - 1)}px; }}"
                f"QFrame#agentTraceTopBar QPushButton:hover {{ background: {pal.q('line', 22)}; }}"
                f"QFrame#agentTraceTopBar QPushButton:checked {{"
                f"  color: {pal.q('accent')}; border: 1px solid {pal.q('accent')};"
                f"  background: {pal.q('accent', 26)}; }}"
                f"QFrame#agentTraceTopSep {{ background: {pal.q('border')}; }}"
            )
        self._status_label.setStyleSheet(
            f"color: {pal.q('text_muted')}; font-family: '{pal.font_family}'; font-size: {small}px;"
        )
        self._search_box.setStyleSheet(
            f"SearchLineEdit {{ background: {pal.q('line', 14)}; border: 1px solid {pal.q('border')};"
            f"  border-radius: 5px; color: {pal.q('text')};"
            f"  font-family: '{pal.font_family}'; font-size: {max(10, fs - 1)}px; }}"
        )
        bottom = self.findChild(QFrame, "agentTraceBottomBar")
        if bottom is not None:
            bottom.setStyleSheet(
                f"QFrame#agentTraceBottomBar {{ background: transparent;"
                f" border-top: 1px solid {pal.q('border')}; }}"
                f"QFrame#agentTraceBottomBar QLabel {{ color: {pal.q('text_muted')};"
                f"  font-family: '{pal.font_family}'; font-size: {small}px; background: transparent; }}"
            )
        # splitter 手柄透明：详情面板自带底色 + 左分隔线（DetailPanel.paintEvent），
        # 加宽手柄只为拖拽热区，视觉边界交给面板自身
        self._splitter.setStyleSheet("QSplitter::handle:horizontal { background: transparent; }")

    # ──────────────────── collector 切换 ────────────────────

    def _switch_collector(self, main_widget: Any) -> None:
        """切换到目标窗口的常驻 collector（同窗口 + 同会话幂等）。

        ⚠️ 「同窗口直接 return」是有前提的：collector 的数据新鲜度依赖 backend
        信号（``_hook_messages_updated`` / ``tool_*`` / ``stream_*``）。但
        **加载历史会话不走任何 backend 信号** —— 主程序走的是
        ``main_widget.session_manager.set_current_session(session)``，既不发
        ``session_changed``（只有 ``backend.set_current_session`` 才发），也不发
        ``_hook_messages_updated``。结果是：加载完历史会话后没有任何东西驱动
        重新投影，卡片一直停在旧会话上（表现为「轨迹不显示 / 显示的还是上一个
        会话」）。故同窗口分支也必须比对 session_id，变了就重投影。
        """
        wid = getattr(main_widget, "_window_id", "") or ""
        sid = self._session_id_of(main_widget)
        if wid and wid == self._active_wid and self._collector is not None:
            # 同窗口：collector 常驻且由 backend 信号驱动同步（切走也在记
            # timing），常规链路数据不过期，无需全量重投影（长会话投影是切
            # 标签卡顿主源之一）。仅当 session_id 变化（加载历史会话等静默
            # 切换）才补一次投影 —— 比对是 O(1)，不是性能热点。
            if sid and sid == self._active_sid:
                return
            self._active_sid = sid
            self._collector.refresh()
            return

        self._unbind_collector_signals()
        self._unbind_backend_stats_signals()
        self._collector = self._hub.collector_for(main_widget)
        self._active_wid = wid if self._collector is not None else ""
        self._active_sid = sid if self._collector is not None else ""

        if self._collector is not None:
            self._collector.refresh()
            self._bind_collector_signals(self._collector)
            self._bind_backend_stats_signals(main_widget)
            self._pull_records()
            self._hub.cleanup_closed(self._active_window_ids())
        else:
            # backend 未就绪：清空展示，等下次 show/tab 切换重试
            self._pull_records()

    @staticmethod
    def _session_id_of(main_widget: Any) -> str:
        """当前窗口 backend 的 session_id；解析不到返回空串（= 不做幂等短路）。"""
        backend = getattr(main_widget, "backend", None)
        if backend is None:
            return ""
        try:
            session = backend.get_current_session()
        except Exception:
            return ""
        return str(getattr(session, "session_id", "") or "")

    @staticmethod
    def _active_window_ids() -> Optional[set]:
        """当前存活的对话窗口 id 集合（非 Tab 模式返回 None = 不清理）。"""
        try:
            from app.widgets.tab_manager_window import TabManagerWindow

            tm = TabManagerWindow.get_instance()
            if tm is None:
                return None
            return {getattr(w, "_window_id", "") for w in tm._windows}
        except Exception:
            return None

    def _bind_collector_signals(self, c: TraceCollector) -> None:
        c.recordsReset.connect(self._on_records_reset)
        c.recordsAppended.connect(self._on_records_appended)
        c.recordsUpdated.connect(self._on_records_updated)
        c.tailChanged.connect(self._on_tail_changed)

    def _unbind_collector_signals(self) -> None:
        if self._collector is None:
            return
        for sig, slot in (
            ("recordsReset", self._on_records_reset),
            ("recordsAppended", self._on_records_appended),
            ("recordsUpdated", self._on_records_updated),
            ("tailChanged", self._on_tail_changed),
        ):
            try:
                getattr(self._collector, sig).disconnect(slot)
            except TypeError, RuntimeError:
                pass

    def _bind_backend_stats_signals(self, main_widget: Any) -> None:
        """订阅 backend 的上下文/会话信号（切窗口时与 collector 同步换绑）。"""
        backend = getattr(main_widget, "backend", None)
        if backend is None:
            return
        try:
            if hasattr(backend, "context_updated"):
                backend.context_updated.connect(self._on_context_updated)
        except Exception:
            pass
        try:
            if hasattr(backend, "session_changed"):
                backend.session_changed.connect(self._on_session_changed)
        except Exception:
            pass

    def _unbind_backend_stats_signals(self) -> None:
        c = self._collector
        if c is None:
            return
        be = getattr(c, "_bound_backend", None)
        if be is None:
            return
        for name, slot in (("context_updated", self._on_context_updated), ("session_changed", self._on_session_changed)):
            sig = getattr(be, name, None)
            if sig is None:
                continue
            try:
                sig.disconnect(slot)
            except TypeError, RuntimeError:
                pass

    # ──────────────────── collector 信号 → UI ────────────────────

    def _visible(self) -> List[TraceRecord]:
        if self._collector is None:
            return []
        return self._collector.visible_records

    def _stable(self) -> List[TraceRecord]:
        """纯落盘投影（不含 tail）—— turn_list 的索引空间基准。

        ⚠️ collector 的增量信号（appended/updated）都发这个空间的索引；
        turn_list._records 若混入 tail（visible_records），tail 每增减一次
        索引就整体漂移，点列表行传给详情的 idx 从此对不上（「左侧选中 A
        右侧显示 B」的根因）。

        ⚠️ 用 ``records`` 而不是自建属性：插件热重载时 hub 常驻的 collector
        可能还是旧类实例（没有新增的 property），访问新属性直接 AttributeError。
        ``records`` 从旧版起就存在且语义相同（纯落盘投影）。
        """
        if self._collector is None:
            return []
        return self._collector.records

    def _pull_records(self) -> None:
        """全量推送（首次显示 / reset / 切换标签页）。"""
        vis = self._visible()
        stable = self._stable()
        self._timeline.set_records(vis)
        self._turn_list.set_records(stable)
        self._sync_bounds(vis)
        # ⚠️ 先 clear 再 set_records：clear() 会把 _records 清空并回到 idle 态，
        # 反过来写等于把刚推入的数据抹掉（详情面板恒显示"未选中条目"）。
        # 详情用 vis（含 tail）：列表 idx 是 stable = vis 的前缀，直接对齐。
        self._detail.clear()
        self._detail.set_records(vis)
        self._hide_detail()  # 全量重置后回到无选中态 → 详情收起
        # 全量重置（切会话 / 切标签页 / 清除）时丢掉时间选区：旧区间在新会话里没意义。
        # 两处都直接设空值、不 emit，避免互相回环。
        self._timeline.clear_range()
        self._turn_list.clear_time_range()
        self._refresh_stats(vis)
        self._maybe_refresh_schema(force=self._schema_ts <= 0)
        self._refresh_system_sections()

    def _on_records_reset(self) -> None:
        self._pull_records()

    def _on_records_appended(self, start: int, count: int) -> None:
        # ⚠️ start/count 是 stable（纯落盘）空间 → 切片也必须用 stable：
        # vis[start:start+count] 会把 tail 头几条错当增量行插进主列表。
        self._turn_list.append_records(self._stable()[start : start + count])
        vis = self._visible()
        self._timeline.set_records(vis)
        self._sync_bounds(vis)
        self._detail.set_records(vis)
        self._refresh_stats(vis)

    def _on_records_updated(self, start: int, count: int) -> None:
        self._turn_list.update_records(start, count)
        vis = self._visible()
        self._timeline.set_records(vis)
        self._sync_bounds(vis)
        self._detail.set_records(vis)
        # 选中行被回填 → 同步详情
        sel = self._turn_list.selected_record_idx
        if sel is not None and start <= sel < start + count:
            self._detail.select(sel)
        self._refresh_stats(vis)

    def _on_tail_changed(self) -> None:
        tail = self._collector.tail if self._collector else []
        self._turn_list.set_tail(tail)
        self._timeline.set_records(self._visible())
        self._refresh_stats(self._visible())

    def _on_context_updated(self, tokens: int, limit: int) -> None:
        self._context_tokens, self._context_limit = tokens, limit
        self._refresh_stats(self._visible())

    def _on_session_changed(self, _sid: str = "") -> None:
        """backend 信号驱动的会话切换 → 重新投影。

        只覆盖「新建会话 / ``backend.set_current_session``」路径；**历史会话
        加载**不发此信号（走 ``session_manager.set_current_session``），由
        :meth:`_switch_collector` 的 session_id 比对 + :meth:`_on_tick` 心跳
        探测兜底。
        """
        c = self._collector
        if c is None:
            return
        try:
            c.refresh()
        except Exception as e:
            logger.warning(f"[agent_trace] 会话切换后重投影失败: {e}")

    def _on_tick(self) -> None:
        """心跳：in-flight 时长走动（只重绘，不重建）+ 会话变更兜底探测。"""
        if not self.isVisible():
            return
        # ⚠️ 历史会话加载是「静默切换」：不发 session_changed、不发
        # _hook_messages_updated。卡片已打开时既没有 showEvent 也没有 tab
        # 切换事件 → 只剩心跳能发现 session_id 变了。仅做 id 字符串比较，
        # 不变就零开销；变了才走 _switch_collector 重投影。
        mw = self._ctx.get("main_widget")
        if mw is not None and self._collector is not None:
            sid = self._session_id_of(mw)
            if sid and sid != self._active_sid:
                self._switch_collector(mw)
        c = self._collector
        if c is not None and c.has_pending:
            self._timeline.update()
            self._turn_list.repaint_pending()
        if c is not None:
            self._dot.set_state("busy" if c.has_pending else "idle")

    # ──────────────────── 工具 schema / 系统提示词 ────────────────────

    def _maybe_refresh_schema(self, force: bool = False) -> None:
        """拉取当前 agent 的工具 schema（节流 60s）→ 详情面板 Tools Schema tab。"""
        now = time.time()
        if not force and now - self._schema_ts < _SCHEMA_TTL:
            return
        self._schema_ts = now
        schemas = self._fetch_tools_schema()
        if schemas:
            self._detail.set_tools_schema(schemas)

    def _fetch_tools_schema(self) -> List[Dict[str, Any]]:
        """优先走 ``ctx.services.get_tools_schema(agent)``（带 deny 过滤），
        失败回退全局 ``ToolRegistry.schemas()``。"""
        services = self._ctx.get("services") or {}
        getter = services.get("get_tools_schema")
        if callable(getter):
            try:
                mw = self._ctx.get("main_widget")
                agent = getattr(mw, "_current_agent", "") if mw is not None else ""
                schemas = getter(agent or "build")
                if schemas:
                    return list(schemas)
            except Exception as e:
                logger.debug(f"[agent_trace] services.get_tools_schema 失败: {e}")
        try:
            from app.tools.registry import ToolRegistry

            return list(ToolRegistry.get_instance().schemas())
        except Exception as e:
            logger.debug(f"[agent_trace] ToolRegistry.schemas 失败: {e}")
            return []

    def _refresh_system_sections(self) -> None:
        """System Prompt tab 的分段内容：仅会话 system_prompt（已含智能体身份，不再追加）。"""
        sections: List[Tuple[str, str]] = []
        c = self._collector
        sys_prompt = ""
        if c is not None:
            try:
                session = c._current_session()
                sys_prompt = (getattr(session, "system_prompt", "") or "").strip()
            except Exception:
                pass
        if sys_prompt:
            sections.append(("Session System Prompt", sys_prompt))
        self._detail.set_system_sections(sections)

    # ──────────────────── 联动 ────────────────────

    def _sync_bounds(self, records: List[TraceRecord]) -> None:
        """时间边界同步给列表（Waterfall 列）与详情（Timing 条）。"""
        t0, t1 = time_bounds(records)
        self._turn_list.set_bounds(t0, t1)
        self._detail.set_bounds(t0, t1)

    def _on_record_selected(self, idx: int) -> None:
        self._show_detail()
        vis = self._visible()
        self._detail.set_records(vis)
        self._detail.select(idx)
        self._timeline.set_selected(idx)

    def _on_record_clicked(self, idx: int) -> None:
        self._turn_list.select_record(idx)

    def _show_detail(self) -> None:
        """点选条目 → 展开详情面板（恢复收起前的 splitter 宽度）。"""
        if self._detail.isVisible():
            return
        self._detail.show()
        sizes = self._detail_sizes or [880, 520]
        total = sum(self._splitter.sizes()) or sum(sizes)
        scale = total / sum(sizes)
        self._splitter.setSizes([round(sizes[0] * scale), round(sizes[1] * scale)])

    def _hide_detail(self) -> None:
        """收起详情面板：点 × 或全量重置（切会话/切标签页 → 无选中态）时调用。"""
        if self._detail.isVisible():
            self._detail_sizes = self._splitter.sizes()
        self._detail.hide()
        self._timeline.set_selected(None)
        self._turn_list.clear_selection()

    def _on_detail_dismissed(self) -> None:
        # × = 整块隐藏详情面板；点选新条目时再恢复（_on_record_selected）
        self._hide_detail()

    def _on_flag_toggled(self, key: str) -> None:
        """顶栏三个开关 → 时间线 flags（可任意组合）。"""
        self._timeline.set_flags(
            self._flag_btns["duration"].isChecked(),
            self._flag_btns["turns"].isChecked(),
            self._flag_btns["calls"].isChecked(),
        )

    # ──────────────────── 底部汇总 ────────────────────

    def _refresh_stats(self, records: list) -> None:
        if not isinstance(records, list):
            records = []
        turns = sum(1 for r in records if r.kind == EntryKind.USER and r.turn_no > 0)
        llm_ms = sum(max(0, r.duration_ms) for r in records if r.kind == EntryKind.ASSISTANT)
        tool_ms = sum(max(0, r.duration_ms) for r in records if r.kind == EntryKind.TOOL)
        total = self._turn_list.total_count
        shown = self._turn_list.shown_count
        if shown == total:
            self._stats_turns.setText(f"{total} 条 · {turns} 轮")
        else:
            self._stats_turns.setText(f"{shown} / {total} 条 · {turns} 轮")
        self._stats_time.setText(
            f"LLM {format_duration(llm_ms)} · 工具 {format_duration(tool_ms)}" if records else "LLM - · 工具 -"
        )
        if self._context_tokens > 0:
            ctx_text = f"上下文 {self._context_tokens / 1000:.1f}K tok"
            if self._context_limit > 0:
                ctx_text += f" · {self._context_tokens * 100 // self._context_limit}%"
            self._stats_ctx.setText(ctx_text)
        else:
            self._stats_ctx.setText("上下文 -")
        # Network 面板风格：右侧「完成」总耗时（首尾时间跨度，不是各项之和）
        t0, t1 = time_bounds(records)
        self._stats_total.setText(f"完成 {format_duration(int((t1 - t0) * 1000))}" if records else "")

    # ──────────────────── 生命周期 ────────────────────

    def deleteLater(self) -> None:  # noqa: N802
        try:
            self._unbind_collector_signals()
            self._unbind_backend_stats_signals()
            self._hub.dispose()
        except Exception:
            pass
        super().deleteLater()
