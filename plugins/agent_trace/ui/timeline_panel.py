# -*- coding: utf-8 -*-
"""agent_trace.TimelinePanel — DeepSeek Harness 风格的时间线面板。

横向条状布局：每个 TraceRecord 一行，按起止时间比例投射到 (0,1) 横轴。
整段属于一个 turn（user prompt → 下一个 user prompt 之前的全部消息）。
顶部摘要：Duration / Turns / Calls。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PyQt5.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QBrush, QColor, QFont, QFontMetrics, QMouseEvent, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from .trace_models import ENTRY_KIND_COLORS, EntryKind, TraceRecord, format_duration


@dataclass
class _Turn:
    """单轮 turn 的派生信息 — 面板用它做布局。"""

    start_ts: float
    end_ts: float  # 真实结束（流式则取 now）
    records: List[int]  # records 在 TraceCollector 中的下标
    user_label_idx: int  # 这一 turn 第一条 USER 条目（用于"Turn N"预览）


class TimelinePanel(QWidget):
    """顶部 timeline + 中段彩色条带。"""

    # 用户点击某 entry → 通知外层联动右侧详情面板 + 中央 list 高亮
    recordClicked = pyqtSignal(int)  # entry index

    ROW_HEIGHT = 22
    LEFT_GUTTER = 96  # 左侧 type 标签占宽
    ROW_PAD = 4
    SUMMARY_H = 36

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._turns: List[_Turn] = []
        self._colors = {
            "bar_bg": "#1E1E1E",
            "row_bg": "transparent",
            "row_bg_alt": "rgba(255,255,255,0.02)",
            "text": "#D0D0D0",
            "text_secondary": "#909090",
            "border": "#2A2A2A",
            "hover": "rgba(255,255,255,0.04)",
            "summary_label": "#909090",
            "summary_value": "#E0E0E0",
        }
        self.setMinimumHeight(self.SUMMARY_H + self.ROW_HEIGHT * 3)
        self.setMouseTracking(True)
        self._hover_idx: Optional[int] = None

    # ──────────────────── 公开 API ────────────────────

    def set_records(self, records: List[TraceRecord]) -> None:
        """整体重置（首次同步或 session 切换）。"""
        self._records = list(records)
        self._rebuild_turns()
        self.update()

    def append_records(self, records: List[TraceRecord], start_idx: int) -> None:
        """增量追加一段 records。"""
        before = len(self._records)
        self._records.extend(records)
        # 重建 turn 时下标应保持稳定：start_idx 是已存在的 records 数。
        self._rebuild_turns()
        # 追加时直接刷新（避免漏出滚动位置）
        self.update()
        _ = before  # 暂未使用 — 保留为未来 hover 行精确化

    def update_record(self, _idx: int) -> None:
        """单条记录 in-flight/end 翻转 — 仅刷新对应行的耗时颜色。"""
        # turn 边界受 in-flight 拉伸影响 → 重建 turn 比换行更稳。
        self._rebuild_turns()
        self.update()

    def clear(self) -> None:
        self._records = []
        self._turns = []
        self._hover_idx = None
        self.update()

    def set_colors(self, colors: dict) -> None:
        """主题色注入（ctx 拉模型）。"""
        if not colors:
            return
        c = colors
        self._colors.update(
            {
                "text": c.get("text_primary", self._colors["text"]),
                "text_secondary": c.get("text_secondary", self._colors["text_secondary"]),
                "border": c.get("border", self._colors["border"]),
                "hover": c.get("hover_bg", self._colors["hover"]),
            }
        )
        is_dark = c.get("is_dark", True)
        self._colors["bar_bg"] = "rgba(255,255,255,0.04)" if is_dark else "rgba(0,0,0,0.04)"
        self._colors["row_bg_alt"] = "rgba(255,255,255,0.02)" if is_dark else "rgba(0,0,0,0.02)"
        self.update()

    # ──────────────────── turn 边界 ────────────────────

    def _rebuild_turns(self) -> None:
        """根据现有 records 划 turn 边界。"""
        if not self._records:
            self._turns = []
            return
        # 用每个 record 的 start_ts 作为横向时间轴；turn 划分：
        # 从一个 USER 开始，到下一个 USER 之前的最后一条 record 结束。
        records = self._records
        turns: List[_Turn] = []
        cur_records: List[int] = []
        user_label_idx: Optional[int] = None
        for i, rec in enumerate(records):
            # 跳过还没 start_ts 的（如系统提示词没有 timestamp 的兜底用 now）
            if rec.start_ts <= 0:
                # 用 turn 起点兜底（取当前所有 cur_records 中第一个 start_ts，或 now）
                anchor = records[cur_records[0]].start_ts if cur_records else time.time()
                cur_records.append(i)
                continue
            if rec.kind == EntryKind.USER and cur_records:
                # 用户来了新一轮：把上一轮结算，开启新 turn
                start_ts = records[cur_records[0]].start_ts
                end_ts = max(records[j].end_ts if records[j].end_ts > 0 else records[j].start_ts for j in cur_records)
                # 流式：如果最近一条 record 还未结束，end 取 now
                if records[cur_records[-1]].is_pending or any(records[j].end_ts <= 0 for j in cur_records[-2:]):
                    end_ts = max(end_ts, time.time())
                turns.append(
                    _Turn(
                        start_ts=start_ts,
                        end_ts=end_ts,
                        records=list(cur_records),
                        user_label_idx=user_label_idx if user_label_idx is not None else cur_records[0],
                    )
                )
                cur_records = []
                user_label_idx = None
            if rec.kind == EntryKind.USER and user_label_idx is None:
                user_label_idx = i
            cur_records.append(i)
        # 收尾
        if cur_records:
            start_ts = records[cur_records[0]].start_ts or time.time()
            end_ts = max((records[j].end_ts if records[j].end_ts > 0 else records[j].start_ts) for j in cur_records)
            # in-flight 拉伸到 now
            if any(records[j].is_pending for j in cur_records):
                end_ts = max(end_ts, time.time())
            turns.append(
                _Turn(
                    start_ts=start_ts,
                    end_ts=end_ts,
                    records=list(cur_records),
                    user_label_idx=user_label_idx if user_label_idx is not None else cur_records[0],
                )
            )
        self._turns = turns

    # ──────────────────── 绘制 ────────────────────

    def sizeHint(self) -> QSize:
        rows = max(1, len(self._records))
        return QSize(800, self.SUMMARY_H + self.ROW_HEIGHT * rows + 16)

    def minimumSizeHint(self) -> QSize:
        return QSize(400, self.SUMMARY_H + self.ROW_HEIGHT * 2 + 16)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt API)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)
            painter.fillRect(self.rect(), QColor(self._colors.get("row_bg", "transparent")))

            self._paint_summary(painter)

            if not self._records:
                self._paint_empty(painter)
                return

            # records 一条一行（单层；multi-turn 在 row 间用细分隔线）
            x0 = self.LEFT_GUTTER
            content_x_start = x0
            content_w = self.width() - content_x_start - self.ROW_PAD

            # 基线时间锚点：第一条有效 record 的 start_ts → 全局最小 start_ts
            anchor_ts = min(
                (r.start_ts for r in self._records if r.start_ts > 0),
                default=time.time(),
            )
            # 全局最大 end_ts（or now）
            now = time.time()
            global_end = max(
                ((r.end_ts if r.end_ts > 0 else r.start_ts) for r in self._records),
                default=now,
            )
            global_end = max(global_end, now)

            def _x_at(ts: float) -> int:
                if global_end <= anchor_ts:
                    return content_x_start
                ratio = (ts - anchor_ts) / (global_end - anchor_ts)
                return int(content_x_start + max(0.0, min(1.0, ratio)) * content_w)

            row_y = self.SUMMARY_H
            for i, rec in enumerate(self._records):
                row_rect = QRect(0, row_y, self.width(), self.ROW_HEIGHT)
                is_hover = self._hover_idx == i
                if is_hover:
                    painter.fillRect(row_rect, QColor(self._colors["hover"]))
                elif i % 2 == 1:
                    painter.fillRect(row_rect, QColor(self._colors["row_bg_alt"]))

                # 左侧 type 标签 + label
                self._paint_row_label(painter, rec, row_rect)

                # 右侧 bar：按 (start_ts, end_ts) 比例
                start = rec.start_ts if rec.start_ts > 0 else anchor_ts
                end = rec.end_ts if rec.end_ts > 0 else (now if rec.is_pending else start)
                bar_x0 = _x_at(start)
                bar_x1 = _x_at(end)
                bar_w = max(2, bar_x1 - bar_x0)
                bar_rect = QRect(bar_x0, row_y + 4, bar_w, self.ROW_HEIGHT - 8)
                color = QColor(ENTRY_KIND_COLORS.get(rec.kind, "#888888"))
                if rec.is_pending:
                    color.setAlpha(180)
                painter.setBrush(QBrush(color))
                painter.setPen(Qt.NoPen)
                painter.drawRoundedRect(bar_rect, 4, 4)

                # bar 内文字（仅当宽 > 60 时）
                if bar_w > 60:
                    text_color = QColor("#202020") if self._is_light_color(color) else QColor("#FFFFFF")
                    font = QFont(self.font())
                    font.setPointSizeF(max(8.0, font.pointSizeF() - 1.0))
                    painter.setFont(font)
                    painter.setPen(text_color)
                    label = rec.label
                    if rec.is_pending:
                        label += " · …"
                    fm = QFontMetrics(font)
                    text_w = fm.horizontalAdvance(label)
                    if text_w <= bar_w - 12:
                        painter.drawText(
                            bar_rect.adjusted(8, 0, -4, 0),
                            Qt.AlignVCenter | Qt.AlignLeft,
                            fm.elidedText(label, Qt.ElideRight, bar_w - 16),
                        )

                row_y += self.ROW_HEIGHT
        finally:
            painter.end()

    def _paint_summary(self, painter: QPainter) -> None:
        rect = QRect(0, 0, self.width(), self.SUMMARY_H)
        painter.fillRect(rect, QColor(self._colors.get("bar_bg", "#1E1E1E")))

        # 底分隔线
        pen = QPen(QColor(self._colors["border"]))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawLine(0, self.SUMMARY_H - 1, self.width(), self.SUMMARY_H - 1)

        font = QFont(self.font())
        font.setBold(True)
        font.setPointSizeF(max(10.0, font.pointSizeF() - 0.5))
        painter.setFont(font)
        painter.setPen(QColor(self._colors["text_secondary"]))

        if not self._records:
            painter.drawText(rect.adjusted(self.LEFT_GUTTER, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, "尚无轨迹数据")
            return

        # 总 Duration / Turns / Calls
        anchor_ts = min((r.start_ts for r in self._records if r.start_ts > 0), default=time.time())
        global_end = max(
            ((r.end_ts if r.end_ts > 0 else r.start_ts) for r in self._records),
            default=time.time(),
        )
        total_ms = max(0, int((global_end - anchor_ts) * 1000))
        turn_count = len(self._turns)
        call_count = sum(1 for r in self._records if r.kind == EntryKind.TOOL)
        items = [
            ("Duration", format_duration(total_ms)),
            ("Turns", str(turn_count)),
            ("Calls", str(call_count)),
        ]
        x = self.LEFT_GUTTER
        for label, value in items:
            text_rect = QRect(x, 0, 220, self.SUMMARY_H)
            # 浅色 label
            painter.setPen(QColor(self._colors["text_secondary"]))
            painter.drawText(text_rect.adjusted(0, 6, 0, 0), Qt.AlignTop | Qt.AlignLeft, label)
            painter.setPen(QColor(self._colors["summary_value"]))
            painter.drawText(text_rect.adjusted(80, 6, 0, 0), Qt.AlignTop | Qt.AlignLeft, value)
            x += 220

    def _paint_empty(self, painter: QPainter) -> None:
        font = QFont(self.font())
        font.setPointSizeF(max(9.0, font.pointSizeF()))
        painter.setFont(font)
        painter.setPen(QColor(self._colors["text_secondary"]))
        rect = QRect(0, self.SUMMARY_H, self.width(), self.height() - self.SUMMARY_H)
        painter.drawText(rect, Qt.AlignCenter, "等待会话开始…")

    def _paint_row_label(self, painter: QPainter, rec: TraceRecord, row_rect: QRect) -> None:
        # 左侧 4px 颜色块
        color = QColor(ENTRY_KIND_COLORS.get(rec.kind, "#888888"))
        painter.fillRect(QRect(0, row_rect.y() + 4, 3, row_rect.height() - 8), color)

        font = QFont(self.font())
        font.setPointSizeF(max(8.5, font.pointSizeF() - 1.5))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(self._colors["text"]))
        # type tag
        type_rect = QRect(8, row_rect.y(), 64, row_rect.height())
        painter.drawText(type_rect, Qt.AlignVCenter | Qt.AlignLeft, rec.kind.label)

        # label
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(self._colors["text_secondary"]))
        label_rect = QRect(64, row_rect.y(), self.LEFT_GUTTER - 64, row_rect.height())
        fm = QFontMetrics(font)
        painter.drawText(
            label_rect.adjusted(0, 0, -4, 0),
            Qt.AlignVCenter | Qt.AlignLeft,
            fm.elidedText(rec.label, Qt.ElideRight, label_rect.width() - 4),
        )

    @staticmethod
    def _is_light_color(c: QColor) -> bool:
        # 粗略亮度估算
        r, g, b = c.red(), c.green(), c.blue()
        return (0.299 * r + 0.587 * g + 0.114 * b) > 160

    # ──────────────────── 交互 ────────────────────

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        idx = self._row_index_at(event.y())
        if idx != self._hover_idx:
            self._hover_idx = idx
            self.update()

    def leaveEvent(self, _event) -> None:  # noqa: N802
        self._hover_idx = None
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        idx = self._row_index_at(event.y())
        if idx is not None and 0 <= idx < len(self._records):
            self.recordClicked.emit(idx)

    def _row_index_at(self, y: int) -> Optional[int]:
        if y < self.SUMMARY_H:
            return None
        rel = y - self.SUMMARY_H
        row = rel // self.ROW_HEIGHT
        if row < 0 or row >= len(self._records):
            return None
        return int(row)
