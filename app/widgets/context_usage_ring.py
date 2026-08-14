# -*- coding: utf-8 -*-

from PyQt5.QtCore import QPoint, QRectF, Qt, QTimer
from PyQt5.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QApplication, QWidget

from app.utils.design_tokens import Colors
from app.widgets.context_usage_tooltip import ContextBreakdownTooltip


class ContextUsageRing(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._percent = 0
        self._ring_color = QColor("#5aa9ff")
        self._compacted_color = QColor("#9b59b6")
        self._track_color = self._compute_track_color()
        self._normal_tokens = 0
        self._compacted_tokens = 0

        self._used_tokens = 0
        self._budget_tokens = 0
        self._compaction = {}
        self._breakdown = []

        self._cache_hit_rate = 0.0
        self._cache_per_request_hit_rate = 0.0
        self._cache_total_input_hit_rate = 0.0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        self._cache_cost_savings = 0.0
        self._cache_hits = 0
        self._cache_misses = 0
        self._requests = 0
        self._cache_data = {}

        self.setFixedSize(22, 22)
        self.setMouseTracking(True)

        self._tooltip = ContextBreakdownTooltip()
        self._tooltip_timer = QTimer(self)
        self._tooltip_timer.setSingleShot(True)
        self._tooltip_timer.timeout.connect(self._show_tooltip)

    def set_usage(
        self,
        percent: int,
        used_tokens: int,
        budget_tokens: int,
        compaction: dict = None,
        normal_tokens: int = 0,
        compacted_tokens: int = 0,
        breakdown: list = None,
        pruned_tokens: int = 0,
    ):

        self._percent = max(0, min(100, int(percent)))
        self._used_tokens = used_tokens
        self._budget_tokens = budget_tokens
        self._normal_tokens = normal_tokens
        self._compacted_tokens = compacted_tokens
        self._compaction = compaction or {}
        self._breakdown = breakdown or []
        self._pruned_tokens = max(0, int(pruned_tokens or 0))

        Colors.refresh()
        ring_normal = QColor(Colors.RING_NORMAL)
        ring_warning = QColor(Colors.RING_WARNING)
        ring_danger = QColor(Colors.RING_DANGER)
        ring_compacted = QColor(Colors.RING_COMPACTED)

        if self._percent >= 90:
            self._ring_color = ring_danger
        elif self._percent >= 70:
            self._ring_color = ring_warning
        else:
            self._ring_color = ring_normal
        self._compacted_color = ring_compacted

        self._rebuild_tooltip()
        self.update()

    def set_cache_stats(
        self,
        hit_rate: float = 0.0,
        read_tokens: int = 0,
        write_tokens: int = 0,
        cost_savings: float = 0.0,
        per_request_hit_rate: float = 0.0,
        total_input_hit_rate: float = 0.0,
        cache_hits: int = 0,
        cache_misses: int = 0,
        requests: int = 0,
    ):
        self._cache_hit_rate = max(0.0, min(1.0, hit_rate))
        self._cache_per_request_hit_rate = max(0.0, min(1.0, per_request_hit_rate))
        self._cache_total_input_hit_rate = max(0.0, min(1.0, total_input_hit_rate))
        self._cache_read_tokens = read_tokens
        self._cache_write_tokens = write_tokens
        self._cache_cost_savings = cost_savings
        self._cache_hits = cache_hits
        self._cache_misses = cache_misses
        self._requests = requests

        self._cache_data = {
            "hit_rate": self._cache_hit_rate,
            "read_tokens": self._cache_read_tokens,
            "write_tokens": self._cache_write_tokens,
            "cost_savings": self._cache_cost_savings,
            "per_request_hit_rate": self._cache_per_request_hit_rate,
            "total_input_hit_rate": self._cache_total_input_hit_rate,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "requests": self._requests,
        }

        self._rebuild_tooltip()
        self.update()

    def _rebuild_tooltip(self):
        self._tooltip.set_data(
            {
                "used_tokens": self._used_tokens,
                "budget_tokens": self._budget_tokens,
                "percent": self._percent,
                "ring_color": self._ring_color.name(),
                "compaction": self._compaction,
                "normal_tokens": self._normal_tokens,
                "compacted_tokens": self._compacted_tokens,
                "breakdown": self._breakdown,
                "cache": self._cache_data,
                "pruned_tokens": getattr(self, "_pruned_tokens", 0),
            }
        )

    def _show_tooltip(self):
        # 每次显示前刷新 tooltip 数据，确保主题色/字体等与当前主题同步
        self._rebuild_tooltip()

        # 即使没有会话 / 模型配置，也给出一个轻量提示，避免「hover 圆环却毫无反馈」。
        # 仅当「既没有预算、也没有占比、也没有明细、也没有缓存」时，构造空状态引导文案。
        if self._budget_tokens <= 0 and self._percent <= 0 and not self._breakdown and not self._cache_data:
            # 空状态覆盖 _rebuild_tooltip 设置的数据
            self._tooltip.set_data(
                {
                    "used_tokens": 0,
                    "budget_tokens": 0,
                    "percent": 0,
                    "ring_color": self._ring_color.name(),
                    "compaction": {},
                    "normal_tokens": 0,
                    "compacted_tokens": 0,
                    "breakdown": [],
                    "cache": {},
                    "empty": True,
                }
            )

        self._tooltip.adjustSize()
        tip_size = self._tooltip.size()

        # tooltip 定位：紧贴 widget 上方或下方显示
        widget_global = self.mapToGlobal(QPoint(0, 0))
        widget_center_x = widget_global.x() + self.width() // 2
        pos_width = max(tip_size.width(), 1)

        window = self.window()
        window_center = window.y() + window.height() / 2
        if widget_global.y() > window_center:
            x = widget_center_x - pos_width // 2
            y = widget_global.y() - tip_size.height() - 4
        else:
            x = widget_center_x - pos_width // 2
            y = widget_global.y() + self.height() + 4

        screen_geom = self.screen().geometry() if self.screen() else QApplication.primaryScreen().geometry()
        if x < screen_geom.left():
            x = screen_geom.left() + 5
        if x + pos_width > screen_geom.right():
            x = screen_geom.right() - pos_width - 5
        if y < screen_geom.top():
            y = screen_geom.top() + 5
        if y + tip_size.height() > screen_geom.bottom():
            y = screen_geom.bottom() - tip_size.height() - 5

        # 预创建原生窗口句柄，确保 move 直接作用于 HWND，
        # 避免 hide() 后 show() 新建窗口时 WM 在默认位置闪一帧。
        self._tooltip.winId()
        self._tooltip.move(x, y)
        self._tooltip.show()

    def _is_dark_theme(self, app) -> bool:
        try:
            palette = app.palette()
            bg = palette.window().color()
            luminance = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
            return luminance < 128
        except Exception:
            return True

    @staticmethod
    def _compute_track_color() -> QColor:
        """计算轨道颜色：浅色主题用深色半透明，深色主题用白色半透明"""
        try:
            from app.utils.theme_manager import theme_manager

            if theme_manager.is_light_theme():
                return QColor(0, 0, 0, 40)
        except Exception:
            pass
        return QColor(255, 255, 255, 40)

    def refresh_theme(self):
        """主题切换后刷新环颜色、轨道颜色及 tooltip 主题色

        调用者保证在调用前 Colors 已 refresh（dispatch_refresh 或 _apply_runtime_ui_settings
        的 preamble 中均会调用），因此此处直接读取 Colors 缓存值。
        """
        # 重新读取环主色（Colors 随主题变化）
        ring_normal = QColor(Colors.RING_NORMAL)
        ring_warning = QColor(Colors.RING_WARNING)
        ring_danger = QColor(Colors.RING_DANGER)
        ring_compacted = QColor(Colors.RING_COMPACTED)

        if self._percent >= 90:
            self._ring_color = ring_danger
        elif self._percent >= 70:
            self._ring_color = ring_warning
        else:
            self._ring_color = ring_normal
        self._compacted_color = ring_compacted

        # 轨道颜色
        self._track_color = self._compute_track_color()

        # tooltip 主题色随主题变化
        self._rebuild_tooltip()
        self.update()

    def refresh_font_size(self):
        """字号变化后刷新 tooltip 内 QLabel 字号

        ContextBreakdownTooltip 不是 main_window 的子组件（独立 Tooltip 窗口），
        apply_font_size_to_widget() 的 findChildren 找不到它，所以这里手动触发
        _rebuild_tooltip() 让 _refresh() 用新的 font_size_css(N) 重新设置 stylesheet。
        """
        self._rebuild_tooltip()
        self.update()

    def enterEvent(self, event):
        self._tooltip_timer.start(300)

    def leaveEvent(self, event):
        self._tooltip_timer.stop()
        self._tooltip.hide()

    def wheelEvent(self, event):
        event.ignore()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        margin = 2
        stroke_w = 2.5

        # === 水填效果：缓存命中率 ===
        if self._cache_hit_rate >= 0.05:
            inner_rect = QRectF(margin + 1, margin + 1, w - 2 * (margin + 1), h - 2 * (margin + 1))
            fill_h = inner_rect.height() * self._cache_hit_rate

            # 裁剪到圆形区域
            clip_path = QPainterPath()
            clip_path.addEllipse(inner_rect)
            painter.setClipPath(clip_path)

            # 渐变填充 (从底部向上)
            grad = QLinearGradient(
                inner_rect.center().x(), inner_rect.bottom(), inner_rect.center().x(), inner_rect.top()
            )
            if self._cache_hit_rate >= 0.8:
                grad.setColorAt(0.0, QColor(74, 222, 128, 160))
                grad.setColorAt(1.0, QColor(74, 222, 128, 60))
            elif self._cache_hit_rate >= 0.5:
                grad.setColorAt(0.0, QColor(250, 204, 21, 160))
                grad.setColorAt(1.0, QColor(250, 204, 21, 60))
            else:
                grad.setColorAt(0.0, QColor(248, 113, 113, 160))
                grad.setColorAt(1.0, QColor(248, 113, 113, 60))

            painter.setPen(Qt.NoPen)
            painter.setBrush(grad)
            painter.drawRect(
                int(inner_rect.left()), int(inner_rect.bottom() - fill_h), int(inner_rect.width()), int(fill_h) + 1
            )

            painter.setClipping(False)

        # === 背景轨道 ===
        rect = self.rect().adjusted(margin, margin, -margin, -margin)
        start_angle = 90 * 16
        track_pen = QPen(self._track_color, stroke_w)
        painter.setPen(track_pen)
        painter.drawArc(rect, 0, 360 * 16)

        total_tokens = self._normal_tokens + self._compacted_tokens

        if total_tokens > 0 and self._compacted_tokens > 0:
            normal_ratio = self._normal_tokens / total_tokens
            compacted_ratio = self._compacted_tokens / total_tokens

            compacted_span = int(-360 * 16 * (compacted_ratio * self._percent / 100))
            compacted_pen = QPen(self._compacted_color, stroke_w)
            painter.setPen(compacted_pen)
            painter.drawArc(rect, start_angle, compacted_span)

            normal_span = int(-360 * 16 * (normal_ratio * self._percent / 100))
            ring_pen = QPen(self._ring_color, stroke_w)
            painter.setPen(ring_pen)
            painter.drawArc(rect, start_angle + compacted_span, normal_span)
        else:
            span_angle = int(-360 * 16 * (self._percent / 100.0))
            ring_pen = QPen(self._ring_color, stroke_w)
            painter.setPen(ring_pen)
            painter.drawArc(rect, start_angle, span_angle)
