# -*- coding: utf-8 -*-
"""
3层同心圆套餐用量控件

显示 5小时限额（外层）/ 一周限额（中层）/ 一月限额（内层）
三层同心圆弧，类似 ContextUsageRing 风格。
只有数据可用时才显示。
"""
from PyQt5.QtCore import QPoint, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QFontMetrics, QPainter, QPen
from PyQt5.QtWidgets import QApplication, QToolTip, QWidget

from app.utils.design_tokens import Colors, _get_global_font, scale_font_size

# 各层对应的标签和颜色基调
LAYER_CONFIG = [
    {"key": "rolling",  "label": "5小时用量", "hue": "#5aa9ff"},   # 蓝色系
    {"key": "weekly",   "label": "每周用量",  "hue": "#9b59b6"},   # 紫色系
    {"key": "monthly",  "label": "每月用量",  "hue": "#2ecc71"},   # 绿色系
]


def _rate_color(base_color: QColor, percent: int) -> QColor:
    """根据用量百分比调整颜色饱和度/明度"""
    if percent >= 90:
        return QColor("#ff6b6b")  # 红色
    if percent >= 70:
        return QColor("#f6c453")  # 黄色
    # 正常范围：用 base_color，稍微根据百分比调暗
    r = base_color.red()
    g = base_color.green()
    b = base_color.blue()
    factor = 1.0 - (percent / 100.0) * 0.3
    return QColor(
        min(255, int(r * factor + 50 * (1 - factor))),
        min(255, int(g * factor + 50 * (1 - factor))),
        min(255, int(b * factor + 50 * (1 - factor))),
    )


class CodingPlanRing(QWidget):
    """3层同心圆套餐用量显示

    最外层: 5小时限额 (rolling)
    中间层: 一周限额 (weekly)
    最内层: 一月限额 (monthly)

    只有数据可用时才显示，否则隐藏。
    """

    def __init__(self, parent=None):
        super().__init__(parent)

        # 三层数据
        self._layers = {
            "rolling":  {"percent": None, "reset_sec": None},
            "weekly":   {"percent": None, "reset_sec": None},
            "monthly":  {"percent": None, "reset_sec": None},
        }
        self._has_data = False

        # 样式参数
        self._track_color = QColor(255, 255, 255, 40)
        self._size = 26  # 比 ContextUsageRing(22) 稍大以容纳三层
        self.setFixedSize(self._size, self._size)
        self.setMouseTracking(True)

        # 工具提示
        self._tooltip_lines: list = []
        self._tooltip_timer = QTimer(self)
        self._tooltip_timer.setSingleShot(True)
        self._tooltip_timer.timeout.connect(self._show_tooltip)

        # 初始隐藏
        self.setVisible(False)

    def set_usage(self, rolling: dict = None, weekly: dict = None,
                  monthly: dict = None) -> bool:
        """设置三层用量数据。

        Args:
            rolling: dict with 'percent'(int 0-100) and 'reset_sec'(int)
            weekly: 同上
            monthly: 同上
            任一项为 None 表示该层无数据（不显示该层弧）。

        Returns:
            True 表示数据有更新
        """
        has_any = False
        for key, data in [("rolling", rolling), ("weekly", weekly), ("monthly", monthly)]:
            if data is not None:
                self._layers[key] = {
                    "percent": max(0, min(100, int(data.get("percent", 0)))),
                    "reset_sec": data.get("reset_sec"),
                }
                has_any = True
            else:
                self._layers[key] = {"percent": None, "reset_sec": None}

        old = self._has_data
        self._has_data = has_any

        if has_any:
            self._rebuild_tooltip()
            self.setVisible(True)
            self.update()
        else:
            self.setVisible(False)

        return old != has_any

    def clear(self):
        """清除数据并隐藏"""
        self._has_data = False
        for key in self._layers:
            self._layers[key] = {"percent": None, "reset_sec": None}
        self._tooltip_lines = []
        self.setVisible(False)

    # ── 工具提示 ─────────────────────────────────────

    def _rebuild_tooltip(self):
        lines = ["套餐用量"]
        for cfg in LAYER_CONFIG:
            key = cfg["key"]
            data = self._layers[key]
            pct = data.get("percent")
            if pct is not None:
                reset = data.get("reset_sec")
                reset_str = self._format_reset(reset) if reset else "即将重置"
                lines.append(f"{cfg['label']}: {pct}% ({reset_str})")
        self._tooltip_lines = lines

    def _format_reset(self, sec: int) -> str:
        if sec is None or sec <= 0:
            return "即将重置"
        days = sec // 86400
        hours = (sec % 86400) // 3600
        minutes = (sec % 3600) // 60
        if days > 0:
            return f"{days}天{hours}小时{minutes}分后重置"
        elif hours > 0:
            return f"{hours}小时{minutes}分后重置"
        else:
            return f"{minutes}分后重置"

    def _show_tooltip(self):
        lines = self._tooltip_lines
        if not lines:
            return
        tooltip_text = "\n".join(lines)

        try:
            Colors.refresh()
            font_family = _get_global_font()
            font_size = scale_font_size(12)
            font_style = f"font-family: '{font_family}'; font-size: {font_size}px;"
            card_bg = Colors.CARD_BG.format(alpha=240)
            tooltip_css = f"""
                QToolTip {{
                    background-color: {card_bg};
                    border: 1px solid {Colors.BORDER};
                    border-radius: 6px;
                    padding: 8px 12px;
                    color: {Colors.TEXT_PRIMARY};
                    {font_style}
                }}
            """
        except Exception:
            font_style = ""
            tooltip_css = f"""
                QToolTip {{
                    background-color: {Colors.CARD_BG_SOLID};
                    border: 1px solid {Colors.BORDER};
                    border-radius: 6px;
                    padding: 8px 12px;
                    color: #e0e4ef;
                    {font_style}
                }}
            """

        self.setStyleSheet(tooltip_css)

        try:
            app = QApplication.instance()
            font = app.font()
            font.setFamily(font_family)
            font.setPointSize(font_size)
            fm = QFontMetrics(font)
            max_width = 0
            for line in lines:
                lw = fm.width(line)
                if lw > max_width:
                    max_width = lw
            tooltip_width = max_width + 24 + 2
            tooltip_height = len(lines) * fm.height() + 16
        except Exception:
            tooltip_width = 220
            tooltip_height = len(lines) * 20 + 16

        # 用窗口右沿定位：圆环在右上角，tooltip 显示在它左侧
        window = self.window()
        window_right = window.x() + window.width()
        widget_top_global = self.mapToGlobal(QPoint(0, 0)).y()
        # 限制定位用的宽度不超过 280px（避免字体度量高估导致偏左）
        pos_width = min(tooltip_width, 280)
        x = window_right - pos_width - 20
        y = widget_top_global + 10

        screen_geom = self.screen().geometry() if self.screen() else QApplication.primaryScreen().geometry()
        if x < screen_geom.left():
            x = screen_geom.left() + 5
        if y < screen_geom.top():
            y = screen_geom.top() + 5
        if y + tooltip_height > screen_geom.bottom():
            y = screen_geom.bottom() - tooltip_height - 5

        QToolTip.showText(QPoint(x, y), tooltip_text, self)

    # ── 鼠标事件 ─────────────────────────────────────

    def enterEvent(self, event):
        self._tooltip_timer.start(300)

    def leaveEvent(self, event):
        self._tooltip_timer.stop()
        QToolTip.hideText()

    def wheelEvent(self, event):
        event.ignore()

    # ── 绘制 ─────────────────────────────────────────

    def paintEvent(self, event):
        if not self._has_data:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        cx = w / 2.0
        cy = h / 2.0

        # 起点：12点钟方向 (90° in Qt = 90*16)
        start_angle = 90 * 16

        # — 外层两个圆环（加粗） —
        ring_params = [
            {"radius": 9.5, "stroke": 3.0, "key": "rolling"},
            {"radius": 6.5, "stroke": 2.5, "key": "weekly"},
        ]

        for rp in ring_params:
            key = rp["key"]
            data = self._layers.get(key, {})
            pct = data.get("percent")
            r = rp["radius"]
            sw = rp["stroke"]
            rect = QRectF(cx - r, cy - r, r * 2, r * 2)

            # 背景轨道
            track_pen = QPen(self._track_color, sw)
            painter.setPen(track_pen)
            painter.drawArc(rect, 0, 360 * 16)

            # 用量弧
            if pct is not None and pct > 0:
                base_hue = "#5aa9ff"
                for cfg in LAYER_CONFIG:
                    if cfg["key"] == key:
                        base_hue = cfg["hue"]
                        break
                ring_color = _rate_color(QColor(base_hue), pct)
                span = int(-360 * 16 * (pct / 100.0))
                ring_pen = QPen(ring_color, sw)
                painter.setPen(ring_pen)
                painter.drawArc(rect, start_angle, span)

        # — 最内层：实心圆 + 扇形用量 —
        inner_data = self._layers.get("monthly", {})
        inner_pct = inner_data.get("percent")
        ri = 4.5
        inner_rect = QRectF(cx - ri, cy - ri, ri * 2, ri * 2)

        # 背景实心圆
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._track_color)
        painter.drawEllipse(inner_rect)

        # 用量扇形
        if inner_pct is not None and inner_pct > 0:
            base_hue = "#5aa9ff"
            for cfg in LAYER_CONFIG:
                if cfg["key"] == "monthly":
                    base_hue = cfg["hue"]
                    break
            inner_color = _rate_color(QColor(base_hue), inner_pct)
            span = int(-360 * 16 * (inner_pct / 100.0))
            painter.setBrush(inner_color)
            painter.drawPie(inner_rect, start_angle, span)

        painter.end()
