# -*- coding: utf-8 -*-
"""
历史问题卡片内容组件

展示当前会话中所有用户提问，点击可快速跳转到对应位置。
通过 BaseSettingsCard 包裹后嵌入 TopCardContainer。
"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css, get_unified_font

_ITEM_MIN_H = 46
_ITEM_SPACING = 6


class _QuestionItem(QWidget):
    """单个历史问题条目"""

    clicked = pyqtSignal(int)

    def __init__(self, index: int, text: str, parent=None):
        super().__init__(parent)
        self._index = index
        self._text = text
        self._hovered = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(_ITEM_MIN_H)
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)

        self._dot = QLabel(f"{self._index + 1}")
        self._dot.setFont(get_unified_font(11, bold=True))
        self._dot.setFixedSize(22, 22)
        self._dot.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._dot)

        self._label = QLabel(self._text)
        self._label.setFont(get_unified_font(13))
        self._label.setWordWrap(True)
        self._label.setAlignment(Qt.AlignVCenter)
        self._label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self._label, 1)

        self._arrow = QLabel("›")
        self._arrow.setFont(get_unified_font(16, bold=True))
        self._arrow.setFixedWidth(18)
        self._arrow.setAlignment(Qt.AlignCenter)
        self._arrow.setVisible(False)
        layout.addWidget(self._arrow)

        self._apply_style()

    def _apply_style(self):
        Colors.refresh()
        bg = Colors.HOVER_BG_STRONG if self._hovered else "transparent"
        dot_bg = Colors.TEXT_ACCENT if self._hovered else "rgba(255, 255, 255, 0.10)"
        text_color = Colors.TEXT_PRIMARY
        arrow_color = Colors.TEXT_ACCENT

        self.setStyleSheet(f"background: {bg}; border-radius: 8px;")
        self._dot.setStyleSheet(
            f"background: {dot_bg}; color: #ffffff; border-radius: 11px;{get_font_family_css()} {font_size_css(11)}"
        )
        self._label.setStyleSheet(
            f"color: {text_color}; background: transparent;{get_font_family_css()} {font_size_css(13)}"
        )
        self._arrow.setStyleSheet(
            f"color: {arrow_color}; background: transparent;{get_font_family_css()} {font_size_css(16)}"
        )

    def enterEvent(self, event):
        self._hovered = True
        self._arrow.setVisible(True)
        self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._arrow.setVisible(False)
        self._apply_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._index)
        super().mousePressEvent(event)


class HistoryQuestionsCardContent(QWidget):
    """历史问题卡片的内容（问题条目列表），由 BaseSettingsCard 包裹"""

    questionClicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._questions = []
        self._items = []
        self._setup_ui()

    def sizeHint(self):
        """显式返回高度，确保 BaseSettingsCard 的 content 模式正确展开"""
        from PyQt5.QtCore import QSize

        if not self._items:
            return super().sizeHint()
        count = len(self._items)
        h = count * _ITEM_MIN_H + (count - 1) * _ITEM_SPACING
        margins = self.layout().contentsMargins()
        h += margins.top() + margins.bottom()
        return QSize(max(300, super().sizeHint().width()), h)

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(_ITEM_SPACING)

        # 空状态（默认显示）
        self._empty_label = QLabel("当前会话暂无历史问题", self)
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setFont(get_unified_font(11))
        Colors.refresh()
        self._empty_label.setStyleSheet(f"color: {Colors.INPUT_PLACEHOLDER}; background: transparent; padding: 24px;")
        main_layout.addWidget(self._empty_label)

    def refresh_style(self):
        Colors.refresh()
        self._empty_label.setStyleSheet(f"color: {Colors.INPUT_PLACEHOLDER}; background: transparent; padding: 24px;")
        for item in self._items:
            item._apply_style()

    def set_questions(self, questions: list):
        """设置问题列表

        Args:
            questions: [(index, text), ...] 列表
        """
        self._questions = questions
        self._rebuild_items()

    def _rebuild_items(self):
        # 清除旧条目
        for item in self._items:
            self.layout().removeWidget(item)
            item.deleteLater()
        self._items.clear()

        has_data = len(self._questions) > 0
        self._empty_label.setVisible(not has_data)

        if not has_data:
            return

        display = self._questions
        if len(display) > 50:
            display = display[-50:]

        for idx, text in display:
            item = _QuestionItem(idx, text, self)
            item.clicked.connect(self._on_item_clicked)
            self._items.append(item)
            self.layout().addWidget(item)

        self.layout().addStretch()

    def _on_item_clicked(self, index: int):
        """条目被点击，发出信号"""
        self.questionClicked.emit(index)
