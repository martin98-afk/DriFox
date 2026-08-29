# -*- coding: utf-8 -*-
"""agent_trace.DetailPanel — 右侧详情面板（DevTools 风格键值表 + 内容区）。

4 个 sub-tabs：Summary / Preview / Raw / Source
- Summary：等宽键值表（Kind / Label / Status / Duration / Start / Source）
- Preview：内容首段（可滚动，等宽）
- Raw：完整原始内容（等宽，自动换行关闭）
- Source：来源 + meta 结构化 JSON
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import SegmentedWidget

from .trace_models import TraceRecord, format_duration

MONO_FAMILY = "Cascadia Mono, Consolas, Menlo, monospace"

# Summary 展示的字段（键 → 取值函数）
_SUMMARY_FIELDS = ("Kind", "Label", "Status", "Duration", "Start", "Source")


class DetailPanel(QWidget):
    """右侧详情面板。"""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._current_idx: Optional[int] = None
        self._value_labels: Dict[str, QLabel] = {}
        self._colors: Dict[str, Any] = {}
        self._build_ui()

    # ──────────────────── 搭建 ────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 顶部 segmented 切换
        self._segmented = SegmentedWidget(self)
        self._segmented.addItem("summary", "Summary")
        self._segmented.addItem("preview", "Preview")
        self._segmented.addItem("raw", "Raw")
        self._segmented.addItem("source", "Source")
        self._segmented.setCurrentItem("summary")
        self._segmented.currentItemChanged.connect(self._on_seg_changed)
        outer.addWidget(self._segmented)

        self._stack = QStackedWidget(self)
        outer.addWidget(self._stack, 1)

        # Summary：等宽键值表
        self._summary = self._build_summary_panel()
        self._stack.addWidget(self._summary)

        # 其余三页：等宽只读文本
        self._preview = self._build_text_page()
        self._raw = self._build_text_page()
        self._source = self._build_text_page()
        for w in (self._preview, self._raw, self._source):
            self._stack.addWidget(w)

        self._apply_idle_message()

    def _build_summary_panel(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("agentTraceSummaryScroll")

        body = QWidget()
        body.setObjectName("agentTraceSummaryBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(0)

        for field in _SUMMARY_FIELDS:
            row = QWidget(body)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            key = QLabel(field, row)
            key.setObjectName(f"agentTraceKey_{field}")
            key.setFixedWidth(76)
            val = QLabel("-", row)
            val.setObjectName(f"agentTraceVal_{field}")
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row_layout.addWidget(key, 0, Qt.AlignTop)
            row_layout.addWidget(val, 1)
            layout.addWidget(row)
            self._value_labels[field] = val

        layout.addStretch(1)
        scroll.setWidget(body)
        return scroll

    def _build_text_page(self) -> QPlainTextEdit:
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setFrameShape(QFrame.NoFrame)
        edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        edit.setUndoRedoEnabled(False)
        edit.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        return edit

    # ──────────────────── 公开 API ────────────────────

    def set_records(self, records: List[TraceRecord]) -> None:
        self._records = list(records)
        if self._current_idx is not None and self._current_idx >= len(self._records):
            self._current_idx = None
        self._refresh_current()

    def select(self, idx: int) -> None:
        if idx < 0 or idx >= len(self._records):
            return
        self._current_idx = idx
        self._refresh_current()

    def clear(self) -> None:
        self._records = []
        self._current_idx = None
        self._apply_idle_message()

    def set_colors(self, colors: Dict[str, Any]) -> None:
        if not colors:
            return
        self._colors = dict(colors)
        self._apply_theme()

    # ──────────────────── 主题 ────────────────────

    def _apply_theme(self) -> None:
        c = self._colors
        text = c.get("text_primary", "#D8D8D8")
        secondary = c.get("text_secondary", "#8A8A8A")
        dim = c.get("text_dim") or secondary
        border = c.get("border", "#333333")
        bg = c.get("card_bg", "transparent")

        for field in _SUMMARY_FIELDS:
            key_lbl = self._summary.findChild(QLabel, f"agentTraceKey_{field}")
            if key_lbl is not None:
                key_lbl.setStyleSheet(f"color: {dim}; font-family: {MONO_FAMILY}; font-size: 11px; padding: 4px 0;")
            val_lbl = self._value_labels.get(field)
            if val_lbl is not None:
                val_lbl.setStyleSheet(f"color: {text}; font-family: {MONO_FAMILY}; font-size: 11px; padding: 4px 0;")

        self._summary.setStyleSheet(f"QScrollArea#agentTraceSummaryScroll {{ background: {bg}; border: none; }}")
        body = self._summary.findChild(QWidget, "agentTraceSummaryBody")
        if body is not None:
            body.setStyleSheet(f"QWidget#agentTraceSummaryBody {{ background: {bg}; }}")

        for edit in (self._preview, self._raw, self._source):
            edit.setStyleSheet(
                f"QPlainTextEdit {{ background: {bg}; color: {text}; border: none; "
                f"font-family: {MONO_FAMILY}; font-size: 11px; padding: 10px 12px; }}"
            )
        _ = border, secondary  # 预留：边框/次要色后续按需使用

    # ──────────────────── 刷新 ────────────────────

    def _on_seg_changed(self, _key: str) -> None:
        self._refresh_current()

    def _refresh_current(self) -> None:
        if not self._records or self._current_idx is None or self._current_idx >= len(self._records):
            self._apply_idle_message()
            return
        rec = self._records[self._current_idx]

        values = {
            "Kind": f"{rec.kind.label}",
            "Label": rec.label,
            "Status": "失败" if rec.is_error else ("进行中…" if rec.is_pending else "完成"),
            "Duration": format_duration(rec.duration_ms),
            "Start": rec.absolute_time,
            "Source": rec.source or "-",
        }
        for field, text in values.items():
            lbl = self._value_labels.get(field)
            if lbl is not None:
                lbl.setText(text)

        self._preview.setPlainText(rec.preview or "（空）")
        self._raw.setPlainText(rec.raw or rec.preview or "（空）")

        source_text = rec.source or "-"
        if rec.meta:
            try:
                meta_json = json.dumps(rec.meta, ensure_ascii=False, indent=2)
            except Exception:
                meta_json = str(rec.meta)
            source_text = f"{source_text}\n\n── meta ──\n{meta_json}"
        self._source.setPlainText(source_text)

    def _apply_idle_message(self) -> None:
        for field in _SUMMARY_FIELDS:
            lbl = self._value_labels.get(field)
            if lbl is not None:
                lbl.setText("-")
        self._preview.setPlainText("点击左侧条目查看内容…")
        self._raw.setPlainText("点击左侧条目查看完整原始内容…")
        self._source.setPlainText("点击左侧条目查看来源…")

    # ──────────────────── 字体 ────────────────────

    def _apply_font(self, font: QFont) -> None:
        _ = font  # 字号统一由 set_colors 的样式表控制，避免与主题样式打架
