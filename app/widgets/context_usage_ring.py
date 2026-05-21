# -*- coding: utf-8 -*-
from PyQt5.QtCore import Qt, QTimer, QPoint
import math
from PyQt5.QtGui import QColor, QPainter, QPen, QFontMetrics
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
        self._cache_hit_rate = 0.0  # 缓存命中率 0.0-1.0
        self._cache_read_tokens = 0
        self._cache_write_tokens = 0
        self._cache_cost_savings = 0.0  # 节省的成本（美元）

        self.setFixedSize(18, 18)
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

        # 从 Colors 获取主题色
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
            "当前上下文占用",
            f"已用: {used_tokens} tokens",
            f"预算: {budget_tokens} tokens",
            f"占比: {self._percent}%",
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
                        f"普通上下文: {normal_tokens} tokens ({actual_ratio}%)",
                        f"压缩上下文: {compacted_tokens} tokens ({compact_ratio}%)",
                        f"压缩条数: {compaction.get('summarized_count', 0)}",
                        f"保留条数: {compaction.get('kept_count', 0)}",
                    ]
                )
            else:
                tooltip_lines.extend(
                    [
                        "",
                        f"压缩条数: {compaction.get('summarized_count', 0)}",
                        f"保留条数: {compaction.get('kept_count', 0)}",
                    ]
                )
            note = str(compaction.get("note", "") or "").strip()
            if note:
                tooltip_lines.append(note)
        elif total_tokens > 0:
            tooltip_lines.append(f"实际消息: {normal_tokens} tokens")

        self._last_tooltip_lines = tooltip_lines
        self.update()

    def set_cache_stats(self, hit_rate: float = 0.0, read_tokens: int = 0, write_tokens: int = 0, cost_savings: float = 0.0):
        """
        设置缓存统计信息

        Args:
            hit_rate: 缓存命中率 (0.0 - 1.0)
            read_tokens: 从缓存读取的 token 数
            write_tokens: 写入缓存的 token 数
            cost_savings: 节省的成本（美元）
        """
        self._cache_hit_rate = max(0.0, min(1.0, hit_rate))
        self._cache_read_tokens = read_tokens
        self._cache_write_tokens = write_tokens
        self._cache_cost_savings = cost_savings

        # 更新 tooltip 内容
        if hit_rate > 0:
            cache_rate_display = f"{hit_rate * 100:.1f}%"
            self._last_tooltip_lines.extend([
                "",
                "━━━━━━━━━━",
                "📊 缓存统计",
                f"命中率: {cache_rate_display}",
            ])
            if read_tokens > 0:
                self._last_tooltip_lines.append(f"缓存读取: {read_tokens:,} tokens")
            if write_tokens > 0:
                self._last_tooltip_lines.append(f"缓存写入: {write_tokens:,} tokens")
            if cost_savings > 0:
                self._last_tooltip_lines.append(f"节省成本: ${cost_savings:.4f}")
        elif read_tokens > 0 or write_tokens > 0:
            self._last_tooltip_lines.extend([
                "",
                "━━━━━━━━━━",
                "📊 缓存统计",
                f"缓存读取: {read_tokens:,} tokens",
                f"缓存写入: {write_tokens:,} tokens",
            ])

        self.update()

    def _show_tooltip(self):
        """使用 QToolTip 显示提示，位置调整为向左延伸"""
        lines = self._last_tooltip_lines
        if not lines:
            return

        # 构建带格式的 tooltip 文本
        tooltip_text = "\n".join(lines)

        # 获取全局字体信息
        try:
            Colors.refresh()
            font_family = _get_global_font()
            font_size = scale_font_size(12)
            font_style = f"font-family: '{font_family}'; font-size: {font_size}px;"
            # 使用 Colors.CARD_BG（应用当前主题色）
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

        # 设置 QToolTip 样式
        self.setStyleSheet(tooltip_css)

        # 计算 tooltip 实际尺寸（根据文本内容）
        try:
            app = QApplication.instance()
            font = app.font()
            font.setFamily(font_family)
            font.setPointSize(font_size)
            fm = QFontMetrics(font)

            # 计算最宽行的宽度
            max_width = 0
            for line in lines:
                line_width = fm.width(line)
                if line_width > max_width:
                    max_width = line_width

            # 加上左右 padding 和边框
            tooltip_width = max_width + 24 + 2
            tooltip_height = len(lines) * fm.height() + 16
        except Exception:
            tooltip_width = 220
            tooltip_height = len(lines) * 20 + 16

        # 计算位置：显示在圆环左侧，右上角对齐
        # QToolTip.showText 的 x 是 tooltip 左边缘
        # tooltip 右侧间隔 2px 贴着圆环左边缘
        top_right_global = self.mapToGlobal(self.rect().topRight())
        # 圆环左边缘 x
        top_left_global = self.mapToGlobal(self.rect().topLeft())
        ring_left_x = top_left_global.x()
        x = ring_left_x - tooltip_width + 30
        y = top_right_global.y()

        # 边界检查
        screen = QApplication.desktop().screenGeometry(self)
        if x < screen.left():
            x = screen.left() + 5
        if y < screen.top():
            y = screen.top() + 5
        if y + tooltip_height > screen.bottom():
            y = screen.bottom() - tooltip_height - 5

        QToolTip.showText(QPoint(x, y), tooltip_text, self)

    def _is_dark_theme(self, app) -> bool:
        """判断是否为深色主题"""
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
        track_pen = QPen(self._track_color, 2.2)
        painter.setPen(track_pen)
        painter.drawArc(rect, 0, 360 * 16)

        # 计算分段绘制
        total_tokens = self._normal_tokens + self._compacted_tokens

        if total_tokens > 0 and self._compacted_tokens > 0:
            normal_ratio = self._normal_tokens / total_tokens
            compacted_ratio = self._compacted_tokens / total_tokens

            compacted_span = int(-360 * 16 * (compacted_ratio * self._percent / 100))
            compacted_pen = QPen(self._compacted_color, 2.2)
            painter.setPen(compacted_pen)
            painter.drawArc(rect, start_angle, compacted_span)

            normal_span = int(-360 * 16 * (normal_ratio * self._percent / 100))
            ring_pen = QPen(self._ring_color, 2.2)
            painter.setPen(ring_pen)
            painter.drawArc(rect, start_angle + compacted_span, normal_span)
        else:
            span_angle = int(-360 * 16 * (self._percent / 100.0))
            ring_pen = QPen(self._ring_color, 2.2)
            painter.setPen(ring_pen)
            painter.drawArc(rect, start_angle, span_angle)

        # 如果有缓存命中率，绘制缓存指示点
        if self._cache_hit_rate > 0:
            # 在圆环顶部（12点方向）绘制一个小指示点
            # 颜色根据命中率变化：绿色(高) -> 黄色(中) -> 红色(低)
            indicator_angle = -90  # 12点方向
            indicator_radians = math.radians(indicator_angle)
            center_x = self.width() / 2
            center_y = self.height() / 2
            radius = self.width() / 2 - 1
            dot_x = center_x + radius * math.cos(indicator_radians)
            dot_y = center_y - radius * math.sin(indicator_radians)  # 注意减号因为 y 轴向下

            # 根据命中率选择颜色
            if self._cache_hit_rate >= 0.8:
                dot_color = QColor("#4ade80")  # 绿色
            elif self._cache_hit_rate >= 0.5:
                dot_color = QColor("#facc15")  # 黄色
            else:
                dot_color = QColor("#f87171")  # 红色

            # 绘制指示点
            painter.setPen(Qt.NoPen)
            painter.setBrush(dot_color)
            painter.drawEllipse(int(dot_x) - 2, int(dot_y) - 2, 4, 4)