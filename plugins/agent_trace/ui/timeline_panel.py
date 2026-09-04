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
# duration 模式下条带的最小可见宽度（像素）。真实耗时可能只占总轴的十万分之
# 几（快工具 13ms / 会话 3min），纯比例下限 0.004 只有 ≈3px，看起来一排
# 刻度线（「断断续续」）。改成像素级下限，保证每个条带至少肉眼可点中。
_MIN_BAR_PX = 5.0
# 顶栏三个开关（非互斥）：比例 / 按轮分段 / 只看工具
FLAGS = ("duration", "turns", "calls")


class TimelinePanel(QWidget):
    """三泳道甘特图（顶部全宽条）。"""

    recordClicked = pyqtSignal(int)  # record index（visible 列表索引）
    # 拖拽选区 → (起始时间戳, 结束时间戳)；对齐 DevTools Network 的
    # Overview 拖选：只显示该时间区间内的条目
    rangeSelected = pyqtSignal(float, float)
    rangeCleared = pyqtSignal()

    # 拖拽位移小于该值视为「点击」而非「拖选」
    _DRAG_SLOP = 5

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        # 三个**独立开关**（不是互斥模式）：
        #   duration — 开：条带宽度按真实时间比例；关：每条等宽（固定长度）
        #   turns    — 开：先按 turn 等分整条轴，段内再按 duration 规则排
        #   calls    — 开：只画 Tools 泳道
        # 默认全关（用户指定）：等宽块视图，想看真实耗时再开 Duration。
        self._flag_duration = False
        self._flag_turns = False
        self._flag_calls = False
        self._selected_idx: Optional[int] = None
        self._hover_idx: Optional[int] = None
        self._hit_areas: List[Tuple[QRect, int]] = []
        # 条带像素几何（idx → (bx0, bx1)）。让 _x_to_time / _time_to_x 与
        # _x_ratio 走同一套几何，避免等宽模式下拖选算出来的时间戳跟视觉位置
        # 对不上（根因 1）。
        self._bar_x: Dict[int, Tuple[int, int]] = {}
        self._pal = ThemePalette()
        self._base_px = 13
        # ── 选区状态 ──
        self._track_x = LANE_LABEL_W
        self._track_w = 100
        self._t0, self._t1 = 0.0, 1.0
        self._range: Optional[Tuple[float, float]] = None  # 已确认选区（时间戳）
        self._drag_from: Optional[int] = None  # 拖拽起点 x
        self._drag_to: Optional[int] = None  # 当前 x
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

    def set_flags(self, duration: bool, turns: bool, calls: bool) -> None:
        """三个显示开关（顶栏 toggle 按钮驱动），可任意组合。"""
        if (
            duration == self._flag_duration
            and turns == self._flag_turns
            and calls == self._flag_calls
        ):
            return
        self._flag_duration = bool(duration)
        self._flag_turns = bool(turns)
        self._flag_calls = bool(calls)
        self.update()

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
        if self._flag_calls:
            return [r for r in self._records if r.kind == EntryKind.TOOL]
        return self._records

    @property
    def bounds(self) -> Tuple[float, float]:
        """当前时间边界（列表的瀑布列复用，保证两处比例一致）。"""
        return time_bounds(self._visible_records())

    def _x_ratio(
        self, rec: TraceRecord, idx: int, total: int, t0: float, t1: float, lane_x0: float, lane_w: float
    ) -> Tuple[float, float]:
        """计算一条记录在泳道内的 (x0, x1) 像素坐标。

        两种宽度语义由 ``duration`` 开关决定：
        - 开：按真实时间比例（span 区间）
        - 关：等宽块（第 idx 条占第 idx 格，块间留 8% 缝隙）
        """
        if self._flag_turns:
            turns = max(1, max((r.turn_no for r in self._records), default=1))
            k = max(0, rec.turn_no - 1)
            seg_x0 = lane_x0 + k / turns * lane_w
            seg_w = lane_w / turns
            if self._flag_duration:
                turn_recs = [r for r in self._records if r.turn_no == rec.turn_no and r.start_ts > 0]
                if not turn_recs:
                    return seg_x0, max(seg_x0 + 3, seg_x0 + seg_w * 0.5)
                tt0, tt1 = time_bounds(turn_recs)
                a, b = self._ratio_global(rec, tt0, tt1)
            else:
                same = [i for i, r in enumerate(self._records) if r.turn_no == rec.turn_no]
                n = max(1, len(same))
                pos = same.index(idx) if idx in same else 0
                a, b = pos / n, (pos + 0.92) / n
            x0, x1 = seg_x0 + a * seg_w, seg_x0 + b * seg_w
            if self._flag_duration:
                x1 = max(x1, x0 + _MIN_BAR_PX)
            return x0, x1

        if self._flag_duration:
            a, b = self._ratio_global(rec, t0, t1)
            x0 = lane_x0 + a * lane_w
            # 像素级最小宽度：真实耗时极短的条带也保持可见/可点
            return x0, max(lane_x0 + b * lane_w, x0 + _MIN_BAR_PX)
        n = max(1, total)
        a, b = idx / n, (idx + 0.92) / n
        return lane_x0 + a * lane_w, lane_x0 + b * lane_w

    @staticmethod
    def _ratio_global(rec: TraceRecord, t0: float, t1: float) -> Tuple[float, float]:
        span = max(1e-6, t1 - t0)
        s = rec.start_ts if rec.start_ts > 0 else t0
        # ⚠️ 用 span_end_ts（占用终点）而不是 end_ts：瞬时消息的 end=0，
        # 直接用 end 会让所有条带塌成最小宽度的碎点（「全是 0ms、不连贯」）。
        e = max(rec.span_end_ts, s)
        a = max(0.0, min(1.0, (s - t0) / span))
        b = max(0.0, min(1.0, (e - t0) / span))
        return a, max(b, a + 0.004)  # 最小可见宽度

    # ──────────────────── 绘制 ────────────────────

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        self._hit_areas = []
        self._bar_x = {}
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
            # 记录几何/时间映射，供拖选换算（x ↔ 时间戳）
            self._track_x, self._track_w, self._t0, self._t1 = track_x, track_w, t0, t1

            lanes = [Lane.TOOLS] if self._flag_calls else list(LANE_ORDER)
            h = (self.height() - TICK_H - 6) / len(lanes)

            self._paint_grid(painter, track_x, track_w, t0, t1)
            for lane_i, lane in enumerate(lanes):
                y = TICK_H + lane_i * h
                self._paint_lane(painter, lane, y, h, track_x, track_w, t0, t1, recs)
            self._paint_ticks(painter, track_x, track_w, t0, t1)
            self._paint_selection(painter)
            self._paint_range(painter)
        finally:
            painter.end()

    # ──────────────────── 时间选区（DevTools Overview 拖选）────────────────────

    def _x_to_time(self, x: int) -> float:
        """x → 时间戳。与 ``_x_ratio`` 走同一套几何（条带宽度语义由 flag 决定）。

        - 落在某条条带 [bx0, bx1] 内 → 在该记录的 (start_ts, span_end_ts) 上插值
        - 落在空白处（段间隙 / 最左/最右外） → 边界外推 + 兜底到 _t0/_t1
        """
        if not self._bar_x:
            return self._t0
        idx, bx0, bx1 = self._bar_x_lookup(x)
        rec = self._records[idx] if 0 <= idx < len(self._records) else None
        if rec is None:
            return self._t0
        s = rec.start_ts if rec.start_ts > 0 else self._t0
        e = max(rec.span_end_ts, s)
        if e <= s:
            return s
        ratio = max(0.0, min(1.0, (x - bx0) / max(1, bx1 - bx0)))
        return s + ratio * (e - s)

    def _time_to_x(self, t: float) -> int:
        """时间戳 → x。与 ``_x_to_time`` 对称，保证选区高亮画到正确位置。"""
        if not self._bar_x:
            return self._track_x
        for idx, (bx0, bx1) in self._bar_x.items():
            rec = self._records[idx] if 0 <= idx < len(self._records) else None
            if rec is None:
                continue
            s = rec.start_ts if rec.start_ts > 0 else self._t0
            e = max(rec.span_end_ts, s)
            if s <= t <= e:
                span = max(1e-6, e - s)
                ratio = (t - s) / span
                return int(bx0 + ratio * (bx1 - bx0))
        # t 在所有条带外：取首/尾端点
        keys = sorted(self._bar_x.keys(), key=lambda i: self._bar_x[i][0])
        if not keys:
            return self._track_x
        if t < self._t0:
            return int(self._bar_x[keys[0]][0])
        return int(self._bar_x[keys[-1]][1])

    def _bar_x_lookup(self, x: int) -> Tuple[int, int, int]:
        """在 ``_bar_x`` 里找 x 落点 → (idx, bx0, bx1)。找不到时取最近端点。"""
        # 落在哪个条带内（条带可能重叠：同一 idx 在多泳道被画，但 _bar_x 用 idx
        # 去重，最后一个写入为准——按当前实现 _paint_lane 是按泳道循环，同一
        # 条目只会出现在自己所属泳道，重叠只在 Calls 关时极少发生）。
        for idx, (bx0, bx1) in self._bar_x.items():
            if bx0 <= x <= bx1:
                return idx, bx0, bx1
        # 空白处：取最左/最右端点
        keys = sorted(self._bar_x.keys(), key=lambda i: self._bar_x[i][0])
        if x < self._bar_x[keys[0]][0]:
            first = keys[0]
            return first, self._bar_x[first][0], self._bar_x[first][0]
        last = keys[-1]
        return last, self._bar_x[last][1], self._bar_x[last][1]

    def _paint_range(self, painter: QPainter) -> None:
        """已确认选区（半透明高亮 + 两侧边界线）与拖拽中的橡皮筋。"""
        rects: List[Tuple[int, int]] = []
        if self._range is not None:
            rects.append((self._time_to_x(self._range[0]), self._time_to_x(self._range[1])))
        if self._drag_from is not None and self._drag_to is not None:
            if abs(self._drag_to - self._drag_from) >= self._DRAG_SLOP:
                rects.append((self._drag_from, self._drag_to))
        for x0, x1 in rects:
            left, right = min(x0, x1), max(x0, x1)
            box = QRect(left, TICK_H - 6, max(2, right - left), self.height() - TICK_H + 2)
            painter.setPen(Qt.NoPen)
            painter.setBrush(with_alpha(QColor(self._pal.accent), 34))
            painter.drawRect(box)
            painter.setPen(QPen(with_alpha(QColor(self._pal.accent), 170), 1))
            painter.drawLine(left, box.y(), left, box.bottom())
            painter.drawLine(right, box.y(), right, box.bottom())

    def set_range(self, t0: Optional[float], t1: Optional[float]) -> None:
        """外部设置选区（切会话等情况由卡片调用；None = 清除）。"""
        if t0 is None or t1 is None:
            self._range = None
        else:
            self._range = (min(t0, t1), max(t0, t1))
        self.update()

    def clear_range(self) -> None:
        self._range = None
        self._drag_from = self._drag_to = None
        self.update()

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
            bx0, bx1 = self._x_ratio(rec, idx, len(recs), t0, t1, track_x, track_w)
            self._bar_x[idx] = (int(bx0), int(bx1))
            raw_w = int(bx1 - bx0)
            # 瞬时事件（span=0，同秒注入）画成 3px 竖线标记，圆角也收敛
            instant = raw_w <= 4
            bar = QRect(int(bx0), bar_y, max(3, raw_w), bar_h)
            color = self._pal.danger if rec.is_error else kind_color(rec.kind)
            if idx == self._hover_idx:
                painter.setBrush(color)
            elif rec.is_pending:
                painter.setBrush(with_alpha(color, 110))
            else:
                painter.setBrush(with_alpha(color, 205))
            painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(QRectF(bar), 1 if instant else 3, 1 if instant else 3)
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
                # 条内数字用占用时长（与条带宽度一致）；封顶时带 ≥ 前缀
                span_txt = f"≥{format_duration_compact(rec.span_ms)}" if rec.meta.get(
                    "span_capped"
                ) else format_duration_compact(rec.span_ms)
                label = f"{rec.label} {span_txt}" if rec.span_ms > 0 else rec.label
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
        if event.button() != Qt.LeftButton:
            return
        self._drag_from = event.pos().x()
        self._drag_to = self._drag_from

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """抬起：位移够大 = 拖选时间区间；否则按「点击」处理。"""
        if self._drag_from is None:
            return
        start_x = self._drag_from
        end_x = event.pos().x()
        self._drag_from = self._drag_to = None
        if abs(end_x - start_x) >= self._DRAG_SLOP:
            ta, tb = self._x_to_time(start_x), self._x_to_time(end_x)
            self._range = (min(ta, tb), max(ta, tb))
            self.update()
            self.rangeSelected.emit(self._range[0], self._range[1])
            return
        hit = self._hit_test(event.pos())
        if hit is not None:
            self.recordClicked.emit(hit)
        elif self._range is not None:
            # 空白处单击 = 清除时间过滤
            self._range = None
            self.update()
            self.rangeCleared.emit()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        from PyQt5.QtWidgets import QToolTip

        if self._drag_from is not None:
            self._drag_to = event.pos().x()
            if abs(self._drag_to - self._drag_from) >= self._DRAG_SLOP:
                self.setCursor(Qt.SplitHCursor)
                self.update()
                return
        hit = self._hit_test(event.pos())
        if hit != self._hover_idx:
            self._hover_idx = hit
            if self._drag_from is None:
                self.setCursor(Qt.PointingHandCursor if hit is not None else Qt.ArrowCursor)
            self.update()
        if hit is not None and 0 <= hit < len(self._records):
                rec = self._records[hit]
                QToolTip.showText(
                    event.globalPos(),
                    f"{rec.kind.label} · {rec.label}\n"
                    f"{rec.absolute_time} · 占用 {rec.span_label}"
                    f" · 耗时 {format_duration_compact(rec.duration_ms)}\n{rec.status}",
                    self,
                )

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if self._hover_idx is not None:
            self._hover_idx = None
            self.setCursor(Qt.ArrowCursor)
            self.update()


def _is_light(c: QColor) -> bool:
    return (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) > 160
