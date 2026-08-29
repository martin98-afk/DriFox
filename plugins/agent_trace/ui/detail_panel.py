# -*- coding: utf-8 -*-
"""agent_trace.DetailPanel — 右侧详情面板（对齐 DeepSeek Harness）。

结构：
- 标题行：[类型徽章] Turn N · label（+ 清除选中 ×）
- 4 个 sub-tabs：Summary / Preview / Raw / Source
  - Summary：键值表（Kind / Label / Status / Duration / Start / Source）
  - Preview：完整消息内容（等宽、自动换行、可滚动、可选中复制）
  - Raw：完整原始内容（不换行）
  - Source：来源 + meta 结构化 JSON

v3 修复：
- **tab 点击无反应**：v2 的 ``_on_seg_changed`` 从不调用
  ``QStackedWidget.setCurrentIndex``，页面永远停在 Summary。
  现按 key→index 映射切换页面。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont
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

from .trace_models import TraceRecord, format_duration, kind_color, with_alpha

MONO_FAMILY = "Cascadia Mono, Consolas, Menlo, monospace"

_SUMMARY_FIELDS = ("Kind", "Label", "Status", "Duration", "Start", "Source")
_SEG_TO_INDEX = {"summary": 0, "preview": 1, "raw": 2, "source": 3}


def _badge_stylesheet(color: QColor, fs: int) -> str:
    """类型徽章样式：类型色文字 + 12% 透明类型色底（with_alpha 派生，防 rgba 黑块）。"""
    bg = with_alpha(color, 30)
    hexv = color.name()
    return (
        "QLabel {"
        f" color: {hexv};"
        f" background: rgba({bg.red()},{bg.green()},{bg.blue()},{bg.alpha()});"
        " border-radius: 3px;"
        f" font-family: {MONO_FAMILY};"
        f" font-size: {max(9, fs - 3)}px; font-weight: 700; letter-spacing: 0.5px;"
        "}"
    )


class DetailPanel(QWidget):
    """右侧详情面板。"""

    dismissRequested = pyqtSignal()  # 点击 × → 清除选中

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._current_idx: Optional[int] = None
        self._value_labels: Dict[str, QLabel] = {}
        self._colors: Dict[str, Any] = {}
        self._is_dark = True
        self._fs = 12  # 基准字号（像素，由 _apply_font 注入）
        self._font_family = "Segoe UI"
        self._build_ui()

    # ──────────────────── 搭建 ────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 标题行：徽章 + label + turn/source + ×
        self._title_bar = QFrame(self)
        self._title_bar.setFixedHeight(44)
        self._title_bar.setObjectName("agentTraceDetailTitle")
        title = QHBoxLayout(self._title_bar)
        title.setContentsMargins(14, 0, 8, 0)
        title.setSpacing(8)
        self._badge = QLabel("----", self._title_bar)
        self._badge.setAlignment(Qt.AlignCenter)
        self._badge.setFixedWidth(86)
        self._badge.setFixedHeight(20)
        title.addWidget(self._badge)
        self._title_label = QLabel("未选中条目", self._title_bar)
        title.addWidget(self._title_label, 1)
        self._close_btn = QFrame(self._title_bar)
        self._close_btn.setFixedSize(24, 24)
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setObjectName("agentTraceDetailClose")
        layout_close = QHBoxLayout(self._close_btn)
        layout_close.setContentsMargins(0, 0, 0, 0)
        close_lbl = QLabel("✕", self._close_btn)
        close_lbl.setAlignment(Qt.AlignCenter)
        layout_close.addWidget(close_lbl)
        self._close_btn.mouseReleaseEvent = lambda ev: self.dismissRequested.emit()  # noqa: ARG005
        title.addWidget(self._close_btn)
        outer.addWidget(self._title_bar)

        # segmented 切换
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

        self._summary = self._build_summary_panel()
        self._stack.addWidget(self._summary)  # index 0
        self._preview = self._build_text_page(wrap=True)
        self._raw = self._build_text_page(wrap=True)
        self._source = self._build_text_page(wrap=False)
        for w in (self._preview, self._raw, self._source):
            self._stack.addWidget(w)  # index 1/2/3

        self._apply_idle()

    def _build_summary_panel(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setObjectName("agentTraceSummaryScroll")

        body = QWidget()
        body.setObjectName("agentTraceSummaryBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(0)

        for i, field in enumerate(_SUMMARY_FIELDS):
            row = QWidget(body)
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(12)
            key = QLabel(field, row)
            key.setFixedWidth(76)
            val = QLabel("-", row)
            val.setWordWrap(True)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            row_layout.addWidget(key, 0, Qt.AlignTop)
            row_layout.addWidget(val, 1)
            layout.addWidget(row)
            self._value_labels[field] = val
            if i < len(_SUMMARY_FIELDS) - 1:
                layout.addSpacing(6)

        layout.addStretch(1)
        scroll.setWidget(body)
        return scroll

    def _build_text_page(self, wrap: bool) -> QPlainTextEdit:
        edit = QPlainTextEdit()
        edit.setReadOnly(True)
        edit.setFrameShape(QFrame.NoFrame)
        edit.setLineWrapMode(QPlainTextEdit.WidgetWidth if wrap else QPlainTextEdit.NoWrap)
        edit.setUndoRedoEnabled(False)
        edit.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        return edit

    # ──────────────────── 公开 API ────────────────────

    def set_records(self, records: List[TraceRecord]) -> None:
        self._records = list(records)
        if self._current_idx is not None and self._current_idx >= len(self._records):
            self._current_idx = None
            self._apply_idle()
        else:
            self._refresh_current()

    def select(self, idx: Optional[int]) -> None:
        """选中记录（None 清除）。tab 保持当前项不重置。"""
        self._current_idx = idx
        self._refresh_current()

    @property
    def current_idx(self) -> Optional[int]:
        return self._current_idx

    def clear(self) -> None:
        self._records = []
        self._current_idx = None
        self._apply_idle()

    def set_colors(self, colors: Dict[str, Any]) -> None:
        if not colors:
            return
        self._colors = dict(colors)
        self._is_dark = bool(colors.get("is_dark", True))
        self._apply_theme()
        self._refresh_current()

    # ──────────────────── 主题 ────────────────────

    def _apply_theme(self) -> None:
        c = self._colors
        text = c.get("text_primary", "#C8C8D0")
        secondary = c.get("text_secondary", "#8A8A94")
        dim = c.get("text_dim") or secondary
        border = c.get("border", "#333333")
        bg = c.get("card_bg", "transparent")
        hover_bg = "rgba(128,128,128,0.15)"
        fs = self._fs
        ff = self._font_family

        self._title_bar.setStyleSheet(
            f"#agentTraceDetailTitle {{ background: transparent; border-bottom: 1px solid {border}; }}"
            f"#agentTraceDetailTitle QLabel {{ color: {text}; font-family: '{ff}'; font-size: {fs}px; }}"
        )
        self._badge.setStyleSheet(_badge_stylesheet(QColor("#888888"), fs))
        for field in _SUMMARY_FIELDS:
            val_lbl = self._value_labels.get(field)
            if val_lbl is not None:
                val_lbl.setStyleSheet(
                    f"color: {text}; font-family: {MONO_FAMILY}; font-size: {max(9, fs - 1)}px; padding: 2px 0;"
                )
        # summary 键列（取 body 内所有非值 label 统一设暗色）
        body = self._summary.findChild(QWidget, "agentTraceSummaryBody")
        if body is not None:
            for lbl in body.findChildren(QLabel):
                if lbl not in self._value_labels.values():
                    lbl.setStyleSheet(
                        f"color: {dim}; font-family: '{ff}'; font-size: {max(9, fs - 1)}px; padding: 2px 0;"
                    )
            body.setStyleSheet(f"QWidget#agentTraceSummaryBody {{ background: {bg}; }}")
        self._summary.setStyleSheet(f"QScrollArea#agentTraceSummaryScroll {{ background: {bg}; border: none; }}")

        for edit in (self._preview, self._raw, self._source):
            edit.setStyleSheet(
                f"QPlainTextEdit {{ background: {bg}; color: {text}; border: none; "
                f"font-family: {MONO_FAMILY}; font-size: {fs}px; padding: 12px 16px; }}"
            )
        self._close_btn.setStyleSheet(
            f"QFrame#agentTraceDetailClose {{ background: transparent; border-radius: 4px; }}"
            f"QFrame#agentTraceDetailClose:hover {{ background: {hover_bg}; }}"
            f"QFrame#agentTraceDetailClose QLabel {{ color: {secondary}; font-size: {max(9, fs - 2)}px; }}"
        )

    # ──────────────────── 刷新 ────────────────────

    def _on_seg_changed(self, key: str) -> None:
        # ★ v2 bug 修复：按 key 切换 QStackedWidget 页面（此前从不切换，
        #   Preview/Raw/Source 点击永远无效）
        idx = _SEG_TO_INDEX.get(key)
        if idx is not None:
            self._stack.setCurrentIndex(idx)
        self._refresh_current()

    def _refresh_current(self) -> None:
        if not self._records or self._current_idx is None or self._current_idx >= len(self._records):
            self._apply_idle()
            return
        rec = self._records[self._current_idx]

        color = kind_color(rec.kind)
        self._badge.setText(rec.kind.label)
        self._badge.setStyleSheet(_badge_stylesheet(color, self._fs))
        turn_part = f"Turn {rec.turn_no} · " if rec.turn_no > 0 else ""
        self._title_label.setText(f"{turn_part}{rec.label}")

        values = {
            "Kind": rec.kind.label,
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

        if hasattr(self, "_preview"):
            self._preview.setPlainText(rec.raw or "（空）")
            self._raw.setPlainText(rec.raw or "（空）")

        source_text = rec.source or "-"
        if rec.meta:
            try:
                meta_json = json.dumps(rec.meta, ensure_ascii=False, indent=2)
            except Exception:
                meta_json = str(rec.meta)
            source_text = f"{source_text}\n\n── meta ──\n{meta_json}"
        self._source.setPlainText(source_text)

    def _apply_idle(self) -> None:
        self._badge.setText("----")
        self._badge.setStyleSheet(_badge_stylesheet(QColor("#888888"), self._fs))
        self._title_label.setText("未选中条目")
        for field in _SUMMARY_FIELDS:
            lbl = self._value_labels.get(field)
            if lbl is not None:
                lbl.setText("-")
        self._preview.setPlainText("点击左侧条目查看消息内容…")
        self._raw.setPlainText("点击左侧条目查看完整原始内容…")
        self._source.setPlainText("点击左侧条目查看来源…")

    # ──────────────────── 字体 ────────────────────

    def _apply_font(self, font: QFont) -> None:
        """字体跟随系统设置（pixelSize）→ 重刷 QSS 字号。"""
        px = font.pixelSize()
        if px <= 0:
            ptf = font.pointSizeF()
            px = int(round(ptf * 4 / 3)) if ptf > 0 else 12  # pt → px 兜底
        px = max(10, min(24, px))
        changed = px != self._fs
        self._fs = px
        fam = font.family()
        if fam:
            self._font_family = fam
        if changed and self._colors:
            self._apply_theme()
            self._refresh_current()
        try:  # SegmentedWidget（qfluentwidgets）项内部 setFont(18) 硬编码，专用 API 才生效
            self._segmented.setItemFontSize(max(10, px - 1))
        except Exception:
            pass
