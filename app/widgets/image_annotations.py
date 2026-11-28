"""普通图片预览层上的草稿标注工具。所有笔迹均使用原图像素坐标。"""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from PyQt5.QtCore import QPointF, QRectF, QTemporaryFile, Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QImage, QKeySequence, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLineEdit, QMessageBox, QShortcut, QToolButton

if TYPE_CHECKING:
    from app.widgets.image_lightbox import ImageLightbox


@dataclass
class Mark:
    path: QPainterPath
    pen: QPen
    brush: QBrush


class ImageAnnotations(QFrame):
    def __init__(self, view: "ImageLightbox", output_dir: Path):
        super().__init__(view)
        self.view = view
        self.output_dir = output_dir
        self.output_path = ""
        self.tool = ""
        self.marks: list[Mark] = []
        self.stroke: Mark | None = None
        self.start = QPointF()
        self.color = QColor("#ff453a")
        self.setObjectName("imageAnnotationTools")
        self.setStyleSheet("""
            QFrame#imageAnnotationTools { background: #292a2d; border-radius: 6px; }
            QToolButton { color: white; background: transparent; border: none; border-radius: 4px; padding: 4px 7px; }
            QToolButton:hover { background: #494b50; }
            QToolButton:checked { background: #565960; }
            QToolButton:disabled { color: #95969a; }
        """)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 3, 4, 3)
        row.setSpacing(2)
        self.toggle = QToolButton(self)
        self.toggle.setText("标注")
        self.toggle.setCheckable(True)
        row.addWidget(self.toggle)
        self.group = QButtonGroup(self)
        for mode, label in (("pen", "画笔"), ("rect", "矩形"), ("arrow", "箭头"), ("text", "文字")):
            button = QToolButton(self)
            button.setText(label)
            button.setCheckable(True)
            button.clicked.connect(lambda _, mode=mode: self.select_tool(mode))
            self.group.addButton(button)
            button.setChecked(mode == "pen")
            row.addWidget(button)
        self.undo_button = QToolButton(self)
        self.undo_button.setText("撤销")
        self.undo_button.setShortcut(QKeySequence.Undo)
        self.undo_button.clicked.connect(self.undo)
        row.addWidget(self.undo_button)
        self.apply_button = QToolButton(self)
        self.apply_button.setText("应用到输入框")
        self.apply_button.clicked.connect(self.apply)
        row.addWidget(self.apply_button)

        self.entry = QLineEdit(view)
        self.entry.setPlaceholderText("输入注释，按 Enter 确认")
        self.entry.setAccessibleName("图片文字注释")
        self.entry.setStyleSheet("background: #292a2d; color: white; border: 1px solid #aaa; padding: 3px;")
        self.entry.hide()
        cancel_text = QShortcut(QKeySequence(Qt.Key.Key_Escape), self.entry)
        cancel_text.setContext(Qt.ShortcutContext.WidgetShortcut)
        cancel_text.activated.connect(self.cancel_text)
        self.entry.editingFinished.connect(self.finish_text)
        self.toggle.toggled.connect(self._toggle_tools)
        self._toggle_tools(False)

    def _toggle_tools(self, editing):
        self.finish_text()
        for button in self.group.buttons():
            button.setVisible(editing)
        self.undo_button.setVisible(editing)
        self.apply_button.setVisible(editing or bool(self.marks))
        self.toggle.setText("平移" if editing else "标注")
        if editing:
            selected = self.group.checkedButton()
            if selected is not None:
                selected.click()
        else:
            self.select_tool("")
        self._refresh()

    def select_tool(self, tool: str):
        self.finish_text()
        self.tool = tool
        self.view.unsetCursor()
        self.view.update()

    def place_toolbar(self):
        self.adjustSize()
        self.move(max(0, (self.view.width() - self.width()) // 2), 6)

    def _refresh(self):
        self.undo_button.setEnabled(bool(self.marks))
        self.apply_button.setEnabled(bool(self.marks))
        self.place_toolbar()
        self.view.update()

    def _image_point(self, pos: QPointF):
        point = (pos - self.view.origin) / self.view.scale
        size = self.view.image.size()
        return QPointF(max(0.0, min(point.x(), size.width())), max(0.0, min(point.y(), size.height())))

    def press_at(self, pos: QPointF):
        self.finish_text()
        if not self.view.image_rect().contains(pos):
            return
        self.start = self._image_point(pos)
        if self.tool == "text":
            self.entry.setFixedWidth(min(220, max(1, self.view.width() - 8)))
            self.entry.clear()
            self.position_entry()
            self.entry.show()
            self.entry.setFocus()
            return
        pen = QPen(
            self.color, 3 / self.view.scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin
        )
        self.stroke = Mark(QPainterPath(self.start), pen, QBrush())

    def position_entry(self):
        pos = self.view.origin + self.start * self.view.scale
        self.entry.move(
            max(0, min(round(pos.x()), self.view.width() - self.entry.width())),
            max(0, min(round(pos.y()), self.view.height() - self.entry.height())),
        )

    def cancel_text(self):
        self.entry.clear()
        self.finish_text()

    def finish_text(self):
        if self.entry.isHidden():
            return
        text = self.entry.text().strip()
        self.entry.hide()
        if not text:
            return
        scale = self.view.scale
        font = QFont(self.view.font())
        font.setPixelSize(max(1, round(18 / scale)))
        font.setBold(True)
        path = QPainterPath()
        path.addText(QPointF(), font, text)
        bounds = path.boundingRect()
        x = max(0.0, min(self.start.x(), self.view.image.width() - bounds.width()))
        y = max(0.0, min(self.start.y(), self.view.image.height() - bounds.height()))
        path.translate(x - bounds.left(), y - bounds.top())
        self.marks.append(Mark(path, QPen(QColor("#221a1a"), 1 / scale), QBrush(self.color)))
        self._refresh()

    def move_to(self, pos: QPointF):
        mark = self.stroke
        if mark is None:
            self.view.setCursor(Qt.CursorShape.IBeamCursor if self.tool == "text" else Qt.CursorShape.CrossCursor)
            return
        end = self._image_point(pos)
        if self.tool == "pen":
            mark.path.lineTo(end)
        elif self.tool == "rect":
            mark.path = QPainterPath()
            mark.path.addRect(QRectF(self.start, end).normalized())
        else:
            mark.path = QPainterPath(self.start)
            mark.path.lineTo(end)
            delta = end - self.start
            angle = math.atan2(delta.y(), delta.x())
            length = min(math.hypot(delta.x(), delta.y()) / 2, mark.pen.widthF() * 4)
            for side in (-0.55, 0.55):
                mark.path.moveTo(end)
                mark.path.lineTo(end - QPointF(math.cos(angle + side), math.sin(angle + side)) * length)
        self.view.update()

    def release_at(self, pos: QPointF):
        if self.stroke is not None:
            self.move_to(pos)
            if not self.stroke.path.isEmpty():
                self.marks.append(self.stroke)
            self.stroke = None
            self._refresh()

    def undo(self):
        if self.marks:
            self.marks.pop()
            self._refresh()

    def draw(self, painter: QPainter):
        painter.setRenderHint(QPainter.Antialiasing)
        for mark in self.marks:
            painter.setPen(mark.pen)
            painter.setBrush(mark.brush)
            painter.drawPath(mark.path)
        if self.stroke:
            painter.setPen(self.stroke.pen)
            painter.setBrush(self.stroke.brush)
            painter.drawPath(self.stroke.path)

    def apply(self):
        self.finish_text()
        if not self.marks:
            return
        result = self.view.image.toImage().convertToFormat(QImage.Format_ARGB32_Premultiplied)
        result.setDevicePixelRatio(1)
        painter = QPainter(result)
        try:
            self.draw(painter)
        finally:
            painter.end()
        try:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            file = QTemporaryFile(str(self.output_dir / "标注_XXXXXX.png"))
            if not file.open() or not result.save(file, "PNG") or not file.flush():
                raise OSError(file.errorString())
            file.close()
            file.setAutoRemove(False)
            self.output_path = file.fileName()
        except OSError as exc:
            QMessageBox.warning(self.view, "无法保存标注图片", str(exc))
            return
        self.view.accept()
