# -*- coding: utf-8 -*-
"""agent_trace.DetailPanel — 右侧详情面板。

4 个 sub-tabs：Summary / Preview / Raw / Source。
- Summary：关键信息表（kind / duration / timestamps / 来源）
- Preview：raw 内容首段预览（用于快速识别）
- Raw：完整原始内容（可滚动文本）
- Source：结构化元数据（meta dict 的 JSON dump + 内部来源字符串）
"""

from __future__ import annotations

import json
from typing import List, Optional

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import SegmentedWidget

from .trace_models import TraceRecord, format_duration


class DetailPanel(QWidget):
    """右侧详情面板。"""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._current_idx: Optional[int] = None
        self._build_ui()

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

        # 4 个内容面板
        self._stack = QStackedWidget(self)
        outer.addWidget(self._stack, 1)

        # Summary
        self._summary = self._build_summary_panel()
        self._stack.addWidget(self._summary)
        # Preview
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        self._preview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._preview.setFrameShape(QFrame.NoFrame)
        self._stack.addWidget(self._preview)
        # Raw
        self._raw = QPlainTextEdit()
        self._raw.setReadOnly(True)
        self._raw.setFrameShape(QFrame.NoFrame)
        self._stack.addWidget(self._raw)
        # Source
        self._source = QPlainTextEdit()
        self._source.setReadOnly(True)
        self._source.setFrameShape(QFrame.NoFrame)
        self._stack.addWidget(self._source)

        self._apply_idle_message()

    @staticmethod
    def _build_summary_panel() -> QWidget:
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.setSpacing(6)

        self_kind = QLabel("Kind", wrap)
        self_kind.setStyleSheet("color: #808080; font-size: 11px;")
        outer.addWidget(self_kind)
        outer.addWidget(_value_label("kind_value", wrap))

        for label in ("Label", "Status", "Duration", "Start", "Source"):
            outer.addWidget(_small_label(label, wrap))
            outer.addWidget(_value_label(label.lower().replace(" ", "_") + "_value", wrap))

        outer.addStretch(1)
        return wrap

    # ──────────────────── 公开 API ────────────────────

    def set_records(self, records: List[TraceRecord]) -> None:
        self._records = list(records)
        if self._current_idx is None or self._current_idx >= len(self._records):
            self._refresh_current()
        else:
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

    def set_colors(self, colors: dict) -> None:
        """主题色注入。"""
        if not colors:
            return
        text = colors.get("text_primary", "#D0D0D0")
        secondary = colors.get("text_secondary", "#909090")
        border = colors.get("border", "#333")
        bg = colors.get("card_bg", "transparent")
        self._stack.setStyleSheet(
            f"QStackedWidget {{ background: {bg}; }}"
            f"QPlainTextEdit {{ background: {bg}; color: {text}; border: 1px solid {border}; "
            f"font-family: Consolas, monospace; font-size: 11px; }}"
        )
        for label in self.findChildren(QLabel):
            label.setStyleSheet(f"color: {secondary}; font-size: 11px;")

    # ──────────────────── 内部刷新 ────────────────────

    def _on_seg_changed(self, key: str) -> None:
        self._stack.setCurrentWidget(self._stack.currentWidget())
        # segmented 已自动切 stack；无需额外处理
        self._refresh_current()  # 只为刷新依赖 seg 的文本

    def _refresh_current(self) -> None:
        if not self._records or self._current_idx is None or self._current_idx >= len(self._records):
            self._apply_idle_message()
            return
        rec = self._records[self._current_idx]
        # Summary
        self._set_value("kind_value", f"{rec.kind.label}   ({rec.kind.value})")
        # 重写一下 Summary 的键
        all_labels = self._summary.findChildren(QLabel)
        value_widgets = [w for w in all_labels if w.objectName().endswith("_value")]
        # kind_value 在创建时第一个；按顺序对应 Label/Status/Duration/Start/Source
        # 把 widget 顺序存起来以便更新更精确
        for w in value_widgets[:6]:
            key = w.objectName()
            if key == "kind_value":
                w.setText(f"{rec.kind.label}   ({rec.kind.value})")
            elif key == "label_value":
                w.setText(rec.label)
            elif key == "status_value":
                if rec.is_error:
                    w.setText("失败")
                elif rec.is_pending:
                    w.setText("进行中…")
                else:
                    w.setText("完成")
            elif key == "duration_value":
                w.setText(format_duration(rec.duration_ms))
            elif key == "start_value":
                w.setText(rec.absolute_time)
            elif key == "source_value":
                w.setText(rec.source)
        # Preview / Raw / Source
        self._preview.setPlainText(rec.preview)
        self._raw.setPlainText(rec.raw if rec.raw else rec.preview)
        source_text = rec.source or ""
        if rec.meta:
            try:
                meta_json = json.dumps(rec.meta, ensure_ascii=False, indent=2)
            except Exception:
                meta_json = str(rec.meta)
            source_text = f"{source_text}\n\nMeta:\n{meta_json}"
        self._source.setPlainText(source_text)

    def _set_value(self, key: str, text: str) -> None:
        for w in self._summary.findChildren(QLabel):
            if w.objectName() == key:
                w.setText(text)
                return

    def _apply_idle_message(self) -> None:
        for w in self._summary.findChildren(QLabel):
            if w.objectName().endswith("_value"):
                w.setText("-" if w.objectName() != "kind_value" else "（未选中）")
        self._preview.setPlainText("点击列表条目查看预览内容…")
        self._raw.setPlainText("点击列表条目查看完整内容…")
        self._source.setPlainText("点击列表条目查看来源信息…")


def _small_label(text: str, parent: QWidget) -> QLabel:
    lbl = QLabel(text, parent)
    lbl.setStyleSheet("color: #808080; font-size: 11px;")
    return lbl


def _value_label(obj_name: str, parent: QWidget) -> QLabel:
    lbl = QLabel("-", parent)
    lbl.setObjectName(obj_name)
    lbl.setWordWrap(True)
    lbl.setStyleSheet("color: #D0D0D0; font-size: 12px; padding: 2px 0 8px 0;")
    return lbl
