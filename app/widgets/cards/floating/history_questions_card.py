# -*- coding: utf-8 -*-
"""
历史问题卡片

展示当前会话中所有用户提问，点击可快速跳转到对应位置。
以卡片形式嵌入 TopCardContainer，与分享卡片一致。
"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, TransparentToolButton

from app.utils.design_tokens import Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import get_font_family_css, get_unified_font

_MAX_VISIBLE_ITEMS = 8
_ITEM_MIN_H = 46
_ITEM_SPACING = 6
_SCROLL_PAD = 6


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
        dot_text = "#ffffff"
        text_color = Colors.TEXT_PRIMARY
        arrow_color = Colors.TEXT_ACCENT

        self.setStyleSheet(f"background: {bg}; border-radius: 8px;")
        self._dot.setStyleSheet(
            f"background: {dot_bg}; color: {dot_text}; border-radius: 11px;{get_font_family_css()} {font_size_css(11)}"
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


class HistoryQuestionsCard(QFrame):
    """历史问题卡片：展示当前会话所有用户提问，点击跳转"""

    questionClicked = pyqtSignal(int)
    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._questions = []
        self._items = []
        self._setup_ui()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self._apply_card_style()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 8, 10, 10)
        main_layout.setSpacing(6)

        # ── 标题栏 ──
        header = QHBoxLayout()
        header.setSpacing(6)

        title_icon = QLabel("💬")
        title_icon.setFont(get_unified_font(13))

        title = QLabel("历史问题")
        title.setFont(get_unified_font(11, bold=True))
        Colors.refresh()
        title.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; background: transparent;")

        self._count_label = QLabel("")
        self._count_label.setFont(get_unified_font(10))
        self._count_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; background: transparent;")

        header.addWidget(title_icon)
        header.addWidget(title)
        header.addSpacing(4)
        header.addWidget(self._count_label)
        header.addStretch()

        self.close_btn = TransparentToolButton(FluentIcon.CLOSE, self)
        self.close_btn.setFixedSize(22, 22)
        self.close_btn.clicked.connect(lambda: self.closed.emit())
        header.addWidget(self.close_btn)

        main_layout.addLayout(header)

        # ── 分隔线 ──
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setFixedHeight(1)
        Colors.refresh()
        sep.setStyleSheet(f"color: {Colors.DIVIDER_COLOR}; background: {Colors.DIVIDER_COLOR}; border: none;")
        main_layout.addWidget(sep)

        # ── 滚动区域 ──
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)
        self._apply_scroll_style()

        self.scroll_content = QWidget()
        self.scroll_content.setStyleSheet("background: transparent;")
        self.scroll_layout = QVBoxLayout(self.scroll_content)
        self.scroll_layout.setContentsMargins(0, 2, 0, 4)
        self.scroll_layout.setSpacing(_ITEM_SPACING)
        self.scroll_area.setWidget(self.scroll_content)

        main_layout.addWidget(self.scroll_area, 1)

        # 空状态
        self._empty_label = QLabel("当前会话暂无历史问题", self)
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setFont(get_unified_font(11))
        self._empty_label.setStyleSheet(f"color: {Colors.INPUT_PLACEHOLDER}; background: transparent; padding: 20px;")
        main_layout.addWidget(self._empty_label)

    def _apply_card_style(self):
        Colors.refresh()
        self.setStyleSheet(
            f"""
            HistoryQuestionsCard {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-radius: 10px;
            }}
            """
        )

    def _apply_scroll_style(self):
        self.scroll_area.setStyleSheet(
            f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollArea > QWidget#qt_scrollarea_viewport {{
                background: transparent;
            }}
            {get_unified_scrollbar_style(8)}
            """
        )

    def refresh_style(self):
        """响应主题切换"""
        self._apply_card_style()
        self._apply_scroll_style()
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
        """重建条目，滚动区自适应高度"""
        for item in self._items:
            self.scroll_layout.removeWidget(item)
            item.deleteLater()
        self._items.clear()

        has_data = len(self._questions) > 0
        self._empty_label.setVisible(not has_data)
        self.scroll_area.setVisible(has_data)
        self._count_label.setVisible(has_data)
        if has_data:
            self._count_label.setText(f"共 {len(self._questions)} 条")

        if not has_data:
            self.scroll_area.setMaximumHeight(0)
            return

        display_questions = self._questions
        if len(display_questions) > 50:
            display_questions = display_questions[-50:]

        for idx, text in display_questions:
            item = _QuestionItem(idx, text, self)
            item.clicked.connect(self._on_item_clicked)
            self._items.append(item)
            self.scroll_layout.addWidget(item)

        # 自适应滚动区高度：最多 _MAX_VISIBLE_ITEMS 条
        item_count = len(display_questions)
        if item_count <= _MAX_VISIBLE_ITEMS:
            self.scroll_area.setMaximumHeight(16777215)
        else:
            max_content = _MAX_VISIBLE_ITEMS * _ITEM_MIN_H + (_MAX_VISIBLE_ITEMS - 1) * _ITEM_SPACING + _SCROLL_PAD
            self.scroll_area.setMaximumHeight(max_content)

    def _on_item_clicked(self, index: int):
        """条目被点击，发出信号并关闭卡片"""
        self.questionClicked.emit(index)
        self.closed.emit()
