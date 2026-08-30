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
- **工具条独占一行**（过滤 chips + 搜索 + 视图切换 + 清除 / 跟随）。
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
from qfluentwidgets import SearchLineEdit, SegmentedWidget

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
        self._context_tokens: int = 0
        self._context_limit: int = 0
        self._pal = ThemePalette()
        self._fs = 13
        self._follow_tail = True
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
        splitter.setHandleWidth(2)
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

        # ④ 汇总栏
        outer.addWidget(self._build_bottom_bar())

        # 信号
        self._turn_list.recordSelected.connect(self._on_record_selected)
        self._timeline.recordClicked.connect(self._on_record_clicked)
        self._detail.dismissRequested.connect(self._on_detail_dismissed)
        self._search_box.textChanged.connect(self._turn_list.set_search)
        self._view_mode.currentItemChanged.connect(self._on_view_switch)
        self._clear_btn.clicked.connect(self._on_clear_clicked)
        self._follow_btn.clicked.connect(self._on_follow_toggled)

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

        self._view_mode = SegmentedWidget(bar)
        self._view_mode.addItem("duration", "Duration")
        self._view_mode.addItem("turns", "Turns")
        self._view_mode.addItem("calls", "Calls")
        self._view_mode.setCurrentItem("duration")
        self._view_mode.setFixedHeight(28)
        layout.addWidget(self._view_mode)

        self._search_box = SearchLineEdit(bar)
        self._search_box.setPlaceholderText("搜索内容 / 工具名…")
        self._search_box.setFixedWidth(220)
        self._search_box.setFixedHeight(28)
        self._search_box.setClearButtonEnabled(True)
        layout.addWidget(self._search_box)

        self._follow_btn = QPushButton("跟随", bar)
        self._follow_btn.setCheckable(True)
        self._follow_btn.setChecked(True)
        self._follow_btn.setFixedHeight(26)
        self._follow_btn.setCursor(Qt.PointingHandCursor)
        self._follow_btn.setToolTip("有新条目时自动滚动到最新一条")
        layout.addWidget(self._follow_btn)

        self._clear_btn = QPushButton("清除", bar)
        self._clear_btn.setFixedHeight(26)
        self._clear_btn.setCursor(Qt.PointingHandCursor)
        self._clear_btn.setToolTip("清空当前轨迹缓存并重新采集（不影响会话消息）")
        layout.addWidget(self._clear_btn)
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

    def _on_tab_switched(self, _payload: Dict[str, Any]) -> None:
        if not self.isVisible():
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
        try:
            self._view_mode.setItemFontSize(max(10, fs - 1))
            from PyQt5.QtGui import QColor as _QColor

            self._view_mode.setIndicatorColor(_QColor(pal.accent), _QColor(pal.accent))
        except Exception as e:
            logger.debug(f"[agent_trace] SegmentedWidget 主题适配跳过: {e}")

        bottom = self.findChild(QFrame, "agentTraceBottomBar")
        if bottom is not None:
            bottom.setStyleSheet(
                f"QFrame#agentTraceBottomBar {{ background: transparent;"
                f" border-top: 1px solid {pal.q('border')}; }}"
                f"QFrame#agentTraceBottomBar QLabel {{ color: {pal.q('text_muted')};"
                f"  font-family: '{pal.font_family}'; font-size: {small}px; background: transparent; }}"
            )

    # ──────────────────── collector 切换 ────────────────────

    def _switch_collector(self, main_widget: Any) -> None:
        """切换到目标窗口的常驻 collector（同窗口幂等）。"""
        wid = getattr(main_widget, "_window_id", "") or ""
        if wid and wid == self._active_wid and self._collector is not None:
            self._collector.refresh()
            return

        self._unbind_collector_signals()
        self._unbind_backend_stats_signals()
        self._collector = self._hub.collector_for(main_widget)
        self._active_wid = wid if self._collector is not None else ""

        if self._collector is not None:
            self._collector.refresh()
            self._bind_collector_signals(self._collector)
            try:
                backend = getattr(main_widget, "backend", None)
                if backend is not None and hasattr(backend, "context_updated"):
                    backend.context_updated.connect(self._on_context_updated)
            except Exception:
                pass
            self._pull_records()
            self._hub.cleanup_closed(self._active_window_ids())
        else:
            # backend 未就绪：清空展示，等下次 show/tab 切换重试
            self._pull_records()

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

    def _unbind_backend_stats_signals(self) -> None:
        c = self._collector
        if c is None:
            return
        be = getattr(c, "_bound_backend", None)
        if be is not None and hasattr(be, "context_updated"):
            try:
                be.context_updated.disconnect(self._on_context_updated)
            except TypeError, RuntimeError:
                pass

    # ──────────────────── collector 信号 → UI ────────────────────

    def _visible(self) -> List[TraceRecord]:
        if self._collector is None:
            return []
        return self._collector.visible_records

    def _pull_records(self) -> None:
        """全量推送（首次显示 / reset / 切换标签页）。"""
        vis = self._visible()
        self._timeline.set_records(vis)
        self._turn_list.set_records(vis)
        self._sync_bounds(vis)
        # ⚠️ 先 clear 再 set_records：clear() 会把 _records 清空并回到 idle 态，
        # 反过来写等于把刚推入的数据抹掉（详情面板恒显示"未选中条目"）。
        self._detail.clear()
        self._detail.set_records(vis)
        self._refresh_stats(vis)
        self._maybe_refresh_schema(force=self._schema_ts <= 0)
        self._refresh_system_sections()

    def _on_records_reset(self) -> None:
        self._pull_records()

    def _on_records_appended(self, start: int, count: int) -> None:
        vis = self._visible()
        self._turn_list.append_records(vis[start : start + count])
        self._timeline.set_records(vis)
        self._sync_bounds(vis)
        self._detail.set_records(vis)
        self._refresh_stats(vis)
        if self._follow_tail:
            self._scroll_to_last()

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
        if self._follow_tail and tail:
            self._scroll_to_last()

    def _on_context_updated(self, tokens: int, limit: int) -> None:
        self._context_tokens, self._context_limit = tokens, limit
        self._refresh_stats(self._visible())

    def _on_tick(self) -> None:
        """心跳：仅当有 in-flight 记录时重绘（时长走动）。"""
        c = self._collector
        if c is not None and c.has_pending:
            self._timeline.update()
            self._turn_list.repaint_pending()
        if c is not None:
            self._dot.set_state("busy" if c.has_pending else "idle")

    def _scroll_to_last(self) -> None:
        self._turn_list.scroll_to_bottom()

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
        """System Prompt tab 的分段内容：会话 system_prompt + 当前 agent 提示词。"""
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
        agent_prompt = self._fetch_agent_prompt()
        if agent_prompt:
            sections.append((f"Agent Prompt · {self._current_agent_name()}", agent_prompt))
        self._detail.set_system_sections(sections)

    def _current_agent_name(self) -> str:
        mw = self._ctx.get("main_widget")
        return str(getattr(mw, "_current_agent", "build") or "build") if mw is not None else "build"

    def _fetch_agent_prompt(self) -> str:
        services = self._ctx.get("services") or {}
        getter = services.get("get_agent_prompt")
        if not callable(getter):
            return ""
        try:
            return (getter(self._current_agent_name()) or "").strip()
        except Exception:
            return ""

    # ──────────────────── 联动 ────────────────────

    def _sync_bounds(self, records: List[TraceRecord]) -> None:
        """时间边界同步给列表（Waterfall 列）与详情（Timing 条）。"""
        t0, t1 = time_bounds(records)
        self._turn_list.set_bounds(t0, t1)
        self._detail.set_bounds(t0, t1)

    def _on_record_selected(self, idx: int) -> None:
        self._detail.show()  # 点击新条目恢复被 × 隐藏的详情面板
        vis = self._visible()
        self._detail.set_records(vis)
        self._detail.select(idx)
        self._timeline.set_selected(idx)

    def _on_record_clicked(self, idx: int) -> None:
        self._turn_list.select_record(idx)

    def _on_detail_dismissed(self) -> None:
        # × = 整块隐藏详情面板；点选新条目时再恢复（_on_record_selected）
        self._detail.hide()
        self._timeline.set_selected(None)
        self._turn_list.clear_selection()

    def _on_view_switch(self, key: str) -> None:
        if isinstance(key, str) and key in ("duration", "turns", "calls"):
            self._timeline.set_mode(key)

    def _on_follow_toggled(self, checked: bool) -> None:
        self._follow_tail = bool(checked)
        if checked:
            self._scroll_to_last()

    def _on_clear_clicked(self) -> None:
        """清空轨迹缓存（timing / in-flight）并重新采集 — 对齐 Network 的 Clear。"""
        c = self._collector
        if c is None:
            return
        self._schema_ts = 0.0
        c.reset()
        self._pull_records()

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
