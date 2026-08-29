# -*- coding: utf-8 -*-
"""agent_trace.TraceCardWidget — 主容器卡片（full 容器，v3）。

布局（对齐 DeepSeek Harness 轨迹页）：

    ┌─────────────────────────────────────────────────────┐
    │ [Duration|Turns|Calls]              [🔍 搜索…]      │ 顶栏 44px
    ├─────────────────────────────────────────────────────┤
    │ Input / Model / Tools 三泳道甘特图（固定高）          │ TimelinePanel
    ├──────────────────────────────┬──────────────────────┤
    │ 过滤 chips + 条目列表        │ DetailPanel          │
    │                              │ (Summary/Preview/…)  │
    ├──────────────────────────────┴──────────────────────┤
    │ N 轮 · M 步 │ LLM Xs · 工具 Ys │ 上下文 K tok        │ 底部统计栏 30px
    └─────────────────────────────────────────────────────┘

v3 核心变化 — **多标签页隔离**：

- 每个对话标签页有独立 backend；:class:`TraceCollectorHub` 为**每个窗口**
  持有一个常驻 collector 后台持续收集（切走也在记 timing）。
- 卡片实现 ``set_context_provider(provider)``（拉模型）：showEvent /
  ``EV_TAB_SWITCHED`` 时重新拉取上下文 → ``main_widget`` 变化即切换到对应
  collector。修复 v2「所有标签页显示第一个标签页的轨迹」。
- collector 信号在切换时断开重连；UI 全量刷新一次。
"""

from __future__ import annotations

import weakref
from typing import Any, Dict, Optional

from loguru import logger
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import SearchLineEdit, SegmentedWidget

from .detail_panel import DetailPanel
from .timeline_panel import TimelinePanel
from .trace_collector import TraceCollector, TraceCollectorHub
from .trace_models import EntryKind, TraceRecord, format_duration
from .turn_list_widget import TurnListWidget

TOP_BAR_H = 44
BOTTOM_BAR_H = 30

_PLUGIN_NAME = "agent_trace"


def _mono_css() -> str:
    return "Cascadia Mono, Consolas, monospace"


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
        self._build_ui()
        self._build_timer()

    # ──────────────────── 搭建 ────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_top_bar())

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setHandleWidth(2)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)

        # 左：三泳道时间线 + 条目列表
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)
        self._timeline = TimelinePanel(left)
        left_layout.addWidget(self._timeline)
        self._turn_list = TurnListWidget(left)
        left_layout.addWidget(self._turn_list, 1)

        # 右：详情
        self._detail = DetailPanel(splitter)
        splitter.addWidget(left)
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([860, 480])

        outer.addWidget(self._build_bottom_bar())

        # 信号
        self._turn_list.recordSelected.connect(self._on_record_selected)
        self._timeline.recordClicked.connect(self._on_record_clicked)
        self._detail.dismissRequested.connect(self._on_detail_dismissed)
        self._search_box.textChanged.connect(self._turn_list.set_search)
        self._view_mode.currentItemChanged.connect(self._on_view_switch)

    def _build_top_bar(self) -> QWidget:
        bar = QFrame(self)
        bar.setFixedHeight(TOP_BAR_H)
        bar.setObjectName("agentTraceTopBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(10)

        self._view_mode = SegmentedWidget(bar)
        self._view_mode.addItem("duration", "Duration")
        self._view_mode.addItem("turns", "Turns")
        self._view_mode.addItem("calls", "Calls")
        self._view_mode.setCurrentItem("duration")
        layout.addWidget(self._view_mode)

        layout.addStretch(1)
        self._search_box = SearchLineEdit(bar)
        self._search_box.setPlaceholderText("搜索消息内容…")
        self._search_box.setFixedWidth(240)
        self._search_box.setFixedHeight(30)
        self._search_box.setClearButtonEnabled(True)
        layout.addWidget(self._search_box)
        return bar

    def _build_bottom_bar(self) -> QWidget:
        bar = QFrame(self)
        bar.setFixedHeight(BOTTOM_BAR_H)
        bar.setObjectName("agentTraceBottomBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 0, 12, 0)
        layout.setSpacing(0)
        mono = _mono_css()
        self._stats_turns = QLabel("0 轮 · 0 步", bar)
        self._stats_time = QLabel("LLM - · 工具 -", bar)
        self._stats_ctx = QLabel("上下文 -", bar)
        for i, lbl in enumerate((self._stats_turns, self._stats_time, self._stats_ctx)):
            lbl.setStyleSheet(f"color: #888; font-family: {mono}; font-size: 11px;")
            layout.addWidget(lbl)
            if i < 2:
                sep = QLabel("  |  ", bar)
                sep.setStyleSheet("color: rgba(128,128,128,0.35); font-size: 11px;")
                layout.addWidget(sep)
        layout.addStretch(1)
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
        """拉模型接口（主程序优先调用）：保存 provider 并按需拉取最新上下文。

        触发时机：首次注入 / showEvent / EV_TAB_SWITCHED（卡片可见时）。
        """
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
        """标签页切换 → 卡片可见时跟随切换到新活跃窗口的轨迹。"""
        if not self.isVisible():
            return
        self._refresh_context()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._ctx_provider is not None:
            self._refresh_context()

    def _refresh_context(self) -> None:
        """拉取最新上下文：更新主题 + 必要时切换 collector。"""
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
        if not colors:
            return
        colors.setdefault("is_dark", bool(self._ctx.get("is_dark", True)))
        # ctx.font_size 是「已应用缩放的像素值」→ 必须 setPixelSize（勿当磅值用）
        self._fs = max(10, int(self._ctx.get("font_size") or 12))
        self._timeline.set_colors(colors)
        self._turn_list.set_colors(colors)
        self._detail.set_colors(colors)
        self._style_bars(colors)

        font_family = self._ctx.get("font_family") or "Segoe UI"
        f = QFont(font_family)
        f.setPixelSize(self._fs)
        self.setFont(f)
        self._timeline._apply_font(f)
        self._turn_list._apply_font(f)
        try:
            # qfluentwidgets 控件内部 setFont(widget, 18) 硬编码像素字号，
            # 外部 QWidget.setFont 无效 → 必须用它们的专用 API
            self._view_mode.setItemFontSize(max(10, self._fs - 1))
            from qfluentwidgets.common.font import setFont as _qw_set_font

            _qw_set_font(self._search_box, max(10, self._fs - 1))
        except Exception as e:
            logger.debug(f"[agent_trace] qfluentwidgets 字体适配跳过: {e}")

    def _style_bars(self, colors: Dict[str, Any]) -> None:
        is_dark = colors.get("is_dark", True)
        text = colors.get("text_primary", "#C8C8D0")
        secondary = colors.get("text_secondary", "#8A8A94")
        border = colors.get("border", "#333333")
        accent = colors.get("accent", "#7AA2F7")
        mono = _mono_css()
        fs = getattr(self, "_fs", 12)
        fs_small = max(10, fs - 2)
        line_c = "255,255,255" if is_dark else "0,0,0"
        top = self.findChild(QFrame, "agentTraceTopBar")
        if top is not None:
            top.setStyleSheet(
                f"#agentTraceTopBar {{ background: transparent; border-bottom: 1px solid {border}; }}"
                f"#agentTraceTopBar QPushButton {{ background: transparent; color: {secondary};"
                f"  border: 1px solid transparent; border-radius: 4px; padding: 0 12px;"
                f"  font-size: {max(10, fs - 1)}px; font-family: {mono}; }}"
                f"#agentTraceTopBar QPushButton:hover {{ background: rgba({line_c},0.08); }}"
                f"#agentTraceTopBar QPushButton:checked {{ color: {accent}; border: 1px solid {accent}; }}"
            )
        self._search_box.setStyleSheet(
            f"SearchLineEdit {{ background: rgba({line_c},0.05); border: 1px solid {border};"
            f"  border-radius: 4px; color: {text}; font-size: {max(10, fs - 1)}px; }}"
        )
        bottom = self.findChild(QFrame, "agentTraceBottomBar")
        if bottom is not None:
            bottom.setStyleSheet(f"#agentTraceBottomBar {{ border-top: 1px solid {border}; }}")
        for lbl in (self._stats_turns, self._stats_time, self._stats_ctx):
            lbl.setStyleSheet(f"color: {secondary}; font-family: {mono}; font-size: {fs_small}px;")

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
            # token 统计（底部栏）跟随当前 backend
            try:
                backend = getattr(main_widget, "backend", None)
                if backend is not None and hasattr(backend, "context_updated"):
                    backend.context_updated.connect(self._on_context_updated)
            except Exception:
                pass
            self._pull_records()
            # 顺手清理已关闭窗口的 collector（防泄漏）
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
        for c in [self._collector]:
            if c is None:
                continue
            be = getattr(c, "_bound_backend", None)
            if be is not None and hasattr(be, "context_updated"):
                try:
                    be.context_updated.disconnect(self._on_context_updated)
                except TypeError, RuntimeError:
                    pass

    # ──────────────────── collector 信号 → UI ────────────────────

    def _visible(self) -> list:
        if self._collector is None:
            return []
        return self._collector.visible_records

    def _pull_records(self) -> None:
        """全量推送（首次显示 / reset / 切换标签页）。"""
        vis = self._visible()
        self._timeline.set_records(vis)
        self._turn_list.set_records(vis)
        self._refresh_stats(vis)
        self._detail.clear()

    def _on_records_reset(self) -> None:
        self._pull_records()

    def _on_records_appended(self, start: int, count: int) -> None:
        vis = self._visible()
        self._turn_list.append_records(vis[start : start + count])
        self._timeline.set_records(vis)
        self._refresh_stats(vis)

    def _on_records_updated(self, start: int, count: int) -> None:
        self._turn_list.update_records(start, count)
        self._timeline.set_records(self._visible())
        # 选中行被回填 → 同步详情
        sel = self._turn_list.selected_record_idx
        if sel is not None and start <= sel < start + count:
            self._detail.set_records(self._visible())
            self._detail.select(sel)

    def _on_tail_changed(self) -> None:
        self._turn_list.set_tail(self._collector.tail if self._collector else [])
        self._timeline.set_records(self._visible())

    def _on_context_updated(self, tokens: int, limit: int) -> None:
        self._context_tokens, self._context_limit = tokens, limit
        self._refresh_stats(self._visible())

    def _on_tick(self) -> None:
        """心跳：仅当有 in-flight 记录时重绘（时长走动 / pending 动画）。"""
        if self._collector is not None and self._collector.has_pending:
            self._timeline.update()
            self._turn_list.repaint_pending()

    # ──────────────────── 选中联动 ────────────────────

    def _on_record_selected(self, idx: int) -> None:
        self._detail.show()  # 点击新条目恢复被 × 隐藏的详情面板
        self._detail.set_records(self._visible())
        self._detail.select(idx)
        self._timeline.set_selected(idx)

    def _on_record_clicked(self, idx: int) -> None:
        self._turn_list.select_record(idx)

    def _on_detail_dismissed(self) -> None:
        # × = 整块隐藏详情面板；点选新条目时再恢复（_on_record_selected）
        self._detail.hide()
        self._timeline.set_selected(None)
        self._turn_list.clear_selection()

    # ──────────────────── 底部统计 ────────────────────

    def _refresh_stats(self, records: list) -> None:
        if not isinstance(records, list):
            records = []
        turns = sum(1 for r in records if r.kind == EntryKind.USER and r.turn_no > 0)
        llm_ms = sum(max(0, r.duration_ms) for r in records if r.kind == EntryKind.ASSISTANT)
        tool_ms = sum(max(0, r.duration_ms) for r in records if r.kind == EntryKind.TOOL)
        self._stats_turns.setText(f"{turns} 轮 · {len(records)} 步")
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

    # ──────────────────── 视图切换 ────────────────────

    def _on_view_switch(self, key: str) -> None:
        if isinstance(key, str) and key in ("duration", "turns", "calls"):
            self._timeline.set_mode(key)

    # ──────────────────── 生命周期 ────────────────────

    def deleteLater(self) -> None:  # noqa: N802
        try:
            self._unbind_collector_signals()
            self._unbind_backend_stats_signals()
            self._hub.dispose()
        except Exception:
            pass
        super().deleteLater()
