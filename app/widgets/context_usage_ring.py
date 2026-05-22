# -*- coding: utf-8 -*-
from PyQt5.QtCore import Qt, QTimer, QPoint
import math
from PyQt5.QtGui import QColor, QPainter, QPen, QFontMetrics, QFont
from PyQt5.QtWidgets import QWidget, QApplication, QToolTip

from app.utils.design_tokens import _get_global_font, scale_font_size, Colors


class ContextUsageRing(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._percent = 0
        self._ring_color = QColor("#5aa9ff")
        self._compacted_color = QColor("#9b59b6")
        self._track_color = QColor(255, 255, 255, 40)
        self._normal_tokens = 0
        self._compacted_tokens = 0

        # 缓存命中相关
        self._cache_hit_rate = 0.0  # token 命中率
        self._cache_per_request_hit_rate = 0.0  # 按请求计数的命中率
        self._cache_total_input_hit_rate = 0.0  # 总输入命中率
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        self._cache_cost_savings = 0.0
        self._cache_hits = 0
        self._cache_misses = 0

        self.setFixedSize(22, 22)
        self.setMouseTracking(True)
        self.setStyleSheet("""
            QToolTip {
                border: none;
                background: transparent;
            }
        """)

        self._last_tooltip_lines = []
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
    ):

        self._percent = max(0, min(100, int(percent)))
        self._normal_tokens = normal_tokens
        self._compacted_tokens = compacted_tokens

        from app.utils.design_tokens import Colors
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

        tooltip_lines = [
            "Context Usage",
            f"Used: {used_tokens:,} tokens",
            f"Budget: {budget_tokens:,} tokens",
            f"Ratio: {self._percent}%",
        ]

        compaction = compaction or {}
        total_tokens = normal_tokens + compacted_tokens
        if compaction.get("active"):
            if total_tokens > 0:
                compact_ratio = int(compacted_tokens / total_tokens * 100)
                actual_ratio = int(normal_tokens / total_tokens * 100)
                tooltip_lines.extend(
                    [
                        "",
                        f"Normal: {normal_tokens:,} ({actual_ratio}%)",
                        f"Compacted: {compacted_tokens:,} ({compact_ratio}%)",
                        f"Summarized: {compaction.get('summarized_count', 0)}",
                        f"Kept: {compaction.get('kept_count', 0)}",
                    ]
                )
            else:
                tooltip_lines.extend(
                    [
                        "",
                        f"Summarized: {compaction.get('summarized_count', 0)}",
                        f"Kept: {compaction.get('kept_count', 0)}",
                    ]
                )
            note = str(compaction.get("note", "") or "").strip()
            if note:
                tooltip_lines.append(note)
        elif total_tokens > 0:
            tooltip_lines.append(f"Messages: {normal_tokens:,} tokens")

        # Append cache stats section (stored from last set_cache_stats call)
        self._append_cache_tooltip(tooltip_lines)

        self._last_tooltip_lines = tooltip_lines
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
    ):
        self._cache_hit_rate = max(0.0, min(1.0, hit_rate))
        self._cache_per_request_hit_rate = max(0.0, min(1.0, per_request_hit_rate))
        self._cache_total_input_hit_rate = max(0.0, min(1.0, total_input_hit_rate))
        self._cache_read_tokens = read_tokens
        self._cache_write_tokens = write_tokens
        self._cache_cost_savings = cost_savings
        self._cache_hits = cache_hits
        self._cache_misses = cache_misses

        self._last_tooltip_lines = [l for l in self._last_tooltip_lines if not l.startswith(("Cache", "  ", "━━", "Save"))]
        self._append_cache_tooltip(self._last_tooltip_lines)

        self.update()

    def _append_cache_tooltip(self, lines: list):
        has_cache_data = (
            self._cache_hit_rate > 0
            or self._cache_read_tokens > 0
            or self._cache_write_tokens > 0
            or self._cache_hits > 0
        )
        if not has_cache_data:
            return

        lines.extend(["", "━" * 20, "Cache Stats"])
        lines.append(f"Token Hit:  {self._cache_hit_rate:.1%}")
        if self._cache_per_request_hit_rate > 0 and abs(self._cache_per_request_hit_rate - self._cache_hit_rate) > 0.01:
            lines.append(f"Req Hit:    {self._cache_per_request_hit_rate:.1%}")
        if self._cache_total_input_hit_rate > 0:
            lines.append(f"Input Hit:  {self._cache_total_input_hit_rate:.1%}")
        if self._cache_read_tokens > 0:
            lines.append(f"Read:       {self._cache_read_tokens:,}")
        if self._cache_write_tokens > 0:
            lines.append(f"Write:      {self._cache_write_tokens:,}")
        if self._cache_cost_savings > 0:
            lines.append(f"Saved:      ${self._cache_cost_savings:.4f}")

    def _show_tooltip(self):
        lines = self._last_tooltip_lines
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
                    border: 1px solid rgba(80, 90, 120, 0.6);
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
                    background-color: rgba(30, 35, 48, 240);
                    border: 1px solid rgba(80, 90, 120, 0.6);
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
                line_width = fm.width(line)
                if line_width > max_width:
                    max_width = line_width

            tooltip_width = max_width + 24 + 2
            tooltip_height = len(lines) * fm.height() + 16
        except Exception:
            tooltip_width = 220
            tooltip_height = len(lines) * 20 + 16

        top_right_global = self.mapToGlobal(self.rect().topRight())
        top_left_global = self.mapToGlobal(self.rect().topLeft())
        ring_left_x = top_left_global.x()
        x = ring_left_x - tooltip_width + 30
        y = top_right_global.y()

        screen = QApplication.desktop().screenGeometry(self)
        if x < screen.left():
            x = screen.left() + 5
        if y < screen.top():
            y = screen.top() + 5
        if y + tooltip_height > screen.bottom():
            y = screen.bottom() - tooltip_height - 5

        QToolTip.showText(QPoint(x, y), tooltip_text, self)

    def _is_dark_theme(self, app) -> bool:
        try:
            palette = app.palette()
            bg = palette.window().color()
            luminance = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
            return luminance < 128
        except Exception:
            return True

    def enterEvent(self, event):
        self._tooltip_timer.start(300)

    def leaveEvent(self, event):
        self._tooltip_timer.stop()
        QToolTip.hideText()

    def wheelEvent(self, event):
        event.ignore()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect().adjusted(2, 2, -2, -2)
        start_angle = 90 * 16

        # 绘制背景轨道
        track_pen = QPen(self._track_color, 2.5)
        painter.setPen(track_pen)
        painter.drawArc(rect, 0, 360 * 16)

        total_tokens = self._normal_tokens + self._compacted_tokens

        if total_tokens > 0 and self._compacted_tokens > 0:
            normal_ratio = self._normal_tokens / total_tokens
            compacted_ratio = self._compacted_tokens / total_tokens

            compacted_span = int(-360 * 16 * (compacted_ratio * self._percent / 100))
            compacted_pen = QPen(self._compacted_color, 2.5)
            painter.setPen(compacted_pen)
            painter.drawArc(rect, start_angle, compacted_span)

            normal_span = int(-360 * 16 * (normal_ratio * self._percent / 100))
            ring_pen = QPen(self._ring_color, 2.5)
            painter.setPen(ring_pen)
            painter.drawArc(rect, start_angle + compacted_span, normal_span)
        else:
            span_angle = int(-360 * 16 * (self._percent / 100.0))
            ring_pen = QPen(self._ring_color, 2.5)
            painter.setPen(ring_pen)
            painter.drawArc(rect, start_angle, span_angle)

        center_x = self.width() / 2
        center_y = self.height() / 2
        radius = self.width() / 2 - 1

        # 主指示点：Token 命中率 (3点方向，偏右)
        if self._cache_hit_rate >= 0.05:
            angle_main = 0  # 3点方向
            rad = math.radians(angle_main)
            dx = center_x + radius * math.cos(rad)
            dy = center_y - radius * math.sin(rad)

            if self._cache_hit_rate >= 0.8:
                dot_color = QColor("#4ade80")
            elif self._cache_hit_rate >= 0.5:
                dot_color = QColor("#facc15")
            else:
                dot_color = QColor("#f87171")

            painter.setPen(Qt.NoPen)
            painter.setBrush(dot_color)
            painter.drawEllipse(int(dx) - 2, int(dy) - 2, 4, 4)

        # 副指示点：Per-Request 命中率 (9点方向，偏左，仅当与token命中率差异较大时显示)
        if (
            self._cache_per_request_hit_rate >= 0.05
            and abs(self._cache_per_request_hit_rate - self._cache_hit_rate) > 0.05
        ):
            angle_secondary = 180  # 9点方向
            rad = math.radians(angle_secondary)
            sx = center_x + radius * math.cos(rad)
            sy = center_y - radius * math.sin(rad)

            if self._cache_per_request_hit_rate >= 0.8:
                s_color = QColor("#4ade80")
            elif self._cache_per_request_hit_rate >= 0.5:
                s_color = QColor("#facc15")
            else:
                s_color = QColor("#f87171")

            painter.setPen(Qt.NoPen)
            painter.setBrush(s_color)
            painter.drawEllipse(int(sx) - 2, int(sy) - 2, 4, 4)
