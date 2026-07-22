# app/widgets/stop_button.py
"""发送/停止二合一按钮 — QPainter 自绘，发送图标 + 停止呼吸动效"""

import math
from typing import Optional

from PyQt5.QtCore import QPointF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PyQt5.QtWidgets import QWidget
from qfluentwidgets import FluentIcon


class SendStopButton(QWidget):
    """发送/停止二合一按钮

    一个控件替代 TransparentToolButton + AnimatedStopButton 两个按钮。

    两种模式：
    - 发送模式 (MODE_SEND)：金色渐变圆底 + FluentIcon.SEND 图标
    - 停止模式 (MODE_STOP)：金色渐变圆底 + 缩放呼吸方块
    """

    clicked = pyqtSignal()

    MODE_SEND = 0
    MODE_STOP = 1

    # 呼吸周期参数
    CYCLE_MS = 2500
    SCALE_AMPLITUDE = 0.10   # 缩放幅度 ±10%
    FRAME_INTERVAL_MS = 33

    # 颜色（纯黑/纯白，适配任何主题）
    COLOR_SQUARE_DARK = "#FFFFFF"  # 深色主题 → 白色方块
    COLOR_SQUARE_LIGHT = "#000000"  # 浅色主题 → 黑色方块

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(34, 34)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

        # 状态
        self._mode = self.MODE_SEND
        self._send_enabled = True   # 发送模式下是否可用（有文本）
        self._hovered = False

        # 动画状态
        self._anim_progress = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._advance)

        # 缓存的图标 pixmap（减少重复绘制开销）
        self._send_icon_cache: Optional[QPixmap] = None

        # 颜色
        self._square_color = QColor(self.COLOR_SQUARE_DARK)

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
        """主题切换时更新方块颜色、清除图标缓存"""
        try:
            from app.utils.theme_manager import theme_manager
            is_light = theme_manager.is_light_theme()
            self._square_color = QColor(
                self.COLOR_SQUARE_LIGHT if is_light else self.COLOR_SQUARE_DARK
            )
        except Exception:
            self._square_color = QColor(self.COLOR_SQUARE_DARK)
        self._send_icon_cache = None
        self.update()

    # ── 模式切换 ──────────────────────────────────────

    def set_send_mode(self):
        """切换到发送模式"""
        self._mode = self.MODE_SEND
        self._timer.stop()
        self._anim_progress = 0.0
        self.update()

    def set_stop_mode(self):
        """切换到停止模式并启动呼吸动画"""
        self._mode = self.MODE_STOP
        self._anim_progress = 0.0
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
        increment = self.FRAME_INTERVAL_MS / self.CYCLE_MS
        self._anim_progress += increment
        if self._anim_progress > 1.0:
            self._anim_progress -= 1.0
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

        if self._mode == self.MODE_SEND and not self._send_enabled:
            # 发送模式 + 禁用 → 纯色背景
            return Colors.TOOLBAR_BG, None  # flat color

        if self._hovered:
            return Colors.SEND_BTN_HOVER_START, Colors.SEND_BTN_HOVER_END
        return Colors.SEND_BTN_START, Colors.SEND_BTN_END

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

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
            # 纯色背景（禁用态）
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
        """绘制 FluentIcon.SEND 发送图标"""
        icon = FluentIcon.SEND.icon()
        icon_size = 18
        x, y = int(cx - icon_size / 2), int(cy - icon_size / 2)
        # 禁用态：50% 透明度模拟 TEXT_SECONDARY 效果，不用 QIcon.Disabled（渲染成纯黑）
        painter.save()
        if not self._send_enabled:
            painter.setOpacity(0.5)
        icon.paint(painter, x, y, icon_size, icon_size, Qt.AlignCenter, QIcon.Normal)
        painter.restore()

    def _draw_stop_square(self, painter: QPainter, cx: float, cy: float):
        """绘制缩放呼吸方块"""
        angle = self._anim_progress * 2.0 * math.pi
        scale = 1.0 + self.SCALE_AMPLITUDE * math.sin(angle)

        base_size = 17          # 方块基准边长
        size = base_size * scale
        rx = 3.5 * scale        # 圆角跟随缩放

        square_path = QPainterPath()
        square_path.addRoundedRect(cx - size / 2, cy - size / 2, size, size, rx, rx)
        painter.fillPath(square_path, self._square_color)

    def __del__(self):
        try:
            from app.utils.theme_manager import theme_manager
            theme_manager.unregister_refresh_target(self)
        except Exception:
            pass
