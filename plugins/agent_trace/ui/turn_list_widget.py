# -*- coding: utf-8 -*-
"""agent_trace.TurnListWidget — 中央条目列表。

每条 TraceRecord 渲染成一行（左侧 type 色块 + type 标签 + 标题 + 预览）。
点击行 → 通知外层 DetailPanel 切换到对应 entry。
"""

from __future__ import annotations

from typing import List, Optional

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import ScrollArea

from .trace_models import ENTRY_KIND_COLORS, EntryKind, TraceRecord, format_duration


class _EntryRow(QFrame):
    """单条 entry 的列表行。"""

    clicked = pyqtSignal(int)

    def __init__(self, idx: int, rec: TraceRecord, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._idx = idx
        self._rec = rec
        self._selected = False
        self.setObjectName("agentTraceEntryRow")
        self._build_ui()

    def _build_ui(self) -> None:
        self.setProperty("selected", False)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 12, 4)
        layout.setSpacing(8)

        # type 色块（4px 宽竖条）
        self._bar = QFrame(self)
        self._bar.setFixedSize(4, 28)
        self._bar.setObjectName("agentTraceEntryBar")
        layout.addWidget(self._bar)

        # type 标签
        self._type_label = QLabel(self._rec.kind.label, self)
        self._type_label.setObjectName("agentTraceEntryType")
        self._type_label.setFixedWidth(78)
        layout.addWidget(self._type_label)

        # 标题 + 预览（垂直堆叠）
        text_box = QWidget(self)
        text_layout = QVBoxLayout(text_box)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(0)
        self._title_label = QLabel(self._rec.label, self)
        self._title_label.setObjectName("agentTraceEntryTitle")
        self._preview_label = QLabel(self._rec.preview, self)
        self._preview_label.setObjectName("agentTraceEntryPreview")
        self._preview_label.setWordWrap(False)
        text_layout.addWidget(self._title_label)
        text_layout.addWidget(self._preview_label)
        layout.addWidget(text_box, 1)

        # 耗时 / 时间戳
        self._meta_label = QLabel(self._format_meta(), self)
        self._meta_label.setObjectName("agentTraceEntryMeta")
        self._meta_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._meta_label.setFixedWidth(140)
        layout.addWidget(self._meta_label)

        self._apply_style()

    def _format_meta(self) -> str:
        parts: List[str] = []
        dur = self._rec.duration_ms
        if dur >= 0:
            parts.append(format_duration(dur))
        ts = self._rec.absolute_time
        if ts and ts != "-":
            # HH:MM:SS.mmm → 截到秒
            parts.append(ts.split(".", 1)[0])
        if self._rec.is_pending:
            parts.append("进行中…")
        if self._rec.is_error:
            parts.append("失败")
        return "   ".join(parts)

    def _apply_style(self, is_dark: bool = True) -> None:
        color = ENTRY_KIND_COLORS.get(self._rec.kind, "#888888")
        # 颜色 bar
        bar_qss = f"background-color: {color}; border: none;"
        self._bar.setStyleSheet(bar_qss)
        # type tag 用色块色 + 半透明背景
        r, g, b = self._hex_to_rgb(color)
        type_qss = (
            f"color: {color}; font-weight: 600; font-size: 11px; "
            f"background-color: rgba({r},{g},{b},0.12); border-radius: 4px; "
            f"padding: 2px 6px;"
        )
        self._type_label.setStyleSheet(type_qss)
        self._type_label.setAlignment(Qt.AlignCenter)
        # title / preview / meta
        title_color = "#E0E0E0" if is_dark else "#202020"
        secondary_color = "#909090" if is_dark else "#666666"
        meta_color = "#808080" if is_dark else "#888888"
        row_hover = "rgba(255,255,255,0.03)" if is_dark else "rgba(0,0,0,0.04)"
        row_border = "1px solid rgba(255,255,255,0.04)" if is_dark else "1px solid rgba(0,0,0,0.06)"
        self._title_label.setStyleSheet(f"font-size: 12px; font-weight: 500; color: {title_color};")
        self._preview_label.setStyleSheet(f"font-size: 11px; color: {secondary_color};")
        self._meta_label.setStyleSheet(f"font-size: 11px; color: {meta_color}; font-family: Consolas, monospace;")
        self.setStyleSheet(
            f"QFrame#agentTraceEntryRow {{ background: transparent; border-bottom: {row_border}; }}"
            f"QFrame#agentTraceEntryRow:hover {{ background-color: {row_hover}; }}"
        )

    def set_selected(self, selected: bool) -> None:
        if self._selected == selected:
            return
        self._selected = selected
        self.setProperty("selected", selected)
        # 重新刷样式
        if selected:
            self.setStyleSheet(
                "QFrame#agentTraceEntryRow { background-color: rgba(122,162,247,0.10); "
                "border-bottom: 1px solid rgba(255,255,255,0.04); }"
            )
        else:
            self.setStyleSheet(
                "QFrame#agentTraceEntryRow { background: transparent; border-bottom: 1px solid rgba(255,255,255,0.04); }"
                "QFrame#agentTraceEntryRow:hover { background-color: rgba(255,255,255,0.03); }"
            )

    @staticmethod
    def _hex_to_rgb(hex_color: str) -> tuple:
        h = hex_color.lstrip("#")
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)

    def mousePressEvent(self, _event) -> None:  # noqa: N802
        self.clicked.emit(self._idx)


class TurnListWidget(QWidget):
    """中央列表 widget — 顶部「Turns / Calls」sub-tabs + 滚动区。"""

    recordSelected = pyqtSignal(int)

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._selected_idx: Optional[int] = None
        self._rows: List[_EntryRow] = []
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # sub-tab 行（仅展示，无强切换语义；现在默认走 Turns）
        self._tab_bar = QFrame(self)
        self._tab_bar.setFixedHeight(40)
        self._tab_bar.setObjectName("agentTraceTabBar")
        tab_bar_layout = QHBoxLayout(self._tab_bar)
        tab_bar_layout.setContentsMargins(12, 8, 12, 8)
        tab_bar_layout.setSpacing(24)
        self._turns_label = QLabel("Turns", self._tab_bar)
        self._turns_label.setStyleSheet("font-size: 13px; font-weight: 600;")
        self._calls_label = QLabel("Calls", self._tab_bar)
        self._calls_label.setStyleSheet("font-size: 13px; color: #808080;")
        tab_bar_layout.addWidget(self._turns_label)
        tab_bar_layout.addWidget(self._calls_label)
        tab_bar_layout.addStretch(1)
        # 搜索框占位（暂未实现过滤）
        self._search_label = QLabel("Search", self._tab_bar)
        self._search_label.setStyleSheet("font-size: 12px; color: #808080;")
        tab_bar_layout.addWidget(self._search_label)
        # 搜索框其实可以是 QLineEdit，但本次仅展示位置
        outer.addWidget(self._tab_bar)

        # 滚动区
        self._scroll = ScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setStyleSheet("background: transparent; border: none;")
        self._container = QWidget()
        self._container.setObjectName("agentTraceListContainer")
        self._container_layout = QVBoxLayout(self._container)
        self._container_layout.setContentsMargins(0, 0, 0, 0)
        self._container_layout.setSpacing(0)
        self._container_layout.addStretch(1)
        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)

    # ──────────────────── 公开 API ────────────────────

    def set_records(self, records: List[TraceRecord]) -> None:
        """整体重置。"""
        self._records = list(records)
        self._selected_idx = None
        self._rebuild_rows()

    def append_records(self, records: List[TraceRecord]) -> None:
        """增量追加。"""
        before = len(self._records)
        self._records.extend(records)
        self._append_rows(start_idx=before)

    def update_record(self, idx: int) -> None:
        """in-flight/end 翻转。"""
        if idx < 0 or idx >= len(self._records):
            return
        if idx < len(self._rows):
            self._rows[idx]._rec = self._records[idx]
            self._rows[idx]._meta_label.setText(self._rows[idx]._format_meta())
        self._refresh_turns_calls_summary()

    def clear(self) -> None:
        self._records = []
        self._selected_idx = None
        self._rebuild_rows()
        self._refresh_turns_calls_summary()

    def select(self, idx: int) -> None:
        self._set_selected(idx)

    def set_colors(self, colors: dict) -> None:
        """主题色注入（ctx 拉模型）。"""
        if not colors:
            return
        text = colors.get("text_primary", "#D0D0D0")
        secondary = colors.get("text_secondary", "#909090")
        border = colors.get("border", "#333")
        bg = colors.get("card_bg", "transparent")
        is_dark = colors.get("is_dark", True)
        if hasattr(self, "_turns_label"):
            self._turns_label.setStyleSheet(f"font-size: 13px; font-weight: 600; color: {text};")
        if hasattr(self, "_calls_label"):
            self._calls_label.setStyleSheet(f"font-size: 13px; color: {secondary};")
        if hasattr(self, "_search_label"):
            self._search_label.setStyleSheet(f"font-size: 12px; color: {secondary};")
        if hasattr(self, "_tab_bar"):
            self._tab_bar.setStyleSheet(
                f"#agentTraceTabBar {{ background: transparent; border-bottom: 1px solid {border}; }}"
            )
        if hasattr(self, "_container"):
            self._container.setStyleSheet(f"#agentTraceListContainer {{ background: {bg}; }}")
        # 重置 row 内的样式，让颜色跟随主题（每 row 独立持有 _apply_style）
        for row in self._rows:
            row._apply_style(is_dark=is_dark)

    def _apply_font(self, font) -> None:
        if not self._rows:
            return
        for row in self._rows:
            row.setFont(font)

    # ──────────────────── 重建 / 渲染 ────────────────────

    def _rebuild_rows(self) -> None:
        # 清空所有现有 row
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows = []
        # 重置 layout 内容（保留 stretch）
        self._clear_layout_keep_stretch(self._container_layout)
        if not self._records:
            self._container_layout.addStretch(1)
            self._refresh_turns_calls_summary()
            return
        self._append_rows(start_idx=0)
        self._refresh_turns_calls_summary()

    def _append_rows(self, start_idx: int) -> None:
        if self._container_layout.count() == 0:
            self._container_layout.addStretch(1)
        # 在 stretch 之前插入新 row（index = count - 1）
        insert_pos = self._container_layout.count() - 1
        for offset, rec in enumerate(self._records[start_idx:]):
            row = _EntryRow(start_idx + offset, rec, self._container)
            row.clicked.connect(self._on_row_clicked)
            self._container_layout.insertWidget(insert_pos + offset, row)
            self._rows.append(row)
        self._refresh_turns_calls_summary()

    @staticmethod
    def _clear_layout_keep_stretch(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    # ──────────────────── 选中态 ────────────────────

    def _on_row_clicked(self, idx: int) -> None:
        self._set_selected(idx)
        self.recordSelected.emit(idx)

    def _set_selected(self, idx: int) -> None:
        if self._selected_idx == idx:
            return
        if self._selected_idx is not None and 0 <= self._selected_idx < len(self._rows):
            self._rows[self._selected_idx].set_selected(False)
        if 0 <= idx < len(self._rows):
            self._rows[idx].set_selected(True)
        self._selected_idx = idx

    def _refresh_turns_calls_summary(self) -> None:
        turns = sum(1 for r in self._records if r.kind == EntryKind.USER)
        calls = sum(1 for r in self._records if r.kind == EntryKind.TOOL)
        self._turns_label.setText(f"Turns · {turns}")
        self._calls_label.setText(f"Calls · {calls}")
