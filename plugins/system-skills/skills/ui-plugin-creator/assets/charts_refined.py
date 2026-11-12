# -*- coding: utf-8 -*-
"""精修图表资产 —— 柱状图 / 折线图，QPainter 自绘，可直接复制进插件。

为什么是「资产」而不是文档片段：这两张图把踩过的坑都固化在代码里了，
复制即得，不必每次重写再踩一遍：

- 左轴留白按 ``QFontMetrics`` 实测最宽刻度，不写死 44/52（大数值会被裁）
- 绘制与 hover 命中共用同一个 ``_margin_left()``（否则高亮错位）
- 刻度用 ``fmt_k``（148.8M），悬停给完整数字
- 悬停说明用自绘实底浮层，不用原生 QToolTip（深色主题读不清）
- 空列表 / 全零 / 单点 / 超大值都有专门分支
- 字号全部来自 ``colors["font_size"]``，宿主放大字号时留白跟着长

契约（与 widgets-charts.md 的模板一致，方便互相替换）::

    chart = BarChartWidget(title="7 日用量")
    chart.set_colors(colors)          # dict，见 _default_colors()
    chart.set_data([("11-06", 0), ("11-07", 1_200_000)], costs=[0.1])
    chart.setMinimumHeight(220)       # 放进卡片布局

``colors`` 键：font_family / font_size / text / text_secondary / grid /
bar_fill / bar_border / line。缺键自动兜底。

自检（无需宿主，离屏渲染出三张图到当前目录）::

    python charts_refined.py
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from PyQt5.QtCore import QPoint, QRectF, Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

# ============================================================
# 通用工具
# ============================================================

def fmt_k(value: Optional[float]) -> str:
    """刻度用短格式：0 / 1.2k / 37.2M / 1.1B。"""
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    sign = "-" if n < 0 else ""
    n = abs(n)
    for limit, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if n >= limit:
            return f"{sign}{n / limit:.1f}{suffix}".replace(f".0{suffix}", suffix)
    return f"{sign}{int(round(n))}"


def fmt_full(value: float) -> str:
    """悬停给完整数字，带千分位。"""
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return "0"


def _default_colors() -> Dict[str, Any]:
    return {
        "font_family": "Microsoft YaHei",
        "font_size": 14,
        "text": "#e5e7eb",
        "text_secondary": "#9ca3af",
        "grid": "#3f3f46",
        "bar_fill": "#c8963e",
        "bar_border": "#8a6428",
        "line": "#22d3ee",
        "surface": "#1f1f23",
    }


# ============================================================
# 自绘悬停浮层（替代原生 QToolTip）
# ============================================================

class ChartTooltip(QWidget):
    """实底浮层。原生 QToolTip 在深色主题下透明底 + 白字，读不清。"""

    def __init__(self) -> None:
        super().__init__(None, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._label = QLabel(self)
        self._label.setContentsMargins(10, 7, 10, 7)
        self._box = QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.addWidget(self._label)

    def show_text(self, global_pos: QPoint, text: str, colors: Dict[str, Any]) -> None:
        family = colors.get("font_family", "Microsoft YaHei")
        size = max(10, int(colors.get("font_size", 14)) - 3)
        self._label.setFont(QFont(family, size))
        self._label.setStyleSheet(
            f"background:{colors.get('surface', '#1f1f23')};"
            f"color:{colors.get('text', '#e5e7eb')};"
            "border-radius:6px;"
        )
        self._label.setText(text)
        self.adjustSize()
        self.move(global_pos + QPoint(14, 14))
        self.show()
        # show 之后按最终尺寸再校正一次：首帧尺寸为 0 会飘到屏幕左上角
        self.adjustSize()
        self.move(global_pos + QPoint(14, 14))

    def hide_tip(self) -> None:
        self.hide()


class _ChartBase(QWidget):
    """刻度 / 留白 / 悬停的公共实现。子类只画主体。"""

    def __init__(self, title: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._title = title
        self._labels: List[str] = []
        self._values: List[int] = []
        self._colors = _default_colors()
        self._hover = -1
        self._tip = ChartTooltip()
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(180)

    # ── 数据与样式 ──
    def set_colors(self, colors: Dict[str, Any]) -> None:
        self._colors = {**_default_colors(), **(colors or {})}
        self.update()

    def set_data(self, data: Sequence[Tuple[str, int]]) -> None:
        self._labels = [str(label) for label, _ in data]
        self._values = [max(0, int(v)) for _, v in data]
        self._hover = -1
        self.update()

    # ── 度量 ──
    def _font(self, delta: int = 0, bold: bool = False) -> QFont:
        size = max(8, int(self._colors.get("font_size", 14)) + delta)
        font = QFont(self._colors.get("font_family", "Microsoft YaHei"), size)
        font.setBold(bold)
        return font

    def _tick_texts(self, ticks: int) -> List[str]:
        top = max(self._values or [1]) or 1
        values = [top * i / ticks for i in range(ticks + 1)]
        if top < 10:
            # 小值不能用 k/M 四舍五入：0.25 与 0.5 会双双变成 "0"，刻度重复
            out: List[str] = []
            for v in values:
                text = f"{v:.1f}".rstrip("0").rstrip(".")
                out.append(text or "0")
            return out
        return [fmt_k(v) for v in values]

    def _margin_left(self) -> int:
        """按最宽刻度实测算左边距。固定值会把 148.8M 这类值裁出可视区。"""
        fm = QFontMetrics(self._font(-6))
        widest = max((fm.horizontalAdvance(t) for t in self._tick_texts(4)), default=0)
        base = 52 if self.width() >= 420 else 44
        return max(base, widest + 10)

    def _plot(self, top: int = 30, bottom: int = 34) -> QRectF:
        left = self._margin_left()
        right = 12 if self.width() >= 400 else 8
        width = max(10, self.width() - left - right)
        height = max(10, self.height() - top - bottom)
        return QRectF(left, top, width, height)

    # ── 绘制辅助 ──
    def _paint_frame(self, painter: QPainter, plot: QRectF, ticks: int = 4) -> None:
        colors = self._colors
        painter.setFont(self._font(-2, bold=True))
        painter.setPen(QPen(QColor(colors.get("text", "#e5e7eb"))))
        painter.drawText(
            QRectF(plot.left(), 2, plot.width(), 22),
            Qt.AlignLeft | Qt.AlignVCenter,
            self._title,
        )
        painter.setFont(self._font(-6))
        for i in range(ticks + 1):
            y = plot.bottom() - plot.height() * i / ticks
            painter.setPen(QPen(QColor(colors.get("grid", "#3f3f46")), 1, Qt.DashLine))
            painter.drawLine(QPoint(int(plot.left()), int(y)), QPoint(int(plot.right()), int(y)))
            painter.setPen(QPen(QColor(colors.get("text_secondary", "#9ca3af"))))
            painter.drawText(
                QRectF(0, y - 9, plot.left() - 8, 18),
                Qt.AlignRight | Qt.AlignVCenter,
                self._tick_texts(ticks)[i],
            )

    def _paint_labels(self, painter: QPainter, plot: QRectF) -> None:
        n = len(self._labels)
        if not n:
            return
        slot = plot.width() / n
        painter.setFont(self._font(-6))
        painter.setPen(QPen(QColor(self._colors.get("text_secondary", "#9ca3af"))))
        stride = 1 if n <= 8 else max(1, n // 8)
        for i in range(0, n, stride):
            cx = plot.left() + slot * (i + 0.5)
            painter.drawText(
                QRectF(cx - slot, plot.bottom() + 4, slot * 2, 18),
                Qt.AlignCenter,
                self._labels[i],
            )

    def _paint_empty(self, painter: QPainter, plot: QRectF) -> None:
        painter.setFont(self._font(-1))
        painter.setPen(QPen(QColor(self._colors.get("text_secondary", "#9ca3af"))))
        painter.drawText(plot, Qt.AlignCenter, "暂无数据")

    def _hover_index(self, x: float, plot: QRectF) -> int:
        n = len(self._values)
        if not n:
            return -1
        slot = plot.width() / n
        idx = int((x - plot.left()) // slot)
        return idx if 0 <= idx < n else -1

    def _show_tip(self, idx: int, global_pos: QPoint) -> None:
        if idx < 0 or idx >= len(self._values):
            self._tip.hide_tip()
            return
        text = f"{self._labels[idx]}\n{fmt_full(self._values[idx])}"
        self._tip.show_text(global_pos, text, self._colors)

    # ── 事件 ──
    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        idx = self._hover_index(event.pos().x(), self._plot())
        if idx != self._hover:
            self._hover = idx
            self.update()
        self._show_tip(idx, event.globalPos())
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        self._tip.hide_tip()
        self.update()
        super().leaveEvent(event)

    def hideEvent(self, event) -> None:  # noqa: N802
        self._tip.hide_tip()
        super().hideEvent(event)


# ============================================================
# 柱状图（可叠一条成本折线）
# ============================================================

class BarChartWidget(_ChartBase):
    """柱 + 可选折线。折线按自身最大值独立缩放。"""

    def __init__(self, title: str = "", parent: Optional[QWidget] = None) -> None:
        super().__init__(title, parent)
        self._costs: List[float] = []

    def set_data(self, data: Sequence[Tuple[str, int]], costs: Optional[Sequence[float]] = None) -> None:
        super().set_data(data)
        self._costs = [float(c or 0) for c in (costs or [])]
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        plot = self._plot()
        self._paint_frame(painter, plot)
        n = len(self._values)
        if not n:
            self._paint_empty(painter, plot)
            return

        top = max(self._values) or 1
        slot = plot.width() / n
        # 单点时柱宽不撑满整宽，也不细成一条线
        bar_w = max(4.0, min(26.0, slot * (0.42 if n > 1 else 0.18)))
        base = QColor(self._colors.get("bar_fill", "#c8963e"))
        border = QColor(self._colors.get("bar_border", "#8a6428"))

        for i, value in enumerate(self._values):
            cx = plot.left() + slot * (i + 0.5)
            h = plot.height() * value / top
            bar = QRectF(cx - bar_w / 2, plot.bottom() - h, bar_w, h)
            grad = QLinearGradient(bar.topLeft(), bar.bottomLeft())
            grad.setColorAt(0.0, base.lighter(112) if i == self._hover else base)
            grad.setColorAt(1.0, base.darker(118))
            painter.setPen(QPen(border, 1))
            painter.setBrush(QBrush(grad))
            # 零值柱给 1px 底线，否则看不出这一列存在
            if h <= 0.5:
                bar = QRectF(bar.left(), bar.bottom() - 1, bar.width(), 1)
            painter.drawRoundedRect(bar, min(4.0, bar_w / 2), min(4.0, bar_w / 2))

        if self._costs and len(self._costs) == n:
            top_cost = max(self._costs) or 1.0
            pts = [
                QPoint(
                    int(plot.left() + slot * (i + 0.5)),
                    int(plot.bottom() - plot.height() * 0.88 * (c / top_cost)),
                )
                for i, c in enumerate(self._costs)
            ]
            pen = QPen(QColor(self._colors.get("line", "#22d3ee")), 2)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            for i in range(len(pts) - 1):
                painter.drawLine(pts[i], pts[i + 1])
            painter.setBrush(QBrush(QColor(self._colors.get("line", "#22d3ee"))))
            painter.setPen(Qt.NoPen)
            for p in pts:
                painter.drawEllipse(p, 2.6, 2.6)

        self._paint_labels(painter, plot)


# ============================================================
# 折线图（带面积填充）
# ============================================================

class LineChartWidget(_ChartBase):
    """折线 + 面积。顶部留 18% 余量，避免峰值点贴边。"""

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        plot = self._plot(top=30, bottom=34)
        self._paint_frame(painter, plot)
        n = len(self._values)
        if not n:
            self._paint_empty(painter, plot)
            return

        top = (max(self._values) or 1) * 1.18
        slot = plot.width() / n
        line = QColor(self._colors.get("line", "#22d3ee"))
        pts = [
            (plot.left() + slot * (i + 0.5), plot.bottom() - plot.height() * v / top)
            for i, v in enumerate(self._values)
        ]

        path = QPainterPath()
        path.moveTo(pts[0][0], plot.bottom())
        for x, y in pts:
            path.lineTo(x, y)
        path.lineTo(pts[-1][0], plot.bottom())
        path.closeSubpath()
        fill = QLinearGradient(plot.topLeft(), plot.bottomLeft())
        fill.setColorAt(0.0, QColor(line.red(), line.green(), line.blue(), 110))
        fill.setColorAt(1.0, QColor(line.red(), line.green(), line.blue(), 12))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(fill))
        painter.drawPath(path)

        pen = QPen(line, 2)
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        for i in range(len(pts) - 1):
            painter.drawLine(QPoint(int(pts[i][0]), int(pts[i][1])), QPoint(int(pts[i + 1][0]), int(pts[i + 1][1])))

        painter.setBrush(QBrush(line))
        painter.setPen(Qt.NoPen)
        for i, (x, y) in enumerate(pts):
            painter.drawEllipse(QPoint(int(x), int(y)), 3.4 if i == self._hover else 2.4, 3.4 if i == self._hover else 2.4)

        self._paint_labels(painter, plot)


# ============================================================
# 自检：离屏渲染三组极端数据（不依赖宿主）
# ============================================================

def _selftest() -> int:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtGui import QImage

    app = __import__("PyQt5.QtWidgets", fromlist=["QApplication"]).QApplication([])
    dark = _default_colors()
    dark["surface"] = "#17171b"
    light = {
        **_default_colors(),
        "text": "#1f2328",
        "text_secondary": "#6b7280",
        "grid": "#d4d4d8",
        "surface": "#ffffff",
    }
    cases = {
        "normal": (
            [("11-06", 0), ("11-07", 3_100_000), ("11-08", 0), ("11-09", 37_600_000),
             ("11-10", 8_800_000), ("11-11", 0), ("11-12", 148_800_000)],
            [0, .2, 0, 2.4, .5, 0, 9.1],
        ),
        "all_zero": ([("a", 0), ("b", 0), ("c", 0)], None),
        "single": ([("only", 12_345)], None),
    }
    for theme, colors, bg in (("dark", dark, "#17171b"), ("light", light, "#ffffff")):
        for name, (data, costs) in cases.items():
            for cls in (BarChartWidget, LineChartWidget):
                w = cls(title=f"{cls.__name__} · {name} · {theme}")
                w.set_colors(colors)
                # 控件默认调色板会把自己的浅色背景画到画布上，盖掉 fill() 的底色
                w.setStyleSheet(f"background:{bg};")
                w.resize(900, 300)
                if cls is BarChartWidget:
                    w.set_data(data, costs)
                else:
                    w.set_data(data)
                img = QImage(900, 300, QImage.Format_ARGB32)
                img.fill(QColor(bg))
                painter = QPainter(img)
                w.render(painter)
                painter.end()
                img.save(f"/tmp/chart_{theme}_{cls.__name__}_{name}.png")
    print("saved /tmp/chart_{dark,light}_{Bar,Line}ChartWidget_{normal,all_zero,single}.png")
    del app
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
