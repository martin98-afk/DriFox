# app/widgets/stop_button.py
"""缩放呼吸动效的停止按钮 — QPainter 自绘，QTimer 驱动动画"""

import math
from typing import Optional

from PyQt5.QtCore import QPointF, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QLinearGradient, QMouseEvent, QPainter, QPainterPath
from PyQt5.QtWidgets import QWidget


class AnimatedStopButton(QWidget):
    """缩放呼吸动效停止按钮

    在按钮中心绘制一个实心方块，方块自身按正弦规律微微放大缩小，
    模拟呼吸起伏。支持深色/浅色主题自动适配颜色。
    """

    clicked = pyqtSignal()

    # 呼吸周期参数
    CYCLE_MS = 2500         # 完整呼吸周期 2.5s
    SCALE_AMPLITUDE = 0.12  # 缩放幅度 ±12%
    FRAME_INTERVAL_MS = 33  # ~30fps

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedSize(34, 34)
        self.setCursor(Qt.PointingHandCursor)

        # 动画状态
        self._anim_progress = 0.0  # 0.0 → 1.0
        self._timer = QTimer(self)
        self._timer.setInterval(self.FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._advance)

        # 颜色（根据主题初始化）
        self._square_color = QColor("#FFFFFF")

        # 注册主题刷新
        self._register_theme()

    def _register_theme(self):
        try:
            from app.utils.theme_manager import theme_manager

            theme_manager.register_refresh_target(self)
            self.refresh_theme()
        except Exception:
            pass

    def refresh_theme(self):
        """主题切换时更新颜色"""
        try:
            from app.utils.theme_manager import theme_manager

            is_light = theme_manager.is_light_theme()
            self._square_color = QColor("#C0392B" if is_light else "#FFFFFF")
        except Exception:
            self._square_color = QColor("#FFFFFF")
        self.update()

    def _advance(self):
        """每帧推进动画进度"""
        increment = self.FRAME_INTERVAL_MS / self.CYCLE_MS
        self._anim_progress += increment
        if self._anim_progress > 1.0:
            self._anim_progress -= 1.0
        self.update()

    def start_animation(self):
        """启动呼吸动画"""
        self._anim_progress = 0.0
        self._timer.start()

    def stop_animation(self):
        """停止呼吸动画"""
        self._timer.stop()
        self._anim_progress = 0.0
        self.update()

    def paintEvent(self, event):
        """自绘缩放呼吸动效"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w, h = self.width(), self.height()
        center_x, center_y = w / 2, h / 2
        radius = min(w, h) / 2

        # 1. 绘制圆形按钮背景（金色渐变）
        from app.utils.design_tokens import Colors

        Colors.refresh()
        bg_path = QPainterPath()
        bg_path.addEllipse(center_x - radius, center_y - radius, radius * 2, radius * 2)

        grad = QLinearGradient(QPointF(0, 0), QPointF(w, h))
        grad.setColorAt(0.0, QColor(Colors.SEND_BTN_START))
        grad.setColorAt(1.0, QColor(Colors.SEND_BTN_END))
        painter.fillPath(bg_path, grad)

        # 2. 计算缩放因子
        angle = self._anim_progress * 2.0 * math.pi
        scale = 1.0 + self.SCALE_AMPLITUDE * math.sin(angle)

        # 3. 绘制中心实心方块
        base_size = 20           # 基础边长
        size = base_size * scale
        rx = 4.0 * scale         # 圆角同步缩放

        square_path = QPainterPath()
        square_path.addRoundedRect(
            center_x - size / 2, center_y - size / 2,
            size, size, rx, rx,
        )
        painter.fillPath(square_path, self._square_color)

        painter.end()

    def mousePressEvent(self, event: QMouseEvent):
        """点击触发停止信号"""
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def __del__(self):
        try:
            from app.utils.theme_manager import theme_manager

            theme_manager.unregister_refresh_target(self)
        except Exception:
            pass
