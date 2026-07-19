# -*- coding: utf-8 -*-
"""图表组件 — 柱状图、折线图、统计卡片、项目分布图

设计约束（闭包）：
- 不导入 app.core 或 app.widgets 内部的任何模块
- 所有绘图直接通过 PyQt5 完成
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolTip,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import IconWidget, isDarkTheme


# ── 主题色 → 图表色转换 ────────────────────────────────


def _chart_colors() -> dict:
    """默认 fallback 配色（无上下文时使用）"""
    dark = isDarkTheme()
    if dark:
        text_color = QColor(255, 255, 255, 200)
        text_secondary = QColor(255, 255, 255, 150)
    else:
        text_color = QColor(30, 30, 30, 220)
        text_secondary = QColor(80, 80, 80, 200)
    return {
        "bar_fill": QColor(98, 160, 234, 200),
        "bar_border": QColor(98, 160, 234),
        "line": QColor(80, 227, 194),
        "line_fill": QColor(80, 227, 194, 60),
        "point": QColor(80, 227, 194),
        "grid": QColor(255, 255, 255, 30),
        "text": text_color,
        "text_secondary": text_secondary,
        "card_bg": QColor(255, 255, 255, 20),
        "accent": QColor(98, 160, 234),
        "accent_fill": QColor(98, 160, 234, 60),
        "warning": QColor(255, 193, 7, 200),
        "success": QColor(80, 227, 194, 200),
        "font_family": "Microsoft YaHei",
        "font_size": 14,
    }


def _make_chart_colors_from_context(ctx: dict) -> dict:
    """将 context 中的主题 colors 映射为图表组件可用的 QColor 字典

    Args:
        ctx: UIPluginRegistry 注入的上下文（含 colors / is_dark 等字段）

    Returns:
        带 QColor 值的图表配色字典，与 _chart_colors() 输出格式一致
    """
    raw = ctx.get("colors", {})
    is_dark = ctx.get("is_dark", True)

    def _qcolor(key: str, fallback_light: str, fallback_dark: str) -> QColor:
        val = raw.get(key, "")
        if val:
            return QColor(val)
        return QColor(fallback_dark if is_dark else fallback_light)

    accent = _qcolor("accent", "#2878dc", "#62a0ea")
    success = _qcolor("success", "#00a888", "#50e3c2")
    if is_dark:
        text_color = QColor(255, 255, 255, 200)
        text_secondary = QColor(255, 255, 255, 150)
    else:
        text_color = QColor(30, 30, 30, 220)
        text_secondary = QColor(80, 80, 80, 200)
    return {
        "bar_fill": accent.lighter(110),
        "bar_border": accent,
        "line": success,
        "line_fill": QColor(success.red(), success.green(), success.blue(), 60),
        "point": success,
        "grid": _qcolor("border", "#cccccc80", "#0000001e"),
        "text": text_color,
        "text_secondary": text_secondary,
        "card_bg": _qcolor("card_bg", "#00000014", "#ffffff14"),
        "accent": accent,
        "accent_fill": QColor(accent.red(), accent.green(), accent.blue(), 60),
        "warning": _qcolor("accent_warm", "#f59e0b", "#ffc107"),
        "success": success,
        "font_family": ctx.get("font_family", "Microsoft YaHei"),
        "font_size": ctx.get("font_size", 14),
    }


# ── 工具函数 ──────────────────────────────────────────────


def _format_number(n: int) -> str:
    """格式化大数字，如 1234 → '1.2k'"""
    if n >= 1000000:
        return f"{n / 1000000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _format_pct(v: float) -> str:
    """格式化百分比"""
    return f"{v * 100:.0f}%"


def _parse_mmdd(date_str: str) -> datetime:
    """将 'mm-dd' 转 datetime，自动判断跨年边界（数据来自近 14 天）"""
    try:
        parts = date_str.split("-")
        month, day = int(parts[0]), int(parts[1])
        now = datetime.now()
        dt = datetime(now.year, month, day)
        if dt > now + timedelta(days=30):
            dt = datetime(now.year - 1, month, day)
        return dt
    except ValueError, IndexError:
        return datetime.now()


def _short_weekday(date_str: str) -> str:
    """将 '01-15' 转换为 '01-15\\n周一' 格式"""
    try:
        dt = _parse_mmdd(date_str)
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        wd = weekdays[dt.weekday()]
        return f"{date_str}\n{wd}"
    except ValueError:
        return date_str


# ══════════════════════════════════════════════════════════
# 柱状图组件
# ══════════════════════════════════════════════════════════


class _BarChartWidget(QWidget):
    """柱状图组件 — 用于展示每日会话数量"""

    def __init__(self, title: str, data: List[Tuple[str, int]], color_key: str = "bar_fill", parent=None):
        super().__init__(parent)
        self._title = title
        self._data = data
        self._color_key = color_key
        self._colors = _chart_colors()
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self._hovered_index = -1

    def set_data(self, data: List[Tuple[str, int]]):
        self._data = data
        self.update()

    def set_colors(self, colors: dict):
        self._colors = colors
        self.update()

    def mouseMoveEvent(self, event):
        if not self._data:
            self._hovered_index = -1
            self.update()
            super().mouseMoveEvent(event)
            return

        w = self.width()
        margin_left = 52 if w >= 420 else 44
        chart_w = w - margin_left - (12 if w >= 400 else 8)

        if chart_w < 10:
            self._hovered_index = -1
            self.update()
            super().mouseMoveEvent(event)
            return

        n = len(self._data)
        bar_spacing = chart_w / n
        pos = event.pos()

        i = int((pos.x() - margin_left) / bar_spacing)
        i = max(0, min(i, n - 1))

        bar_x = margin_left + i * bar_spacing
        if bar_x <= pos.x() <= bar_x + bar_spacing:
            self._hovered_index = i
        else:
            self._hovered_index = -1

        self.update()

        if self._hovered_index >= 0:
            label, value = self._data[self._hovered_index]
            try:
                parts = label.split("-")
                if len(parts) == 2:
                    dt = _parse_mmdd(label)
                    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                    date_str = dt.strftime("%m-%d") + f" ({weekdays[dt.weekday()]})"
                else:
                    date_str = label
            except ValueError, IndexError:
                date_str = label
            QToolTip.showText(event.globalPos(), f"📊 {date_str}\n会话数: {value}", self)

        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hovered_index = -1
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        if not self._data:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        colors = self._colors
        font_family = colors.get("font_family", "Microsoft YaHei")
        base_font_size = colors.get("font_size", 14)
        w = self.width()
        h = self.height()

        margin_left = 52 if w >= 420 else 44
        margin_right = 12 if w >= 400 else 8
        margin_top = 34
        margin_bottom = 48 if w >= 400 else 40

        chart_w = w - margin_left - margin_right
        chart_h = h - margin_top - margin_bottom

        if chart_w < 10 or chart_h < 10:
            painter.end()
            return

        # ── 标题 ──
        title_size = max(round(base_font_size * 10 / 14), 8)
        painter.setPen(colors["text"])
        title_font = QFont(font_family, title_size, QFont.Bold)
        painter.setFont(title_font)
        painter.drawText(QRectF(margin_left, 4, chart_w, 22), Qt.AlignLeft | Qt.AlignVCenter, self._title)

        # ── 数据范围 ──
        values = [v for _, v in self._data]
        max_val = max(values) if values else 1
        if max_val == 0:
            max_val = 1

        # ── Y 轴 ──
        painter.setPen(colors["grid"])
        y_ticks = 4
        tick_font_size = max(round(base_font_size * 8 / 14), 7)
        for i in range(y_ticks + 1):
            y = margin_top + chart_h * (1 - i / y_ticks)
            painter.drawLine(QPointF(margin_left, y), QPointF(w - margin_right, y))
            val = int(max_val * i / y_ticks)
            painter.setPen(colors["text_secondary"])
            tick_font = QFont(font_family, tick_font_size)
            painter.setFont(tick_font)
            painter.drawText(
                QRectF(2, y - 12, margin_left - 8, 24),
                Qt.AlignRight | Qt.AlignVCenter,
                str(val),
            )
            painter.setPen(colors["grid"])

        # ── 柱状图 ──
        n = len(self._data)
        if n == 0:
            painter.end()
            return

        bar_width = chart_w / n * (0.65 if w >= 400 else 0.55)
        bar_spacing = chart_w / n

        bar_color = colors.get(self._color_key, colors["bar_fill"])
        border_color = colors.get(self._color_key.replace("fill", "border"), colors["bar_border"])

        x_tick_size = max(round(base_font_size * 7 / 14), 6)
        val_font_size = max(round(base_font_size * 9 / 14), 7)

        for i, (label, value) in enumerate(self._data):
            x = margin_left + i * bar_spacing + (bar_spacing - bar_width) / 2
            bar_h = (value / max_val) * chart_h if max_val > 0 else 0
            y = margin_top + chart_h - bar_h

            rect = QRectF(x, y, bar_width, max(bar_h, 0))
            path = QPainterPath()
            path.addRoundedRect(rect, 3, 3)
            if i == self._hovered_index:
                hover_color = QColor(bar_color).lighter(130)
                painter.fillPath(path, hover_color)
                painter.setPen(QPen(QColor(border_color).lighter(150), 2))
            else:
                painter.fillPath(path, bar_color)
                painter.setPen(QPen(border_color, 1))
            painter.drawPath(path)

            # X 轴标签
            painter.setPen(colors["text_secondary"])
            tick_font = QFont(font_family, x_tick_size)
            painter.setFont(tick_font)
            try:
                parts = label.split("-")
                if len(parts) == 2:
                    dt = _parse_mmdd(label)
                    wd = dt.weekday()
                    if wd in (0, 5, 6):
                        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                        display_label = f"{parts[0]}-{parts[1]}\n{weekdays[wd]}"
                    else:
                        display_label = f"{parts[0]}-{parts[1]}"
                else:
                    display_label = label
            except ValueError, IndexError:
                display_label = label

            painter.drawText(
                QRectF(x - bar_spacing / 2, h - margin_bottom + 4, bar_spacing, 36),
                Qt.AlignCenter,
                display_label,
            )

            if value > 0 and (w >= 350 or value >= max_val * 0.3):
                painter.setPen(colors["text"])
                val_font = QFont(font_family, val_font_size, QFont.Bold)
                painter.setFont(val_font)
                label_y = y - 20
                if label_y < margin_top:
                    label_y = y + 4
                if label_y + 16 > h - margin_bottom:
                    label_y = y - 12
                painter.drawText(
                    QRectF(x, label_y, bar_width, 16),
                    Qt.AlignCenter,
                    str(value),
                )

        painter.end()


# ══════════════════════════════════════════════════════════
# 折线图组件
# ══════════════════════════════════════════════════════════


class _LineChartWidget(QWidget):
    """折线图组件 — 用于展示消息量 / token 趋势"""

    def __init__(self, title: str, data: List[Tuple[str, int]], color_key: str = "line", parent=None):
        super().__init__(parent)
        self._title = title
        self._data = data
        self._color_key = color_key
        self._colors = _chart_colors()
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self._hovered_index = -1

    def set_data(self, data: List[Tuple[str, int]]):
        self._data = data
        self.update()

    def set_colors(self, colors: dict):
        self._colors = colors
        self.update()

    def mouseMoveEvent(self, event):
        if not self._data or len(self._data) < 1:
            self._hovered_index = -1
            self.update()
            super().mouseMoveEvent(event)
            return

        w = self.width()
        margin_left = 52 if w >= 420 else 44
        margin_right = 12 if w >= 400 else 8
        chart_w = w - margin_left - margin_right

        if chart_w < 10:
            self._hovered_index = -1
            self.update()
            super().mouseMoveEvent(event)
            return

        n = len(self._data)
        pos = event.pos()

        ratio = (pos.x() - margin_left) / chart_w
        i = int(round(ratio * (n - 1)))
        i = max(0, min(i, n - 1))

        pt_x = margin_left + chart_w * i / (n - 1) if n > 1 else margin_left + chart_w / 2

        if abs(pos.x() - pt_x) <= 30:
            self._hovered_index = i
        else:
            self._hovered_index = -1

        self.update()

        if self._hovered_index >= 0:
            label, value = self._data[self._hovered_index]
            try:
                parts = label.split("-")
                if len(parts) == 2:
                    dt = _parse_mmdd(label)
                    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                    date_str = dt.strftime("%m-%d") + f" ({weekdays[dt.weekday()]})"
                else:
                    date_str = label
            except ValueError, IndexError:
                date_str = label
            QToolTip.showText(event.globalPos(), f"📈 {date_str}\n{_format_number(value)}", self)

        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hovered_index = -1
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        if not self._data:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        colors = self._colors
        font_family = colors.get("font_family", "Microsoft YaHei")
        base_font_size = colors.get("font_size", 14)
        w = self.width()
        h = self.height()

        margin_left = 52 if w >= 420 else 44
        margin_right = 12 if w >= 400 else 8
        margin_top = 34
        margin_bottom = 48 if w >= 400 else 40

        chart_w = w - margin_left - margin_right
        chart_h = h - margin_top - margin_bottom

        if chart_w < 10 or chart_h < 10:
            painter.end()
            return

        # ── 标题 ──
        title_size = max(round(base_font_size * 10 / 14), 8)
        painter.setPen(colors["text"])
        title_font = QFont(font_family, title_size, QFont.Bold)
        painter.setFont(title_font)
        painter.drawText(QRectF(margin_left, 4, chart_w, 22), Qt.AlignLeft | Qt.AlignVCenter, self._title)

        # ── 数据范围 ──
        values = [v for _, v in self._data]
        max_val = max(values) if values else 1
        min_val = min(values) if values else 0
        if max_val == min_val:
            max_val = max_val + 1 or 2
            min_val = 0
        top_margin = max_val * 0.3
        adjusted_max = max_val + top_margin

        # ── 网格 + Y 轴 ──
        painter.setPen(colors["grid"])
        y_ticks = 4
        tick_font_size = max(round(base_font_size * 8 / 14), 7)
        for i in range(y_ticks + 1):
            y = margin_top + chart_h * (1 - i / y_ticks)
            painter.drawLine(QPointF(margin_left, y), QPointF(w - margin_right, y))
            val = int(min_val + (adjusted_max - min_val) * i / y_ticks)
            painter.setPen(colors["text_secondary"])
            tick_font = QFont(font_family, tick_font_size)
            painter.setFont(tick_font)
            painter.drawText(
                QRectF(2, y - 12, margin_left - 8, 24),
                Qt.AlignRight | Qt.AlignVCenter,
                _format_number(val),
            )
            painter.setPen(colors["grid"])

        # ── 折线图 ──
        n = len(self._data)
        if n < 1:
            painter.end()
            return

        line_color = colors.get(self._color_key, colors["line"])
        fill_color = colors.get(f"{self._color_key}_fill", colors["line_fill"])
        point_color = colors.get(f"{self._color_key}_point", colors["point"])

        points: List[QPointF] = []
        for i, (_, value) in enumerate(self._data):
            x = margin_left + chart_w * i / (n - 1) if n > 1 else margin_left + chart_w / 2
            ratio = (value - min_val) / (adjusted_max - min_val) if adjusted_max > min_val else 0.5
            y = margin_top + chart_h - chart_h * ratio
            points.append(QPointF(x, y))

        # ── 填充区域 ──
        if len(points) >= 2:
            path = QPainterPath()
            path.moveTo(points[0])
            for pt in points[1:]:
                path.lineTo(pt)
            path.lineTo(points[-1].x(), margin_top + chart_h)
            path.lineTo(points[0].x(), margin_top + chart_h)
            path.closeSubpath()
            painter.fillPath(path, fill_color)

        # ── 连线 ──
        pen = QPen(line_color, 2.5)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        for i in range(len(points) - 1):
            painter.drawLine(points[i], points[i + 1])

        # ── 数据点 + 标签 ──
        val_font_size = max(round(base_font_size * 9 / 14), 7)
        label_w = 60
        label_h = 20
        for i, (_, value) in enumerate(self._data):
            pt = points[i]
            painter.setPen(Qt.NoPen)
            if i == self._hovered_index:
                painter.setBrush(point_color.lighter(150))
                painter.drawEllipse(pt, 6, 6)
                painter.setPen(QPen(colors["text_secondary"], 1, Qt.DashLine))
                painter.drawLine(QPointF(pt.x(), margin_top), QPointF(pt.x(), h - margin_bottom))
            else:
                painter.setBrush(point_color)
                painter.drawEllipse(pt, 3, 3)
            painter.setBrush(Qt.NoBrush)

            label_y = pt.y() - label_h - 4
            if label_y < margin_top:
                label_y = pt.y() + 6
                if label_y + label_h > h - margin_bottom + 6:
                    label_y = pt.y() - label_h // 2
            label_x = pt.x() - label_w / 2
            if label_x < 2:
                label_x = 2
            elif label_x + label_w > w - 2:
                label_x = w - 2 - label_w
            if value > 0:
                painter.setPen(colors["text"])
                val_font = QFont(font_family, val_font_size, QFont.Bold)
                painter.setFont(val_font)
                painter.drawText(
                    QRectF(label_x, label_y, label_w, label_h),
                    Qt.AlignCenter,
                    _format_number(value),
                )

        # ── X 轴标签 ──
        x_tick_size = max(round(base_font_size * 7 / 14), 6)
        painter.setPen(colors["text_secondary"])
        tick_font = QFont(font_family, x_tick_size)
        painter.setFont(tick_font)
        for i, (label, _) in enumerate(self._data):
            x = margin_left + chart_w * i / (n - 1) if n > 1 else margin_left + chart_w / 2
            if n > 10 and i % 2 != 0:
                continue
            try:
                parts = label.split("-")
                if len(parts) == 2:
                    dt = _parse_mmdd(label)
                    wd = dt.weekday()
                    if wd in (0, 5, 6):
                        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                        display = f"{parts[0]}-{parts[1]}\n{weekdays[wd]}"
                    else:
                        display = label
                else:
                    display = label
            except ValueError, IndexError:
                display = label

            x_spacing = chart_w / n if n > 0 else chart_w
            painter.drawText(
                QRectF(x - x_spacing, h - margin_bottom + 4, x_spacing * 2, 36),
                Qt.AlignCenter,
                display,
            )

        painter.end()


# ══════════════════════════════════════════════════════════
# 统计信息卡片
# ══════════════════════════════════════════════════════════


class _StatCard(QFrame):
    """单个统计信息卡片"""

    def __init__(self, icon, title: str, value: str, subtitle: str = "", extra_info: str = "", parent=None):
        super().__init__(parent)
        self._icon = icon
        self._title = title
        self._value = value
        self._subtitle = subtitle
        self._extra_info = extra_info
        self._colors = _chart_colors()
        self.setup_ui()

    def set_colors(self, colors: dict):
        self._colors = colors
        self._apply_card_style()

    def _apply_card_style(self):
        tc = self._colors.get("text", QColor(255, 255, 255, 180))
        tcs = self._colors.get("text_secondary", QColor(255, 255, 255, 100))
        font_family = self._colors.get("font_family", "Microsoft YaHei")
        base_font_size = self._colors.get("font_size", 14)
        text_color = f"rgba({tc.red()},{tc.green()},{tc.blue()},{tc.alpha()})"
        text_sec = f"rgba({tcs.red()},{tcs.green()},{tcs.blue()},{tcs.alpha()})"
        val_size = max(round(base_font_size * 22 / 14), 16)
        sub_size = max(round(base_font_size * 11 / 14), 9)
        extra_size = max(round(base_font_size * 10 / 14), 8)
        for child in self.findChildren(QLabel):
            obj_name = child.objectName()
            if obj_name == "statValue":
                child.setStyleSheet(
                    f"color: {text_color}; font-size: {val_size}px; font-weight: bold; "
                    f"font-family: '{font_family}'; background: transparent;"
                )
            elif obj_name == "statSub":
                child.setStyleSheet(
                    f"color: {text_sec}; font-size: {sub_size}px; "
                    f"font-family: '{font_family}'; background: transparent;"
                )
            elif obj_name == "statExtra":
                child.setStyleSheet(
                    f"color: {text_sec}; font-size: {extra_size}px; opacity: 0.85; "
                    f"font-family: '{font_family}'; background: transparent;"
                )
            else:
                child.setStyleSheet(
                    f"color: {text_sec}; font-size: {sub_size}px; "
                    f"font-family: '{font_family}'; background: transparent;"
                )

    def setup_ui(self):
        self.setObjectName("statCard")
        self.setStyleSheet(
            "#statCard { background: transparent; border: 1px solid rgba(128,128,128,0.12); "
            "border-radius: 10px; padding: 0px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        font_family = self._colors.get("font_family", "Microsoft YaHei")
        base_font_size = self._colors.get("font_size", 14)
        val_size = max(round(base_font_size * 22 / 14), 16)
        sub_size = max(round(base_font_size * 11 / 14), 9)

        tc = self._colors.get("text", QColor(255, 255, 255, 180))
        tcs = self._colors.get("text_secondary", QColor(255, 255, 255, 100))
        text_color = f"rgba({tc.red()},{tc.green()},{tc.blue()},{tc.alpha()})"
        text_sec = f"rgba({tcs.red()},{tcs.green()},{tcs.blue()},{tcs.alpha()})"

        # 图标 + 标题行
        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        icon_w = IconWidget(self._icon, self)
        icon_w.setFixedSize(16, 16)
        top_row.addWidget(icon_w)
        title_lb = QLabel(self._title, self)
        title_lb.setObjectName("statTitle")
        title_lb.setStyleSheet(
            f"color: {text_sec}; font-size: {sub_size}px; font-family: '{font_family}'; background: transparent;"
        )
        top_row.addWidget(title_lb)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        # 值
        val_lb = QLabel(self._value, self)
        val_lb.setObjectName("statValue")
        val_lb.setStyleSheet(
            f"color: {text_color}; font-size: {val_size}px; font-weight: bold; "
            f"font-family: '{font_family}'; background: transparent;"
        )
        layout.addWidget(val_lb)

        if self._subtitle:
            sub_lb = QLabel(self._subtitle, self)
            sub_lb.setObjectName("statSub")
            sub_lb.setStyleSheet(
                f"color: {text_sec}; font-size: {sub_size}px; font-family: '{font_family}'; background: transparent;"
            )
            layout.addWidget(sub_lb)

        if self._extra_info:
            extra_size = max(round(base_font_size * 10 / 14), 8)
            extra_lb = QLabel(self._extra_info, self)
            extra_lb.setObjectName("statExtra")
            extra_lb.setStyleSheet(
                f"color: {text_sec}; font-size: {extra_size}px; opacity: 0.85; "
                f"font-family: '{font_family}'; background: transparent;"
            )
            layout.addWidget(extra_lb)


# ══════════════════════════════════════════════════════════
# 项目分布水平柱状图
# ══════════════════════════════════════════════════════════


class _ProjectBarWidget(QWidget):
    """项目分布水平柱状图"""

    def __init__(self, data: List[Tuple[str, int]], title: str = "📁 项目分布", parent=None):
        super().__init__(parent)
        self._data = data
        self._title = title
        self._colors = _chart_colors()
        self.setMinimumHeight(160)
        self.setMaximumHeight(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMouseTracking(True)
        self._hovered_index = -1

    def set_data(self, data: List[Tuple[str, int]]):
        self._data = data
        self.update()

    def set_colors(self, colors: dict):
        self._colors = colors
        self.update()

    def mouseMoveEvent(self, event):
        if not self._data:
            self._hovered_index = -1
            self.update()
            super().mouseMoveEvent(event)
            return

        h = self.height()
        margin_top = 28
        margin_bottom = 8
        chart_h = h - margin_top - margin_bottom

        if chart_h < 10:
            self._hovered_index = -1
            self.update()
            super().mouseMoveEvent(event)
            return

        n = len(self._data)
        pos = event.pos()
        row_h = chart_h / n

        i = int((pos.y() - margin_top) / row_h)
        i = max(0, min(i, n - 1))

        row_y = margin_top + i * row_h
        if row_y <= pos.y() <= row_y + row_h:
            self._hovered_index = i
        else:
            self._hovered_index = -1

        self.update()

        if self._hovered_index >= 0:
            label, value = self._data[self._hovered_index]
            QToolTip.showText(event.globalPos(), f"📁 {label}\n会话数: {value}", self)

        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hovered_index = -1
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        if not self._data:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        colors = self._colors
        font_family = colors.get("font_family", "Microsoft YaHei")
        base_font_size = colors.get("font_size", 14)
        w = self.width()
        h = self.height()

        title_h = 24
        margin_left = 16
        margin_right = 16
        margin_top = title_h + 4
        margin_bottom = 8

        chart_w = w - margin_left - margin_right
        chart_h = h - margin_top - margin_bottom

        if chart_w < 10 or chart_h < 10:
            painter.end()
            return

        # ── 标题 ──
        title_size = max(round(base_font_size * 10 / 14), 8)
        painter.setPen(colors["text"])
        title_font = QFont(font_family, title_size, QFont.Bold)
        painter.setFont(title_font)
        painter.drawText(QRectF(margin_left, 2, chart_w, title_h), Qt.AlignLeft | Qt.AlignVCenter, self._title)

        n = len(self._data)
        if n == 0:
            painter.end()
            return

        max_val = max(v for _, v in self._data)
        if max_val == 0:
            max_val = 1

        row_h = chart_h / n
        bar_h = max(row_h * 0.6, 14)
        bar_h = min(bar_h, 28)

        label_font_size = max(round(base_font_size * 10 / 14), 8)
        val_font_size = max(round(base_font_size * 9 / 14), 8)

        for i, (label, value) in enumerate(self._data):
            y = margin_top + i * row_h + (row_h - bar_h) / 2

            painter.setPen(colors["text"])
            label_font = QFont(font_family, label_font_size)
            painter.setFont(label_font)
            display_label = label if len(label) <= 12 else label[:11] + "…"
            painter.drawText(
                QRectF(margin_left, y, 80, bar_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                display_label,
            )

            bar_w = (value / max_val) * (chart_w - 80 - 60) if max_val > 0 else 0
            bar_x = margin_left + 84

            bar_color = QColor(colors["accent"])
            bar_color.setAlpha(180)
            path = QPainterPath()
            path.addRoundedRect(QRectF(bar_x, y + 2, max(bar_w, 2), bar_h - 4), 4, 4)
            if i == self._hovered_index:
                hover_color = QColor(bar_color).lighter(140)
                painter.fillPath(path, hover_color)
                painter.setPen(QPen(QColor(colors["accent"]).lighter(150), 2))
            else:
                painter.fillPath(path, bar_color)
                painter.setPen(QPen(colors["accent"], 1))
            painter.drawPath(path)

            painter.setPen(colors["text"])
            val_font = QFont(font_family, val_font_size, QFont.Bold)
            painter.setFont(val_font)
            painter.drawText(
                QRectF(bar_x + max(bar_w, 2) + 6, y, 50, bar_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                str(value),
            )

        painter.end()
