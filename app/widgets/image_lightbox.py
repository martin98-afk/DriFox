"""聊天图片的窗口内看图画布，保留原图，仅绘制视口内的源矩形。"""

import math

from PyQt5.QtCore import QEvent, QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import QApplication, QDialog

from app.utils.utils import get_unified_font


class ImageLightbox(QDialog):
    def __init__(self, pixmap, parent=None, *, annotation_dir=None):
        owner = parent.window() if parent is not None else QApplication.activeWindow()
        super().__init__(owner, Qt.Dialog | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.annotations = None
        self.owner = owner
        self.image = pixmap
        self.setWindowModality(Qt.WindowModal)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAccessibleName('图片预览')
        self.setFont(get_unified_font(10))
        self.setMouseTracking(True)
        self.origin = QPointF()
        self.scale = 1.
        self.fitted = True
        self.press = self.last = None
        self.dragged = False
        if owner:
            owner.installEventFilter(self)
        self._sync_window()
        self.fit()
        if annotation_dir is not None:
            from app.widgets.image_annotations import ImageAnnotations

            self.annotations = ImageAnnotations(self, annotation_dir)

    def _sync_window(self):
        if self.owner:
            rect = self.owner.rect()
            rect.moveTopLeft(self.owner.mapToGlobal(rect.topLeft()))
        else:
            rect = QApplication.primaryScreen().availableGeometry()
        self.setGeometry(rect)

    def eventFilter(self, obj, event):
        if obj is self.owner:
            if event.type() in (QEvent.Move, QEvent.Resize):
                self._sync_window()
            elif event.type() in (QEvent.Hide, QEvent.Close):
                self.reject()
        return False

    def fit(self):
        self.scale = min(max(1, self.width() - 32) / self.image.width(), max(1, self.height() - 62) / self.image.height(), 1.)
        self.origin = QPointF((self.width() - self.image.width() * self.scale) / 2, (self.height() - self.image.height() * self.scale) / 2)
        self.fitted = True
        self.update()

    def resizeEvent(self, event):
        if self.fitted:
            self.fit()
        if self.annotations is not None:
            self.annotations.place_toolbar()
        super().resizeEvent(event)

    def zoom(self, scale, anchor):
        # 最小保持至少一个源方向可见；上限仅防浮点失真，不再按屏幕或 5 倍截断。
        scale = min(2. ** 20, max(1. / max(self.image.width(), self.image.height()), scale))
        self.origin = anchor - (anchor - self.origin) * (scale / self.scale)
        self.scale = scale
        self.fitted = False
        self.update()

    def image_rect(self):
        return QRectF(self.origin.x(), self.origin.y(), self.image.width() * self.scale, self.image.height() * self.scale)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 214))
        dst = self.image_rect().intersected(QRectF(self.rect()))
        if not dst.isEmpty():
            src = QRectF((dst.x() - self.origin.x()) / self.scale, (dst.y() - self.origin.y()) / self.scale, dst.width() / self.scale, dst.height() / self.scale)
            p.setRenderHint(QPainter.SmoothPixmapTransform, self.scale < 1.)
            p.drawPixmap(dst, self.image, src)
            if self.annotations is not None:
                p.save()
                p.setClipRect(dst)
                p.translate(self.origin)
                p.scale(self.scale, self.scale)
                self.annotations.draw(p)
                p.restore()
                if self.annotations.entry.isVisible():
                    self.annotations.position_entry()
        p.setFont(self.font())
        p.setPen(Qt.white)
        text = f'{self.scale * 100:.0f}%   ·   滚轮缩放   ·   拖动平移   ·   双击适应 / 原图   ·   Esc 关闭'
        if self.annotations is not None and self.annotations.tool:
            gesture = "点击图片输入文字" if self.annotations.tool == "text" else "在图片上拖动标记"
            text = f"{self.scale * 100:.0f}%   ·   {gesture}   ·   滚轮缩放   ·   Esc 关闭"
        hint = self.rect().adjusted(8, self.height() - self.fontMetrics().height() - 12, -8, -5)
        p.fillRect(hint, QColor(0, 0, 0, 148))
        p.drawText(hint, Qt.AlignCenter, text)

    def wheelEvent(self, event):
        delta = event.pixelDelta().y() or event.angleDelta().y()
        self.zoom(self.scale * math.exp(max(-3., min(3., delta / 600))), event.posF())
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.annotations is not None and self.annotations.tool:
                self.annotations.press_at(event.localPos())
                return
            self.press = self.last = event.localPos()
            self.dragged = False
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        p = event.localPos()
        if self.annotations is not None and self.annotations.tool:
            self.annotations.move_to(p)
            return
        if self.press is not None and event.buttons() & Qt.LeftButton:
            if self.dragged or (p - self.press).manhattanLength() >= QApplication.startDragDistance():
                self.origin += p - self.last
                self.last = p
                self.dragged = True
                self.fitted = False
                self.update()
        else:
            self.setCursor(Qt.OpenHandCursor if self.image_rect().contains(p) else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.annotations is not None and self.annotations.tool:
                self.annotations.release_at(event.localPos())
                return
            if self.press is not None and not self.dragged and not self.image_rect().contains(self.press):
                self.reject()
            self.press = self.last = None
            self.unsetCursor()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.annotations is not None and self.annotations.tool:
                return
            if self.fitted:
                self.zoom(1., event.localPos())
            else:
                self.fit()

    def keyPressEvent(self, event):
        key = event.key()
        center = QPointF(self.rect().center())
        if key in (Qt.Key_Plus, Qt.Key_Equal, Qt.Key_Minus):
            self.zoom(self.scale * (1 / 1.25 if key == Qt.Key_Minus else 1.25), center)
        elif key == Qt.Key_0:
            self.fit()
        elif key == Qt.Key_1:
            self.zoom(1., center)
        elif key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            dx, dy = {Qt.Key_Left: (40, 0), Qt.Key_Right: (-40, 0), Qt.Key_Up: (0, 40), Qt.Key_Down: (0, -40)}[key]
            self.origin += QPointF(dx, dy)
            self.fitted = False
            self.update()
        else:
            super().keyPressEvent(event)
