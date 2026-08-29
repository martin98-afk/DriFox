# -*- coding: utf-8 -*-
"""agent_trace.TurnListWidget — 中央条目列表（对齐 DeepSeek Harness）。

实现要点：
- ``QListWidget`` + 自定义 ``QStyledItemDelegate`` 自绘行（无动态 widget 时序坑）。
- **类型徽章**：圆角小块 + 类型色文字 + 12% 透明类型色底（deepseek 风格）。
  徽章宽度按最长标签（ASSISTANT）以 QFontMetrics 实测，杜绝文字被裁。
- **右侧列语义**：TOOL / ASSISTANT 显示精确时长；SYSTEM / USER / CONTEXT
  显示绝对时间（消息类 hook 同秒注入时长的 0ms 满屏没有信息量）。
- **字体跟随系统设置**：ctx.font_size 是「已应用缩放的像素值」，一律
  ``QFont.setPixelSize``（v3 曾误传给 QFont 当磅值 → 字体过大遮挡）。
- **tail 增量化**（v3）：``set_tail`` 只增删尾部 in-flight 行，主列表 items
  不再全量重建 → 修「历史记录一直在刷新」的滚动跳顶/闪烁。
- 颜色一律 hex + :func:`with_alpha` 派生（禁 rgba 字符串，防黑块，坑 P022）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontMetrics, QPainter
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
    TraceRecord,
    format_duration,
    kind_color,
    with_alpha,
)

FILTER_CHIPS = (
    ("all", "全部", None),
    ("system", "系统", EntryKind.SYSTEM),
    ("user", "用户", EntryKind.USER),
    ("context", "上下文", EntryKind.CONTEXT),
    ("assistant", "助手", EntryKind.ASSISTANT),
    ("tool", "工具", EntryKind.TOOL),
)

# 这些类型的行右侧显示精确时长；其余显示绝对时间
DURATION_KINDS = (EntryKind.TOOL, EntryKind.ASSISTANT)

_MONO_FAMILY = "Cascadia Mono"


class _EntryDelegate(QStyledItemDelegate):
    """自绘单条 entry 行（徽章 + 内容 + 右侧时间/时长）。

    列宽 / 行高按当前字体实测（set_base_font 时重算）→ 高 DPI / 大字号
    下不遮挡。
    """

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._colors: Dict[str, Any] = {
            "text": "#C8C8D0",
            "text_secondary": "#8A8A94",
            "text_dim": "#6E6E78",
            "selected": "#7AA2F7",
            "line": "#FFFFFF",
        }
        self._is_dark = True
        self._base_px = 12
        self._row_h = 28
        self._badge_w = 78
        self._meta_w = 78
        self._turn_w = 80
        self._apply_metrics()

    # ── 字体 / 度量 ──

    def _mono_font(self, delta_px: int = 0, bold: bool = False) -> QFont:
        f = QFont(_MONO_FAMILY)
        f.setStyleHint(QFont.Monospace)
        f.setPixelSize(max(9, self._base_px + delta_px))
        f.setBold(bold)
        return f

    def _apply_metrics(self) -> None:
        """按当前字号实测列宽与行高。"""
        bfm = QFontMetrics(self._mono_font(-2, bold=True))
        self._badge_w = bfm.horizontalAdvance("ASSISTANT") + 16
        mfm = QFontMetrics(self._mono_font(-2))
        self._meta_w = max(mfm.horizontalAdvance("00:00:00"), mfm.horizontalAdvance("18m 31s")) + 16
        self._turn_w = mfm.horizontalAdvance("Turn 999") + 12
        self._row_h = max(26, self._base_px + 14)

    def set_base_font(self, font: QFont) -> None:
        px = font.pixelSize()
        if px <= 0:
            px = font.pointSizeF()
            px = int(round(px * 4 / 3)) if px > 0 else 12  # pt → px 兜底
        self._base_px = max(10, min(24, px))
        self._apply_metrics()

    # ── 主题 ──

    def set_colors(self, colors: Dict[str, Any]) -> None:
        if not colors:
            return
        self._is_dark = colors.get("is_dark", True)
        line = QColor("#FFFFFF") if self._is_dark else QColor("#000000")
        self._colors.update(
            {
                "text": colors.get("text_primary", self._colors["text"]),
                "text_secondary": colors.get("text_secondary", self._colors["text_secondary"]),
                "text_dim": colors.get("text_dim") or self._colors["text_dim"],
                "selected": colors.get("accent") or self._colors["selected"],
                "line": line,
            }
        )

    # ── 绘制 ──

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), self._row_h)

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        rec: Optional[TraceRecord] = index.data(Qt.UserRole)
        if rec is None:
            return

        rect = option.rect
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        c = self._colors

        painter.save()
        try:
            painter.setRenderHint(QPainter.TextAntialiasing, True)

            # ── 行背景（透明度全部 with_alpha 派生，杜绝 rgba 字符串黑块）──
            if selected:
                painter.fillRect(rect, with_alpha(QColor(c["selected"]), 26))
                painter.fillRect(rect.x(), rect.y(), 3, rect.height(), QColor(c["selected"]))
            elif hovered:
                painter.fillRect(rect, with_alpha(QColor(c["line"]), 10))

            # turn 分组线（真实 USER 行的上边线）
            if rec.meta.get("turn_start"):
                painter.setPen(with_alpha(QColor(c["selected"]), 120))
                painter.drawLine(rect.x() + 3, rect.y(), rect.right(), rect.y())

            # 行分隔线
            painter.setPen(with_alpha(QColor(c["line"]), 18))
            painter.drawLine(rect.x() + 3, rect.bottom(), rect.right(), rect.bottom())

            main_font = self._mono_font()
            small_font = self._mono_font(-2)

            # ① 类型徽章：固定宽（按 ASSISTANT 实测）+ 类型色底/字
            color = kind_color(rec.kind)
            badge_w = self._badge_w
            badge = QRect(12, rect.y() + (rect.height() - 18) // 2, badge_w, 18)
            painter.setPen(Qt.NoPen)
            painter.setBrush(with_alpha(color, 30))
            painter.drawRoundedRect(badge, 3, 3)
            painter.setFont(small_font)
            painter.setPen(color)
            painter.drawText(badge, Qt.AlignCenter, rec.kind.label)

            # ② 主文本：内容首行（TOOL 行含 tool 名前缀）
            col_main = 12 + badge_w + 10
            right_reserve = self._meta_w + 14
            is_turn_start = bool(rec.meta.get("turn_start")) and rec.turn_no > 0
            if is_turn_start:
                right_reserve += self._turn_w  # 预留 Turn N 标注位，防重叠
            avail_w = max(40, rect.width() - col_main - right_reserve)
            painter.setFont(main_font)
            fm = QFontMetrics(main_font)
            main_text = _row_main_text(rec)
            painter.setPen(QColor(c["text"]))
            painter.drawText(
                QRect(col_main, rect.y(), avail_w, rect.height()),
                Qt.AlignVCenter | Qt.AlignLeft,
                fm.elidedText(main_text, Qt.ElideRight, avail_w),
            )

            # ③ 右侧列：TOOL/ASSISTANT 显示时长（错误红/进行中金/完成暗）；
            #    消息类显示绝对时间
            painter.setFont(small_font)
            if rec.kind in DURATION_KINDS:
                if rec.is_error:
                    painter.setPen(QColor("#F7768E"))
                elif rec.is_pending:
                    painter.setPen(QColor("#E0AF68"))
                else:
                    painter.setPen(QColor(c["text_dim"]))
                # 0 = 无精确耗时数据（历史回放）→ 显示 "-" 而非误导性 "0 ms"
                meta_text = format_duration(rec.duration_ms) if rec.duration_ms > 0 else "-"
            else:
                painter.setPen(QColor(c["text_dim"]))
                meta_text = rec.absolute_time
            painter.drawText(
                QRect(rect.width() - self._meta_w - 14, rect.y(), self._meta_w, rect.height()),
                Qt.AlignVCenter | Qt.AlignRight,
                meta_text,
            )

            # ④ turn 标注（turn 分组行右侧、时长列左边）
            if is_turn_start:
                painter.setFont(small_font)
                painter.setPen(with_alpha(QColor(c["selected"]), 210))
                painter.drawText(
                    QRect(rect.width() - right_reserve, rect.y(), self._turn_w - 6, rect.height()),
                    Qt.AlignVCenter | Qt.AlignRight,
                    f"Turn {rec.turn_no}",
                )
        finally:
            painter.restore()


def _row_main_text(rec: TraceRecord) -> str:
    """行主文本：TOOL 行带 tool 名前缀（对齐 deepseek `skill {"name":...}`）；其余为内容首行。"""
    if rec.kind == EntryKind.TOOL:
        return f"{rec.label} {rec.preview}".strip()
    return rec.preview


class TurnListWidget(QWidget):
    """中央列表：过滤条 + QListWidget 条目列表（+ in-flight 尾巴）。"""

    recordSelected = pyqtSignal(int)  # record 索引（visible_records 空间）

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._records: List[TraceRecord] = []
        self._tail: List[TraceRecord] = []
        self._visible_map: List[int] = []  # 可见行 → record 索引
        self._selected_rec_idx: Optional[int] = None
        self._filter_kind: Optional[EntryKind] = None
        self._filter_kind_label: str = "all"
        self._search_text: str = ""
        self._delegate = _EntryDelegate(self)
        self._build_ui()

    # ──────────────────── 搭建 ────────────────────

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 过滤条：类型 chips + 结果计数
        self._bar = QFrame(self)
        self._bar.setFixedHeight(36)
        self._bar.setObjectName("agentTraceFilterBar")
        bar = QHBoxLayout(self._bar)
        bar.setContentsMargins(10, 0, 12, 0)
        bar.setSpacing(4)
        self._chip_group = QButtonGroup(self)
        self._chip_group.setExclusive(True)
        for key, label, kind in FILTER_CHIPS:
            btn = QPushButton(label, self._bar)
            btn.setCheckable(True)
            btn.setFixedHeight(24)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setChecked(key == "all")
            btn.clicked.connect(lambda _=False, k=key: self._on_chip(k))
            self._chip_group.addButton(btn)
            bar.addWidget(btn)
        bar.addStretch(1)
        self._count_label = QLabel(self._bar)
        bar.addWidget(self._count_label)
        outer.addWidget(self._bar)

        self._list = QListWidget(self)
        self._list.setItemDelegate(self._delegate)
        self._list.setUniformItemSizes(True)
        self._list.setSelectionMode(QListWidget.SingleSelection)
        self._list.setFrameShape(QFrame.NoFrame)
        self._list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self._list.currentRowChanged.connect(self._on_current_row_changed)
        outer.addWidget(self._list, 1)

        # 空态提示（records+tail 全空时显示）
        self._empty_label = QLabel("暂无轨迹 — 发送一条消息后这里会实时出现", self._list)
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._empty_label.hide()

    def _on_chip(self, key: str) -> None:
        self._filter_kind_label = key
        self._filter_kind = dict((k, kind) for k, _l, kind in FILTER_CHIPS).get(key)
        self._refilter()

    # ──────────────────── 公开 API ────────────────────

    @property
    def _row_h(self) -> int:
        return self._delegate._row_h

    def set_records(self, records: List[TraceRecord]) -> None:
        """整体重置（保留过滤与选中）。"""
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
        """v3 增量化：只调整尾部 in-flight 行，主列表 items 不动、滚动不跳。

        tail 行固定排在可见 records 段之后，无选中状态（NoItemFlags）。
        """
        old_tail = self._tail
        self._tail = list(tail)
        old_n, new_n = len(old_tail), len(self._tail)

        # 删多余的旧行
        if new_n < old_n:
            for _ in range(old_n - new_n):
                if self._list.count() > len(self._visible_map):
                    self._list.takeItem(self._list.count() - 1)
        # 补缺的新行 / 刷新既有行内容
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
                item.setSizeHint(QSize(0, self._row_h))
                self._list.addItem(item)
        self._update_empty()
        self._update_count()
        self._list.viewport().update()

    def set_search(self, text: str) -> None:
        self._search_text = (text or "").strip().lower()
        self._refilter()

    def repaint_pending(self) -> None:
        """心跳重绘（pending 行时长实时计算，刷 viewport 即可）。"""
        self._list.viewport().update()

    def clear_selection(self) -> None:
        self._selected_rec_idx = None
        self._list.setCurrentRow(-1)

    def set_colors(self, colors: Dict[str, Any]) -> None:
        self._delegate.set_colors(colors)
        if not colors:
            return
        is_dark = colors.get("is_dark", True)
        secondary = colors.get("text_secondary", "#8A8A94")
        border = colors.get("border", "#333333")
        accent = colors.get("accent", "#7AA2F7")
        line_c = "255,255,255" if is_dark else "0,0,0"
        fs = self._delegate._base_px

        self._bar.setStyleSheet(
            f"#agentTraceFilterBar {{ background: transparent; border-bottom: 1px solid {border}; }}"
            f"QPushButton {{ background: transparent; color: {secondary}; border: 1px solid transparent;"
            f"  border-radius: 4px; padding: 0 10px; font-size: {max(10, fs - 1)}px; }}"
            f"QPushButton:hover {{ background: rgba({line_c},0.08); }}"
            f"QPushButton:checked {{ color: {accent}; border: 1px solid {accent}; background: transparent; }}"
        )
        self._count_label.setStyleSheet(f"color: {secondary}; font-size: {max(10, fs - 2)}px;")
        self._empty_label.setStyleSheet(f"color: {secondary}; font-size: {fs}px; background: transparent;")
        self._list.setStyleSheet(
            "QListWidget { background: transparent; border: none; outline: none; }"
            "QListWidget::item { border: none; }"
            "QListWidget::item:selected { background: transparent; }"
        )
        self._list.viewport().update()

    def _apply_font(self, font: QFont) -> None:
        """字体跟随系统设置（pixelSize）→ 重算列宽/行高并刷新既有行高。"""
        self._delegate.set_base_font(font)
        rh = QSize(0, self._row_h)
        for i in range(self._list.count()):
            item = self._list.item(i)
            if item is not None:
                item.setSizeHint(rh)
        self._list.viewport().update()

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
        self._update_count()

    def select_record(self, idx: int) -> None:
        """按 record 索引选中（必要时滚动到可见）。"""
        row = self._row_of_record(idx)
        if row is not None:
            self._list.setCurrentRow(row)

    # ──────────────────── 内部 ────────────────────

    def _match(self, rec: TraceRecord) -> bool:
        if self._filter_kind is not None and rec.kind != self._filter_kind:
            return False
        if self._search_text:
            hay = f"{rec.label}\n{rec.preview}\n{rec.raw}".lower()
            if self._search_text not in hay:
                return False
        return True

    def _refilter(self) -> None:
        self._visible_map = [i for i, r in enumerate(self._records) if self._match(r)]
        self._rebuild_items()
        self._restore_selection()

    def _rebuild_items(self) -> None:
        self._list.clear()
        for i in self._visible_map:
            self._list.addItem(self._make_item(i, self._records[i]))
        # in-flight 尾巴（固定追加在末尾，不可选中）
        for rec in self._tail:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, rec)
            item.setFlags(Qt.NoItemFlags)
            item.setSizeHint(QSize(0, self._row_h))
            self._list.addItem(item)
        self._update_empty()
        self._update_count()

    def _append_if_visible(self, record_indices: List[int]) -> None:
        tail_start_row = self._list.count() - len(self._tail)
        inserted = 0
        for i in record_indices:
            if 0 <= i < len(self._records) and self._match(self._records[i]):
                row = tail_start_row + inserted
                self._list.insertItem(row, self._make_item(i, self._records[i]))
                self._visible_map.append(i)
                self._visible_map.sort()
                inserted += 1
        self._update_empty()
        self._update_count()

    def _make_item(self, idx: int, rec: TraceRecord) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, rec)
        item.setData(Qt.UserRole + 1, idx)
        item.setSizeHint(QSize(0, self._row_h))
        item.setToolTip(f"{rec.kind.label} · {rec.label}\n{rec.source}")
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

    def _update_count(self) -> None:
        total = len(self._records) + len(self._tail)
        shown = len(self._visible_map) + len(self._tail)
        if shown == total:
            self._count_label.setText(f"{total} 条")
        else:
            self._count_label.setText(f"{shown} / {total} 条")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._empty_label.isVisible():
            self._empty_label.setGeometry(self._list.viewport().rect())
