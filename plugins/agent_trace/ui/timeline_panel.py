# -*- coding: utf-8 -*-
"""agent_trace.TimelinePanel — 顶部时间线条（全宽独立一行）。

对齐 Chrome DevTools Network 顶部的 **Overview 瀑布图**：整条会话按真实时间
比例铺开，三泳道分别代表 Input / Model / Tools（DeepSeek Harness 语义）。

    ┌──────────────────────────────────────────────────────────────┐
    │ 0s        2.5s        5.0s        7.5s        10.2s          │ 刻度 + 网格
    │ Input  ▓▓▓▓░░░░▓▓▓▓▓▓▓▓░░░░░░░░░░                            │
    │ Model     ░░░▓▓▓▓▓▓▓░░░░▓▓▓▓▓░░                              │
    │ Tools           ░░▓▓░░░▓▓░░░▓▓░                               │
    └──────────────────────────────────────────────────────────────┘

三种模式（顶栏 Duration / Turns / Calls 切换）：
- duration：真实时间比例（全 session 时间轴）
- turns：每个 turn 等宽一段，段内仍按真实时间比例
- calls：只画 Tools 泳道

交互：hover 高亮 + tooltip（类型 · 名称 · 时长 · 绝对时间），点击条带选中记录。

⚠️ 全部颜色走 :class:`ThemePalette`（QColor 已解析 rgba）——历史 bug：
直接用 ``QColor(colors["text_secondary"])`` 解析 rgba 字符串失败返回黑色，
深色主题下 "Input/Model/Tools" 与刻度是**黑字**。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import QRect, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import QWidget

from .trace_models import (
    LANE_ORDER,
    EntryKind,
    Lane,
    ThemePalette,
    TraceRecord,
    format_duration_compact,
    kind_color,
    time_bounds,
    with_alpha,
)

TICK_H = 20
LANE_ROW_H = 26
LANE_LABEL_W = 58
PANEL_H = TICK_H + len(LANE_ORDER) * LANE_ROW_H + 8
PAD_R = 12
MODES = ("duration", "turns", "calls")


class TimelinePanel(QWidget):
    """三泳道甘特图（顶部全宽条）。"""

    recordClicked = pyqtSignal(int)  # record index（visible 列表索引）

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._mode: str = "duration"
        self._selected_idx: Optional[int] = None
        self._hover_idx: Optional[int] = None
        self._hit_areas: List[Tuple[QRect, int]] = []
        self._pal = ThemePalette()
        self._base_px = 13
        self.setMouseTracking(True)
        self.setFixedHeight(PANEL_H)
        self.setCursor(Qt.ArrowCursor)

    def _font(self, delta_px: int = 0, bold: bool = False) -> QFont:
        """泳道标签 / 条带文字 / 空态 → **系统 UI 字体**。"""
        f = QFont(self._pal.font_family)
        f.setPixelSize(max(9, self._base_px + delta_px))
        f.setBold(bold)
        return f

    def _num_font(self, delta_px: int = 0) -> QFont:
        """刻度数字用等宽（位数变化时不错位）。"""
        f = QFont(self._pal.mono_family)
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

    def set_palette(self, pal: ThemePalette) -> None:
        self._pal = pal
        self.update()

    def set_colors(self, colors: Dict[str, Any], is_dark: bool = True) -> None:
        if not colors:
            return
        self._pal = ThemePalette.from_theme(colors, is_dark, mono_family=self._pal.mono_family)
        self.update()

    def _apply_font(self, font: QFont) -> None:
        px = font.pixelSize()
        if px <= 0:
            ptf = font.pointSizeF()
            px = int(round(ptf * 4 / 3)) if ptf > 0 else 13
        self._base_px = max(10, min(24, px))
        fam = font.family()
        if fam:
            self._pal.font_family = fam
        self.update()

    # ──────────────────── 数据切片 ────────────────────

    def _visible_records(self) -> List[TraceRecord]:
        if self._mode == "calls":
            return [r for r in self._records if r.kind == EntryKind.TOOL]
        return self._records

    @property
    def bounds(self) -> Tuple[float, float]:
        """当前时间边界（列表的瀑布列复用，保证两处比例一致）。"""
        return time_bounds(self._visible_records())

    def _x_ratio(
        self, rec: TraceRecord, t0: float, t1: float, lane_x0: float, lane_w: float
    ) -> Tuple[float, float]:
        """计算一条记录在泳道内的 (x0, x1) 像素坐标。"""
        if self._mode == "duration":
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
        tt0, tt1 = time_bounds(turn_recs)
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
            t0, t1 = time_bounds(recs)

            lanes = [Lane.TOOLS] if self._mode == "calls" else list(LANE_ORDER)
            h = (self.height() - TICK_H - 6) / len(lanes)

            self._paint_grid(painter, track_x, track_w, t0, t1)
            for lane_i, lane in enumerate(lanes):
                y = TICK_H + lane_i * h
                self._paint_lane(painter, lane, y, h, track_x, track_w, t0, t1, recs)
            self._paint_ticks(painter, track_x, track_w, t0, t1)
            self._paint_selection(painter)
        finally:
            painter.end()

    def _paint_grid(self, painter: QPainter, track_x: int, track_w: int, t0: float, t1: float) -> None:
        """纵向网格线（4 等分）+ 轨道外框。"""
        pen = QPen(self._pal.line_at(22), 1, Qt.DotLine)
        painter.setPen(pen)
        for k in range(1, 4):
            gx = track_x + int(track_w * k / 4)
            painter.drawLine(gx, TICK_H - 4, gx, self.height() - 4)

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
        # 泳道标签（右对齐，主题次级色 —— 曾经的黑字 bug 就在这里）
        f = self._font(-2)
        painter.setFont(f)
        painter.setPen(QColor(self._pal.text_muted))
        painter.drawText(QRect(0, int(y), LANE_LABEL_W - 8, int(h)), Qt.AlignVCenter | Qt.AlignRight, lane.value)

        # 泳道底轨
        track = QRect(track_x, int(y + 3), track_w, int(h - 6))
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._pal.track)
        painter.drawRoundedRect(QRectF(track), 4, 4)

        bar_h = min(16, max(10, track.height() - 8))
        bar_y = track.y() + (track.height() - bar_h) // 2
        for idx, rec in enumerate(recs):
            if rec.lane != lane:
                continue
            bx0, bx1 = self._x_ratio(rec, t0, t1, track_x, track_w)
            bar = QRect(int(bx0), bar_y, max(4, int(bx1 - bx0)), bar_h)
            color = self._pal.danger if rec.is_error else kind_color(rec.kind)
            if idx == self._hover_idx:
                painter.setBrush(color)
            elif rec.is_pending:
                painter.setBrush(with_alpha(color, 110))
            else:
                painter.setBrush(with_alpha(color, 205))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(QRectF(bar), 3, 3)
            self._hit_areas.append((QRect(bar), idx))

            # in-flight：右端加一个走动的省略号点
            if rec.is_pending:
                painter.setBrush(self._pal.warning)
                d = min(4, bar.height() // 3)
                painter.drawEllipse(bar.right() - d - 2, bar.center().y() - d // 2, d, d)

            if bar.width() > 54:
                txt = QColor("#101010") if _is_light(color) else QColor("#FFFFFF")
                painter.setPen(txt)
                fm = QFontMetrics(f)
                label = f"{rec.label} {format_duration_compact(rec.duration_ms)}"
                painter.drawText(
                    bar.adjusted(6, 0, -4, 0),
                    Qt.AlignVCenter | Qt.AlignLeft,
                    fm.elidedText(label, Qt.ElideRight, bar.width() - 10),
                )

    def _paint_selection(self, painter: QPainter) -> None:
        if self._selected_idx is None:
            return
        for rect, idx in self._hit_areas:
            if idx == self._selected_idx:
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(self._pal.accent, 2))
                painter.drawRoundedRect(QRectF(rect.adjusted(-1, -1, 0, 0)), 4, 4)
                return

    def _paint_ticks(self, painter: QPainter, track_x: int, track_w: int, t0: float, t1: float) -> None:
        """顶部时间刻度：0 / 25% / 50% / 75% / 总时长。"""
        f = self._num_font(-3)
        painter.setFont(f)
        total_ms = int((t1 - t0) * 1000)
        for k in range(5):
            if k in (0, 4):
                continue
            gx = track_x + int(track_w * k / 4)
            label = format_duration_compact(int(total_ms * k / 4))
            painter.setPen(QColor(self._pal.text_muted))
            painter.drawText(
                QRect(gx - 40, 0, 80, TICK_H - 2),
                Qt.AlignVCenter | Qt.AlignHCenter,
                label,
            )
        painter.setPen(QColor(self._pal.text_secondary))
        painter.drawText(QRect(track_x, 0, 60, TICK_H - 2), Qt.AlignVCenter | Qt.AlignLeft, "0s")
        painter.drawText(
            QRect(track_x + track_w - 80, 0, 80, TICK_H - 2),
            Qt.AlignVCenter | Qt.AlignRight,
            format_duration_compact(total_ms),
        )

    def _paint_empty(self, painter: QPainter) -> None:
        painter.setFont(self._font())
        painter.setPen(QColor(self._pal.text_muted))
        painter.drawText(self.rect(), Qt.AlignCenter, "暂无轨迹 — 发送一条消息后这里会出现时间线")

    # ──────────────────── 交互 ────────────────────

    def _hit_test(self, pos) -> Optional[int]:
        for rect, idx in self._hit_areas:
            if rect.contains(pos):
                return idx
        return None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        hit = self._hit_test(event.pos())
        if hit is not None:
            self.recordClicked.emit(hit)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        from PyQt5.QtWidgets import QToolTip

        hit = self._hit_test(event.pos())
        if hit != self._hover_idx:
            self._hover_idx = hit
            self.setCursor(Qt.PointingHandCursor if hit is not None else Qt.ArrowCursor)
            self.update()
        if hit is not None and 0 <= hit < len(self._records):
            rec = self._records[hit]
            QToolTip.showText(
                event.globalPos(),
                f"{rec.kind.label} · {rec.label}\n"
                f"{rec.absolute_time} · {format_duration_compact(rec.duration_ms)} · {rec.status}",
                self,
            )

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if self._hover_idx is not None:
            self._hover_idx = None
            self.setCursor(Qt.ArrowCursor)
            self.update()


def _is_light(c: QColor) -> bool:
    return (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) > 160
