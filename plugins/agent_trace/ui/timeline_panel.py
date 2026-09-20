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

顶栏三态开关（互斥，由 :class:`TraceCardWidget` 保证）：

- **等宽**（默认）：每条等宽铺满整轴，看的是「有哪些条目」；
- **Duration**：条带宽度按真实耗时比例；
- **Token**：条带宽度按 token 占比（排序轴仍是时间序，只是槽宽按 token 分配）。

三者共用同一条时间视口：滚轮以鼠标所在时刻为锚点缩放，等宽 / Token 下
只铺视口内的记录且宽度在窗内**重新归一化**，Duration 下窗外条带被剪裁。
一路缩小到覆盖全量时自动复位；放大后底部出现平移滚动条。

⚠️ Token 模式首帧要遍历全部记录取 ``TraceRecord.tokens``（无预填的会走
tiktoken 估算），之后命中 ``meta["tokens"]`` 缓存，不再重算。

交互：hover 高亮 + tooltip（类型 · 名称 · 时长 · 绝对时间），点击条带选中记录。

⚠️ 全部颜色走 :class:`ThemePalette`（QColor 已解析 rgba）——历史 bug：
直接用 ``QColor(colors["text_secondary"])`` 解析 rgba 字符串失败返回黑色，
深色主题下 "Input/Model/Tools" 与刻度是**黑字**。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from PyQt5.QtCore import QRect, QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import QScrollBar, QWidget

from .trace_models import (
    LANE_ORDER,
    Lane,
    ThemePalette,
    TraceRecord,
    format_duration_compact,
    format_tokens,
    kind_color,
    time_bounds,
    with_alpha,
)

TICK_H = 20
LANE_ROW_H = 26
LANE_LABEL_W = 58
PANEL_H = TICK_H + len(LANE_ORDER) * LANE_ROW_H + 8
PAD_R = 12
# 底部视口平移滚动条高度（仅 Duration 开且已放大时出现）
SCROLL_H = 12
# duration 模式下条带的最小可见宽度（像素）。真实耗时可能只占总轴的十万分之
# 几（快工具 13ms / 会话 3min），纯比例下限 0.004 只有 ≈3px，看起来一排
# 刻度线（「断断续续」）。改成像素级下限，保证每个条带至少肉眼可点中。
# Token 模式同样适用（一条 30 tok 的 hook 对一条 8k tok 的回复，纯比例只有
# 3px），见 :func:`_token_slot_spans`。
_MIN_BAR_PX = 5.0

# 槽位模式：条带按「时间序」依次占槽，宽度语义由模式决定
MODE_EQUAL = "equal"  # 每条等宽（默认）
MODE_DURATION = "duration"  # 宽度 ∝ 真实耗时
MODE_TOKEN = "token"  # 宽度 ∝ token 占比


def _token_slot_spans(
    order: List[int],
    vals: List[int],
    lane_w: float,
    min_px: float = _MIN_BAR_PX,
    gap_px: float = 1.0,
) -> Dict[int, Tuple[float, float]]:
    """按 token 占比分配槽位，返回 ``idx → (a, b)`` 归一化区间。

    纯比例会让小条目塌成看不见的碎点（30 tok vs 8k tok ⇒ 3px），所以带像素
    级下限，用 **water-filling** 分配：先按占比分，宽度不足 ``min_px`` 的条目
    上调到下限，剩余宽度再按占比分给其余条目，迭代到没有新条目触底。

    条目多到 ``n * min_px >= 可用宽``（几千条）时下限无解，退化为等分铺满，
    与等宽模式同构 —— 此时保证「每条都可见」优先于「宽度反映占比」。
    """
    n = len(order)
    if n <= 0 or lane_w <= 1.0:
        return {}
    # 条间缝隙：只有「每条都放得下 min_px + 缝」时才留缝。条目极多时（500 条
    # × 1px 已超过轨道宽）缝隙会把 avail 挤成 0 → 所有条带塌成零点几像素，
    # 整条轴看起来是空的。此时退化为无缝铺满（保「都可见」优先于「有缝隙」）。
    if n * (min_px + gap_px) > lane_w:
        gap_px = 0.0
    avail = max(1.0, lane_w - gap_px * n)  # 扣掉条间缝隙后可分配的总宽
    floor_px = min(min_px, avail / n)
    total = float(sum(max(0, v) for v in vals))
    weights = [float(max(0, v)) for v in vals] if total > 0 else [1.0] * n
    if total <= 0:
        total = float(n)

    widths = [0.0] * n
    fixed: set = set()

    def _free_width(idx: int, rem_px: float, free_idx: List[int], free_total: float) -> float:
        """剩余宽度按权重分给 ``idx``（全零权重时均分）。

        ⚠️ ``free_total`` 必须由调用方**每轮算一次**传进来，不能在函数里现算：
        它与 ``idx`` 无关（整轮所有条目共用同一分母），而旧实现在此对每个 i
        重新 ``sum`` 一遍 → 每轮 O(n²)。实测 Token 模式 ``paintEvent``：
        n=1000 时 43ms、n=4000 时 652ms（`_hover_idx` 一变就重绘，鼠标划过
        整个条带都在掉帧）。
        """
        if free_total > 0:
            return rem_px * weights[idx] / free_total
        return rem_px / len(free_idx) if free_idx else 0.0

    for _ in range(24):
        free = [i for i in range(n) if i not in fixed]
        rem_px = avail - floor_px * len(fixed)
        free_total = sum(weights[i] for i in free)
        newly = [i for i in free if _free_width(i, rem_px, free, free_total) < floor_px]
        if not newly:
            for i in free:
                widths[i] = _free_width(i, rem_px, free, free_total)
            break
        for i in newly:
            fixed.add(i)
            widths[i] = floor_px
    else:
        # 极端分布下迭代未收敛 → 剩余宽度均分（保证总长仍铺满）
        free = [i for i in range(n) if i not in fixed]
        rem_px = max(0.0, avail - floor_px * len(fixed))
        for i in free:
            widths[i] = rem_px / len(free) if free else 0.0

    out: Dict[int, Tuple[float, float]] = {}
    cum = 0.0
    for k, idx in enumerate(order):
        a = cum / lane_w
        cum += widths[k] + gap_px
        b = (cum - gap_px) / lane_w
        out[idx] = (a, max(a + 1e-5, b))
    return out


def _tok_label(value: int) -> str:
    """Token 刻度文案（``format_tokens`` 对 0 返回「—」，刻度起点要显式 0）。"""
    return "0" if value <= 0 else format_tokens(int(value))


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
        # 宽度模式：等宽（默认，看「有哪些条目」）/ Duration（看耗时）/ Token（看占比）
        self._mode = MODE_EQUAL
        self._selected_idx: Optional[int] = None
        self._hover_idx: Optional[int] = None
        self._hit_areas: List[Tuple[QRect, int]] = []
        # 条带像素几何（idx → (bx0, bx1)）仅命中测试用（_hit_areas）；
        # 时间换算走 _x_to_time（**按模式分派**：Duration 线性、槽位域反查）
        self._pal = ThemePalette()
        self._base_px = 13
        # ── 选区状态 ──
        self._track_x = LANE_LABEL_W
        self._track_w = 100
        self._t0, self._t1 = 0.0, 1.0  # 当前视口（paintEvent 每帧刷新）
        self._full_t0, self._full_t1 = 0.0, 1.0  # session 全量边界（刻度偏移基准）
        self._range: Optional[Tuple[float, float]] = None  # 已确认选区（时间戳）
        self._drag_from: Optional[int] = None  # 拖拽起点 x
        self._drag_to: Optional[int] = None  # 当前 x
        self._press_hit: Optional[int] = None  # 按下时锁定的命中目标（点击容错）
        # ── 缩放窗口：两种语义，按模式选用（互斥）──
        # Duration：``_view`` 时间窗（条带 x 与时间线性同源）
        # 等宽 / Token：``_iwin`` 条目窗 [i0, i1)（条带 x 是槽位域：窗内
        #   条目按序号铺满，与自身时刻无关）
        #
        # ⚠️ 槽位模式**不能**沿用时间窗：窗一变、窗内条目数就变，同一 idx 的
        # 槽序随之漂移 → 缩放的条带乱跳（实测漂移 255~313px、条目被甩出视野）。
        # 条目窗直接固定「显示哪几条」，槽序才稳定，锚定才有可能。
        self._view: Optional[Tuple[float, float]] = None
        self._iwin: Optional[Tuple[int, int]] = None
        # 铺满布局（等宽 / Token 共用）：视口内记录按序分槽，paintEvent 每帧重建
        self._slot_order: List[int] = []  # 槽序 → idx
        self._slot_span: Dict[int, Tuple[float, float]] = {}  # idx → (a, b) 归一化
        self._token_total: int = 0  # Token 模式：视口内 token 总量（刻度用）
        self.setMouseTracking(True)
        # 泳道绘制区仍是 PANEL_H，底部 SCROLL_H 留给视口平移滚动条
        self.setFixedHeight(PANEL_H + SCROLL_H)
        self.setCursor(Qt.ArrowCursor)
        # ── 视口平移滚动条 ──
        self._scrollbar = QScrollBar(Qt.Horizontal, self)
        self._scrollbar.hide()
        self._scrollbar.setCursor(Qt.PointingHandCursor)
        self._scrollbar.setStyleSheet(
            f"QScrollBar:horizontal{{background:transparent;height:{SCROLL_H}px;margin:0;}}"
            "QScrollBar::handle:horizontal{background:rgba(102,198,255,90);border-radius:3px;min-width:24px;}"
            "QScrollBar::handle:horizontal:hover{background:rgba(102,198,255,140);}"
            "QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0;height:0;}"
            "QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal{background:transparent;}"
        )
        self._scrollbar.valueChanged.connect(self._on_scroll_moved)

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
        self._clamp_windows()
        self._sync_scrollbar()
        self.update()

    def set_selected(self, idx: Optional[int]) -> None:
        self._selected_idx = idx
        self.update()

    def set_mode(self, mode: str) -> None:
        """宽度模式（顶栏三态按钮驱动）：``equal`` / ``duration`` / ``token``。

        时间轴 / 拖选在三态共用；缩放窗口按模式换算，切换后视野保持连续。
        """
        if mode not in (MODE_EQUAL, MODE_DURATION, MODE_TOKEN) or mode == self._mode:
            return
        was_duration = self._mode == MODE_DURATION
        self._mode = mode
        if was_duration and mode != MODE_DURATION:
            self._view_to_iwin()
        elif not was_duration and mode == MODE_DURATION:
            self._iwin_to_view()
        self._sync_scrollbar()
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

    @property
    def bounds(self) -> Tuple[float, float]:
        """当前时间边界（列表的瀑布列复用，保证两处比例一致）。"""
        return time_bounds(self._records)

    def _x_ratio(
        self,
        rec: TraceRecord,
        idx: int,
        total: int,
        t0: float,
        t1: float,
        lane_x0: float,
        lane_w: float,
        slot: Optional[Tuple[float, float]] = None,
    ) -> Tuple[float, float]:
        """计算一条记录在泳道内的 (x0, x1) 像素坐标。

        宽度语义由 :attr:`_mode` 决定：
        - ``duration``：按真实时间比例（span 区间）
        - ``equal`` / ``token``：槽位铺满（槽区间由 paintEvent 预算，归一化 a/b；
          等宽每槽等长，Token 每槽宽 ∝ token 占比）
        """
        if self._mode == MODE_DURATION:
            a, b = self._ratio_global(rec, t0, t1)
            x0 = lane_x0 + a * lane_w
            # 像素级最小宽度：真实耗时极短的条带也保持可见/可点
            return x0, max(lane_x0 + b * lane_w, x0 + _MIN_BAR_PX)
        if slot is None:
            slot = self._slot_span.get(idx)
        if slot is None:
            n = max(1, total)
            slot = (idx / n, (idx + 0.92) / n)
        return lane_x0 + slot[0] * lane_w, lane_x0 + slot[1] * lane_w

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
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)

            recs = self._records
            if not recs:
                self._paint_empty(painter)
                return

            track_x = LANE_LABEL_W
            track_w = max(40, self.width() - track_x - PAD_R)
            # 时间窗：Duration 用 _view（缩放结果），槽位模式仍按 _view 反推
            # 出时间范围供刻度/拖选使用（槽位模式放大时 _view 由 _iwin 同步）。
            t0, t1 = self._view if self._view else time_bounds(recs)
            # 记录几何/时间映射，供拖选/缩放换算（x ↔ 时间戳）
            self._full_t0, self._full_t1 = time_bounds(recs)
            self._track_x, self._track_w, self._t0, self._t1 = track_x, track_w, t0, t1
            # 铺满布局（等宽 / Token）：窗内记录按序分槽
            # ⚠️ 槽位模式的窗口是**条目窗** _iwin（不是时间窗）：时间窗会让
            # 窗内条目数随缩放变化、槽序漂移，条带乱跳（见 __init__ 注释）。
            self._slot_order = []
            self._slot_span = {}
            self._token_total = 0
            if self._mode != MODE_DURATION:
                n_all = len(recs)
                if self._iwin is not None:
                    i0, i1 = self._iwin
                    order = list(range(max(0, i0), min(n_all, i1 + 1)))
                else:
                    order = list(range(n_all))
                self._slot_order = order
                if self._mode == MODE_TOKEN:
                    vals = [max(0, recs[i].tokens) for i in order]
                    self._token_total = int(sum(vals))
                    self._slot_span = _token_slot_spans(order, vals, track_w)
                else:
                    n = len(order)
                    self._slot_span = {i: (k / n, (k + 0.92) / n) for k, i in enumerate(order)} if n else {}

            h = (PANEL_H - TICK_H - 6) / len(LANE_ORDER)

            self._paint_grid(painter, track_x, track_w, t0, t1)
            for lane_i, lane in enumerate(LANE_ORDER):
                y = TICK_H + lane_i * h
                self._paint_lane(painter, lane, y, h, track_x, track_w, t0, t1, recs)
            if self._mode == MODE_TOKEN:
                self._paint_token_ticks(painter, track_x, track_w)
            else:
                self._paint_ticks(painter, track_x, track_w, t0, t1)
            self._paint_selection(painter)
            self._paint_range(painter)
        finally:
            painter.end()

    # ──────────────────── 时间选区（DevTools Overview 拖选）────────────────────

    def _slot_at_frac(self, frac: float) -> Tuple[int, Optional[int], float]:
        """轴上的归一化位置 → ``(槽序 k, record idx, 槽内占比)``。

        等宽槽长相同可直接整除，Token 模式槽宽不等（∝ token 占比）→ 二分查找。
        空时返回 ``(0, None, 0.0)``。
        """
        order = self._slot_order
        if not order:
            return 0, None, 0.0
        n = len(order)
        frac = max(0.0, min(1.0, frac))
        if self._mode == MODE_TOKEN:
            for k, idx in enumerate(order):
                a, b = self._slot_span.get(idx, (0.0, 0.0))
                if frac < b or k == n - 1:
                    cell = (frac - a) / max(1e-9, b - a)
                    return k, idx, max(0.0, min(1.0, cell))
            return n - 1, order[-1], 1.0
        k = min(n - 1, int(frac * n))
        return k, order[k], min(1.0, frac * n - k)

    def _x_to_time(self, x: int) -> float:
        """x → 时间戳。与条带绘制同一套几何（按当前模式同源换算）。

        - duration：线性时间映射（视口 _t0/_t1）
        - 等宽 / Token：槽位域换算，格内位置映射到该条带自己的
          [start, span_end] 区间。框选命中哪几格 = 筛出哪几条，视觉一致。
        """
        frac = max(0.0, min(1.0, (x - self._track_x) / max(1, self._track_w)))
        if self._mode != MODE_DURATION:
            _k, idx, cell = self._slot_at_frac(frac)
            if idx is None:
                return self._t0
            rec = self._records[idx]
            s = rec.start_ts if rec.start_ts > 0 else self._t0
            e = max(rec.span_end_ts, s)
            return s + cell * (e - s)
        span = max(1e-6, self._t1 - self._t0)
        return self._t0 + frac * span

    def _time_to_x(self, t: float) -> int:
        """时间戳 → x。与 ``_x_to_time`` 对称，保证选区高亮画到正确位置。"""
        if self._mode != MODE_DURATION:
            order = self._slot_order
            if not order:
                return self._track_x
            for k, idx in enumerate(order):
                rec = self._records[idx]
                s = rec.start_ts if rec.start_ts > 0 else self._t0
                e = max(rec.span_end_ts, s)
                a, b = self._slot_span.get(idx, (k / max(1, len(order)), (k + 1) / max(1, len(order))))
                if s <= t <= e:
                    cell = (t - s) / max(1e-6, e - s)
                    return int(self._track_x + (a + cell * (b - a)) * self._track_w)
                if t < s:
                    # 时间上落在当前条之前（空档/开头）→ 当前槽左端
                    return int(self._track_x + a * self._track_w)
            return int(self._track_x + self._track_w)  # 超过末条 → 轴右端
        span = max(1e-6, self._t1 - self._t0)
        frac = max(0.0, min(1.0, (t - self._t0) / span))
        return int(self._track_x + frac * self._track_w)

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
            box = QRect(left, TICK_H - 6, max(2, right - left), PANEL_H - TICK_H + 2)
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

    def focus_window(self, t0: Optional[float], t1: Optional[float], pad_ratio: float = 0.04) -> None:
        """把缩放窗口收窄到 ``[t0, t1]``（只看某一轮时用）；None = 复位全量。

        ⚠️ 视口两端各留一点余量（``pad_ratio``）：正好卡在边界上时首尾条带会
        贴死轴的两端，看起来像被裁掉。留余量后条带落在中间，仍是「放大到这
        一轮」。不与全量相同时才设窗口，避免出现「放大了但看不出区别」的空
        缩放状态。

        ⚠️ 槽位模式必须换算成**条目窗**（写 ``_iwin``）：只设 ``_view`` 的话
        刻度轴会跟着变、条带却仍按全量铺满，两者错位（该模式下条带位置由
        ``_iwin`` 决定）。
        """
        if t0 is None or t1 is None:
            self._view = None
            self._iwin = None
            self._sync_scrollbar()
            self.update()
            return
        full_t0, full_t1 = time_bounds(self._records)
        a, b = min(t0, t1), max(t0, t1)
        if b <= a:
            b = a + max(0.05, (full_t1 - full_t0) * 0.001)  # 单点轮次：给个最小窗
        pad = (b - a) * max(0.0, pad_ratio)
        a, b = a - pad, b + pad
        span = max(1e-3, b - a)
        if span >= full_t1 - full_t0:
            self._view = None  # 与全量等宽 → 无需缩放
            self._iwin = None
        else:
            a = max(full_t0, min(full_t1 - span, a))
            self._view = (a, a + span)
            self._view_to_iwin()
        self._sync_scrollbar()
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
            painter.drawLine(gx, TICK_H - 4, gx, PANEL_H - 4)

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
            s_v = rec.start_ts if rec.start_ts > 0 else t0
            e_v = max(rec.span_end_ts, s_v)
            slot: Optional[Tuple[float, float]] = None
            if self._slot_order:
                # 铺满布局（等宽 / Token）：只画视口内分到槽的记录
                slot = self._slot_span.get(idx)
                if slot is None:
                    continue
            else:
                # 视口剪裁：与当前时间窗无重叠的条带不画，否则被 clamp 到
                # 边界后又被 _MIN_BAR_PX 撑成 5px 块，堆积在两头。
                # 用严格不等：端点相接（e==t0 / s==t1）仍画，避免误杀瞬时条带。
                if e_v < t0 or s_v > t1:
                    continue
            bx0, bx1 = self._x_ratio(rec, idx, len(recs), t0, t1, track_x, track_w, slot)
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
        """顶部时间刻度：显示视口各点相对 session 起点的绝对偏移。

        ⚠️ 不能画相对视口的 0~总时长：缩放后左端仍是 "0s" 会误导成
        "时间线从头开始"；必须用全量边界 _full_t0 做偏移基准。
        """
        f = self._num_font(-3)
        painter.setFont(f)
        base = self._full_t0
        for k in range(5):
            if k in (0, 4):
                continue
            gx = track_x + int(track_w * k / 4)
            t = t0 + (t1 - t0) * k / 4
            label = format_duration_compact(int((t - base) * 1000))
            painter.setPen(QColor(self._pal.text_muted))
            painter.drawText(
                QRect(gx - 40, 0, 80, TICK_H - 2),
                Qt.AlignVCenter | Qt.AlignHCenter,
                label,
            )
        painter.setPen(QColor(self._pal.text_secondary))
        painter.drawText(
            QRect(track_x, 0, 60, TICK_H - 2),
            Qt.AlignVCenter | Qt.AlignLeft,
            format_duration_compact(int((t0 - base) * 1000)),
        )
        painter.drawText(
            QRect(track_x + track_w - 80, 0, 80, TICK_H - 2),
            Qt.AlignVCenter | Qt.AlignRight,
            format_duration_compact(int((t1 - base) * 1000)),
        )

    def _paint_token_ticks(self, painter: QPainter, track_x: int, track_w: int) -> None:
        """Token 模式刻度：轴位置不再是时间，换画**累积 token**。

        ⚠️ 不能沿用时间刻度：Token 模式下 x 位置与时间不成线性（条的先后顺序
        是时间序，但宽度按 token 占比），时间刻度会对不上条带边界。改画该轴
        位置对应的累积 token 数，与条带宽度口径同源。
        总量为 0（全部条目都无 token）时退化为百分比刻度。
        """
        f = self._num_font(-3)
        painter.setFont(f)
        total = self._token_total
        for k in range(5):
            if k in (0, 4):
                continue
            gx = track_x + int(track_w * k / 4)
            label = _tok_label(round(total * k / 4)) if total > 0 else f"{k * 25}%"
            painter.setPen(QColor(self._pal.text_muted))
            painter.drawText(
                QRect(gx - 40, 0, 80, TICK_H - 2),
                Qt.AlignVCenter | Qt.AlignHCenter,
                label,
            )
        painter.setPen(QColor(self._pal.text_secondary))
        painter.drawText(
            QRect(track_x, 0, 80, TICK_H - 2),
            Qt.AlignVCenter | Qt.AlignLeft,
            _tok_label(0) if total > 0 else "0%",
        )
        painter.drawText(
            QRect(track_x + track_w - 80, 0, 80, TICK_H - 2),
            Qt.AlignVCenter | Qt.AlignRight,
            f"{_tok_label(total)} tok" if total > 0 else "100%",
        )

    def _paint_empty(self, painter: QPainter) -> None:
        painter.setFont(self._font())
        painter.setPen(QColor(self._pal.text_muted))
        painter.drawText(self.rect(), Qt.AlignCenter, "暂无轨迹 — 发送一条消息后这里会出现时间线")

    # ──────────────────── 交互 ────────────────────

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._scrollbar.setGeometry(
            LANE_LABEL_W,
            self.height() - SCROLL_H,
            max(40, self.width() - LANE_LABEL_W - PAD_R),
            SCROLL_H,
        )

    def _sync_scrollbar(self) -> None:
        """滚动条 ↔ 缩放窗口同步：未放大时隐藏，放大后出现且可拖。

        两种模式的窗口语义不同，映射也不同：
        - Duration：时间窗 → 滚动条 = 毫秒位移
        - 等宽 / Token：**条目窗** → 滚动条 = 条目位移
        """
        sb = self._scrollbar
        n_all = len(self._records)
        if self._mode == MODE_DURATION:
            if self._view is None or not n_all:
                sb.hide()
                return
            full_t0, full_t1 = time_bounds(self._records)
            full_ms = max(1, int((full_t1 - full_t0) * 1000))
            span_ms = max(1, int((self._view[1] - self._view[0]) * 1000))
            sb.blockSignals(True)
            sb.setRange(0, max(0, full_ms - span_ms))
            sb.setPageStep(span_ms)
            sb.setSingleStep(max(1, span_ms // 20))
            sb.setValue(int((self._view[0] - full_t0) * 1000))
            sb.blockSignals(False)
        else:
            if self._iwin is None or not n_all:
                sb.hide()
                return
            i0, i1 = self._iwin
            width = max(1, i1 - i0 + 1)
            sb.blockSignals(True)
            sb.setRange(0, max(0, n_all - width))
            sb.setPageStep(width)
            sb.setSingleStep(max(1, width // 20))
            sb.setValue(i0)
            sb.blockSignals(False)
        self._scrollbar.setGeometry(
            LANE_LABEL_W,
            self.height() - SCROLL_H,
            max(40, self.width() - LANE_LABEL_W - PAD_R),
            SCROLL_H,
        )
        sb.show()

    def _on_scroll_moved(self, value: int) -> None:
        """拖滚动条 → 窗口平移（缩放状态不变）。"""
        n_all = len(self._records)
        if self._mode == MODE_DURATION:
            if self._view is None or not n_all:
                return
            full_t0, full_t1 = time_bounds(self._records)
            span = self._view[1] - self._view[0]
            v0 = full_t0 + value / 1000.0
            v0 = max(full_t0, min(full_t1 - span, v0))
            self._view = (v0, v0 + span)
        else:
            if self._iwin is None or not n_all:
                return
            i0, i1 = self._iwin
            width = max(1, i1 - i0 + 1)
            i0 = max(0, min(n_all - width, int(value)))
            self._iwin = (i0, i0 + width - 1)
            self._iwin_to_view()
        self.update()

    def _clamp_windows(self) -> None:
        """数据变化后夹紧当前模式使用的窗口（两种语义共存，各自独立夹）。"""
        n_all = len(self._records)
        if self._view is not None:
            full_t0, full_t1 = time_bounds(self._records)
            span = max(1e-6, self._view[1] - self._view[0])
            if full_t1 - full_t0 <= span:
                self._view = None
            else:
                v0 = max(full_t0, min(full_t1 - span, self._view[0]))
                self._view = (v0, v0 + span)
        if self._iwin is not None:
            i0, i1 = self._iwin
            width = max(1, i1 - i0 + 1)
            if width >= n_all:
                self._iwin = None
            else:
                i0 = max(0, min(n_all - width, i0))
                self._iwin = (i0, i0 + width - 1)

    # ──────────────────── 模式间窗口换算 ────────────────────

    def _iwin_to_view(self) -> None:
        """条目窗 → 时间窗（供刻度/拖选/模式切换保持视野连续）。

        取窗内条目首尾的占用区间；窗为空则清掉 _view。仅槽位模式的派生值，
        不反向读取（避免两个源互相同步变成环）。
        """
        recs = self._records
        if self._iwin is None or not recs:
            self._view = None
            return
        i0, i1 = self._iwin
        i0, i1 = max(0, i0), min(len(recs) - 1, i1)
        if i0 > i1:
            self._view = None
            return
        starts = [recs[i].start_ts for i in range(i0, i1 + 1) if recs[i].start_ts > 0]
        ends = [max(recs[i].span_end_ts, recs[i].start_ts) for i in range(i0, i1 + 1) if recs[i].start_ts > 0]
        if not starts:
            self._view = None
            return
        a, b = min(starts), max(ends)
        if b <= a:
            b = a + 0.001
        self._view = (a, b)

    def _view_to_iwin(self) -> None:
        """时间窗 → 条目窗（Duration 切到槽位模式时换算，保证视野连续）。"""
        recs = self._records
        if self._view is None or not recs:
            self._iwin = None
            return
        t0, t1 = self._view
        hits = [
            i
            for i, r in enumerate(recs)
            if r.start_ts > 0 and max(r.span_end_ts, r.start_ts) >= t0 and r.start_ts <= t1
        ]
        if not hits or len(hits) >= len(recs):
            self._iwin = None
            return
        self._iwin = (hits[0], hits[-1])

    def wheelEvent(self, event) -> None:  # noqa: N802
        """滚轮缩放（以鼠标所指为锚点），三种模式共用入口、按模式分派窗口。

        ⚠️ 锚点必须用全局光标映射：QWheelEvent.pos() 在部分 Windows 环境
        返回错误坐标，导致锚点恒在左端 → 放大永远从时间轴起点开始。

        两种窗口语义（见 ``__init__`` 的 _view / _iwin 说明）：
        - **Duration**：缩的是**时间窗**。x 与时间线性同源，锚点取光标时刻，
          新窗按 ``anchor - frac * new_span`` 反推。
        - **等宽 / Token**：缩的是**条目窗**。条带 x 是槽位域（与自身时刻
          无关），时间插值算出的锚点毫无意义 —— 旧实现正是这么写的，实测
          漂移 255~313px、多数条目被甩出视野。改为：锚点 = 光标所压条目的
          槽序，按同比例收窄窗内条目数，使该条在窗内的相对位置保持不变。
        """
        if not self._records:
            return
        n_all = len(self._records)
        pos = self.mapFromGlobal(QCursor.pos())
        frac = max(0.0, min(1.0, (pos.x() - self._track_x) / max(1, self._track_w)))
        # 上滚(angleDelta>0)=放大（窗口收窄）；下滚=缩小（窗口放宽）
        k = 0.8 if event.angleDelta().y() > 0 else 1.25

        if self._mode == MODE_DURATION:
            full_t0, full_t1 = time_bounds(self._records)
            t0, t1 = self._view if self._view else (full_t0, full_t1)
            span = max(1e-6, t1 - t0)
            in_track = self._track_x <= pos.x() <= self._track_x + self._track_w
            anchor = self._x_to_time(pos.x()) if in_track else t0 + frac * span
            new_span = max(0.02, span * k)  # 最小窗口 20ms，防无限放大
            if new_span >= full_t1 - full_t0:
                self._view = None  # 窗口已覆盖全量 → 复位
            else:
                v0 = anchor - frac * new_span
                v0 = max(full_t0, min(full_t1 - new_span, v0))
                self._view = (v0, v0 + new_span)
        else:
            i0, i1 = self._iwin if self._iwin else (0, n_all - 1)
            width = max(1, i1 - i0 + 1)
            # ── 锚点：光标所指条目 + 光标在该条带内的相对位置 ──
            # ⚠️ 必须用条带的**真实归一化区间**（_slot_span）反推 cell，不能拿
            # 「槽序中心 (k+0.5)/n」当锚点：槽序中心与条带中心差 0.04/n，且
            # 缩放后 n 变小，这个偏差按 -0.5/n′·track_w 累积（实测 n′≈19 时
            # 单次跳 41px，连滚 5 次上百 px）。
            hit: Optional[int] = None
            cell = 0.5
            for rect, idx in self._hit_areas:
                if rect.left() <= pos.x() <= rect.right():
                    hit = idx
                    break
            if hit is not None:
                ab = self._slot_span.get(hit)
                if ab is not None and ab[1] > ab[0]:
                    cell = max(0.0, min(1.0, (frac - ab[0]) / (ab[1] - ab[0])))
            else:
                # 空白处：按像素位置折算窗内条目，锚定其中心
                hit = max(i0, min(i1, i0 + int(frac * width)))
            # ⚠️ 窗口宽度是**整数条数**，缩放进/出必须保证每次至少变化 1 条。
            # 只写 ``max(1, round(width * k))`` 会有双向不动点：宽度 2 时
            # round(2*0.8)=2（放大不动）、round(2*1.25)=2（缩小也不动 ——
            # Python 的 round 把 .5 归到偶数侧）；宽度 1 同样双向锁死
            # round(0.8)=1、round(1.25)=1。窗口不变 → 滚动条与画面均无变化，
            # 表现为「放大到最大后就无法缩小」。Duration 分支用浮点新窗
            # （见上方 ``new_span``）天然无此问题。
            if k < 1.0:
                new_width = max(1, min(width - 1, int(round(width * k))))
            else:
                new_width = max(width + 1, int(round(width * k)))
            if new_width >= n_all:
                self._iwin = None  # 窗口已覆盖全部条目 → 复位
                self._view = None
            elif self._mode == MODE_TOKEN:
                i0n = self._token_window_start(hit, cell, frac, new_width)
                if i0n is None:
                    i0n = self._ordinal_window_start(hit, cell, i0, width, new_width)
                self._iwin = (i0n, i0n + new_width - 1)
                self._iwin_to_view()
            else:
                i0n = self._ordinal_window_start(hit, cell, i0, width, new_width)
                self._iwin = (i0n, i0n + new_width - 1)
                self._iwin_to_view()
        self._sync_scrollbar()
        self.update()
        # ⚠️ 必须显式 accept：Qt5 的 ``QWidget::wheelEvent`` 默认实现是
        # ``event->ignore()``，未接受的 Wheel 会沿 parent 链向上传播。本面板
        # 若只做缩放不消费，事件会一路浮到 TabManagerWindow._chat_wrapper，
        # 命中其 ``QEvent.Wheel`` 分支 → ``_forward_wheel_to_scroll_area()``
        # → ``chat_scroll_area.wheelEvent(event)``，表现为「滚泳道图，聊天
        # 消息列表跟着滚」。那条转发分支是给限宽居中留白区用的（留白处没有
        # 子控件接收滚轮），不能靠它区分，只能在源头消费掉。
        #
        # 上面的 ``if not self._records: return`` 早退路径**特意不 accept**：
        # 无数据时泳道图没有可缩放内容，让事件正常上浮去滚对话区。
        event.accept()

    def _ordinal_window_start(self, hit: int, cell: float, i0: int, width: int, new_width: int) -> int:
        """等宽模式：窗内每条等宽 → 序号即位置。

        保持「光标处」在窗内的归一化位置不变：
        ``(hit - i0 + cell) / width == (hit - i0′ + cell) / new_width``
        """
        p_win = (hit - i0 + cell) / max(1, width)
        start = int(round(hit + cell - p_win * new_width))
        return max(0, min(len(self._records) - new_width, start))

    def _token_window_start(self, hit: int, cell: float, frac: float, new_width: int) -> Optional[int]:
        """Token 模式：槽宽 ∝ token 占比 → 必须用**权重前缀和**求解。

        ⚠️ 不能沿用序号公式（``_ordinal_window_start``）：序号空间里相邻两条
        等距，而 Token 模式一条 20k 的回复与一条 200 的 hook 宽度差百倍，
        序号位置与像素位置完全不成比例 —— 实测连滚 5 次漂移 323px、锚点条目
        直接跑出视野。

        目标：让「光标所指的那一点」在新窗内的像素占比仍等于 ``frac``。
        前缀和 ``P`` 下，该点累计权重 ``A = P[hit] + cell·tokens[hit]``，
        在窗 ``[i0′, i0′+new_width)`` 内的占比是
        ``(A − P[i0′]) / (P[i0′+new_width] − P[i0′])``。
        候选 i0′ 必须满足锚点条目在窗内，即 ``i0′ ∈ [hit−new_width+1, hit]``
        —— 至多 ``new_width`` 个，逐个求占比取最近者（O(new_width)，滚轮事件
        量级下可忽略）。

        返回 None = 无法求解（窗内权重全 0，如全是无 token 的条目），
        由调用方退回序号公式。
        """
        recs = self._records
        n_all = len(recs)
        pref = [0] * (n_all + 1)
        for i, r in enumerate(recs):
            pref[i + 1] = pref[i] + max(0, r.tokens)
        anchor = pref[hit] + cell * max(0, recs[hit].tokens)
        lo = max(0, hit - new_width + 1)
        hi = min(hit, n_all - new_width)
        if lo > hi:
            return None
        best: Optional[Tuple[float, int]] = None
        for cand in range(lo, hi + 1):
            total = pref[cand + new_width] - pref[cand]
            if total <= 0:
                continue
            pos = (anchor - pref[cand]) / total
            d = abs(pos - frac)
            if best is None or d < best[0]:
                best = (d, cand)
        return best[1] if best is not None else None

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
        # ⚠️ 命中目标在**按下时**就锁定。Token 模式下小条带被 _MIN_BAR_PX 压到
        # 5px 宽（等宽模式是几十上百 px），抬手时手指抖 3px 就滑出条带 →
        # release 重新命中失败 → 点击静默丢失（用户报「token 模式下点泳道图
        # 节点无法跳转」）。流式期间 _token_total 变化还会让条带重新分配位置，
        # 按下与抬起的命中区可能已不是同一块。
        self._press_hit = self._hit_test(event.pos())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        """抬起：位移够大 = 拖选时间区间；否则按「点击」处理。"""
        if self._drag_from is None:
            return
        start_x = self._drag_from
        end_x = event.pos().x()
        self._drag_from = self._drag_to = None
        press_hit, self._press_hit = self._press_hit, None
        if abs(end_x - start_x) >= self._DRAG_SLOP:
            ta, tb = self._x_to_time(start_x), self._x_to_time(end_x)
            self._range = (min(ta, tb), max(ta, tb))
            self.update()
            self.rangeSelected.emit(self._range[0], self._range[1])
            return
        # 未超拖拽阈值 = 点击：优先用按下时锁定的目标（抬手微动不丢点击），
        # 按下落在空白处时才回退到抬起位置（留一点容错）。
        hit = press_hit if press_hit is not None else self._hit_test(event.pos())
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
            extra = ""
            if self._mode == MODE_TOKEN and self._token_total > 0:
                share = rec.tokens * 100.0 / self._token_total
                extra = f" · {format_tokens(rec.tokens)} tok · 占 {share:.1f}%"
            QToolTip.showText(
                event.globalPos(),
                f"{rec.kind.label} · {rec.label}\n"
                f"{rec.absolute_time} · 占用 {rec.span_label}"
                f" · 耗时 {format_duration_compact(rec.duration_ms)}{extra}\n{rec.status}",
                self,
            )

    def leaveEvent(self, _event) -> None:  # noqa: N802
        if self._hover_idx is not None:
            self._hover_idx = None
            self.setCursor(Qt.ArrowCursor)
            self.update()


def _is_light(c: QColor) -> bool:
    return (0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()) > 160
