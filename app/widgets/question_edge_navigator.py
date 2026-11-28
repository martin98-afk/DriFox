"""聊天视口左侧的提问刻度和单轮预览。"""

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass, field
from functools import cached_property
from hashlib import blake2s

from loguru import logger
from PyQt5.QtCore import QEasingCurve, QEvent, QPoint, QRect, QRectF, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QFontMetrics, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QAbstractButton, QWidget

from app.core.conversation.message_content import _is_hook_message, content_to_text
from app.utils import app_state
from app.utils.color_parse import parse_color
from app.utils.design_tokens import Colors
from app.utils.session_preview import get_reply_preview
from app.utils.utils import get_unified_font


@dataclass
class QuestionPreview:
    title: str
    key: str
    replies: list = field(default_factory=list, repr=False)

    @cached_property
    def excerpt(self):
        # 有工具调用的轮次可能有多条 assistant 消息。只在首次预览时提取最后一段可读回复。
        for content in reversed(self.replies):
            text = get_reply_preview(content_to_text(content), max_len=220)
            if text:
                return text
        return ""


def question_previews(messages):
    rows = []
    occurrences = Counter()
    for msg in messages:
        role = msg.get("role")
        if role == "user" and not _is_hook_message(msg):
            text = content_to_text(msg.get("content"))
            title = " ".join(text[:180].split()) + ("…" if len(text) > 180 else "")
            stamp = msg.get("ts_ms") or msg.get("timestamp", "")
            digest = blake2s(text.encode("utf-8", errors="replace"), digest_size=12).hexdigest()
            identity = f"{stamp}/{digest}"
            occurrences[identity] += 1
            rows.append(QuestionPreview(title or "附件", f"{identity}/{occurrences[identity]}"))
        elif role == "assistant" and rows and msg.get("content"):
            rows[-1].replies.append(msg["content"])
    return rows


_RAIL_WIDTH = 44
_PITCH = 12
_PAD = 5
_WINDOW_FLAGS = Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint | Qt.WindowDoesNotAcceptFocus


class _Surface(QWidget):
    """无焦点原生浮层，保持在聊天内嵌网页之上。"""

    def __init__(self, nav):
        super().__init__(nav.window(), _WINDOW_FLAGS)
        self.nav = nav
        nav.destroyed.connect(self.deleteLater)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def enterEvent(self, event):
        self.nav.leave_timer.stop()

    def leaveEvent(self, event):
        self.nav.enter_timer.stop()
        if self.nav.opened and not self.nav.keyboard:
            self.nav.leave_timer.start()


class _Rail(_Surface):
    def paintEvent(self, event):
        nav = self.nav
        painter = QPainter(self)
        # Windows 层叠窗口的零透明像素会穿透，刻度之间也要能命中。
        painter.fillRect(self.rect(), QColor(0, 0, 0, 1))
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        for slot in range(nav.count_visible()):
            distance = abs(slot - nav.lens)
            weight = max(0.0, 1 - distance / 4) ** 1.5
            emphasis = weight * nav.progress
            reading = slot == nav.slot_for_row(nav.reading)
            length = 7 + (8 * reading) * (1 - nav.progress) + 23 * emphasis
            length = min(length, max(1, self.width() - 5))
            color = QColor(nav.ink)
            color.setAlpha(round(64 + 110 * reading * (1 - nav.progress) + 185 * emphasis**3))
            painter.setBrush(color)
            y = _PAD + (slot + 0.5) * _PITCH
            painter.drawRoundedRect(QRectF(0, y - 1.1, length, 2.2), 0.5, 0.5)
            if nav.bookmark_in_slot(slot) is not None:
                painter.drawEllipse(QRectF(2 + length, y - 1.25, 2.5, 2.5))

    def row_at(self, pos):
        slot = min(self.nav.count_visible() - 1, max(0, (pos.y() - _PAD) // _PITCH))
        return self.nav.row_for_slot(slot)

    def _point(self, pos):
        nav = self.nav
        nav.keyboard = False
        nav.select(self.row_at(pos))
        if not nav.opened and not nav.enter_timer.isActive():
            nav.enter_timer.start()

    def enterEvent(self, event):
        super().enterEvent(event)
        self._point(self.mapFromGlobal(QCursor.pos()))

    def mouseMoveEvent(self, event):
        self._point(event.pos())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.nav.choose(self.row_at(event.pos()))

    def wheelEvent(self, event):
        nav = self.nav
        delta = event.pixelDelta().y() / _PITCH or event.angleDelta().y() / 120
        nav.wheel_remainder += delta
        steps = int(nav.wheel_remainder)
        nav.wheel_remainder -= steps
        row = self.row_at(event.pos())
        slot = nav.slot_for_row(row)
        start, end = nav.slot_bounds(slot)
        nav.select(max(start, min(end - 1, row - steps)))
        nav.expand()
        self.update()
        event.accept()


class _BookmarkButton(QAbstractButton):
    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(26, 26)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setAccessibleName("问题书签")
        self.setToolTip("切换问题书签")

    def paintEvent(self, event):
        nav = self.parentWidget().nav
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setOpacity(nav.progress)
        if self.underMouse():
            tint = QColor(nav.ink)
            tint.setAlpha(18)
            painter.setPen(Qt.NoPen)
            painter.setBrush(tint)
            painter.drawRoundedRect(QRectF(0, 0, 26, 26), 7, 7)
        path = QPainterPath()
        path.moveTo(7.5, 8)
        path.quadTo(7.5, 6, 9.5, 6)
        path.lineTo(16.5, 6)
        path.quadTo(18.5, 6, 18.5, 8)
        path.lineTo(18.5, 20)
        path.lineTo(13, 17)
        path.lineTo(7.5, 20)
        path.closeSubpath()
        painter.setPen(QPen(nav.ink if self.isChecked() else nav.secondary, 1.4))
        painter.setBrush(nav.ink if self.isChecked() else Qt.NoBrush)
        painter.drawPath(path)


class _Preview(_Surface):
    _wrap = Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap | Qt.TextWrapAnywhere

    def __init__(self, nav):
        super().__init__(nav)
        self.bookmark = _BookmarkButton(self)
        self.bookmark.clicked.connect(nav.toggle_bookmark)
        self.layout_key = None
        self.title = self.body = ""

    def prepare(self, row, width, max_height):
        nav = self.nav
        key = (id(row), width, max_height, nav.count_visible())
        if key == self.layout_key:
            return
        self.layout_key = key
        fm = QFontMetrics(nav.font())
        title_metrics = QFontMetrics(nav.title_font)
        self.title_height = max(26, title_metrics.height())
        self.title = title_metrics.elidedText(row.title, Qt.ElideRight, max(0, width - 64))
        start, end = nav.slot_bounds(nav.slot_for_row(nav.current))
        self.caption = f"第 {nav.current + 1} / {len(nav.rows)} 问 · 滚轮逐问浏览" if end - start > 1 else ""
        self.caption_height = fm.height() + 3 if self.caption else 0
        self.body = row.excerpt
        body_width = max(1, width - 36)
        bounds = QRect(0, 0, body_width, 10000)

        def height(text):
            return fm.boundingRect(bounds, self._wrap, text).height() if text else 0

        limit = min(fm.height() + 2 * fm.lineSpacing(), max_height - self.title_height - self.caption_height - 38)
        if limit < fm.height():
            self.body = ""
        elif height(self.body) > limit:
            lo, hi = 0, len(self.body)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if height(self.body[:mid] + "…") <= limit:
                    lo = mid
                else:
                    hi = mid - 1
            self.body = self.body[:lo].rstrip() + "…"
        self.resize(
            width,
            min(max_height, 32 + self.title_height + self.caption_height + (4 + height(self.body) if self.body else 0)),
        )
        self.bookmark.move(width - 42, 15)
        self.bookmark.setChecked(row.key in nav.bookmarks)
        self.setAccessibleName(row.title)
        self.setAccessibleDescription(self.body)

    def paintEvent(self, event):
        nav = self.nav
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setOpacity(nav.progress)
        card = QRectF(self.rect()).adjusted(6.5, 4.5, -6.5, -8.5)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 4))
        for spread in range(5, 0, -1):
            painter.drawRoundedRect(card.adjusted(-spread, 2 - spread, spread, 2 + spread), 13 + spread, 13 + spread)
        painter.setPen(nav.border)
        painter.setBrush(nav.background)
        painter.drawRoundedRect(card, 13, 13)
        painter.setPen(nav.ink)
        painter.setFont(nav.title_font)
        painter.drawText(QRect(18, 15, self.width() - 64, self.title_height), Qt.AlignVCenter, self.title)
        painter.setPen(nav.secondary)
        painter.setFont(nav.font())
        y = 19 + self.title_height
        if self.caption:
            caption = QFontMetrics(nav.font()).elidedText(self.caption, Qt.ElideRight, self.width() - 36)
            painter.drawText(QRect(18, y, self.width() - 36, self.caption_height), Qt.AlignVCenter, caption)
        painter.drawText(QRect(18, y + self.caption_height, self.width() - 36, self.height()), self._wrap, self.body)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.nav.opened:
            self.nav.choose(self.nav.current)


class QuestionEdgeNavigator(QWidget):
    questionClicked = pyqtSignal(int)
    refreshRequested = pyqtSignal()

    def __init__(self, viewport, scroll_area=None):
        super().__init__(viewport)
        self.viewport = viewport
        self.scroll_area = scroll_area
        self.rows = []
        self.session_id = None
        self.current = self.reading = 0
        self.progress = self.lens = self.wheel_remainder = 0.0
        self.opened = self.keyboard = False
        self.surface = self.preview = None
        self.bookmarks = set()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("提问历史，回车展开，方向键选择，B 切换书签")
        self.resize(1, 1)
        self.enter_timer = QTimer(self, singleShot=True, interval=65)
        self.leave_timer = QTimer(self, singleShot=True, interval=150)
        self.enter_timer.timeout.connect(self.expand)
        self.leave_timer.timeout.connect(self._leave)
        self.anim = QVariantAnimation(self)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._frame)
        self.anim.finished.connect(self._settled)
        self.track = QVariantAnimation(self)
        self.track.setDuration(115)
        self.track.setEasingCurve(QEasingCurve.OutCubic)
        self.track.valueChanged.connect(self._track_frame)
        self._ancestors = []
        self._watch_ancestors()
        self.refresh_style()
        self.hide()

    def _watch_ancestors(self):
        for widget in self._ancestors:
            widget.removeEventFilter(self)
        self._ancestors = []
        self.panel = self.scroll_area or self.viewport
        widget = self.viewport
        while widget is not None:
            if widget.objectName() == "chatWrapper":
                self.panel = widget
            widget.installEventFilter(self)
            self._ancestors.append(widget)
            widget = widget.parentWidget()

    def refresh_style(self):
        self.setFont(get_unified_font(11))
        self.title_font = get_unified_font(11, bold=True)
        self.background = parse_color(Colors.CARD_BG.format(alpha=255))
        self.ink = parse_color(Colors.TEXT_PRIMARY)
        self.secondary = parse_color(Colors.TEXT_SECONDARY)
        self.border = parse_color(Colors.DIVIDER_COLOR)
        if self.preview:
            self.preview.layout_key = None
        self._sync()

    refresh_theme = refresh_style

    def set_questions(self, previews, session_id):
        changed = session_id != self.session_id
        if changed or not previews:
            self.collapse(immediate=True)
            self.current = self.reading = 0
            self.lens = self.wheel_remainder = 0.0
        if changed:
            self.session_id = session_id
            self.bookmarks = set(app_state.get(self._bookmark_state_key(), []))
        self.rows = previews
        self._index_bookmarks()
        self.reading = min(self.reading, max(0, len(previews) - 1))
        self.current = min(self.current, max(0, len(previews) - 1))
        if previews and self.surface is None:
            self.surface = _Rail(self)
            self.surface.setAccessibleName("历史问题刻度")
            self.preview = _Preview(self)
        if self.preview:
            self.preview.layout_key = None
        self._sync_lens()
        self.setVisible(bool(previews))
        self._sync()

    def _bookmark_state_key(self):
        return f"question_bookmarks/{self.session_id}"

    def toggle_bookmark(self):
        if self.opened and self.rows:
            key = self.rows[self.current].key
            self.bookmarks = set(app_state.get(self._bookmark_state_key(), []))
            self.bookmarks.symmetric_difference_update({key})
            try:
                app_state.set(self._bookmark_state_key(), sorted(self.bookmarks))
            except OSError as error:
                logger.warning(f"问题书签暂时仅保存在内存中：{error}")
            self._index_bookmarks()
            self.preview.bookmark.setChecked(key in self.bookmarks)
            self.surface.update()

    def _index_bookmarks(self):
        self.marked_rows = [i for i, row in enumerate(self.rows) if row.key in self.bookmarks]

    def bookmark_in_slot(self, slot):
        if self.marked_rows:
            start, end = self.slot_bounds(slot)
            pos = bisect_left(self.marked_rows, start)
            if pos < len(self.marked_rows) and self.marked_rows[pos] < end:
                return self.marked_rows[pos]
        return None

    def count_visible(self):
        # 短会话一问一线；超出可用高度才合并。区段只取决于问题数和视口高度。
        capacity = max(1, (self.viewport.height() * 2 // 3 - 2 * _PAD) // _PITCH)
        return min(len(self.rows), capacity)

    def slot_for_row(self, row):
        return row * self.count_visible() // max(1, len(self.rows))

    def slot_bounds(self, slot):
        count = self.count_visible()
        n = len(self.rows)
        return (slot * n + count - 1) // count, ((slot + 1) * n + count - 1) // count

    def row_for_slot(self, slot):
        if (self.opened or self.enter_timer.isActive()) and self.slot_for_row(self.current) == slot:
            return self.current
        marked = self.bookmark_in_slot(slot)
        if marked is not None:
            return marked
        start, end = self.slot_bounds(slot)
        return end - 1 if end == len(self.rows) else start

    def _sync_lens(self):
        self.track.stop()
        self.lens = float(self.slot_for_row(self.current))

    def set_reading(self, row):
        if 0 <= row < len(self.rows) and self.reading != row:
            self.reading = row
            if not self.opened and not self.enter_timer.isActive() and self.progress == 0:
                self._follow_reading()
            self.surface.update()

    def _follow_reading(self):
        self.current = self.reading
        self._sync_lens()

    def select(self, row):
        if not 0 <= row < len(self.rows):
            return
        target = float(self.slot_for_row(row))
        if self.current == row and self.lens == target and self.track.state() != self.track.Running:
            return
        self.current = row
        if self.track.endValue() != target or self.track.state() != self.track.Running:
            self.track.stop()
            if self.progress and target != self.lens:
                self.track.setStartValue(self.lens)
                self.track.setEndValue(target)
                self.track.start()
            else:
                self.lens = target
        self._sync()

    def _sync(self):
        if not self.surface:
            return
        if not self.isVisible() or not self.rows or self.window().isMinimized():
            self.surface.hide()
            self.preview.hide()
            return
        bounds = self.viewport.rect()
        height = min(bounds.height(), 2 * _PAD + self.count_visible() * _PITCH)
        left = self.panel.mapToGlobal(QPoint()).x() - self.viewport.mapToGlobal(QPoint()).x()
        top = (bounds.height() - height) // 2
        origin = self.viewport.mapToGlobal(QPoint(0, 0))
        # 只使用面板已有留白；窄屏热区收为细条，悬停后在原位浮出。
        idle_width = min(_RAIL_WIDTH, max(12, -left))
        width = min(bounds.width(), round(idle_width + (_RAIL_WIDTH - idle_width) * self.progress))
        self.surface.setGeometry(origin.x() + left, origin.y() + top, width, height)
        self.surface.show()
        self.surface.update()
        x = left + width
        width = min(374, bounds.width() - left - _RAIL_WIDTH)
        if self.progress <= 0 or width < 84 or bounds.height() < 58:
            self.preview.hide()
            return
        self.preview.prepare(self.rows[self.current], width, bounds.height())
        y = round(top + _PAD + (self.lens + 0.5) * _PITCH - self.preview.height() / 2)
        self.preview.move(origin + QPoint(x, max(0, min(y, bounds.height() - self.preview.height()))))
        self.preview.show()
        self.preview.update()
        self.preview.bookmark.update()

    def _track_frame(self, value):
        self.lens = float(value)
        self._sync()

    def _frame(self, value):
        self.progress = float(value)
        self._sync()

    def _settled(self):
        if not self.opened and not self.enter_timer.isActive():
            self._follow_reading()
            self._sync()

    def _slide(self, target):
        self.anim.stop()
        self.anim.setStartValue(self.progress)
        self.anim.setEndValue(target)
        self.anim.setDuration(max(1, round(165 * abs(target - self.progress))))
        self.anim.start()

    def expand(self, keyboard=False):
        if self.rows and self.isVisible():
            self.keyboard = keyboard
            self.leave_timer.stop()
            if not self.opened:
                self.opened = True
                self._slide(1.0)

    def collapse(self, immediate=False):
        self.enter_timer.stop()
        self.leave_timer.stop()
        self.keyboard = False
        self.opened = False
        if immediate:
            self.track.stop()
            self.anim.stop()
            self._frame(0.0)
        elif self.progress or self.anim.state() == self.anim.Running:
            self._slide(0.0)

    def _leave(self):
        if not any(w.isVisible() and w.geometry().contains(QCursor.pos()) for w in (self.surface, self.preview)):
            self.collapse()

    def choose(self, idx):
        if 0 <= idx < len(self.rows):
            self.collapse()
            self.questionClicked.emit(idx)

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            if self.opened:
                self.choose(self.current)
            else:
                self.select(self.reading)
                self.expand(keyboard=True)
        elif key == Qt.Key_Escape:
            self.collapse()
        elif self.opened and key == Qt.Key_B:
            self.toggle_bookmark()
        elif self.opened and key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Home, Qt.Key_End):
            self.keyboard = True
            self.leave_timer.stop()
            row = {
                Qt.Key_Up: self.current - 1,
                Qt.Key_Down: self.current + 1,
                Qt.Key_Home: 0,
                Qt.Key_End: len(self.rows) - 1,
            }[key]
            self.select(row)
        else:
            super().keyPressEvent(event)

    def focusOutEvent(self, event):
        if self.keyboard:
            self.collapse()
        super().focusOutEvent(event)

    def showEvent(self, event):
        self._sync()

    def hideEvent(self, event):
        self.collapse(immediate=True)
        if self.surface:
            self.preview.hide()
            self.surface.hide()

    def eventFilter(self, obj, event):
        typ = event.type()
        if typ in (QEvent.Resize, QEvent.Move, QEvent.WindowStateChange, QEvent.Show):
            if typ == QEvent.Resize and obj is self.viewport:
                self._sync_lens()
            self._sync()
        elif typ == QEvent.ParentChange:
            self._watch_ancestors()
            if self.surface and self.surface.parentWidget() is not self.window():
                for layer in (self.surface, self.preview):
                    layer.setParent(self.window(), _WINDOW_FLAGS)
            self._sync()
        elif typ == QEvent.Hide and self.surface:
            self.preview.hide()
            self.surface.hide()
        return False
