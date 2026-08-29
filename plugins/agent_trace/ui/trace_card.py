# -*- coding: utf-8 -*-
"""agent_trace.TraceCardWidget — 主容器卡片（full 容器）。

布局：
    +---------------------------------------------------+
    |  Timeline  (DeepSeek-style 条带 + 摘要)            |
    +--------------------------------+------------------+
    |                                |                  |
    |   TurnList (中央条目)          |   DetailPanel    |
    |                                |   (右侧四 tab)   |
    |                                |                  |
    +--------------------------------+------------------+

数据流：
    TraceCardWidget.set_context(ctx)
        → 创建 / 重绑 TraceCollector 监 backend 信号
        → 把 recordsAppended / recordUpdated / recordsReset 接给面板
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from loguru import logger
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import QFrame, QScrollArea, QSplitter, QVBoxLayout, QWidget

from .detail_panel import DetailPanel
from .timeline_panel import TimelinePanel
from .trace_collector import TraceCollector
from .turn_list_widget import TurnListWidget

# Timeline 区域最大高度（超出则内部滚动），保证下方列表有足够空间
TIMELINE_MAX_H = 200


class TraceCardWidget(QWidget):
    """轨迹主组件（浮动卡片，container='full'）。"""

    # 向 CardManager 通知关闭（保留扩展位）
    closed = None  # type: ignore[assignment]

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._ctx: Dict[str, Any] = {}
        self._collector: Optional[TraceCollector] = None
        self._bound_main_widget: Optional[Any] = None
        self._build_ui()

    # ──────────────────── 搭建 ────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setHandleWidth(2)
        splitter.setChildrenCollapsible(False)
        outer.addWidget(splitter, 1)

        # 左：Timeline（可滚动，固定高度上限）+ TurnList
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(0)

        # Timeline 包一层 ScrollArea：记录多时内部滚动，不挤压下方列表。
        # 直接 addWidget(timeline) 时它只拿到 minimumSizeHint（约 84px），
        # 6 条以上记录会被截断看不见 —— 这是"时间轴只显示两行"的根因。
        self._timeline_scroll = QScrollArea(left)
        self._timeline_scroll.setWidgetResizable(True)
        self._timeline_scroll.setFrameShape(QFrame.NoFrame)
        self._timeline_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._timeline_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._timeline_scroll.setFixedHeight(TIMELINE_MAX_H)
        self._timeline = TimelinePanel()
        self._timeline_scroll.setWidget(self._timeline)
        left_layout.addWidget(self._timeline_scroll)

        self._turn_list = TurnListWidget(left)
        left_layout.addWidget(self._turn_list, 1)

        # 右：Detail
        self._detail = DetailPanel(splitter)

        splitter.addWidget(left)
        splitter.addWidget(self._detail)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([820, 460])

        # 信号：turn list 点击 → 同步 detail
        self._turn_list.recordSelected.connect(self._on_record_selected)
        # timeline 点击 → 同步 list select
        self._timeline.recordClicked.connect(self._on_record_clicked)

    # ──────────────────── 上下文注入（主程序调用） ────────────────────

    def set_context(self, ctx: Dict[str, Any]) -> None:
        """主程序首次 show 时注入上下文（含 main_widget / colors 等）。

        后续主题切换由 _apply_latest_theme（在主程序全量 set_context 时随之刷新）。
        """
        self._ctx = dict(ctx or {})
        self._apply_latest_theme()

        # 在第一次 set_context 时尝试绑定 collector。后续 context 切换可能
        # 改变 main_widget —— 以检测到的为准。
        main_widget = ctx.get("main_widget")
        if main_widget is not None and main_widget is not self._bound_main_widget:
            self._bind_collector(main_widget)
        elif main_widget is None and self._collector is None:
            # 未注入 main_widget 时不强求（测试场景）
            pass

        # 首次显示时主动拉一次，避免"卡片打开了但空白"
        self._pull_records()

    def _apply_latest_theme(self) -> None:
        colors = dict(self._ctx.get("colors") or {})
        if not colors:
            return
        # 子面板需要 is_dark 判断深浅（判断 grid / hover / alt 行的 alpha 方向）
        colors.setdefault("is_dark", bool(self._ctx.get("is_dark", True)))
        self._timeline.set_colors(colors)
        self._turn_list.set_colors(colors)
        self._detail.set_colors(colors)

        font_family = self._ctx.get("font_family") or "Segoe UI"
        font_size = self._ctx.get("font_size") or 12
        f = QFont(font_family, font_size)
        self.setFont(f)
        self._timeline._apply_font(f)
        self._turn_list._apply_font(f)
        self._detail._apply_font(f)

    # ──────────────────── 采集器绑定 ────────────────────

    def _bind_collector(self, main_widget: Any) -> None:
        """绑定到 main_widget.backend 创建的 TraceCollector。"""
        if self._collector is not None:
            try:
                self._collector.detach()
            except Exception:
                pass
            self._collector = None
        backend = getattr(main_widget, "backend", None)
        if backend is None:
            logger.warning("[agent_trace] main_widget.backend 尚未初始化，延迟绑定")
            self._bound_main_widget = main_widget
            return
        self._collector = TraceCollector(self)
        # signals → ui
        self._collector.recordsAppended.connect(self._on_records_appended)
        self._collector.recordUpdated.connect(self._on_record_updated)
        self._collector.recordsReset.connect(self._on_records_reset)
        try:
            self._collector.attach(backend, main_widget)
        except Exception as e:
            logger.warning(f"[agent_trace] collector.attach failed: {e}")
        self._bound_main_widget = main_widget

    def _ensure_bound(self) -> None:
        """延迟兜底：如果 set_context 时 main_widget 还没 backend，事件触发时再尝试。"""
        if self._collector is not None:
            return
        main_widget = self._bound_main_widget
        if main_widget is not None:
            self._bind_collector(main_widget)

    # ──────────────────── 信号回调 ────────────────────

    def _pull_records(self) -> None:
        """把 collector 当前快照推给三个面板（首次显示 / 手动刷新用）。"""
        if self._collector is None:
            return
        records = self._collector.records
        self._timeline.set_records(records)
        self._turn_list.set_records(records, keep_selection=True)
        self._detail.set_records(records)
        sel = self._turn_list._selected_idx
        if sel is not None and sel < len(records):
            self._detail.select(sel)

    def _on_records_reset(self) -> None:
        self._timeline.clear()
        self._turn_list.clear()
        self._detail.clear()
        # 一次清空后立即 lazy-bind（首次 set_context 时 backend 可能尚未就绪）
        self._ensure_bound()

    def _on_records_appended(self, start_idx: int) -> None:
        """新记录追加 —— 增量加列表项，避免长会话下全量重建卡顿。"""
        if self._collector is None:
            return
        records = self._collector.records
        # timeline 需要重算 turn 边界（纯计算，成本低）
        self._timeline.set_records(records)
        # 列表走增量（保留既有 item 与选中态）
        if start_idx <= len(self._turn_list._records):
            self._turn_list.append_records(records[start_idx:])
        else:
            self._turn_list.set_records(records, keep_selection=True)
        self._detail.set_records(records)
        sel = self._turn_list._selected_idx
        if sel is not None and sel < len(records):
            self._detail.select(sel)

    def _on_record_updated(self, idx: int) -> None:
        if self._collector is None:
            return
        self._timeline.update_record(idx)
        self._turn_list.update_record(idx)
        if self._turn_list._selected_idx == idx:
            self._detail.set_records(self._collector.records)
            self._detail.select(idx)

    def _on_record_clicked(self, idx: int) -> None:
        """时间线条带被点击 → 选中列表对应行（会连带刷新右侧详情）。"""
        self._turn_list.select(idx)

    def _on_record_selected(self, idx: int) -> None:
        """列表选中变化 → 刷新右侧详情。"""
        if self._collector is not None:
            self._detail.set_records(self._collector.records)
        self._detail.select(idx)

    # ──────────────────── 生命周期 ────────────────────

    def deleteLater(self) -> None:  # noqa: N802
        try:
            if self._collector is not None:
                self._collector.detach()
        except Exception:
            pass
        super().deleteLater()
