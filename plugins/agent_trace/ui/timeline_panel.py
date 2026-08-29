# -*- coding: utf-8 -*-
"""agent_trace.TimelinePanel — 三泳道甘特图时间线（对齐 DeepSeek Harness）。

布局（固定高度，不再内部滚动 — 泳道恒定 3 行）：

    ┌─────────────────────────────────────────────────────┐
    │ 0s        25%        50%        75%       100%      │ 刻度区 TICK_H
    │ Input  ▓▓▓▓░░░░▓▓▓▓▓▓▓▓░░░░░░░░░░                   │
    │ Model     ░░░▓▓▓▓▓▓▓░░░░▓▓▓▓▓░░                     │ 泳道区 3×LANE_ROW_H
    │ Tools           ░░▓▓░░░▓▓░░░▓▓░                      │
    └─────────────────────────────────────────────────────┘

三种模式（顶栏 Duration / Turns / Calls 切换）：
- duration：真实时间比例（全 session 时间轴）
- turns：每个 turn 等宽一段，段内仍按真实时间比例
- calls：只画 Tools 泳道

交互：hover 显示 tooltip（类型 · 名称 · 时长 · 绝对时间），点击条带选中记录。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import QRect, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from .trace_models import (
    LANE_ORDER,
    EntryKind,
    Lane,
    TraceRecord,
    format_duration_compact,
    kind_color,
    with_alpha,
)

TICK_H = 18
LANE_ROW_H = 30
LANE_LABEL_W = 64
PANEL_H = TICK_H + len(LANE_ORDER) * LANE_ROW_H + 6
PAD_R = 12
BAR_INSET = 6

MODES = ("duration", "turns", "calls")


class TimelinePanel(QWidget):
    """三泳道甘特图。"""

    recordClicked = pyqtSignal(int)  # record index（visible 列表索引）

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._mode: str = "duration"
        self._selected_idx: Optional[int] = None
        self._hover_idx: Optional[int] = None
        self._hit_areas: List[Tuple[QRect, int]] = []
        self._colors: Dict[str, str] = {
            "text": "#C8C8D0",
            "text_dim": "#6E6E78",
            "track": "#000000",
            "grid": "#FFFFFF",
            "selected": "#7AA2F7",
        }
        self._base_px = 12  # 基准字号（像素，由 _apply_font 注入）
        self.setMouseTracking(True)
        self.setFixedHeight(PANEL_H)

    def _font(self, delta_px: int = 0) -> QFont:
        """等宽字体（像素字号，跟随系统设置）。"""
        f = QFont("Cascadia Mono")
        f.setStyleHint(QFont.Monospace)
        f.setPixelSize(max(9, self._base_px + delta_px))
        return f

    # ──────────────────── 公开 API ────────────────────

    def set_records(self, records: List[TraceRecord]) -> None:
        self._records = list(records)
        self.update()

    def set_selected(self, idx: Optional[int]) -> None:
        self._selected_idx = idx
        self.update()

    def set_mode(self, mode: str) -> None:
        if mode in MODES and mode != self._mode:
            self._mode = mode
            self.update()

    @property
    def mode(self) -> str:
        return self._mode

    def set_colors(self, colors: Dict[str, Any]) -> None:
        if not colors:
            return
        is_dark = colors.get("is_dark", True)
        self._colors.update(
            {
                "text": colors.get("text_primary", self._colors["text"]),
                "text_dim": colors.get("text_secondary", self._colors["text_dim"]),
                # 泳道底色：深色主题叠白、浅色主题叠黑（低透明度，用 with_alpha 派生）
                "track": "#FFFFFF" if is_dark else "#000000",
                "grid": "#FFFFFF" if is_dark else "#000000",
                "selected": colors.get("accent") or self._colors["selected"],
            }
        )
        self.update()

    def _apply_font(self, font: QFont) -> None:
        """字体跟随系统设置（pixelSize）。"""
        px = font.pixelSize()
        if px <= 0:
            ptf = font.pointSizeF()
            px = int(round(ptf * 4 / 3)) if ptf > 0 else 12  # pt → px 兜底
        self._base_px = max(10, min(24, px))
        self.update()

    # ──────────────────── 数据切片 ────────────────────

    def _visible_records(self) -> List[TraceRecord]:
        """当前模式下参与绘制的记录（calls 模式只看工具）。"""
        if self._mode == "calls":
            return [r for r in self._records if r.kind == EntryKind.TOOL]
        return self._records

    def _time_bounds(self, recs: List[TraceRecord]) -> Tuple[float, float]:
        """返回 (t0, t1)。无有效时间时返回 (0, 1) 防除零。"""
        starts = [r.start_ts for r in recs if r.start_ts > 0]
        if not starts:
            return 0.0, 1.0
        t0 = min(starts)
        ends = [max(r.end_ts, r.start_ts) for r in recs if r.start_ts > 0]
        t1 = max(ends) if ends else t0
        if t1 <= t0:
            t1 = t0 + 1.0
        return t0, t1

    def _x_ratio(self, rec: TraceRecord, t0: float, t1: float, lane_x0: float, lane_w: float) -> Tuple[float, float]:
        """计算一条记录在泳道内的 (x0, x1) 像素坐标。"""
        duration_mode = self._mode == "duration"
        if duration_mode:
            a, b = self._ratio_global(rec, t0, t1)
            return lane_x0 + a * lane_w, lane_x0 + b * lane_w
        # turns 模式：turn 等分，段内按真实时间线性
        turns = max(1, max((r.turn_no for r in self._records), default=1))
        k = max(0, rec.turn_no - 1)
        seg_x0 = lane_x0 + k / turns * lane_w
        seg_w = lane_w / turns
        turn_recs = [r for r in self._records if r.turn_no == rec.turn_no and r.start_ts > 0]
        if not turn_recs:
            return seg_x0, max(seg_x0 + 3, seg_x0 + seg_w * 0.5)
        tt0, tt1 = self._time_bounds(turn_recs)
        a, b = self._ratio_global(rec, tt0, tt1)
        return seg_x0 + a * seg_w, seg_x0 + b * seg_w

    @staticmethod
    def _ratio_global(rec: TraceRecord, t0: float, t1: float) -> Tuple[float, float]:
        span = max(1e-6, t1 - t0)
        s = rec.start_ts if rec.start_ts > 0 else t0
        e = rec.end_ts if rec.end_ts > s else s
        a = max(0.0, min(1.0, (s - t0) / span))
        b = max(0.0, min(1.0, (e - t0) / span))
        return a, max(b, a + 0.004)  # 最小可见宽度

    # ──────────────────── 绘制 ────────────────────

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        self._hit_areas = []
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)

            recs = self._visible_records()
            if not recs:
                self._paint_empty(painter)
                return

            track_x = LANE_LABEL_W
            track_w = max(40, self.width() - track_x - PAD_R)
            t0, t1 = self._time_bounds(recs)

            lanes = [Lane.TOOLS] if self._mode == "calls" else list(LANE_ORDER)
            h = (self.height() - TICK_H - 4) / len(lanes)

            for lane_i, lane in enumerate(lanes):
                y = TICK_H + lane_i * h
                self._paint_lane(painter, lane, y, h, track_x, track_w, t0, t1, recs)

            # 选中描边最后补画（保证高亮在最上层）
            self._paint_selection(painter)
            if self._mode != "calls":
                self._paint_ticks(painter, track_x, track_w, t0, t1)
        finally:
            painter.end()

    def _paint_lane(
        self,
        painter: QPainter,
        lane: Lane,
        y: float,
        h: float,
        track_x: int,
        track_w: int,
        t0: float,
        t1: float,
        recs: List[TraceRecord],
    ) -> None:
        # 泳道标签
        # 泳道标签
        f = self._font(-1)
        painter.setFont(f)
        painter.setPen(QColor(self._colors["text_dim"]))
        painter.drawText(QRect(0, int(y), LANE_LABEL_W - 8, int(h)), Qt.AlignVCenter | Qt.AlignRight, lane.value)

        # 泳道底轨（极淡空槽，圆角）
        track = QRect(track_x, int(y + 4), track_w, int(h - 8))
        painter.setPen(Qt.NoPen)
        painter.setBrush(with_alpha(QColor(self._colors["track"]), 10))
        painter.drawRoundedRect(track, 4, 4)

        # 条带（收窄居中，留出轨道呼吸感）
        bar_h = min(16, max(10, track.height() - 8))
        bar_y = track.y() + (track.height() - bar_h) // 2
        for idx, rec in enumerate(recs):
            if rec.lane != lane:
                continue
            bx0, bx1 = self._x_ratio(rec, t0, t1, track_x, track_w)
            bar = QRect(int(bx0), bar_y, max(4, int(bx1 - bx0)), bar_h)
            color = kind_color(rec.kind)
            if idx == self._hover_idx:
                painter.setBrush(color)
            else:
                painter.setBrush(with_alpha(color, 150) if rec.is_pending else with_alpha(color, 200))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(bar, 3, 3)
            self._hit_areas.append((QRect(bar), idx))

            if bar.width() > 52:
                txt = QColor("#101010") if _is_light(color) else QColor("#FFFFFF")
                painter.setPen(txt)
                fm = QFontMetrics(f)
                label = f"{rec.label} {format_duration_compact(rec.duration_ms)}"
                if rec.is_pending:
                    label += " …"
                painter.drawText(
                    bar.adjusted(6, 0, -4, 0),
                    Qt.AlignVCenter | Qt.AlignLeft,
                    fm.elidedText(label, Qt.ElideRight, bar.width() - 10),
                )

    def _paint_selection(self, painter: QPainter) -> None:
        if self._selected_idx is None:
            return
        accent = self._colors.get("selected") or self._colors["text"]
        for rect, idx in self._hit_areas:
            if idx == self._selected_idx:
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor(accent), 2))
                painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 4, 4)
                return

    def _paint_ticks(self, painter: QPainter, track_x: int, track_w: int, t0: float, t1: float) -> None:
        """顶部时间刻度（4 等分 + 总时长标注）。"""
        f = self._font(-2)
        painter.setFont(f)
        painter.setPen(QColor(self._colors["text_dim"]))
        total_ms = int((t1 - t0) * 1000)
        painter.drawText(QRect(track_x, 0, track_w, TICK_H - 2), Qt.AlignVCenter | Qt.AlignLeft, "0s")
        painter.drawText(
            QRect(track_x, 0, track_w, TICK_H - 2), Qt.AlignVCenter | Qt.AlignRight, format_duration_compact(total_ms)
        )
        pen = QPen(with_alpha(QColor(self._colors["grid"]), 28), 1, Qt.DotLine)
        for k in range(1, 4):
            gx = track_x + int(track_w * k / 4)
            painter.setPen(pen)
            painter.drawLine(gx, TICK_H, gx, self.height() - 2)

    def _paint_empty(self, painter: QPainter) -> None:
        painter.setFont(self._font())
        painter.setPen(QColor(self._colors["text_dim"]))
        painter.drawText(self.rect(), Qt.AlignCenter, "暂无轨迹 — 发送一条消息开始记录")

    # ──────────────────── 交互 ────────────────────

    def mousePressEvent(self, event) -> None:  # noqa: N802
        for rect, idx in self._hit_areas:
            if rect.contains(event.pos()):
                self.recordClicked.emit(idx)
                return

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        from PyQt5.QtWidgets import QToolTip

        hit = None
        for rect, idx in self._hit_areas:
            if rect.contains(event.pos()):
                hit = idx
                break
        if hit != self._hover_idx:
            self._hover_idx = hit
            self.update()
        if hit is not None and 0 <= hit < len(self._records):
            rec = self._records[hit]
            QToolTip.showText(
                event.globalPos(),
                f"{rec.kind.label} · {rec.label}\n{rec.absolute_time} · {format_duration_compact(rec.duration_ms)}",
                self,
            )

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if self._hover_idx is not None:
            self._hover_idx = None
            self.update()


def _is_light(c: QColor) -> bool:
    return (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) > 160
