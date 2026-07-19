# -*- coding: utf-8 -*-
"""ContextUsageStatsCard 浮动卡片 — 统计最近对话的上下文用量图表

功能：
- 最近 14 天会话活跃度柱状图
- 最近 14 天消息量趋势折线图
- 总体统计数据（总会话数、总消息数、平均消息数/会话、压缩率）
- 当前会话信息（消息数、估算 token 数）
- 所有数据异步从 SQLite 数据库读取，不阻塞 UI

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有文件操作直接通过 sqlite3/stdlib 完成
- 基于 .drifox/sessions.db 文件直接读取数据
"""

from typing import Callable, Optional

from PyQt5.QtCore import QEvent, QSize, QThread, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    IconWidget,
    ScrollArea,
    ToolButton,
    TransparentToolButton,
    isDarkTheme,
)

from .charts import (
    _BarChartWidget,
    _LineChartWidget,
    _ProjectBarWidget,
    _StatCard,
    _chart_colors,
    _format_number,
    _make_chart_colors_from_context,
)
from .data import _DataWorker


class ContextUsageStatsCard(QWidget):
    """上下文用量统计浮动卡片"""

    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._context_provider: Optional[Callable[[], dict]] = None
        self._worker_thread: Optional[QThread] = None
        self._worker: Optional[_DataWorker] = None
        self._stats_data: Optional[dict] = None
        self._chart_style: Optional[dict] = None
        self._header_icon: Optional[IconWidget] = None
        self._setup_ui()

    # ── 拉模型上下文注入 ──

    def set_context_provider(self, provider: Callable[[], dict]):
        self._context_provider = provider

    def show_card(self):
        self._apply_latest_theme()
        self._apply_plugin_icon()
        self._async_load_data()
        self.setVisible(True)

    def _apply_plugin_icon(self):
        if self._context_provider is None or self._header_icon is None:
            return
        try:
            from PyQt5.QtGui import QIcon

            ctx = self._context_provider()
            icon_info = ctx.get("plugin_icon", {})
            theme = "dark" if isDarkTheme() else "light"
            icon_path = icon_info.get(theme, "")
            if icon_path:
                self._header_icon.setIcon(QIcon(icon_path))
        except Exception:
            pass

    def _apply_latest_theme(self):
        if self._context_provider is None:
            return
        try:
            ctx = self._context_provider()
            self._chart_style = _make_chart_colors_from_context(ctx)
        except Exception:
            self._chart_style = _chart_colors()

        cs = self._chart_style
        font_family = cs.get("font_family", "Microsoft YaHei")
        font_size = cs.get("font_size", 14)
        tc = cs.get("text", QColor(255, 255, 255, 180))
        text_color = f"rgba({tc.red()},{tc.green()},{tc.blue()},{tc.alpha()})"

        for child in self.findChildren(QLabel):
            try:
                current = child.styleSheet()
                weight_part = "font-weight: bold;" if "font-weight" in current else ""
                child.setStyleSheet(
                    f"color: {text_color}; font-size: {font_size}px; "
                    f"font-family: '{font_family}'; {weight_part}"
                    "background: transparent;"
                )
            except RuntimeError:
                pass

    # ── 界面搭建 ──

    def _setup_ui(self):
        self.setMinimumWidth(300)
        self.setMinimumHeight(0)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet("ContextUsageStatsCard { background: transparent; }")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        ff = "Microsoft YaHei"
        fs = 14
        tc_default = "rgba(255,255,255,200)"
        tcs_default = "rgba(255,255,255,150)"

        # ── 头部 ──
        header = QWidget(self)
        header.setStyleSheet("background: transparent;")
        hly = QHBoxLayout(header)
        hly.setContentsMargins(16, 12, 16, 4)
        hly.setSpacing(8)

        icon = IconWidget(FluentIcon.HISTORY, header)
        icon.setFixedSize(22, 22)
        hly.addWidget(icon)
        self._header_icon = icon

        title = QLabel("上下文用量统计", header)
        title.setStyleSheet(
            f"color: {tc_default}; font-size: {fs}px; font-weight: bold; font-family: '{ff}'; background: transparent;"
        )
        hly.addWidget(title)

        self._status_lb = QLabel("", header)
        self._status_lb.setStyleSheet(
            f"color: {tcs_default}; font-size: {fs}px; font-family: '{ff}'; background: transparent;"
        )
        hly.addWidget(self._status_lb)
        hly.addStretch(1)

        self._refresh_btn = ToolButton(FluentIcon.SYNC, header)
        self._refresh_btn.setToolTip("刷新数据")
        self._refresh_btn.clicked.connect(self._async_load_data)
        hly.addWidget(self._refresh_btn)

        close_btn = TransparentToolButton(FluentIcon.CLOSE, header)
        close_btn.setFixedSize(24, 24)
        close_btn.setToolTip("关闭")
        close_btn.clicked.connect(self._on_close)
        hly.addWidget(close_btn)

        root.addWidget(header)

        # ── 分隔线 ──
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("background: rgba(128,128,128,0.15); max-height: 1px;")
        root.addWidget(sep)

        # ── 滚动内容 ──
        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet(
            "ScrollArea { background: transparent; border: none; }"
            "ScrollArea > QWidget > QWidget { background: transparent; }"
        )
        self._content = QWidget(self._scroll)
        self._content.setStyleSheet("background: transparent;")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(16, 12, 16, 12)
        self._content_layout.setSpacing(16)
        self._content_layout.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

        # ── 加载中占位 ──
        self._empty_lb = QLabel("正在加载统计数据…", self)
        self._empty_lb.setAlignment(Qt.AlignCenter)
        self._empty_lb.setStyleSheet(f"color: {tcs_default}; font-family: '{ff}'; background: transparent;")
        self._empty_lb.setVisible(True)
        root.addWidget(self._empty_lb)

    # ── 高度模式 ──

    def sizeHint(self):
        base = super().sizeHint()
        win = self.window()
        if win and win.height() > 0:
            return QSize(max(base.width(), 200), int(win.height() * 0.85))
        return base

    def showEvent(self, event):
        super().showEvent(event)
        win = self.window()
        if win:
            win.installEventFilter(self)
            self.updateGeometry()

    def eventFilter(self, obj, event):
        if obj is self.window() and event.type() == QEvent.Resize:
            self.updateGeometry()
        return super().eventFilter(obj, event)

    # ── 数据加载 ──

    def _async_load_data(self):
        self._set_loading(True)
        self._cleanup_worker()

        worker = _DataWorker()
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_data_loaded)
        worker.error.connect(self._on_load_error)
        worker.finished.connect(thread.quit)
        worker.error.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._worker, self._worker_thread = worker, thread
        thread.start()

    def _on_data_loaded(self, data: dict):
        self._stats_data = data
        self._set_loading(False)

        if data.get("error"):
            self._empty_lb.setText(f"数据加载失败: {data['error'][:60]}")
            self._empty_lb.setVisible(True)
            return

        self._render_stats(data)

    def _on_load_error(self, err: str):
        self._set_loading(False)
        self._empty_lb.setText(f"数据读取异常: {err[:60]}")
        self._empty_lb.setVisible(True)

    def _set_loading(self, loading: bool):
        self._refresh_btn.setEnabled(not loading)
        if loading:
            self._status_lb.setText("读取中…")
            self._empty_lb.setVisible(True)
        else:
            self._status_lb.setText("")
            self._empty_lb.setVisible(False)

    # ── 渲染数据 ──

    def _render_stats(self, data: dict):
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        total_sessions = data.get("total_sessions", 0)
        total_messages = data.get("total_messages", 0)
        total_tokens = data.get("total_tokens", 0)
        avg_daily_tokens = data.get("avg_daily_tokens", 0)
        avg_msgs = data.get("avg_messages_per_session", 0.0)
        avg_daily = round(total_sessions / 14, 1) if total_sessions > 0 else 0.0

        # ── 概要统计卡片行 ──
        stats_row = QHBoxLayout()
        stats_row.setSpacing(8)

        stat_cards = [
            (
                FluentIcon.FONT,
                "总 token 数",
                _format_number(total_tokens),
                f"日均 {_format_number(avg_daily_tokens)}（近 14 天）",
                "",
            ),
            (FluentIcon.CHAT, "总会话数", str(total_sessions), f"平均 {avg_daily} 次/天", ""),
            (FluentIcon.MESSAGE, "总消息数", _format_number(total_messages), f"平均 {avg_msgs} 条/会话", ""),
        ]

        for ic, title, val, sub, extra in stat_cards:
            card = _StatCard(ic, title, val, sub, extra_info=extra)
            if self._chart_style:
                card.set_colors(self._chart_style)
            stats_row.addWidget(card)

        stats_widget = QWidget()
        stats_widget.setLayout(stats_row)
        stats_widget.setStyleSheet("background: transparent;")
        self._content_layout.addWidget(stats_widget)

        cs = self._chart_style

        # ── Token 用量折线图 ──
        daily_tokens = data.get("daily_tokens", [])
        if daily_tokens and any(v for _, v in daily_tokens):
            widget = _LineChartWidget("🔤 估算 Token 用量趋势", daily_tokens, color_key="accent")
            if cs:
                widget.set_colors(cs)
            self._content_layout.addWidget(widget)

        # ── 消息量趋势折线图 ──
        daily_messages = data.get("daily_messages", [])
        if daily_messages and any(v for _, v in daily_messages):
            widget = _LineChartWidget("📈 每日消息量趋势", daily_messages, color_key="accent")
            if cs:
                widget.set_colors(cs)
            self._content_layout.addWidget(widget)

        # ── 会话活跃度柱状图 ──
        daily_sessions = data.get("daily_sessions", [])
        if daily_sessions and any(v for _, v in daily_sessions):
            widget = _BarChartWidget("📊 每日会话活跃度", daily_sessions)
            if cs:
                widget.set_colors(cs)
            self._content_layout.addWidget(widget)

        # ── 项目分布柱状图 ──
        sessions_per_project = data.get("sessions_per_project", {})
        if sessions_per_project:
            sorted_projects = sorted(sessions_per_project.items(), key=lambda x: -x[1])[:8]
            proj_bar = _ProjectBarWidget(sorted_projects)
            if cs:
                proj_bar.set_colors(cs)
            self._content_layout.addWidget(proj_bar)

        # ── 无数据提示 ──
        if not daily_sessions or not any(v for _, v in daily_sessions):
            empty_hint = QLabel("暂无会话数据，开始对话后将自动生成统计。", self._content)
            empty_hint.setAlignment(Qt.AlignCenter)
            ff = cs.get("font_family", "Microsoft YaHei") if cs else "Microsoft YaHei"
            tcs2 = cs.get("text_secondary", QColor(255, 255, 255, 100)) if cs else QColor(255, 255, 255, 100)
            ec = f"rgba({tcs2.red()},{tcs2.green()},{tcs2.blue()},{tcs2.alpha()})"
            empty_hint.setStyleSheet(
                f"color: {ec}; font-family: '{ff}'; font-size: 13px; background: transparent; padding: 40px;"
            )
            self._content_layout.addWidget(empty_hint)

    # ── 关闭 ──

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()

    def _cleanup_worker(self):
        if self._worker_thread is not None:
            try:
                self._worker_thread.quit()
                self._worker_thread.wait(500)
            except RuntimeError:
                pass
            self._worker_thread = None
        self._worker = None

    def deleteLater(self):
        self._cleanup_worker()
        super().deleteLater()
