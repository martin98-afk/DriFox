# app/widgets/stop_button.py
"""发送/停止二合一按钮 — QPainter 自绘，发送图标 + 停止呼吸动效"""

import math
from typing import Optional

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget
from qfluentwidgets import FluentIcon


class SendStopButton(QWidget):
    """发送/停止二合一按钮

    一个控件替代 TransparentToolButton + AnimatedStopButton 两个按钮。

    两种模式：
    - 发送模式 (MODE_SEND)：金色渐变圆底 + FluentIcon.SEND 图标
    - 停止模式 (MODE_STOP)：金色渐变圆底 + 缩放呼吸方块
    """

    clicked = Signal()

    MODE_SEND = 0
    MODE_STOP = 1

    # 呼吸周期参数
    CYCLE_MS = 2500
    SCALE_AMPLITUDE = 0.14  # 缩放幅度 ±14%
    FRAME_INTERVAL_MS = 33
    MORPH_VERTICES = 32  # 多边形顶点数（越多越平滑）
    GLOW_STRENGTH = 0.55  # 辉光最大强度因子

    # 停止方块颜色（深浅主题统一用白色）
    SQUARE_COLOR = "#FFFFFF"

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(34, 34)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

        # 状态
        self._mode = self.MODE_SEND
        self._send_enabled = True  # 发送模式下是否可用（有文本）
        self._hovered = False

        # 动画状态 — 两个独立连续累计的相位（弧度），保证 sin() 回绕点连续
        self._phase_scale = 0.0  # 缩放相位，每个 CYCLE_MS 走 2π
        self._phase_shape = 0.0  # 形状相位，比缩放稍慢
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._advance)

        # 颜色
        self._square_color = QColor(self.SQUARE_COLOR)

        # 注册主题刷新
        self._register_theme()

    # ── 主题 ──────────────────────────────────────────

    def _register_theme(self):
        try:
            from app.utils.theme_manager import theme_manager

            theme_manager.register_refresh_target(self)
            self.refresh_theme()
        except Exception:
            pass

    def refresh_theme(self):
        """主题切换时清除图标缓存"""
        self._square_color = QColor(self.SQUARE_COLOR)
        self._send_icon_cache = None
        self.update()

    # ── 模式切换 ──────────────────────────────────────

    def set_send_mode(self):
        """切换到发送模式"""
        self._mode = self.MODE_SEND
        self._timer.stop()
        self._phase_scale = 0.0
        self._phase_shape = 0.0
        self.update()

    def set_stop_mode(self):
        """切换到停止模式并启动呼吸动画"""
        self._mode = self.MODE_STOP
        self._phase_scale = 0.0
        self._phase_shape = 0.0
        self._timer.start()
        self.update()

    def set_send_enabled(self, enabled: bool):
        """设置发送模式下按钮是否可用"""
        self._send_enabled = enabled
        self.update()

    def is_stop_mode(self) -> bool:
        return self._mode == self.MODE_STOP

    # ── 动画 ──────────────────────────────────────────

    def _advance(self):
        """每帧推进两个相位；回绕到 [0, 2π) 时 sin 值天然连续"""
        delta = self.FRAME_INTERVAL_MS / self.CYCLE_MS * 2 * math.pi
        self._phase_scale += delta
        self._phase_shape += delta * 0.7  # 形状相位比缩放稍慢
        if self._phase_scale >= 2 * math.pi:
            self._phase_scale -= 2 * math.pi
        if self._phase_shape >= 2 * math.pi:
            self._phase_shape -= 2 * math.pi
        self.update()

    # ── 事件 ──────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def enterEvent(self, event):
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    # ── 绘制 ──────────────────────────────────────────

    def _get_bg_colors(self):
        """根据当前状态获取背景色（单色或渐变起止色）"""
        from app.utils.design_tokens import Colors

        Colors.refresh()

        # 禁用态不改变背景色，仅靠图标半透明区分
        if self._hovered:
            return Colors.SEND_BTN_HOVER_START, Colors.SEND_BTN_HOVER_END
        return Colors.SEND_BTN_START, Colors.SEND_BTN_END

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 禁用态：整体降透明度，金色渐变自然变淡（主题色淡色系）
        disabled = self._mode == self.MODE_SEND and not self._send_enabled
        if disabled:
            painter.setOpacity(0.4)

        w, h = self.width(), self.height()
        center_x, center_y = w / 2, h / 2

        # 1. 绘制按钮背景（圆角矩形，半径取自主题）
        # _get_bg_colors 中已 Colors.refresh()，这里直接读缓存值
        from app.utils.design_tokens import Colors as _C

        btn_r = _C.SEND_BTN_RADIUS

        bg_path = QPainterPath()
        bg_path.addRoundedRect(0, 0, w, h, btn_r, btn_r)

        start_color, end_color = self._get_bg_colors()
        if end_color is None:
            painter.fillPath(bg_path, QColor(start_color))
        else:
            grad = QLinearGradient(QPointF(0, 0), QPointF(w, h))
            grad.setColorAt(0.0, QColor(start_color))
            grad.setColorAt(1.0, QColor(end_color))
            painter.fillPath(bg_path, grad)

        # 2. 根据模式绘制内容
        if self._mode == self.MODE_SEND:
            self._draw_send_icon(painter, center_x, center_y)
        else:
            self._draw_stop_square(painter, center_x, center_y)

        painter.end()

    def _draw_send_icon(self, painter: QPainter, cx: float, cy: float):
        """绘制 FluentIcon.SEND 发送图标（透明度由 paintEvent 统一控制）"""
        icon = FluentIcon.SEND.icon()
        icon_size = 18
        x, y = int(cx - icon_size / 2), int(cy - icon_size / 2)
        icon.paint(painter, x, y, icon_size, icon_size, Qt.AlignCenter, QIcon.Normal)

    def _draw_stop_square(self, painter: QPainter, cx: float, cy: float):
        """多边形呼吸停止动画

        两层效果：
        1. 径向辉光脉冲 — 背后扩散/收缩的白色光晕
        2. 多边形变形 — 32 顶点在「方形 ↔ 八角 ↔ 正圆」之间平滑过渡
        """
        scale = 1.0 + self.SCALE_AMPLITUDE * math.sin(self._phase_scale)
        base_size = 17
        half = base_size * scale / 2.0

        # 1. 径向辉光脉冲（先画，在形状下层）
        self._draw_glow_pulse(painter, cx, cy, half)

        # 2. 多边形变形：t=0 方形 → t=0.5 八角 → t=1 正圆
        t = (math.sin(self._phase_shape) + 1.0) / 2.0
        morph_path = self._build_morph_path(half, t)

        painter.save()
        painter.translate(cx, cy)
        painter.fillPath(morph_path, self._square_color)
        painter.restore()

    def _build_morph_path(self, half: float, t: float) -> QPainterPath:
        """构建方形→正圆平滑变形路径（坐标相对于中心）。

        原理：对每个顶点，计算方形半径和圆形半径，在二者间插值。
        方形半径公式：half / max(|cos θ|, |sin θ|)
        圆形半径公式：half
        """
        n = self.MORPH_VERTICES
        path = QPainterPath()

        for i in range(n):
            angle = 2.0 * math.pi * i / n - math.pi / 2.0  # 从 12 点钟开始
            cos_a = math.cos(angle)
            sin_a = math.sin(angle)

            # 该角度上方形边界的距离
            denom = max(abs(cos_a), abs(sin_a))
            square_r = half / denom if denom > 0.001 else half * 1.42
            circle_r = half

            r = circle_r + (square_r - circle_r) * (1.0 - t)

            x = r * cos_a
            y = r * sin_a
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        path.closeSubpath()
        return path

    def _draw_glow_pulse(self, painter: QPainter, cx: float, cy: float, half: float):
        """径向辉光脉冲 — 中心最亮、边缘透明，呼吸节奏与方块同步。"""
        pulse = (math.sin(self._phase_scale + math.pi / 2.0) + 1.0) / 2.0  # 0..1，与缩放相位差90°
        glow_radius = half * (1.2 + pulse * 0.8)  # 辉光半径 1.2x~2.0x
        center_alpha = int(255 * self.GLOW_STRENGTH * pulse)
        edge_alpha = 0

        gradient = QRadialGradient(QPointF(cx, cy), glow_radius)
        gradient.setColorAt(0.0, QColor(255, 255, 255, center_alpha))
        gradient.setColorAt(0.3, QColor(255, 255, 255, int(center_alpha * 0.45)))
        gradient.setColorAt(0.6, QColor(255, 255, 255, int(center_alpha * 0.12)))
        gradient.setColorAt(1.0, QColor(255, 255, 255, edge_alpha))

        painter.setBrush(gradient)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(cx, cy), glow_radius, glow_radius)

    def __del__(self):
        try:
            from app.utils.theme_manager import theme_manager

            theme_manager.unregister_refresh_target(self)
        except Exception:
            pass
