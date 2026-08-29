# -*- coding: utf-8 -*-
"""agent_trace.TurnListWidget — 中央条目列表（DeepSeek Harness / DevTools 风格）。

实现要点：
- 用 ``QListWidget`` + 自定义 ``QStyledItemDelegate`` 自绘每行，而不是手搓
  QWidget 行 —— 避免动态 widget 的 show 时序问题（父容器未 show 时子 widget
  ``isVisible()`` 为 False，会导致"列表空白 / 右侧详情不刷新"）。
- 行布局（等宽字体 + 紧凑行高，对齐 DevTools Network 面板观感）：

      ● SYSTEM    System Prompt       你是 DriFox 智能助手…        0.1s
      ● USER      User                帮我看看这个项目结构          0.0s
      ● HOOK      SessionStart        <system-reminder> 会话已启动  0.2s
      ● ASSISTANT Assistant           我先读一下目录…               2.1s
      ● TOOL      list_dir            {"path": "."}                0.8s

- 选中态由 QListWidget 统一维护（setCurrentRow），不受数据刷新影响。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QModelIndex, QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .trace_models import (
    ENTRY_KIND_COLORS,
    EntryKind,
    TraceRecord,
    format_duration,
)

# ── 行布局常量 ──
ROW_H = 24
COL_DOT = 10  # 圆点 x
COL_TYPE = 26  # type 标签 x
TYPE_W = 76  # type 标签宽度
COL_MAIN = COL_TYPE + TYPE_W + 8  # 主文本 x
META_W = 84  # 右侧耗时列宽（右对齐）
PAD_R = 12  # 右边距


def _mono_font(base: QFont, size_delta: float = -1.0) -> QFont:
    """派生等宽字体（DevTools 观感）。"""
    f = QFont(base)
    f.setFamily("Cascadia Mono")
    f.setStyleHint(QFont.Monospace)
    f.setPointSizeF(max(7.5, base.pointSizeF() + size_delta))
    return f


class _EntryDelegate(QStyledItemDelegate):
    """自绘单条 entry 行。"""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._colors: Dict[str, str] = {
            "text": "#D8D8D8",
            "text_secondary": "#8A8A8A",
            "text_dim": "#6E6E6E",
            "selected_bg": "rgba(122,162,247,0.16)",
            "selected_line": "#7AA2F7",
            "hover_bg": "rgba(255,255,255,0.045)",
            "row_alt": "rgba(255,255,255,0.018)",
            "row_line": "rgba(255,255,255,0.045)",
        }
        self._mono_base = QFont("Cascadia Mono", 9)

    def set_colors(self, colors: Dict[str, Any]) -> None:
        if not colors:
            return
        is_dark = colors.get("is_dark", True)
        hover_bg = colors.get("hover_bg") or ("rgba(255,255,255,0.045)" if is_dark else "rgba(0,0,0,0.040)")
        self._colors.update(
            {
                "text": colors.get("text_primary", self._colors["text"]),
                "text_secondary": colors.get("text_secondary", self._colors["text_secondary"]),
                "row_alt": "rgba(255,255,255,0.018)" if is_dark else "rgba(0,0,0,0.022)",
                "row_line": "rgba(255,255,255,0.045)" if is_dark else "rgba(0,0,0,0.055)",
                "hover_bg": hover_bg,
            }
        )

    def set_base_font(self, font: QFont) -> None:
        self._mono_base = font

    # ── 尺寸 ──
    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), ROW_H)

    # ── 绘制 ──
    def paint(self, painter: QPainter, option, index: QModelIndex) -> None:
        rec: Optional[TraceRecord] = index.data(Qt.UserRole)
        if rec is None:
            return

        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)

        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)

            # 行背景
            if selected:
                painter.fillRect(rect, QColor(self._colors["selected_bg"]))
                painter.fillRect(QRect(0, rect.y(), 2, rect.height()), QColor(self._colors["selected_line"]))
            elif hovered:
                painter.fillRect(rect, QColor(self._colors["hover_bg"]))
            elif index.row() % 2 == 1:
                painter.fillRect(rect, QColor(self._colors["row_alt"]))

            # 行分隔线
            painter.setPen(QColor(self._colors["row_line"]))
            painter.drawLine(rect.x(), rect.bottom(), rect.right(), rect.bottom())

            mono = _mono_font(self._mono_base, -1.0)

            # ① 类型圆点
            color = QColor(ENTRY_KIND_COLORS.get(rec.kind, "#888888"))
            cy = rect.center().y()
            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(COL_DOT, cy - 3, 6, 6)

            # ② type 标签（等宽粗体，用类型色）
            type_font = QFont(mono)
            type_font.setBold(True)
            type_font.setPointSizeF(max(7.0, mono.pointSizeF() - 0.5))
            painter.setFont(type_font)
            painter.setPen(color)
            type_rect = QRect(COL_TYPE, rect.y(), TYPE_W, rect.height())
            painter.drawText(type_rect, Qt.AlignVCenter | Qt.AlignLeft, rec.kind.label)

            # ③ 主文本：label + preview（label 常规色，preview 次要色）
            main_font = QFont(mono)
            painter.setFont(main_font)
            fm = QFontMetrics(main_font)

            meta_w = META_W
            avail_w = max(40, rect.width() - COL_MAIN - meta_w - PAD_R)

            label_text = rec.label
            label_w = fm.horizontalAdvance(label_text)
            gap = 10
            preview_avail = max(20, avail_w - label_w - gap)

            base_y = rect.y()
            h = rect.height()

            painter.setPen(QColor(self._colors["text"]))
            painter.drawText(
                QRect(COL_MAIN, base_y, min(label_w, avail_w), h),
                Qt.AlignVCenter | Qt.AlignLeft,
                fm.elidedText(label_text, Qt.ElideRight, min(label_w, avail_w)),
            )

            if preview_avail > 30:
                painter.setPen(QColor(self._colors["text_secondary"]))
                painter.drawText(
                    QRect(COL_MAIN + label_w + gap, base_y, preview_avail, h),
                    Qt.AlignVCenter | Qt.AlignLeft,
                    fm.elidedText(rec.preview, Qt.ElideRight, preview_avail),
                )

            # ④ 右侧耗时（等宽，右对齐）；失败标红、进行中显示省略动画点
            meta_font = QFont(mono)
            meta_font.setPointSizeF(max(7.0, mono.pointSizeF() - 0.5))
            painter.setFont(meta_font)
            if rec.is_error:
                painter.setPen(QColor("#F7768E"))
            elif rec.is_pending:
                painter.setPen(QColor("#E0AF68"))
            else:
                painter.setPen(QColor(self._colors["text_dim"]))
            meta_rect = QRect(rect.width() - meta_w - PAD_R, base_y, meta_w, h)
            painter.drawText(meta_rect, Qt.AlignVCenter | Qt.AlignRight, format_duration(rec.duration_ms))
        finally:
            painter.restore()


class TurnListWidget(QWidget):
    """中央列表：顶部摘要条 + QListWidget 条目列表。"""

    recordSelected = pyqtSignal(int)

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._selected_idx: Optional[int] = None
        self._delegate = _EntryDelegate(self)
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 顶部摘要条（Turns / Calls）
        self._tab_bar = QFrame(self)
        self._tab_bar.setFixedHeight(34)
        self._tab_bar.setObjectName("agentTraceTabBar")
        bar = QHBoxLayout(self._tab_bar)
        bar.setContentsMargins(12, 0, 12, 0)
        bar.setSpacing(18)
        self._turns_label = QLabel("Turns", self._tab_bar)
        self._calls_label = QLabel("Calls", self._tab_bar)
        bar.addWidget(self._turns_label)
        bar.addWidget(self._calls_label)
        bar.addStretch(1)
        outer.addWidget(self._tab_bar)

        # 条目列表
        self._list = QListWidget(self)
        self._list.setItemDelegate(self._delegate)
        self._list.setAlternatingRowColors(False)
        self._list.setUniformItemSizes(True)
        self._list.setSelectionMode(QListWidget.SingleSelection)
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self._list.currentRowChanged.connect(self._on_current_row_changed)
        outer.addWidget(self._list, 1)

        self._apply_list_style()

    def _apply_list_style(self) -> None:
        self._list.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; }"
            "QListWidget::item { border: none; }"
            "QListWidget::item:selected { background: transparent; }"
        )

    # ──────────────────── 公开 API ────────────────────

    def set_records(self, records: List[TraceRecord], keep_selection: bool = True) -> None:
        """整体重置。默认保留当前选中行（实时刷新时避免选中态丢失）。"""
        prev = self._selected_idx
        self._records = list(records)
        self._rebuild_items()
        if keep_selection and prev is not None and 0 <= prev < len(self._records):
            self._selected_idx = prev
            self._list.setCurrentRow(prev)
        else:
            self._selected_idx = None
        self._refresh_summary()

    def append_records(self, records: List[TraceRecord]) -> None:
        before = len(self._records)
        self._records.extend(records)
        self._append_items(start_idx=before)
        self._refresh_summary()

    def update_record(self, idx: int) -> None:
        if 0 <= idx < self._list.count():
            item = self._list.item(idx)
            if item is not None:
                item.setData(Qt.UserRole, self._records[idx])
            self._list.viewport().update()
        self._refresh_summary()

    def clear(self) -> None:
        self._records = []
        self._selected_idx = None
        self._list.clear()
        self._refresh_summary()

    def select(self, idx: int) -> None:
        if 0 <= idx < len(self._records):
            self._list.setCurrentRow(idx)

    def set_colors(self, colors: Dict[str, Any]) -> None:
        self._delegate.set_colors(colors)
        if not colors:
            return
        text = colors.get("text_primary", "#D8D8D8")
        secondary = colors.get("text_secondary", "#8A8A8A")
        border = colors.get("border", "#333333")
        bg = colors.get("card_bg", "transparent")
        mono = "Cascadia Mono, Consolas, monospace"
        self._setStyleSheetSafe(
            self._tab_bar,
            f"#agentTraceTabBar {{ background: transparent; border-bottom: 1px solid {border}; }}",
        )
        self._turns_label.setStyleSheet(f"color: {text}; font-family: {mono}; font-size: 11px;")
        self._calls_label.setStyleSheet(f"color: {secondary}; font-family: {mono}; font-size: 11px;")
        self._list.setStyleSheet(
            f"QListWidget {{ background: {bg}; border: none; outline: none; }}QListWidget::item {{ border: none; }}"
        )
        self._list.viewport().update()

    @staticmethod
    def _setStyleSheetSafe(widget: QWidget, qss: str) -> None:  # noqa: N802
        try:
            widget.setStyleSheet(qss)
        except RuntimeError:
            pass

    def _apply_font(self, font: QFont) -> None:
        self._delegate.set_base_font(font)
        self._list.viewport().update()

    # ──────────────────── 内部 ────────────────────

    def _make_item(self, idx: int, rec: TraceRecord) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, rec)
        item.setData(Qt.UserRole + 1, idx)
        item.setSizeHint(QSize(0, ROW_H))
        item.setToolTip(f"{rec.kind.label} · {rec.label}\n{rec.source}")
        return item

    def _rebuild_items(self) -> None:
        self._list.clear()
        for i, rec in enumerate(self._records):
            self._list.addItem(self._make_item(i, rec))

    def _append_items(self, start_idx: int) -> None:
        for i in range(start_idx, len(self._records)):
            self._list.addItem(self._make_item(i, self._records[i]))

    def _on_current_row_changed(self, row: int) -> None:
        if row is None or row < 0 or row >= len(self._records):
            return
        self._selected_idx = row
        self.recordSelected.emit(row)

    def _refresh_summary(self) -> None:
        turns = sum(1 for r in self._records if r.kind == EntryKind.USER)
        calls = sum(1 for r in self._records if r.kind == EntryKind.TOOL)
        self._turns_label.setText(f"Turns  {turns}")
        self._calls_label.setText(f"Calls  {calls}")
