# -*- coding: utf-8 -*-
"""
3层同心圆套餐用量控件

显示 5小时限额（外层）/ 一周限额（中层）/ 一月限额（内层）
三层同心圆弧，类似 ContextUsageRing 风格。
只有数据可用时才显示。

Tooltip 改造为自定义弹出控件（带 3 个进度条），显示用量百分比、
重置倒计时，进度条末端显示百分比数字。
"""

from PyQt5.QtCore import QPoint, QRectF, Qt, QTimer
from PyQt5.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt5.QtWidgets import QApplication, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.utils.design_tokens import _get_global_font, font_size_css
from app.utils.utils import get_font_family_css

# 各层对应的标签和颜色基调
LAYER_CONFIG = [
    {"key": "rolling", "label": "5小时用量", "hue": "#5aa9ff"},  # 蓝色系
    {"key": "weekly", "label": "每周用量", "hue": "#9b59b6"},  # 紫色系
    {"key": "monthly", "label": "每月用量", "hue": "#2ecc71"},  # 绿色系
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


def _format_reset(sec: int) -> str:
    """将秒数格式化为可读的剩余时间"""
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


def _parse_color(value, fallback: str = "#212126") -> QColor:
    """将主题色字符串解析为 QColor。"""
    if isinstance(value, QColor):
        return value
    s = str(value or "").strip()
    try:
        if s.startswith("#"):
            c = QColor(s)
            if c.isValid():
                return c
        elif s.lower().startswith(("rgba(", "rgb(")):
            inner = s[s.index("(") + 1 : s.rindex(")")]
            parts = [p.strip() for p in inner.split(",")]
            if len(parts) >= 3:
                r, g, b = (int(round(float(parts[i]))) for i in range(3))
                a = 255
                if len(parts) >= 4:
                    av = float(parts[3])
                    a = int(round(av * 255)) if av <= 1 else int(round(av))
                return QColor(
                    max(0, min(255, r)),
                    max(0, min(255, g)),
                    max(0, min(255, b)),
                    max(0, min(255, a)),
                )
        else:
            c = QColor(s)
            if c.isValid():
                return c
    except Exception:
        pass
    fc = QColor(fallback)
    return fc if fc.isValid() else QColor(33, 33, 38, 246)


# ─── 进度条组件 ─────────────────────────────────


class _PlanProgressBar(QWidget):
    """单条用量进度条，末端显示百分比数字"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(18)
        self._percent = 0
        self._bar_color = QColor("#5aa9ff")
        self._track_color = self._compute_track_color()

    @staticmethod
    def _compute_track_color() -> QColor:
        """轨道颜色：浅色用深色半透明，深色用白色半透明"""
        try:
            from app.utils.theme_manager import theme_manager
            if theme_manager.is_light_theme():
                return QColor(0, 0, 0, 28)
        except Exception:
            pass
        return QColor(255, 255, 255, 28)

    @staticmethod
    def _compute_text_color(on_fill: bool) -> QColor:
        """文字颜色"""
        try:
            from app.utils.theme_manager import theme_manager
            if theme_manager.is_light_theme():
                if on_fill:
                    return Qt.white  # 填充区上用白字不变
                return QColor(0, 0, 0, 180)
        except Exception:
            pass
        if on_fill:
            return Qt.white
        return QColor(255, 255, 255, 180)

    def refresh_theme(self):
        """主题切换后刷新轨道颜色"""
        self._track_color = self._compute_track_color()
        self.update()

    def set_data(self, percent: int, color: QColor):
        self._percent = max(0, min(100, percent))
        self._bar_color = color
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)

        w = self.width()
        h = self.height()
        r = h / 2.0

        # — 背景轨道 —
        painter.setBrush(self._track_color)
        painter.drawRoundedRect(0, 0, w, h, r, r)

        # — 填充 —
        fill_w = 0
        if self._percent > 0:
            fill_w = int(w * self._percent / 100.0)
            fill_w = max(fill_w, int(r * 2))  # 最少显示圆头
            clip = QPainterPath()
            clip.addRoundedRect(0, 0, fill_w, h, r, r)
            painter.setClipPath(clip)
            painter.setBrush(self._bar_color)
            painter.drawRoundedRect(0, 0, w, h, r, r)
            painter.setClipping(False)

        # — 百分比文字（右侧） —
        painter.setPen(Qt.white)
        font = QFont(_get_global_font(), 10)
        font.setWeight(QFont.Bold)
        painter.setFont(font)

        pct_text = f"{self._percent}%"
        fm = QFontMetrics(font)
        text_w = fm.width(pct_text)
        text_x = w - text_w - 6  # 距右边缘 6px
        text_y = int((h + fm.ascent()) / 2) - 1

        # 数字在进度条填充区上时用白字，否则用半透明
        if text_x < fill_w and self._percent > 0:
            painter.setPen(self._compute_text_color(on_fill=True))
        else:
            painter.setPen(self._compute_text_color(on_fill=False))

        painter.drawText(text_x, text_y, pct_text)
        painter.setPen(Qt.NoPen)


# ─── Tooltip 弹出控件 ──────────────────────────


class CodingPlanTooltip(QWidget):
    """套餐用量浮动卡片 — 带 3 个进度条"""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._load_theme_colors()
        self._build_ui()

    def _load_theme_colors(self):
        """从当前主题读取卡片背景/边框/文字色"""
        try:
            from app.utils.design_tokens import current_theme

            theme = current_theme()
        except Exception:
            theme = {}
        self._card_bg = theme.get("card_bg_solid") or "rgba(33, 33, 38, 0.96)"
        self._border = theme.get("border") or "#3d3d3d"
        self._text_primary = theme.get("text_primary") or "#ffffff"
        self._text_secondary = theme.get("text_secondary") or "rgba(255, 255, 255, 0.5)"

    def _build_ui(self):
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(6)

        # 标题
        self._title = QLabel("套餐用量")
        self._title.setStyleSheet(
            f"color: {self._text_primary}; font-weight: 600; {get_font_family_css()} {font_size_css(13)}"
        )
        self._layout.addWidget(self._title)

        # 3 层行容器（避免每次重建整个 layout）
        self._layer_rows: list = []  # [(container, label_w, reset_w, bar), ...]

        for cfg in LAYER_CONFIG:
            container = QWidget(self)
            vlay = QVBoxLayout(container)
            vlay.setContentsMargins(0, 4, 0, 0)
            vlay.setSpacing(3)

            # 标题行：标签 + 重置时间
            hrow = QHBoxLayout()
            hrow.setSpacing(8)

            label_w = QLabel(cfg["label"], container)
            label_w.setStyleSheet(
                f"color: {self._text_primary}; {get_font_family_css()} {font_size_css(12)}"
            )

            reset_w = QLabel("", container)
            reset_w.setStyleSheet(
                f"color: {self._text_secondary}; {get_font_family_css()} {font_size_css(11)}"
            )

            hrow.addWidget(label_w)
            hrow.addStretch(1)
            hrow.addWidget(reset_w)
            vlay.addLayout(hrow)

            # 进度条
            bar = _PlanProgressBar(container)
            bar.set_data(0, QColor(cfg["hue"]))
            vlay.addWidget(bar)

            self._layout.addWidget(container)
            self._layer_rows.append((container, label_w, reset_w, bar))

    # ---------- 数据更新 ----------

    def set_data(self, layers: dict):
        """layers: {"rolling": {"percent": int, "reset_sec": int}, ...}"""
        self._load_theme_colors()
        self._title.setStyleSheet(
            f"color: {self._text_primary}; font-weight: 600; {get_font_family_css()} {font_size_css(13)}"
        )

        has_any = False
        for i, cfg in enumerate(LAYER_CONFIG):
            key = cfg["key"]
            data = layers.get(key) or {}
            pct = data.get("percent")
            reset_sec = data.get("reset_sec")
            container, label_w, reset_w, bar = self._layer_rows[i]

            if pct is not None:
                has_any = True
                pct_int = max(0, min(100, int(pct)))
                reset_str = _format_reset(reset_sec) if reset_sec else "即将重置"
                reset_w.setText(reset_str)
                container.setVisible(True)
                bar.set_data(pct_int, _rate_color(QColor(cfg["hue"]), pct_int))
            else:
                container.setVisible(False)

            # 随主题切换刷新标签样式（颜色可能变化）
            label_w.setStyleSheet(
                f"color: {self._text_primary}; {get_font_family_css()} {font_size_css(12)}"
            )
            reset_w.setStyleSheet(
                f"color: {self._text_secondary}; {get_font_family_css()} {font_size_css(11)}"
            )

        self._title.setVisible(has_any)
        self.adjustSize()

    # ---------- 绘制卡片背景 ----------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        bg = _parse_color(self._card_bg, "#212126")
        painter.setBrush(bg)
        r = 8
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), r, r)

        # 边框
        painter.setPen(_parse_color(self._border, "#3d3d3d"))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), r, r)
        painter.setPen(Qt.NoPen)


# ─── 主控件 ──────────────────────────────────


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
            "rolling": {"percent": None, "reset_sec": None},
            "weekly": {"percent": None, "reset_sec": None},
            "monthly": {"percent": None, "reset_sec": None},
        }
        self._has_data = False

        # 样式参数
        self._track_color = self._compute_track_color()
        self._size = 26  # 比 ContextUsageRing(22) 稍大以容纳三层
        self.setFixedSize(self._size, self._size)
        self.setMouseTracking(True)

        # 自定义 tooltip 控件
        self._tooltip = CodingPlanTooltip()
        self._tooltip_timer = QTimer(self)
        self._tooltip_timer.setSingleShot(True)
        self._tooltip_timer.timeout.connect(self._show_tooltip)

        # 初始隐藏
        self.setVisible(False)

    def set_usage(self, rolling: dict = None, weekly: dict = None, monthly: dict = None) -> bool:
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
        self._tooltip.hide()
        self.setVisible(False)

    @staticmethod
    def _compute_track_color() -> QColor:
        """轨道颜色：浅色用深色半透明，深色用白色半透明"""
        try:
            from app.utils.theme_manager import theme_manager
            if theme_manager.is_light_theme():
                return QColor(0, 0, 0, 40)
        except Exception:
            pass
        return QColor(255, 255, 255, 40)

    def refresh_theme(self):
        """主题切换后刷新轨道颜色及 tooltip 主题色"""
        self._track_color = self._compute_track_color()
        self._rebuild_tooltip()
        self.update()

    def refresh_font_size(self):
        """字号变化后刷新 tooltip 内 QLabel 字号

        CodingPlanTooltip 不是 main_window 的子组件（独立 Tooltip 窗口），
        apply_font_size_to_widget() 的 findChildren 找不到它，所以这里手动触发
        _rebuild_tooltip() 让 set_data() 用新的 font_size_css(N) 重新设置 stylesheet。
        """
        self._rebuild_tooltip()
        self.update()

    # ── 工具提示 ─────────────────────────────────────

    def _rebuild_tooltip(self):
        self._tooltip.set_data(self._layers)

    def _show_tooltip(self):
        if not self._has_data:
            return

        # 每次显示前刷新 tooltip 数据，确保主题色/字体等与当前主题同步
        self._rebuild_tooltip()
        tip_size = self._tooltip.size()
        pos_width = max(tip_size.width(), 1)

        # tooltip 定位：紧贴 widget 上方或下方显示
        widget_global = self.mapToGlobal(QPoint(0, 0))
        widget_center_x = widget_global.x() + self.width() // 2

        window = self.window()
        window_center = window.y() + window.height() / 2
        if widget_global.y() > window_center:
            x = widget_center_x - pos_width // 2
            y = widget_global.y() - tip_size.height() - 4
        else:
            x = widget_center_x - pos_width // 2
            y = widget_global.y() + self.height() + 4

        screen_geom = (
            self.screen().geometry()
            if self.screen()
            else QApplication.primaryScreen().geometry()
        )
        if x < screen_geom.left():
            x = screen_geom.left() + 5
        if x + pos_width > screen_geom.right():
            x = screen_geom.right() - pos_width - 5
        if y < screen_geom.top():
            y = screen_geom.top() + 5
        if y + tip_size.height() > screen_geom.bottom():
            y = screen_geom.bottom() - tip_size.height() - 5

        self._tooltip.move(x, y)
        self._tooltip.show()

    # ── 鼠标事件 ─────────────────────────────────────

    def enterEvent(self, event):
        self._tooltip_timer.start(300)

    def leaveEvent(self, event):
        self._tooltip_timer.stop()
        # 延迟隐藏，给鼠标从 ring 移到 tooltip 卡片 100ms 缓冲
        QTimer.singleShot(100, self._tooltip.hide)

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
