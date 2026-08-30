# -*- coding: utf-8 -*-
"""agent_trace.TurnListWidget — 中央条目表格（对齐 Chrome DevTools Network）。

与旧版（纯 label + 时长两列）的差异：

- **多列表格**：``Name | Type | Size | Time | Waterfall`` —— 与浏览器 Network
  面板同构（Name/Status/Type/Size/Time/Waterfall）。
- **Waterfall 列**：每行内嵌迷你甘特条，比例与顶部时间线共用同一 ``(t0, t1)``。
- **列头可点击排序**：Size / Time / Type / Name（再点一次反向，第三次回到
  时间序）。
- **滚动条用主程序统一样式**（``get_unified_scrollbar_style``），不再用默认
  系统滚动条。
- 颜色一律走 :class:`ThemePalette`（QColor 已解析 rgba）—— 修深色主题下
  次级文字变黑的历史 bug。
- **tail 增量化**：``set_tail`` 只增删尾部 in-flight 行，主列表 items 不重建
  → 不会滚动跳顶/闪烁。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QRect, QRectF, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .trace_models import (
    EntryKind,
    ThemePalette,
    TraceRecord,
    format_duration,
    format_tokens,
    kind_color,
    with_alpha,
)

# 类型过滤 chips（label 与 kind）
FILTER_CHIPS = (
    ("all", "全部", None),
    ("system", "系统", EntryKind.SYSTEM),
    ("user", "用户", EntryKind.USER),
    ("context", "钩子", EntryKind.CONTEXT),
    ("assistant", "助手", EntryKind.ASSISTANT),
    ("tool", "工具", EntryKind.TOOL),
)

# 列定义：(key, 标题, 默认宽, 对齐)
# 列宽按「紧凑优先」：Time 列已并入 Waterfall（条 + 右侧占用时长），
# Size 列换成更短的 Tokens（占用）。
_COLUMNS = (
    ("name", "Name", 0, Qt.AlignLeft),  # 0 = 自适应
    ("type", "Type", 78, Qt.AlignLeft),  # 实际宽由 set_type_width 按最长标签实测
    ("tokens", "Tokens", 58, Qt.AlignRight),
    ("waterfall", "Waterfall", 176, Qt.AlignLeft),
)
_PAD_L = 12
_PAD_R = 12
_HEADER_H = 28
_FILTER_H = 36


class _Columns:
    """列宽计算器 — 表头与行 delegate 共用，保证像素级对齐。"""

    def __init__(self) -> None:
        self.fixed = {k: w for k, _t, w, _a in _COLUMNS if w}
        self.min_waterfall = 120

    def set_type_width(self, width: int) -> None:
        """Type 列宽按最长类型标签（ASSISTANT）实测，杜绝文字被裁。"""
        self.fixed["type"] = max(64, width)

    def name_w(self, total_w: int) -> int:
        rest = sum(self.fixed.values())
        return max(120, total_w - _PAD_L - _PAD_R - rest)

    def waterfall_w(self, total_w: int) -> int:
        w = self.fixed["waterfall"]
        spare = total_w - (_PAD_L + _PAD_R + sum(v for k, v in self.fixed.items() if k != "waterfall") + 120)
        if spare < w:
            return max(self.min_waterfall, spare)
        return w

    def rect_of(self, key: str, total_w: int, y: int, h: int) -> QRect:
        x = _PAD_L
        for k, _t, _w, _a in _COLUMNS:
            w = self.name_w(total_w) if k == "name" else (self.waterfall_w(total_w) if k == "waterfall" else self.fixed[k])
            if k == key:
                return QRect(x, y, w, h)
            x += w
        return QRect(x, y, 0, h)


class _HeaderWidget(QFrame):
    """可点击排序的列头（Network 面板风格：底边线 + 排序箭头）。"""

    sortChanged = pyqtSignal(str, bool)  # (column_key, descending)

    _ORDER = ("name", "type", "tokens", "waterfall")

    def __init__(self, cols: _Columns, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._cols = cols
        self._pal = ThemePalette()
        self._base_px = 13
        self._sort_key: Optional[str] = None
        self._desc = False
        self.setFixedHeight(_HEADER_H)
        self.setCursor(Qt.ArrowCursor)

    def set_palette(self, pal: ThemePalette, base_px: int) -> None:
        self._pal = pal
        self._base_px = base_px
        # Type 列宽按最长标签实测（ASSISTANT），否则徽章文字会被裁掉
        f = QFont(pal.font_family)
        f.setPixelSize(max(9, base_px - 2))
        f.setBold(True)
        self._cols.set_type_width(QFontMetrics(f).horizontalAdvance("ASSISTANT") + 18)
        self.update()

    def set_sort(self, key: Optional[str], desc: bool) -> None:
        self._sort_key, self._desc = key, desc
        self.update()

    def _font(self, bold: bool = False) -> QFont:
        """列头用**系统 UI 字体**（不是等宽）：与主程序界面同一套字。"""
        f = QFont(self._pal.font_family)
        f.setPixelSize(max(9, self._base_px - 2))
        f.setBold(bold)
        return f

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.TextAntialiasing, True)
            p.setFont(self._font())
            total = self.width()
            for key, title, _w, align in _COLUMNS:
                if key == "waterfall":
                    w = self._cols.waterfall_w(total)
                elif key == "name":
                    w = self._cols.name_w(total)
                else:
                    w = self._cols.fixed[key]
                x = _PAD_L
                for k2, _t2, _w2, _a2 in _COLUMNS:
                    if k2 == key:
                        break
                    x += (
                        self._cols.name_w(total)
                        if k2 == "name"
                        else (self._cols.waterfall_w(total) if k2 == "waterfall" else self._cols.fixed[k2])
                    )
                rect = QRect(x, 0, w, self.height())
                active = self._sort_key == key
                p.setPen(QColor(self._pal.accent if active else self._pal.text_muted))
                label = title
                if active:
                    label = f"{title} {'▼' if self._desc else '▲'}"
                if key in self._ORDER and key != self._sort_key:
                    p.setPen(QColor(self._pal.text_muted))
                p.drawText(rect.adjusted(0, 0, -6 if align == Qt.AlignRight else 0, 0), align | Qt.AlignVCenter, label)
            p.setPen(QPen(self._pal.line_at(30), 1))
            p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        finally:
            p.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        total = self.width()
        x = _PAD_L
        for key, _t, _w, _a in _COLUMNS:
            w = (
                self._cols.name_w(total)
                if key == "name"
                else (self._cols.waterfall_w(total) if key == "waterfall" else self._cols.fixed[key])
            )
            if key in self._ORDER and x <= event.pos().x() < x + w:
                if self._sort_key == key:
                    if self._desc:
                        self._sort_key, self._desc = None, False  # 第三次 → 回到时间序
                    else:
                        self._desc = True
                else:
                    self._sort_key, self._desc = key, key in ("tokens", "waterfall")
                self.sortChanged.emit(self._sort_key or "index", self._desc)
                self.update()
                return
            x += w
        super().mousePressEvent(event)


class _RowDelegate(QStyledItemDelegate):
    """自绘一行 entry（状态点 + Name + Type 徽章 + Size + Time + Waterfall）。"""

    def __init__(self, cols: _Columns, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._cols = cols
        self._pal = ThemePalette()
        self._base_px = 13
        self._bounds = (0.0, 1.0)

    # ── 配置 ──

    def set_palette(self, pal: ThemePalette, base_px: int) -> None:
        self._pal = pal
        self._base_px = max(10, min(24, base_px))

    def set_bounds(self, t0: float, t1: float) -> None:
        self._bounds = (t0, t1 if t1 > t0 else t0 + 1.0)

    @property
    def row_h(self) -> int:
        return max(24, self._base_px + 13)

    def _font(self, delta_px: int = 0, bold: bool = False) -> QFont:
        """正文用**系统 UI 字体**（Name 列 / 类型徽章 / Turn 徽章）。"""
        f = QFont(self._pal.font_family)
        f.setPixelSize(max(9, self._base_px + delta_px))
        f.setBold(bold)
        return f

    def _num_font(self, delta_px: int = 0, bold: bool = False) -> QFont:
        """数字列（Size / Time / Waterfall）用等宽，保证右对齐时列位对齐。"""
        f = QFont(self._pal.mono_family)
        f.setStyleHint(QFont.Monospace)
        f.setPixelSize(max(9, self._base_px + delta_px))
        f.setBold(bold)
        return f

    # ── 绘制 ──

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), self.row_h)

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        rec: Optional[TraceRecord] = index.data(Qt.UserRole)
        if rec is None:
            return

        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        pal = self._pal
        cols = self._cols

        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setRenderHint(QPainter.TextAntialiasing, True)

            # 行背景
            if selected:
                painter.fillRect(rect, QColor(pal.selected_bg))
                painter.fillRect(QRect(rect.x(), rect.y(), 3, rect.height()), QColor(pal.accent))
            elif hovered:
                painter.fillRect(rect, pal.line_at(14))
            elif index.row() % 2 == 1:
                painter.fillRect(rect, pal.line_at(7))

            # turn 分组线
            if rec.meta.get("turn_start"):
                painter.setPen(QPen(with_alpha(QColor(pal.accent), 90), 1))
                painter.drawLine(rect.x(), rect.y(), rect.right(), rect.y())

            main_font = self._font()
            small_font = self._font(-2, bold=True)  # 徽章：小号加粗 UI 字体
            num_font = self._num_font(-1)
            color = pal.danger if rec.is_error else kind_color(rec.kind)

            # ── Name 列 ──
            name_rect = cols.rect_of("name", rect.width(), rect.y(), rect.height())
            x = name_rect.x()
            # 状态点
            dot_d = 6
            dot_y = rect.y() + (rect.height() - dot_d) // 2
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(pal.danger if rec.is_error else (pal.warning if rec.is_pending else pal.success)))
            painter.drawEllipse(x, dot_y, dot_d, dot_d)
            x += dot_d + 8

            # Turn 前缀徽章
            if rec.meta.get("turn_start") and rec.turn_no > 0:
                tfm = QFontMetrics(small_font)
                tag = f"T{rec.turn_no}"
                tw = tfm.horizontalAdvance(tag) + 10
                painter.setBrush(with_alpha(QColor(pal.accent), 36))
                painter.drawRoundedRect(QRectF(x, dot_y - 2, tw, dot_d + 4), 3, 3)
                painter.setPen(QColor(pal.accent))
                painter.setFont(small_font)
                painter.drawText(QRect(x, rect.y(), tw, rect.height()), Qt.AlignCenter, tag)
                x += tw + 6

            # 主文本：label（亮） + preview（暗）
            avail = max(30, name_rect.right() - x)
            painter.setFont(main_font)
            fm = QFontMetrics(main_font)
            head = rec.label + "  "
            painter.setPen(QColor(pal.text))
            head_w = fm.horizontalAdvance(head)
            if head_w > avail:
                painter.drawText(
                    QRect(x, rect.y(), avail, rect.height()),
                    Qt.AlignVCenter | Qt.AlignLeft,
                    fm.elidedText(rec.label, Qt.ElideRight, avail),
                )
            else:
                painter.drawText(QRect(x, rect.y(), head_w, rect.height()), Qt.AlignVCenter | Qt.AlignLeft, head)
                rest = avail - head_w
                if rest > 20:
                    painter.setPen(QColor(pal.text_muted))
                    painter.drawText(
                        QRect(x + head_w, rect.y(), rest, rect.height()),
                        Qt.AlignVCenter | Qt.AlignLeft,
                        fm.elidedText(rec.preview.replace("\n", " "), Qt.ElideRight, rest),
                    )

            # ── Type 列：类型徽章 ──
            type_rect = cols.rect_of("type", rect.width(), rect.y(), rect.height())
            painter.setFont(small_font)
            bfm = QFontMetrics(small_font)
            bw = min(type_rect.width() - 6, bfm.horizontalAdvance(rec.kind.label) + 14)
            badge = QRect(type_rect.x(), rect.y() + (rect.height() - 17) // 2, bw, 17)
            painter.setPen(Qt.NoPen)
            painter.setBrush(with_alpha(color, 34))
            painter.drawRoundedRect(QRectF(badge), 3, 3)
            painter.setPen(color)
            painter.drawText(badge, Qt.AlignCenter, rec.kind.label)

            # ── Tokens 列（占用）──
            tk_rect = cols.rect_of("tokens", rect.width(), rect.y(), rect.height())
            painter.setFont(num_font)
            painter.setPen(QColor(pal.text_muted))
            painter.drawText(tk_rect.adjusted(0, 0, -8, 0), Qt.AlignVCenter | Qt.AlignRight, format_tokens(rec.tokens))

            # ── Waterfall 列（条 + 右侧占用时长；点列头按时间排序）──
            wf_rect = cols.rect_of("waterfall", rect.width(), rect.y(), rect.height())
            self._paint_waterfall(painter, rec, wf_rect, color)

            # 行分隔线
            painter.setPen(QPen(pal.line_at(16), 1))
            painter.drawLine(rect.x() + 3, rect.bottom(), rect.right(), rect.bottom())
        finally:
            painter.restore()

    def _paint_waterfall(self, painter: QPainter, rec: TraceRecord, rect: QRect, color: QColor) -> None:
        """行内迷你甘特条 + 右侧占用时长（Time 列已并入本列）。

        条带用 **span（占用区间）** 而不是 end_ts：瞬时消息 end=0，用 end 会让
        所有条塌成最小宽度的碎点；数字同样显示 span_ms，与条宽一致。
        """
        pal = self._pal
        t0, t1 = self._bounds
        span = max(1e-6, t1 - t0)
        track_y = rect.y() + (rect.height() - 10) // 2
        label_w = 64  # 右侧占用时长文字位
        track_w = max(30, rect.width() - label_w - 8)
        painter.setPen(Qt.NoPen)
        painter.setBrush(pal.track)
        painter.drawRoundedRect(QRectF(rect.x(), track_y, track_w, 10), 3, 3)

        # 右侧时长文字（错误红 / 进行中金 / 完成暗）
        painter.setFont(self._num_font(-2))
        painter.setPen(QColor(pal.danger if rec.is_error else (pal.warning if rec.is_pending else pal.text_muted)))
        painter.drawText(
            QRect(rect.x() + track_w + 8, rect.y(), label_w, rect.height()),
            Qt.AlignVCenter | Qt.AlignLeft,
            rec.span_label,
        )

        if rec.start_ts <= 0:
            return
        s = rec.start_ts
        e = max(rec.span_end_ts, s)
        a = max(0.0, min(1.0, (s - t0) / span))
        b = max(0.0, min(1.0, (e - t0) / span))
        x0 = rect.x() + a * track_w
        w = (b - a) * track_w
        # 瞬时事件（同秒注入，span=0）→ 画成 3px 竖线标记，而不是最小条带
        painter.setBrush(with_alpha(color, 150) if rec.is_pending else with_alpha(color, 215))
        painter.drawRoundedRect(QRectF(x0, track_y, max(3.0, w), 10), 2 if w > 3 else 1, 2 if w > 3 else 1)
        if rec.is_pending:  # 未完成：条尾渐隐，视觉上"还在跑"
            painter.setBrush(with_alpha(color, 70))
            painter.drawRoundedRect(QRectF(x0 + w, track_y, min(8, rect.x() + track_w - x0 - w), 10), 2, 2)


class TurnListWidget(QWidget):
    """中央表格：可排序列头 + QListWidget 条目（+ in-flight 尾巴）。"""

    recordSelected = pyqtSignal(int)  # record 索引（visible_records 空间）
    # 用户点掉「时间区间」chip → 卡片同步清掉时间线上的选区
    timeRangeCleared = pyqtSignal()

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._tail: List[TraceRecord] = []
        self._visible_map: List[int] = []  # 可见行 → record 索引
        self._selected_rec_idx: Optional[int] = None
        self._filter_kind: Optional[EntryKind] = None
        self._search_text: str = ""
        self._time_range: Optional[Tuple[float, float]] = None  # 时间线拖选出的区间
        self._bounds = (0.0, 1.0)  # 会话时间边界（算区间相对秒数用）
        self._sort_key = "index"
        self._sort_desc = False
        self._pal = ThemePalette()
        self._base_px = 13
        self._cols = _Columns()
        self._build_ui()

    # ──────────────────── 搭建 ────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ① 类型过滤条（时间线下方、列表上方 —— 顶栏只留搜索/视图/操作）
        self._filter_bar = self._build_filter_bar()
        outer.addWidget(self._filter_bar)

        # ② 可排序列表头
        self._header = _HeaderWidget(self._cols, self)
        self._header.sortChanged.connect(self._on_sort_changed)
        outer.addWidget(self._header)

        self._delegate = _RowDelegate(self._cols, self)
        self._list = QListWidget(self)
        self._list.setItemDelegate(self._delegate)
        self._list.setUniformItemSizes(True)
        self._list.setSelectionMode(QListWidget.SingleSelection)
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self._list.currentRowChanged.connect(self._on_current_row_changed)
        outer.addWidget(self._list, 1)

        # 空态提示（无脚本运行时）
        self._empty_label = _EmptyHint(self._list)
        self._empty_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._empty_label.hide()

    def _build_filter_bar(self) -> QWidget:
        """类型过滤 chips + 结果计数（独占一行，放在时间线与列表之间）。"""
        bar = QFrame(self)
        bar.setFixedHeight(_FILTER_H)
        bar.setObjectName("agentTraceFilterBar")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 0, 12, 0)
        lay.setSpacing(4)
        self._chip_group = QButtonGroup(bar)
        self._chip_group.setExclusive(True)
        for key, label, kind in FILTER_CHIPS:
            btn = QPushButton(label, bar)
            btn.setCheckable(True)
            btn.setFixedHeight(24)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setChecked(key == "all")
            btn.clicked.connect(lambda _=False, k=kind: self.set_filter_kind(k))
            self._chip_group.addButton(btn)
            lay.addWidget(btn)

        # 时间线拖选出的区间 chip（默认隐藏，点它清除时间过滤）
        self._range_chip = QPushButton(bar)
        self._range_chip.setObjectName("agentTraceRangeChip")
        self._range_chip.setFixedHeight(24)
        self._range_chip.setCursor(Qt.PointingHandCursor)
        self._range_chip.setToolTip("点击清除时间区间过滤")
        self._range_chip.clicked.connect(self._on_range_chip_clicked)
        self._range_chip.hide()
        lay.addWidget(self._range_chip)

        lay.addStretch(1)
        self._count_label = QLabel(bar)
        lay.addWidget(self._count_label)
        return bar

    # ──────────────────── 公开 API ────────────────────

    def set_records(self, records: List[TraceRecord]) -> None:
        self._records = list(records)
        self._refilter()

    def append_records(self, records: List[TraceRecord]) -> None:
        base = len(self._records)
        self._records.extend(records)
        self._append_if_visible(list(range(base, len(self._records))))

    def update_records(self, start: int, count: int) -> None:
        """回填变化 → 刷新对应可见行的数据（不重建 item）。"""
        for i in range(start, min(start + count, len(self._records))):
            row = self._row_of_record(i)
            if row is not None:
                item = self._list.item(row)
                if item is not None:
                    item.setData(Qt.UserRole, self._records[i])
        self._list.viewport().update()

    def set_tail(self, tail: List[TraceRecord]) -> None:
        """增量化：只调整尾部 in-flight 行，主列表 items 不动、滚动不跳。"""
        old_tail = self._tail
        self._tail = list(tail)
        old_n, new_n = len(old_tail), len(self._tail)
        if new_n < old_n:
            for _ in range(old_n - new_n):
                if self._list.count() > len(self._visible_map):
                    self._list.takeItem(self._list.count() - 1)
        for i, rec in enumerate(self._tail):
            row = len(self._visible_map) + i
            if row < self._list.count():
                item = self._list.item(row)
                if item is not None:
                    item.setData(Qt.UserRole, rec)
            else:
                item = QListWidgetItem()
                item.setData(Qt.UserRole, rec)
                item.setFlags(Qt.NoItemFlags)
                item.setSizeHint(QSize(0, self._delegate.row_h))
                self._list.addItem(item)
        self._update_empty()
        self._list.viewport().update()

    def set_search(self, text: str) -> None:
        self._search_text = (text or "").strip().lower()
        self._refilter()

    def set_filter_kind(self, kind: Optional[EntryKind]) -> None:
        self._filter_kind = kind
        self._refilter()

    # ──────────────────── 时间区间过滤（时间线拖选）────────────────────

    def set_time_range(self, t0: Optional[float], t1: Optional[float]) -> None:
        """只保留与 [t0, t1] **有重叠**的条目；传 None 清除过滤。"""
        if t0 is None or t1 is None:
            self._time_range = None
        else:
            self._time_range = (min(t0, t1), max(t0, t1))
        self._update_range_chip()
        self._refilter()

    def clear_time_range(self) -> None:
        """外部（时间线空白单击）清除过滤 —— 不回发 timeRangeCleared，避免回环。"""
        self._time_range = None
        self._update_range_chip()
        self._refilter()

    def _on_range_chip_clicked(self) -> None:
        self._time_range = None
        self._update_range_chip()
        self._refilter()
        self.timeRangeCleared.emit()

    def _update_range_chip(self) -> None:
        rng = self._time_range
        if rng is None:
            self._range_chip.hide()
            return
        base = self._bounds[0] if self._bounds[0] > 0 else 0.0
        a = max(0.0, rng[0] - base)
        b = max(0.0, rng[1] - base)
        self._range_chip.setText(f"{a:.1f}s – {b:.1f}s  ✕")
        self._range_chip.show()

    def set_bounds(self, t0: float, t1: float) -> None:
        """注入时间边界（与顶部时间线共用比例）→ Waterfall 列 + 区间 chip 文案。"""
        self._bounds = (t0, t1)
        self._delegate.set_bounds(t0, t1)
        self._update_range_chip()
        self._list.viewport().update()

    def repaint_pending(self) -> None:
        """心跳重绘（pending 行时长实时计算）。"""
        self._list.viewport().update()

    def scroll_to_bottom(self) -> None:
        """跟随最新条目（对齐 Network 面板的 auto-scroll）。"""
        self._list.scrollToBottom()

    def clear_selection(self) -> None:
        self._selected_rec_idx = None
        self._list.setCurrentRow(-1)

    @property
    def selected_record_idx(self) -> Optional[int]:
        return self._selected_rec_idx

    def clear(self) -> None:
        self._records = []
        self._tail = []
        self._visible_map = []
        self._selected_rec_idx = None
        self._list.clear()
        self._update_empty()

    def select_record(self, idx: int) -> None:
        row = self._row_of_record(idx)
        if row is not None:
            self._list.setCurrentRow(row)

    @property
    def shown_count(self) -> int:
        return len(self._visible_map) + len(self._tail)

    @property
    def total_count(self) -> int:
        return len(self._records) + len(self._tail)

    # ──────────────────── 主题 / 字体 ────────────────────

    def set_colors(self, colors: dict, is_dark: bool = True) -> None:
        if not colors:
            return
        self._pal = ThemePalette.from_theme(colors, is_dark, mono_family=self._pal.mono_family)
        self._apply_palette()

    def set_palette(self, pal: ThemePalette) -> None:
        self._pal = pal
        self._apply_palette()

    def _apply_palette(self) -> None:
        pal = self._pal
        fs = self._base_px
        self._delegate.set_palette(pal, self._base_px)
        self._header.set_palette(pal, self._base_px)
        rh = QSize(0, self._delegate.row_h)
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is not None:
                item.setSizeHint(rh)
        self.setStyleSheet(_list_qss(pal))
        # 过滤条：chips 用系统 UI 字体
        self._filter_bar.setStyleSheet(
            f"QFrame#agentTraceFilterBar {{ background: transparent;"
            f" border-bottom: 1px solid {pal.q('border')}; }}"
            f"QFrame#agentTraceFilterBar QPushButton {{"
            f"  background: transparent; color: {pal.q('text_secondary')};"
            f"  border: 1px solid transparent; border-radius: 5px; padding: 0 10px;"
            f"  font-family: '{pal.font_family}'; font-size: {max(10, fs - 1)}px; }}"
            f"QFrame#agentTraceFilterBar QPushButton:hover {{ background: {pal.q('line', 22)}; }}"
            f"QFrame#agentTraceFilterBar QPushButton:checked {{"
            f"  color: {pal.q('accent')}; border: 1px solid {pal.q('accent')};"
            f"  background: {pal.q('accent', 26)}; }}"
            # 时间区间 chip：虚线边框 + 更具体的选择器（盖掉上面的通用规则）
            f"QFrame#agentTraceFilterBar QPushButton#agentTraceRangeChip {{"
            f"  color: {pal.q('accent')}; border: 1px dashed {pal.q('accent')};"
            f"  background: {pal.q('accent', 22)}; padding: 0 8px;"
            f"  font-family: '{pal.font_family}'; font-size: {max(10, fs - 2)}px; }}"
            f"QFrame#agentTraceFilterBar QPushButton#agentTraceRangeChip:hover {{"
            f"  background: {pal.q('accent', 44)}; }}"
        )
        self._count_label.setStyleSheet(
            f"color: {pal.q('text_muted')}; font-family: '{pal.font_family}'; font-size: {max(10, fs - 2)}px;"
        )
        self._empty_label.set_palette(pal)
        self._list.viewport().update()

    def _apply_font(self, font: QFont) -> None:
        px = font.pixelSize()
        if px <= 0:
            ptf = font.pointSizeF()
            px = int(round(ptf * 4 / 3)) if ptf > 0 else 13
        self._base_px = max(10, min(24, px))
        fam = font.family()
        if fam:
            self._pal.font_family = fam
        self._apply_palette()

    # ──────────────────── 内部 ────────────────────

    def _on_sort_changed(self, key: str, desc: bool) -> None:
        self._sort_key = key or "index"
        self._sort_desc = desc
        self._refilter()

    def _match(self, rec: TraceRecord) -> bool:
        if self._filter_kind is not None and rec.kind != self._filter_kind:
            return False
        if self._time_range is not None:
            # 用「占用区间与选区是否重叠」判定：跨越选区边界的长条目也该留下
            t0, t1 = self._time_range
            s = rec.start_ts if rec.start_ts > 0 else 0.0
            e = rec.span_end_ts if rec.span_end_ts > s else s
            # 完全无时间信息（s==e==0）→ 无法判断与选区关系，剔除
            if e <= 0:
                return False
            if s > t1 or e < t0:
                return False
        if self._search_text:
            hay = f"{rec.label}\n{rec.preview}\n{rec.raw}".lower()
            if self._search_text not in hay:
                return False
        return True

    def _sorted_indices(self) -> List[int]:
        idxs = [i for i, r in enumerate(self._records) if self._match(r)]
        if self._sort_key == "index":
            return idxs
        reverse = self._sort_desc
        if self._sort_key == "tokens":
            key = lambda i: self._records[i].tokens  # noqa: E731
        elif self._sort_key == "waterfall":
            key = lambda i: self._records[i].start_ts or 0.0  # noqa: E731
        elif self._sort_key == "type":
            key = lambda i: self._records[i].kind.value  # noqa: E731
        else:  # name
            key = lambda i: (self._records[i].label or "").lower()  # noqa: E731
        try:
            return sorted(idxs, key=key, reverse=reverse)
        except Exception:
            return idxs

    def _refilter(self) -> None:
        self._visible_map = self._sorted_indices()
        self._rebuild_items()
        self._restore_selection()

    def _rebuild_items(self) -> None:
        self._list.clear()
        for i in self._visible_map:
            self._list.addItem(self._make_item(i, self._records[i]))
        for rec in self._tail:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, rec)
            item.setFlags(Qt.NoItemFlags)
            item.setSizeHint(QSize(0, self._delegate.row_h))
            self._list.addItem(item)
        self._update_empty()

    def _append_if_visible(self, record_indices: List[int]) -> None:
        if self._sort_key != "index":
            # 非时间序 → 增量插入会破坏排序，直接整体重建（追加频率低）
            self._refilter()
            return
        tail_start_row = self._list.count() - len(self._tail)
        inserted = 0
        for i in record_indices:
            if 0 <= i < len(self._records) and self._match(self._records[i]):
                row = tail_start_row + inserted
                self._list.insertItem(row, self._make_item(i, self._records[i]))
                self._visible_map.append(i)
                inserted += 1
        if inserted:
            self._visible_map.sort()
        self._update_empty()

    def _make_item(self, idx: int, rec: TraceRecord) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, rec)
        item.setData(Qt.UserRole + 1, idx)
        item.setSizeHint(QSize(0, self._delegate.row_h))
        item.setToolTip(
            f"{rec.kind.label} · {rec.label}\n"
            f"{rec.absolute_time} · {format_duration(rec.duration_ms)} · {rec.tokens} tok\n"
            f"{rec.source}"
        )
        return item

    def _row_of_record(self, idx: int) -> Optional[int]:
        for row, rec_i in enumerate(self._visible_map):
            if rec_i == idx:
                return row
        return None

    def _restore_selection(self) -> None:
        if self._selected_rec_idx is not None and self._row_of_record(self._selected_rec_idx) is not None:
            row = self._row_of_record(self._selected_rec_idx)
            self._list.setCurrentRow(row if row is not None else -1)
        else:
            self._selected_rec_idx = None
            self._list.setCurrentRow(-1)

    def _on_current_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._visible_map):
            return
        rec_idx = self._visible_map[row]
        self._selected_rec_idx = rec_idx
        self.recordSelected.emit(rec_idx)

    def _update_empty(self) -> None:
        empty = self._list.count() == 0
        self._empty_label.setVisible(empty)
        if empty:
            self._empty_label.setGeometry(self._list.viewport().rect())
        self._update_count()

    def _update_count(self) -> None:
        total = self.total_count
        shown = self.shown_count
        self._count_label.setText(f"{total} 条" if shown == total else f"{shown} / {total} 条")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._empty_label.isVisible():
            self._empty_label.setGeometry(self._list.viewport().rect())
        self._header.update()


class _EmptyHint(QFrame):
    """空态提示（列表无内容时居中显示）。"""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._pal = ThemePalette()
        self._text = "暂无轨迹记录"

    def set_palette(self, pal: ThemePalette) -> None:
        self._pal = pal
        self.update()

    def set_text(self, text: str) -> None:
        self._text = text
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        try:
            f = QFont(self._pal.font_family)
            f.setPixelSize(max(11, self._pal.font_px))
            p.setFont(f)
            p.setPen(QColor(self._pal.text_muted))
            p.drawText(self.rect(), Qt.AlignCenter, self._text)
        finally:
            p.end()


def _fallback_scrollbar(pal: ThemePalette) -> str:
    """主程序样式不可用时的兜底（同一套视觉：细长圆角、无箭头）。"""
    return (
        "QScrollBar:vertical { background: transparent; width: 8px; margin: 0; }"
        f"QScrollBar::handle:vertical {{ background: {pal.q('line', 60)}; border-radius: 4px; min-height: 30px; }}"
        f"QScrollBar::handle:vertical:hover {{ background: {pal.q('line', 95)}; }}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }"
        "QScrollBar:horizontal { background: transparent; height: 8px; margin: 0; }"
        f"QScrollBar::handle:horizontal {{ background: {pal.q('line', 60)}; border-radius: 4px; min-width: 30px; }}"
        "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }"
        "QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: none; }"
    )


def unified_scrollbar(width: int = 8) -> str:
    """主程序统一滚动条样式（``app.utils.design_tokens``），失败回退内联。"""
    try:
        from app.utils.design_tokens import get_unified_scrollbar_style

        return get_unified_scrollbar_style(width)
    except Exception:
        return ""


def _list_qss(pal: ThemePalette) -> str:
    bar = unified_scrollbar(8) or _fallback_scrollbar(pal)
    return (
        "QListWidget { background: transparent; border: none; outline: none; }"
        "QListWidget::item { border: none; background: transparent; }"
        "QListWidget::item:selected { background: transparent; }"
        + bar
    )
