# -*- coding: utf-8 -*-
"""全屏选区截图遮罩窗（冻结底图方案）。

进入选区时抓主屏全屏作底图：遮罩窗铺底图（所见即所截），选区外叠半透明
暗遮罩，松手从底图按高 DPI 换算裁剪物理像素区域，粘贴出去尺寸与屏幕一致。
"""

from PyQt5.QtCore import Qt, QRect, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QWidget

# 选区小于该逻辑边长视为误触（点击），静默取消
MIN_SELECTION = 4

# 选区外暗遮罩透明度
_DIM_ALPHA = 120

# 选区边框色（青色，截图工具惯例，不依赖主题）
_BORDER = QColor("#00adb5")


def _physical_rect(logical: QRect, dpr: float) -> QRect:
    """逻辑坐标矩形 → 物理像素矩形（Windows 高 DPI 换算）。"""
    if dpr <= 1.0:
        return QRect(logical)
    return QRect(
        round(logical.x() * dpr),
        round(logical.y() * dpr),
        round(logical.width() * dpr),
        round(logical.height() * dpr),
    )


class _ScreenshotOverlay(QWidget):
    """全屏遮罩窗：拖框选区，captured 信号携带选区 QPixmap。

    生命周期：show() → 左键拖框 → 松手发 captured（或过小发 cancelled）→ close。
    Esc / 右键 → 发 cancelled → close。WA_DeleteOnClose 防窗体残留。
    """

    captured = pyqtSignal(QPixmap)
    cancelled = pyqtSignal()

    def __init__(self, base_pixmap: QPixmap, screen_rect: QRect):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._base = base_pixmap
        self._origin = None  # 拖拽起点（逻辑坐标，None = 未按下）
        self._selection = QRect()  # 当前选区（逻辑坐标）
        self.setCursor(Qt.CrossCursor)
        self.setGeometry(screen_rect)

    # ── 交互 ──

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._origin = event.pos()
            self._selection = QRect()
        elif event.button() == Qt.RightButton:
            self._cancel()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._origin is None:
            return
        self._selection = QRect(self._origin, event.pos()).normalized()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.LeftButton or self._origin is None:
            return
        self._origin = None
        sel = self._selection
        if sel.width() < MIN_SELECTION or sel.height() < MIN_SELECTION:
            self._cancel()  # 误触（点击），静默取消
            return
        dpr = float(self._base.devicePixelRatio() or 1.0)
        shot = self._base.copy(_physical_rect(sel, dpr))
        shot.setDevicePixelRatio(dpr)
        self.captured.emit(shot)
        self.close()
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self._cancel()
            event.accept()
            return
        super().keyPressEvent(event)

    def _cancel(self) -> None:
        self.cancelled.emit()
        self.close()

    # ── 绘制 ──

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        # 物理尺寸底图拉伸铺满逻辑窗口（等效缩小 dpr 倍，画面正确）
        p.drawPixmap(self.rect(), self._base)
        if self._selection.isEmpty():
            return
        sel = self._selection
        w, h = self.width(), self.height()
        dim = QColor(0, 0, 0, _DIM_ALPHA)
        # 选区外四块暗遮罩
        p.fillRect(0, 0, w, sel.top(), dim)
        p.fillRect(0, sel.top(), sel.left(), sel.height(), dim)
        p.fillRect(sel.right() + 1, sel.top(), w - sel.right() - 1, sel.height(), dim)
        p.fillRect(0, sel.bottom() + 1, w, h - sel.bottom() - 1, dim)
        # 选区边框
        p.setPen(QPen(_BORDER, 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(sel)
        # 尺寸角标：选区右上角上方，黑底白字
        label = f"{sel.width()}×{sel.height()}"
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(label) + 10
        ty = max(0, sel.top() - 22)
        badge = QRect(sel.right() - tw + 1, ty, tw, 20)
        p.setPen(Qt.NoPen)
        p.fillRect(badge, QColor(0, 0, 0, 170))
        p.setPen(QColor("#ffffff"))
        p.drawText(badge, Qt.AlignCenter, label)
