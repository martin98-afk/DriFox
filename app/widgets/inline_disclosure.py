"""消息底部面板共用的高度过渡与键盘折叠按钮。"""
from PyQt5.QtCore import QAbstractAnimation, QEasingCurve, QObject, QPointF, Qt, QVariantAnimation, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import QToolButton

from app.utils.design_tokens import Animations, Colors, qcolor_from_token


class DisclosureButton(QToolButton):
    """箭头与内容共用进度，按钮始终可用 Tab 聚焦。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._progress = 0.0
        self.setFixedSize(24, 24)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setAutoRaise(True)
        self.setAccessibleName("展开或折叠列表")

    def set_progress(self, progress):
        self._progress = progress
        self.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if not event.isAutoRepeat():
                self.click()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = qcolor_from_token(Colors.TEXT_PRIMARY if self.underMouse() else Colors.TEXT_SECONDARY)
        if self.hasFocus() or self.underMouse():
            fill = QColor(color)
            fill.setAlpha(30 if self.isDown() else 18)
            painter.setBrush(fill)
            painter.setPen(qcolor_from_token(Colors.TEXT_ACCENT) if self.hasFocus() else Qt.NoPen)
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), 5, 5)
        painter.translate(self.width() / 2, self.height() / 2)
        painter.rotate(90 * self._progress)
        painter.setPen(QPen(color, 1.65, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.drawPolyline(QPolygonF([QPointF(-2, -4), QPointF(2, 0), QPointF(-2, 4)]))


class DisclosureMotion(QObject):
    """只在过渡期间限制内容高度；数据重建或反向时从当前帧继续。"""

    progressChanged = pyqtSignal(float)

    def __init__(self, body, changed, settled=None):
        super().__init__(body)
        self._body = body
        self._changed = changed
        self._settled = settled
        self._expanded = not body.isHidden()
        self._progress = float(self._expanded)
        self._natural_max = body.maximumHeight()
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.valueChanged.connect(self._frame)
        self._anim.finished.connect(self._finish)

    def _natural_height(self):
        return min(self._natural_max, max(0, self._body.sizeHint().height()))

    def set_expanded(self, expanded, animate=True):
        body = self._body
        running = self._anim.state() == QAbstractAnimation.Running
        start = body.maximumHeight() if running else (0 if body.isHidden() else body.height())
        self._anim.stop()
        self._expanded = expanded
        host = body.parentWidget()
        if not animate or not host or not host.isVisible() or not Animations.motion_enabled():
            self._finish()
            return
        self._from_h = start
        self._to_h = self._natural_height() if expanded else 0
        self._from_p = self._progress
        if start == self._to_h and self._progress == float(expanded):
            self._finish()
            return
        body.setMaximumHeight(start)
        body.show()
        self._changed()
        duration = Animations.ENTER_MS if expanded else Animations.EXIT_MS
        fraction = max(abs(float(expanded) - self._from_p), abs(start - self._to_h) / max(1, self._natural_height(), start))
        self._anim.setDuration(max(1, round(duration * min(1.0, fraction))))
        self._anim.setEasingCurve(QEasingCurve(Animations.EASE_ENTER if expanded else Animations.EASE_EXIT))
        self._anim.start()

    def _frame(self, value):
        self._progress = self._from_p + (float(self._expanded) - self._from_p) * value
        self.progressChanged.emit(self._progress)
        height = round(self._from_h + (self._to_h - self._from_h) * value)
        if self._body.maximumHeight() != height:
            self._body.setMaximumHeight(height)
            self._changed()

    def refresh_target(self):
        if self._expanded and self._anim.state() == QAbstractAnimation.Running and self._natural_height() != self._to_h:
            self.set_expanded(True)

    def _finish(self):
        self._body.setVisible(self._expanded)
        self._body.setMaximumHeight(self._natural_max)
        self._progress = float(self._expanded)
        self.progressChanged.emit(self._progress)
        if self._settled is not None:
            self._settled(self._expanded)
        self._changed()
