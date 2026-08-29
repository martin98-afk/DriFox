# -*- coding: utf-8 -*-
"""agent_trace.TimelinePanel — DeepSeek Harness / DevTools 风格时间线。

布局：

    ┌──────────────────────────────────────────────────────────┐
    │ Duration 8.24s    Turns 2    Calls 3                     │  摘要条 32px
    ├──────────────────────────────────────────────────────────┤
    │ SYSTEM      ▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │
    │ USER         ▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │
    │ CONTEXT        ▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ │
    │ ASSISTANT         ▓▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░ │
    │ TOOL                     ▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░ │
    └──────────────────────────────────────────────────────────┘

与 ``TurnListWidget`` 共享配色与列宽常量，保证视觉对齐。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from .trace_models import (
    ENTRY_KIND_COLORS,
    EntryKind,
    TraceRecord,
    format_duration,
)

# ── 与 TurnListWidget 对齐的列常量 ──
COL_DOT = 10
COL_TYPE = 26
TYPE_W = 76
COL_MAIN = COL_TYPE + TYPE_W + 8
PAD_R = 12

ROW_H = 22
SUMMARY_H = 32
BAR_INSET = 5  # 条带上下内缩

MONO_FAMILY = "Cascadia Mono, Consolas, Menlo, monospace"


@dataclass
class _Turn:
    """单轮 turn 的派生信息。"""

    start_ts: float
    end_ts: float
    records: List[int]


class TimelinePanel(QWidget):
    """顶部时间线：摘要条 + 每条记录一行横条。"""

    recordClicked = pyqtSignal(int)

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._turns: List[_Turn] = []
        self._hover_idx: Optional[int] = None
        self._colors: Dict[str, str] = {
            "text": "#D8D8D8",
            "text_secondary": "#8A8A8A",
            "text_dim": "#6E6E6E",
            "border": "#333333",
            "summary_bg": "transparent",
            "row_hover": "rgba(255,255,255,0.045)",
            "row_alt": "rgba(255,255,255,0.018)",
            "grid": "rgba(255,255,255,0.05)",
            "track": "rgba(255,255,255,0.035)",
        }
        self._base_font = QFont("Cascadia Mono", 9)
        self.setMouseTracking(True)
        self.setMinimumHeight(SUMMARY_H + ROW_H * 2 + 8)

    # ──────────────────── 公开 API ────────────────────

    def set_records(self, records: List[TraceRecord]) -> None:
        self._records = list(records)
        self._rebuild_turns()
        self.update()

    def append_records(self, records: List[TraceRecord], _start_idx: int = 0) -> None:
        self._records.extend(records)
        self._rebuild_turns()
        self.update()

    def update_record(self, _idx: int) -> None:
        self._rebuild_turns()
        self.update()

    def clear(self) -> None:
        self._records = []
        self._turns = []
        self._hover_idx = None
        self.update()

    def set_colors(self, colors: Dict[str, Any]) -> None:
        if not colors:
            return
        is_dark = colors.get("is_dark", True)
        self._colors.update(
            {
                "text": colors.get("text_primary", self._colors["text"]),
                "text_secondary": colors.get("text_secondary", self._colors["text_secondary"]),
                "border": colors.get("border", self._colors["border"]),
                "row_hover": colors.get("hover_bg") or ("rgba(255,255,255,0.045)" if is_dark else "rgba(0,0,0,0.040)"),
                "row_alt": "rgba(255,255,255,0.018)" if is_dark else "rgba(0,0,0,0.022)",
                "grid": "rgba(255,255,255,0.05)" if is_dark else "rgba(0,0,0,0.06)",
                "track": "rgba(255,255,255,0.035)" if is_dark else "rgba(0,0,0,0.045)",
            }
        )
        self.update()

    def _apply_font(self, font: QFont) -> None:
        self._base_font = font
        self.update()

    # ──────────────────── turn 划分 ────────────────────

    def _rebuild_turns(self) -> None:
        if not self._records:
            self._turns = []
            return
        recs = self._records
        turns: List[_Turn] = []
        cur: List[int] = []
        for i, rec in enumerate(recs):
            if rec.kind == EntryKind.USER and cur:
                turns.append(self._mk_turn(recs, cur))
                cur = []
            cur.append(i)
        if cur:
            turns.append(self._mk_turn(recs, cur))
        self._turns = turns

    @staticmethod
    def _mk_turn(recs: List[TraceRecord], idxs: List[int]) -> _Turn:
        start = min((recs[j].start_ts for j in idxs if recs[j].start_ts > 0), default=time.time())
        end = max((recs[j].end_ts if recs[j].end_ts > 0 else recs[j].start_ts) for j in idxs)
        if any(recs[j].is_pending for j in idxs):
            end = max(end, time.time())
        return _Turn(start_ts=start, end_ts=end, records=list(idxs))

    # ──────────────────── 尺寸 ────────────────────

    def sizeHint(self) -> QSize:  # noqa: N802
        rows = max(1, len(self._records))
        return QSize(800, SUMMARY_H + rows * ROW_H + 8)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(360, SUMMARY_H + ROW_H * 2 + 8)

    # ──────────────────── 绘制 ────────────────────

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)

            self._paint_summary(painter)

            if not self._records:
                self._paint_empty(painter)
                return

            x0 = COL_MAIN
            track_w = max(20, self.width() - x0 - PAD_R)

            anchor = min((r.start_ts for r in self._records if r.start_ts > 0), default=time.time())
            now = time.time()
            g_end = max(
                ((r.end_ts if r.end_ts > 0 else r.start_ts) for r in self._records),
                default=now,
            )
            g_end = max(g_end, now) if any(r.is_pending for r in self._records) else g_end
            span = max(1e-6, g_end - anchor)

            def x_at(ts: float) -> int:
                ratio = (ts - anchor) / span
                return int(x0 + max(0.0, min(1.0, ratio)) * track_w)

            self._paint_grid(painter, x0, track_w)

            y = SUMMARY_H
            for i, rec in enumerate(self._records):
                rect = QRect(0, y, self.width(), ROW_H)
                if self._hover_idx == i:
                    painter.fillRect(rect, QColor(self._colors["row_hover"]))
                elif i % 2 == 1:
                    painter.fillRect(rect, QColor(self._colors["row_alt"]))

                self._paint_row_label(painter, rec, rect)

                start = rec.start_ts if rec.start_ts > 0 else anchor
                end = rec.end_ts if rec.end_ts > 0 else (now if rec.is_pending else start)
                bx0 = x_at(start)
                bx1 = max(bx0 + 3, x_at(end))
                bar = QRect(bx0, y + BAR_INSET, bx1 - bx0, ROW_H - BAR_INSET * 2)

                color = QColor(ENTRY_KIND_COLORS.get(rec.kind, "#888888"))
                painter.setBrush(QBrush(color if not rec.is_pending else _alpha(color, 150)))
                painter.setPen(Qt.NoPen)
                painter.drawRoundedRect(bar, 3, 3)

                # 条内文字（窄条不画）
                if bar.width() > 56:
                    txt_color = QColor("#101010") if _is_light(color) else QColor("#FFFFFF")
                    f = QFont(self._base_font)
                    f.setPointSizeF(max(7.0, self._base_font.pointSizeF() - 1.0))
                    painter.setFont(f)
                    painter.setPen(txt_color)
                    fm = QFontMetrics(f)
                    label = rec.label + (" …" if rec.is_pending else "")
                    painter.drawText(
                        bar.adjusted(6, 0, -4, 0),
                        Qt.AlignVCenter | Qt.AlignLeft,
                        fm.elidedText(label, Qt.ElideRight, bar.width() - 10),
                    )
                y += ROW_H
        finally:
            painter.end()

    def _paint_summary(self, painter: QPainter) -> None:
        rect = QRect(0, 0, self.width(), SUMMARY_H)
        painter.fillRect(rect, QColor(self._colors["summary_bg"]))

        painter.setPen(QColor(self._colors["border"]))
        painter.drawLine(0, SUMMARY_H - 1, self.width(), SUMMARY_H - 1)

        f = QFont(self._base_font)
        f.setPointSizeF(max(7.5, self._base_font.pointSizeF() - 0.5))
        painter.setFont(f)

        if not self._records:
            painter.setPen(QColor(self._colors["text_dim"]))
            painter.drawText(rect.adjusted(COL_MAIN, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, "等待会话开始…")
            return

        anchor = min((r.start_ts for r in self._records if r.start_ts > 0), default=time.time())
        g_end = max(((r.end_ts if r.end_ts > 0 else r.start_ts) for r in self._records), default=time.time())
        total_ms = max(0, int((g_end - anchor) * 1000))

        items = [
            ("Duration", format_duration(total_ms)),
            ("Turns", str(len(self._turns))),
            ("Calls", str(sum(1 for r in self._records if r.kind == EntryKind.TOOL))),
        ]
        x = COL_MAIN
        for label, value in items:
            painter.setPen(QColor(self._colors["text_dim"]))
            painter.drawText(QRect(x, 0, 62, SUMMARY_H - 1), Qt.AlignVCenter | Qt.AlignLeft, label)
            painter.setPen(QColor(self._colors["text"]))
            painter.drawText(QRect(x + 62, 0, 96, SUMMARY_H - 1), Qt.AlignVCenter | Qt.AlignLeft, value)
            x += 186

    def _paint_grid(self, painter: QPainter, x0: int, w: int) -> None:
        """时间刻度竖线（4 等分）。"""
        painter.setPen(QPen(QColor(self._colors["grid"]), 1, Qt.DotLine))
        for k in range(1, 4):
            gx = x0 + int(w * k / 4)
            painter.drawLine(gx, SUMMARY_H, gx, self.height())

    def _paint_row_label(self, painter: QPainter, rec: TraceRecord, rect: QRect) -> None:
        color = QColor(ENTRY_KIND_COLORS.get(rec.kind, "#888888"))
        cy = rect.center().y()
        painter.setBrush(color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(COL_DOT, cy - 3, 6, 6)

        f = QFont(self._base_font)
        f.setBold(True)
        f.setPointSizeF(max(7.0, self._base_font.pointSizeF() - 0.5))
        painter.setFont(f)
        painter.setPen(color)
        painter.drawText(
            QRect(COL_TYPE, rect.y(), TYPE_W, rect.height()), Qt.AlignVCenter | Qt.AlignLeft, rec.kind.label
        )

        f.setBold(False)
        painter.setFont(f)
        painter.setPen(QColor(self._colors["text_dim"]))
        fm = QFontMetrics(f)
        painter.drawText(
            QRect(COL_TYPE, rect.y(), 0, rect.height()),
            Qt.AlignVCenter | Qt.AlignLeft,
            "",
        )
        _ = fm

    def _paint_empty(self, painter: QPainter) -> None:
        f = QFont(self._base_font)
        painter.setFont(f)
        painter.setPen(QColor(self._colors["text_dim"]))
        rect = QRect(0, SUMMARY_H, self.width(), max(0, self.height() - SUMMARY_H))
        painter.drawText(rect, Qt.AlignCenter, "暂无轨迹数据")

    # ──────────────────── 交互 ────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802
        idx = self._row_at(event.y())
        if idx is not None:
            self.recordClicked.emit(idx)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        idx = self._row_at(event.y())
        if idx != self._hover_idx:
            self._hover_idx = idx
            self.update()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if self._hover_idx is not None:
            self._hover_idx = None
            self.update()

    def _row_at(self, y: int) -> Optional[int]:
        if y < SUMMARY_H:
            return None
        row = (y - SUMMARY_H) // ROW_H
        if 0 <= row < len(self._records):
            return int(row)
        return None


def _alpha(c: QColor, a: int) -> QColor:
    out = QColor(c)
    out.setAlpha(a)
    return out


def _is_light(c: QColor) -> bool:
    return (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) > 160
