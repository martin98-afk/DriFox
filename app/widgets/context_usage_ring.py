# -*- coding: utf-8 -*-
from PyQt5.QtCore import Qt, QTimer, QPoint, QRectF
import math
from PyQt5.QtGui import QColor, QPainter, QPen, QFontMetrics, QPainterPath, QLinearGradient
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

        self._cache_hit_rate = 0.0
        self._cache_per_request_hit_rate = 0.0
        self._cache_total_input_hit_rate = 0.0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        self._cache_cost_savings = 0.0
        self._cache_hits = 0
        self._cache_misses = 0
        self._requests = 0

        self.setFixedSize(22, 22)
        self.setMouseTracking(True)
        self.setStyleSheet("""
            QToolTip {
                border: none;
                background: transparent;
            }
        """)

        self._cache_lines: list = []
        self._context_lines: list = []
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

        lines = [
            "当前上下文占用",
            f"已用: {used_tokens:,} tokens",
            f"预算: {budget_tokens:,} tokens",
            f"占比: {self._percent}%",
        ]

        compaction = compaction or {}
        total_tokens = normal_tokens + compacted_tokens
        if compaction.get("active"):
            if total_tokens > 0:
                compact_ratio = int(compacted_tokens / total_tokens * 100)
                actual_ratio = int(normal_tokens / total_tokens * 100)
                lines.extend([
                    "",
                    f"普通上下文: {normal_tokens:,} tokens ({actual_ratio}%)",
                    f"压缩上下文: {compacted_tokens:,} tokens ({compact_ratio}%)",
                    f"压缩条数: {compaction.get('summarized_count', 0)}",
                    f"保留条数: {compaction.get('kept_count', 0)}",
                ])
            else:
                lines.extend([
                    "",
                    f"压缩条数: {compaction.get('summarized_count', 0)}",
                    f"保留条数: {compaction.get('kept_count', 0)}",
                ])
            note = str(compaction.get("note", "") or "").strip()
            if note:
                lines.append(note)
        elif total_tokens > 0:
            lines.append(f"实际消息: {normal_tokens:,} tokens")

        self._context_lines = lines
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

        has_data = (
            self._cache_hit_rate > 0
            or self._cache_read_tokens > 0
            or self._cache_write_tokens > 0
            or self._cache_hits > 0
        )
        if has_data:
            lines = ["", "━" * 20, "缓存统计"]
            lines.append(f"命中率: {self._cache_hit_rate:.1%}")
            if self._requests > 0:
                per_req = self._cache_per_request_hit_rate
                lines.append(f"请求命中: {self._cache_hits}/{self._requests} ({per_req:.1%})")
            if self._cache_total_input_hit_rate > 0:
                lines.append(f"输入占比: {self._cache_total_input_hit_rate:.1%}")
            if self._cache_read_tokens > 0 or self._cache_write_tokens > 0:
                avg_prompt = (self._cache_read_tokens + self._cache_write_tokens) / max(self._requests, 1)
                lines.append(f"均次缓存: {int(avg_prompt):,} tokens")
            if self._cache_cost_savings > 0:
                lines.append(f"节省成本: ${self._cache_cost_savings:.4f}")
            self._cache_lines = lines
        else:
            self._cache_lines = []

        self._rebuild_tooltip()
        self.update()

    def _rebuild_tooltip(self):
        self._last_tooltip_lines = self._context_lines + self._cache_lines

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
                inner_rect.center().x(), inner_rect.bottom(),
                inner_rect.center().x(), inner_rect.top()
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
                int(inner_rect.left()), int(inner_rect.bottom() - fill_h),
                int(inner_rect.width()), int(fill_h) + 1
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
