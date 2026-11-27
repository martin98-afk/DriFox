"""同一组刻度收拢为提问索引、展开为逐行预览。只绘制可见行。"""

from PyQt5.QtCore import QEasingCurve, QEvent, QPoint, QRect, QRectF, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QFontMetrics, QPainter
from PyQt5.QtWidgets import QWidget

from app.utils.design_tokens import Colors
from app.utils.color_parse import parse_color
from app.utils.utils import get_unified_font


class _Outline(QWidget):
    """随 owner 的原生工具窗口；网页的原生表面不能插到刻度和文字之间。"""

    def __init__(self, nav):
        super().__init__(nav.window(), Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint | Qt.WindowDoesNotAcceptFocus)
        nav.destroyed.connect(self.deleteLater)
        self.nav = nav
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName('本轮提问')

    def paintEvent(self, event):
        n = self.nav
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        k = n.progress
        if not (n.hovered or n.hasFocus() or k > 0):
            p.fillRect(self.rect(), QColor(0, 0, 0, 1))
            return
        rect = QRectF(self.rect()).adjusted(.5, .5, -.5, -.5)
        bg = QColor(n.background)
        # 收起时仍保留可命中的透明热区（Windows 层叠窗口的 alpha=0 会穿透）。
        bg.setAlpha(round(12 + 243 * k))
        p.setBrush(bg)
        border = QColor(n.border)
        border.setAlpha(round(border.alpha() * k))
        p.setPen(border)
        p.drawRoundedRect(rect, 9, 9)
        h = n.row_height()
        total = n.count_visible()
        p.setFont(n.font())
        for idx in range(n.offset, n.offset + total):
            y = 6 + (idx - n.offset) * h
            active = idx == n.current
            if active and k > 0:
                hover = QColor(n.accent)
                hover.setAlpha(round(24 * k))
                p.setPen(Qt.NoPen)
                p.setBrush(hover)
                p.drawRoundedRect(QRectF(4, y, self.width() - 8, h), 5, 5)
            tick = QColor(n.accent if idx == n.reading else n.ink)
            tick.setAlpha(235 if idx == n.reading else 120)
            p.setBrush(tick)
            p.setPen(Qt.NoPen)
            length = 14 if idx == n.reading else 8
            p.drawRoundedRect(QRectF(self.width() - 6 - length, y + h / 2 - 1, length, 2), 1, 1)
            if k > .02:
                # 文字最终位置不动，随窗口向左揭开，不横向飞入。
                left = self.width() - n.full_width() + 14
                label = QColor(n.accent if active else n.ink)
                label.setAlpha(round(255 * k))
                p.setPen(label)
                p.drawText(QRectF(left, y, 32, h), Qt.AlignVCenter, str(idx + 1).zfill(2))
                p.drawText(QRectF(left + 34, y, n.text_width(), h), Qt.AlignVCenter, n.label(idx))

    def enterEvent(self, event):
        self.nav.hovered = True
        self.update()
        self.nav.leave_timer.stop()
        if not self.nav.opened:
            self.nav.enter_timer.start()

    def leaveEvent(self, event):
        self.nav._set_hovered(self.nav._approach_rect.contains(self.nav.viewport.mapFromGlobal(QCursor.pos())))
        self.nav.enter_timer.stop()
        if self.nav.opened and not self.nav.keyboard:
            self.nav.leave_timer.start()

    def mouseMoveEvent(self, event):
        if self.nav.progress > .95:
            self.nav.current = self.nav.row_at(event.pos())
            self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.nav.opened:
                self.nav.choose(self.nav.row_at(event.pos()))
            else:
                self.nav.expand()

    def wheelEvent(self, event):
        n = self.nav
        if n.opened:
            dy = event.pixelDelta().y() or event.angleDelta().y() / 120 * 3 * n.row_height()
            n.wheel_remainder += dy / n.row_height()
            steps = int(n.wheel_remainder)
            n.wheel_remainder -= steps
            n.offset = max(0, min(len(n.rows) - n.count_visible(), n.offset - steps))
            n.current = n.row_at(event.pos())
            self.update()
            event.accept()


class QuestionEdgeNavigator(QWidget):
    questionClicked = pyqtSignal(int)
    refreshRequested = pyqtSignal()

    def __init__(self, viewport):
        super().__init__(viewport)
        self.viewport = viewport
        self.rows = []
        self.session_id = None
        self.current = self.offset = self.reading = 0
        self.hovered = False
        self._approach_rect = QRect()
        viewport.setAttribute(Qt.WA_Hover)
        self.wheel_remainder = self.progress = 0.
        self.opened = self.keyboard = False
        self.surface = None
        self._labels = {}
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName('提问历史：回车展开，方向键选择')
        self.resize(1, 1)  # 留在宿主焦点链，浮层自身不激活、不抢输入焦点。
        self.enter_timer = QTimer(self, singleShot=True, interval=95)
        self.leave_timer = QTimer(self, singleShot=True, interval=180)
        self.enter_timer.timeout.connect(self.expand)
        self.leave_timer.timeout.connect(self._leave)
        self.anim = QVariantAnimation(self)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._frame)
        self._ancestors = []
        self._watch_ancestors()
        self.refresh_style()
        self.hide()

    def _watch_ancestors(self):
        for w in self._ancestors:
            w.removeEventFilter(self)
        self._ancestors = []
        w = self.viewport
        while w is not None:
            w.installEventFilter(self)
            self._ancestors.append(w)
            w = w.parentWidget()

    def refresh_style(self):
        self.setFont(get_unified_font(11))
        self.background = parse_color(Colors.CARD_BG.format(alpha=255))
        self.ink = parse_color(Colors.TEXT_PRIMARY)
        self.accent = parse_color(Colors.TEXT_ACCENT)
        self.border = parse_color(Colors.DIVIDER_COLOR)
        self._labels.clear()
        self._update_approach_rect()
        if self.surface:
            self.surface.update()

    refresh_theme = refresh_style

    def set_questions(self, previews, session_id):
        changed = session_id != self.session_id
        if changed or not previews:
            self.collapse(immediate=True)
            self.current = self.offset = self.reading = 0
            self.wheel_remainder = 0.
        if not changed and previews == self.rows:
            return
        self.rows, self.session_id = previews, session_id
        self._labels.clear()
        self.reading = min(self.reading, max(0, len(previews) - 1))
        self.current = min(self.current, max(0, len(previews) - 1))
        self.offset = min(self.offset, max(0, len(previews) - self.count_visible()))
        if previews and self.surface is None:
            self.surface = _Outline(self)
        self._update_approach_rect()
        self.setVisible(bool(previews))
        self._sync()

    def set_reading(self, row):
        if not 0 <= row < len(self.rows) or self.reading == row:
            return
        self.reading = row
        if not self.opened:
            self._follow_reading()
        if self.surface and (self.hovered or self.opened or self.hasFocus()):
            self.surface.update()

    def _follow_reading(self):
        self.current = self.reading
        self.offset = max(0, min(self.reading - self.count_visible() // 2, len(self.rows) - self.count_visible()))

    def count_visible(self):
        return min(len(self.rows), 12, max(1, (self.viewport.height() - 22) // self.expanded_row_height()))

    def full_width(self):
        return max(22, min(340, self.viewport.width() - 14))

    def text_width(self):
        return max(0, self.full_width() - 80)

    def label(self, idx):
        if idx not in self._labels:
            if len(self._labels) >= 96:
                self._labels.clear()
            self._labels[idx] = QFontMetrics(self.font()).elidedText(self.rows[idx], Qt.ElideRight, self.text_width())
        return self._labels[idx]

    def expanded_row_height(self):
        return max(30, QFontMetrics(self.font()).height() + 8)

    def row_height(self):
        return 7 + (self.expanded_row_height() - 7) * self.progress

    def row_at(self, pos):
        row = int((pos.y() - 6) // self.row_height())
        return self.offset + row if 0 <= row < self.count_visible() else -1

    def _set_hovered(self, inside):
        if inside != self.hovered:
            self.hovered = inside
            if self.surface:
                self.surface.update()

    def _update_approach_rect(self):
        width = self.full_width()
        height = 12 + self.count_visible() * self.expanded_row_height()
        self._approach_rect = QRect(self.viewport.width() - 5 - width, (self.viewport.height() - height) // 2, width, height)

    def _sync(self):
        if not self.surface:
            return
        if not self.isVisible() or not self.rows or self.window().isMinimized():
            self.surface.hide()
            return
        width = round(22 + (self.full_width() - 22) * self.progress)
        height = round(12 + self.count_visible() * self.row_height())
        anchor = self.viewport.mapToGlobal(QPoint(self.viewport.width() - 5, (self.viewport.height() - height) // 2))
        self.surface.setGeometry(anchor.x() - width, anchor.y(), width, height)
        if not self.surface.isVisible():
            self.surface.show()
        self.surface.update()

    def _frame(self, value):
        self.progress = float(value)
        self._sync()

    def _slide(self, target):
        self.anim.stop()
        self.anim.setStartValue(self.progress)
        self.anim.setEndValue(target)
        self.anim.setDuration(max(1, round(145 * abs(target - self.progress))))
        self.anim.start()

    def expand(self, keyboard=False):
        if self.rows and self.isVisible():
            self.keyboard = keyboard
            self.leave_timer.stop()
            if not self.opened:
                self._follow_reading()
                self.opened = True
                self._slide(1.)

    def collapse(self, immediate=False):
        self.enter_timer.stop()
        self.leave_timer.stop()
        self.keyboard = False
        if immediate:
            self.opened = False
            self.anim.stop()
            self._frame(0.)
        elif self.opened:
            self.opened = False
            self._slide(0.)

    def _leave(self):
        if self.surface and not self.surface.geometry().contains(QCursor.pos()):
            self.collapse()

    def choose(self, idx):
        if 0 <= idx < len(self.rows):
            self.current = idx
            self.collapse()
            self.questionClicked.emit(idx)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if self.opened:
                self.choose(self.current)
            else:
                self.current = max(0, self.current)
                self.expand(keyboard=True)
        elif key == Qt.Key_Escape:
            self.collapse()
        elif self.opened and key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Home, Qt.Key_End):
            self.current = {Qt.Key_Up: max(0, self.current - 1), Qt.Key_Down: min(len(self.rows) - 1, self.current + 1),
                            Qt.Key_Home: 0, Qt.Key_End: len(self.rows) - 1}[key]
            self.offset = min(self.current, max(self.offset, self.current - self.count_visible() + 1))
            self.surface.update()
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event):
        if self.keyboard:
            self.collapse()
        super().focusOutEvent(event)

    def showEvent(self, event):
        self._sync()

    def hideEvent(self, event):
        self.hovered = False
        self.collapse(immediate=True)
        if self.surface:
            self.surface.hide()

    def eventFilter(self, obj, event):
        typ = event.type()
        if obj is self.viewport and self.surface and self.isVisible():
            if typ in (QEvent.HoverMove, QEvent.HoverEnter):
                self._set_hovered(self._approach_rect.contains(event.pos()))
            elif typ == QEvent.HoverLeave:
                self._set_hovered(self.surface.underMouse())
        if typ in (QEvent.Resize, QEvent.Move, QEvent.WindowStateChange, QEvent.Show):
            if typ == QEvent.Resize:
                self._labels.clear()
                self._update_approach_rect()
                self.offset = min(self.offset, max(0, len(self.rows) - self.count_visible()))
            self._sync()
        elif typ == QEvent.ParentChange:
            self._watch_ancestors()
            self._sync()
        elif typ == QEvent.Hide and self.surface:
            self.surface.hide()
        return False
