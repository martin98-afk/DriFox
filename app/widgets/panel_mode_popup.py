# -*- coding: utf-8 -*-
"""对话页面板显示模式选择悬浮框

在 TabPanel 顶部「竖向 ⋯」按钮下弹出，供用户在「列表模式 / 工作区树模式」间切换。

选用 ``Qt.Popup`` 而非 QMenu 的原因：需要「标题 + 主标签 + 说明文字 + 勾选态」的
多行排版，QMenu 的 ::item 自绘空间不够；而 Qt.Popup 自带「点击外部自动关闭」
与「不抢焦点」语义，正好是这个场景要的。

约束：
- 背景自绘（圆角 + 边框），因为 ``Colors.CARD_BG_SOLID`` 是 rgba() 字符串，
  QSS 能解析但 QPainter 需要 QColor，统一走本模块的 ``_parse_rgba``。
- ``WA_DeleteOnClose`` + ``destroyed`` 信号让宿主清掉悬空引用（二次点击收起）。
"""

from typing import List, Tuple

from PyQt5.QtCore import QRectF, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPainterPath
from PyQt5.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css, get_unified_font


def _parse_rgba(rgba_str: str) -> QColor:
    """解析 'rgba(r,g,b,a)' / '#rrggbb' / 颜色名 → QColor（QColor 不认 CSS rgba）"""
    try:
        text = (rgba_str or "").strip()
        if text.startswith("rgba(") or text.startswith("rgb("):
            parts = text.strip("rgba() ").split(",")
            r, g, b = int(float(parts[0])), int(float(parts[1])), int(float(parts[2]))
            if len(parts) > 3:
                a_raw = parts[3].strip()
                a = int(float(a_raw) * 255) if float(a_raw) <= 1 else int(float(a_raw))
            else:
                a = 255
            return QColor(r, g, b, a)
    except Exception:
        pass
    return QColor(rgba_str)


class _ModeRow(QFrame):
    """单个模式选项行：主标签 + 说明 + 右侧勾"""

    clicked = pyqtSignal(str)

    def __init__(self, mode: str, label: str, desc: str, checked: bool, parent=None):
        super().__init__(parent)
        self._mode = mode
        self._checked = checked
        self._hovered = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(42)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(0)

        self._label = QLabel(label, self)
        self._label.setFont(get_unified_font(12))
        self._desc = QLabel(desc, self)
        self._desc.setFont(get_unified_font(10))
        text_box.addWidget(self._label)
        text_box.addWidget(self._desc)
        layout.addLayout(text_box, 1)

        self._check = QLabel("✓" if checked else "", self)
        self._check.setFont(get_unified_font(12))
        layout.addWidget(self._check)

        self._apply_appearance()

    def _apply_appearance(self):
        Colors.refresh()
        self.setStyleSheet("QFrame { background: transparent; border: none; }")
        self._label.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(12)};"
        )
        self._desc.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(10)};"
        )
        self._check.setStyleSheet(
            f"color: {Colors.TEXT_PRIMARY}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(12)};"
        )

    def refresh_style(self):
        self._apply_appearance()

    def enterEvent(self, event):  # noqa: N802 - Qt 约定
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802 - Qt 约定
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802 - Qt 约定
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._mode)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event):  # noqa: N802 - Qt 约定
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        if self._hovered or self._checked:
            path = QPainterPath()
            path.addRoundedRect(QRectF(2, 2, self.width() - 4, self.height() - 4), 6, 6)
            if self._hovered:
                painter.fillPath(path, _parse_rgba(Colors.HOVER_BG))
            if self._checked:
                painter.fillPath(path, _parse_rgba(Colors.SELECTED_BG))
        super().paintEvent(event)


class PanelModePopup(QWidget):
    """模式选择悬浮框

    Args:
        options: [(mode, 主标签, 说明), ...]
        current: 当前选中的 mode
    """

    modeSelected = pyqtSignal(str)

    def __init__(self, options: List[Tuple[str, str, str]], current: str, parent=None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._rows: List[_ModeRow] = []
        self._build_ui(options, current)

    def _build_ui(self, options, current):
        Colors.refresh()
        self._container = QWidget(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._container)

        layout = QVBoxLayout(self._container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(2)

        title = QLabel("对话页显示模式", self._container)
        title.setFont(get_unified_font(11))
        title.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; background: transparent; "
            f"{get_font_family_css()} {font_size_css(11)}; padding: 0px 6px;"
        )
        layout.addWidget(title)

        for mode, label, desc in options:
            row = _ModeRow(mode, label, desc, mode == current, self._container)
            row.clicked.connect(self._on_row_clicked)
            layout.addWidget(row)
            self._rows.append(row)

    def _on_row_clicked(self, mode: str):
        self.modeSelected.emit(mode)
        self.close()

    def refresh_style(self):
        for row in self._rows:
            row.refresh_style()

    def sizeHint(self):  # noqa: N802 - Qt 约定
        return self._container.sizeHint()

    def paintEvent(self, event):  # noqa: N802 - Qt 约定
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -1.5, -1.5)
        painter.setPen(Qt.NoPen)
        painter.setBrush(_parse_rgba(Colors.CARD_BG_SOLID))
        painter.drawRoundedRect(rect, 10, 10)
        painter.setPen(_parse_rgba(Colors.BORDER))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, 10, 10)
