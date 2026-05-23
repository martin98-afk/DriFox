# -*- coding: utf-8 -*-
from functools import partial

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import PrimaryPushButton, FluentIcon

from app.utils.design_tokens import Colors
from app.utils.utils import get_unified_font, get_font_family_css
from app.widgets.cards.settings.system_card_frame import SystemCardFrame


class WrappedOptionButton(QPushButton):
    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._selected = False
        self._setup_ui(text)

    def _setup_ui(self, text: str):
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.setText("")
        self.setMinimumHeight(44)
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(10)

        self.label = QLabel(text, self)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.label.setFont(get_unified_font(10))
        self.label.setStyleSheet(f"color: {Colors.REALTIME_TEXT}; background: transparent;")
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        layout.addWidget(self.label)

        self.hint_label = QLabel("点击选择", self)
        self.hint_label.setFont(get_unified_font(9))
        self.hint_label.setStyleSheet(f"color: {Colors.REALTIME_ACCENT}; background: transparent;")
        self.hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.hint_label, 0, Qt.AlignRight | Qt.AlignVCenter)

        self._apply_state_style()

    def text(self):
        return self.label.text()

    def _apply_state_style(self):
        Colors.refresh()
        background = "rgba(255, 255, 255, 0.05)"
        text_color = Colors.REALTIME_TEXT
        hint_color = Colors.REALTIME_ACCENT
        if self._selected:
            background = Colors.REALTIME_TAG_BG
            text_color = "#ffffff"
        self.setStyleSheet(
            f"""
            QPushButton {{
                background-color: {background};
                border: 1px solid {Colors.REALTIME_TAG_BORDER};
                border-radius: 8px;
                text-align: left;
            }}
            QPushButton:hover {{
                background-color: {Colors.REALTIME_TAG_BG};
                border: 1px solid {Colors.REALTIME_ACCENT};
            }}
            QPushButton:pressed {{
                background-color: {Colors.REALTIME_TAG_BG};
                border: 1px solid {Colors.REALTIME_ACCENT};
            }}
            """
        )
        self.label.setStyleSheet(f"color: {text_color}; background: transparent;")
        self.hint_label.setStyleSheet(
            f"color: {hint_color}; background: transparent; font-size: 9pt;"
        )

    def set_selected(self, selected: bool):
        self._selected = selected
        self._apply_state_style()


class WrappedCheckOption(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self._hovered = False
        self._setup_ui(text)

    def _setup_ui(self, text: str):
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        self.checkbox = QCheckBox("", self)
        self.checkbox.setCursor(Qt.PointingHandCursor)
        self.checkbox.setStyleSheet(
            f"""
            QCheckBox {{
                background: transparent;
                border: none;
                padding: 0;
                margin: 0;
            }}
            QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                border-radius: 4px;
                border: 1px solid {Colors.REALTIME_TAG_BORDER};
                background-color: {Colors.REALTIME_BG};
            }}
            QCheckBox::indicator:checked {{
                background-color: {Colors.REALTIME_ACCENT};
                border-color: {Colors.REALTIME_ACCENT};
            }}
            """
        )

        self.label = QLabel(text, self)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.label.setFont(get_unified_font(10))
        self.label.setStyleSheet(f"color: {Colors.REALTIME_TEXT}; background: transparent;")
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        layout.addWidget(self.checkbox, 0, Qt.AlignTop)
        layout.addWidget(self.label, 1)

        self.checkbox.toggled.connect(self.toggled.emit)
        self.checkbox.toggled.connect(lambda _checked: self._apply_state_style())
        self._apply_state_style()

    def text(self):
        return self.label.text()

    def isChecked(self):
        return self.checkbox.isChecked()

    def setChecked(self, checked: bool):
        self.checkbox.setChecked(checked)

    def _apply_state_style(self):
        Colors.refresh()
        background = "rgba(255, 255, 255, 0.04)"
        border = Colors.REALTIME_TAG_BORDER
        if self._hovered:
            background = Colors.REALTIME_TAG_BG
            border = Colors.REALTIME_ACCENT
        if self.checkbox.isChecked():
            border = Colors.REALTIME_ACCENT
            background = Colors.REALTIME_TAG_BG
        self.setStyleSheet(
            f"""
            WrappedCheckOption {{
                background-color: {background};
                border: 1px solid {border};
                border-radius: 8px;
            }}
            """
        )

    def enterEvent(self, event):
        self._hovered = True
        self._apply_state_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        self._apply_state_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.checkbox.toggle()
        super().mousePressEvent(event)


class QuestionFloatingWidget(SystemCardFrame):
    """悬浮提问卡片，支持单选、多选和切换为文本输入"""

    answered = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, parent=None):
        self._question = ""
        self._options = []
        self._multiple = False
        self._text_input_mode = False
        self._option_widgets = []
        super().__init__(parent)
        # 覆盖 SystemCardFrame 的固定高度
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self._override_style()
        self._setup_question_ui()

    def _override_style(self):
        """覆盖 SystemCardFrame 样式，使用 REALTIME 颜色"""
        Colors.refresh()
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-radius: 8px;
            }}
        """)

    def refresh_style(self):
        """响应主题切换"""
        Colors.refresh()
        self._override_style()
        self._refresh_question_label_style()
        self._apply_toggle_btn_style()
        self._apply_text_input_style()
        self._apply_confirm_btn_style()

    def _setup_question_ui(self):
        """设置卡片内容（替换 SystemCardFrame 的 scroll_area）"""
        # 移除现有的 scroll_area
        if hasattr(self, 'scroll_area') and self.scroll_area:
            self.scroll_area.deleteLater()

        # 清空 content_layout 中的所有 widget
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 设置自定义标题
        self.icon_label.setText("?")
        self.title_label.setText("等待你的选择")

        # ── 模式标签 ──
        self._mode_hint_label = QLabel("", self)
        self._mode_hint_label.setFont(get_unified_font(9))
        self._mode_hint_label.setStyleSheet(
            f"""
            color: {Colors.REALTIME_ACCENT};
            background-color: {Colors.REALTIME_TAG_BG};
            border: 1px solid {Colors.REALTIME_TAG_BORDER};
            border-radius: 10px;
            padding: 2px 8px;
            """
        )
        self._mode_hint_label.setVisible(False)
        self._header_layout.insertWidget(2, self._mode_hint_label)

        # ── 问题文本 ──
        self._question_label = QLabel("", self)
        self._question_label.setFont(get_unified_font(10))
        self._refresh_question_label_style()
        self._question_label.setWordWrap(True)
        self._question_label.setMinimumHeight(28)
        self._content_layout.addWidget(self._question_label)

        # ── 选项区（ScrollArea）──
        self._options_scroll = QScrollArea(self)
        self._options_scroll.setWidgetResizable(True)
        self._options_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._options_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._options_scroll.setMaximumHeight(300)
        self._options_scroll.setMinimumHeight(0)
        self._options_scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollArea > QWidget > QWidget { background: transparent; }
            QScrollBar:vertical { width: 6px; background: transparent; }
            QScrollBar::handle:vertical { background: #555; border-radius: 3px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

        self._options_scroll_content = QWidget()
        self._options_scroll_content.setStyleSheet("background: transparent;")
        self._options_layout = QGridLayout(self._options_scroll_content)
        self._options_layout.setContentsMargins(0, 0, 0, 0)
        self._options_layout.setHorizontalSpacing(10)
        self._options_layout.setVerticalSpacing(10)

        self._options_scroll.setWidget(self._options_scroll_content)
        self._content_layout.addWidget(self._options_scroll, 1)

        # ── 文本输入（默认隐藏）──
        self._text_input = QTextEdit(self)
        self._text_input.setPlaceholderText("输入你想补充的内容")
        self._text_input.setFont(get_unified_font(10))
        self._text_input.setMaximumHeight(104)
        self._text_input.setVisible(False)
        self._text_input.textChanged.connect(self._update_submit_state)
        self._apply_text_input_style()
        self._content_layout.addWidget(self._text_input)

        # ── 底部栏：提示 + 切换按钮 ──
        self._bottom_bar = QHBoxLayout()
        self._bottom_bar.setSpacing(8)

        self._custom_hint_label = QLabel("没有合适的选项？", self)
        self._custom_hint_label.setFont(get_unified_font(9))
        self._custom_hint_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")

        self._toggle_text_mode_btn = QPushButton("改为输入", self)
        self._toggle_text_mode_btn.setCursor(Qt.PointingHandCursor)
        self._apply_toggle_btn_style()
        self._toggle_text_mode_btn.clicked.connect(self._toggle_text_mode)

        self._bottom_bar.addWidget(self._custom_hint_label)
        self._bottom_bar.addStretch()
        self._bottom_bar.addWidget(self._toggle_text_mode_btn)
        self._content_layout.addLayout(self._bottom_bar)

        # ── Footer：选择提示 + 提交按钮 ──
        self._footer_layout = QHBoxLayout()
        self._footer_layout.setSpacing(8)

        self._selection_hint_label = QLabel("", self)
        self._selection_hint_label.setFont(get_unified_font(9))
        self._selection_hint_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")

        self._confirm_btn = PrimaryPushButton("提交", self)
        self._confirm_btn.setCursor(Qt.PointingHandCursor)
        self._confirm_btn.clicked.connect(self._on_confirm)
        self._apply_confirm_btn_style()

        self._footer_layout.addWidget(self._selection_hint_label)
        self._footer_layout.addStretch()
        self._footer_layout.addWidget(self._confirm_btn)
        self._content_layout.addLayout(self._footer_layout)

        self._update_mode_ui()

    def _refresh_question_label_style(self):
        self._question_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")

    def _apply_toggle_btn_style(self):
        self._toggle_text_mode_btn.setStyleSheet(
            f"""
            QPushButton {{
                color: {Colors.REALTIME_ACCENT};
                background-color: {Colors.REALTIME_TAG_BG};
                border: 1px solid {Colors.REALTIME_TAG_BORDER};
                border-radius: 6px;
                padding: 6px 12px;
                {get_font_family_css()} font-size: 11px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: {Colors.REALTIME_TAG_BG.replace("0.15", "0.25")};
                border-color: {Colors.REALTIME_ACCENT};
            }}
            """
        )

    def _apply_text_input_style(self):
        self._text_input.setStyleSheet(
            f"""
            QTextEdit {{
                background-color: {Colors.REALTIME_BG};
                color: {Colors.REALTIME_TEXT};
                border: 1px solid {Colors.REALTIME_TAG_BORDER};
                border-radius: 8px;
                padding: 10px 12px;
                selection-background-color: {Colors.REALTIME_ACCENT};
                {get_font_family_css()} font-size: 10pt;
            }}
            QTextEdit:focus {{
                border-color: {Colors.REALTIME_ACCENT};
            }}
            """
        )

    def _apply_confirm_btn_style(self):
        self._confirm_btn.setStyleSheet(
            f"""
            PrimaryPushButton {{
                background-color: {Colors.REALTIME_ACCENT};
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 7px 18px;
                {get_font_family_css()} font-size: 11px;
                font-weight: bold;
            }}
            PrimaryPushButton:hover {{
                background-color: {Colors.REALTIME_BORDER};
            }}
            PrimaryPushButton:disabled {{
                background-color: #3f4b5f;
                color: #93a0b4;
            }}
            """
        )

    def _clear_options(self):
        self._option_widgets = []
        while self._options_layout.count():
            item = self._options_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    def _option_label(self, option):
        if isinstance(option, dict):
            return option.get("label", str(option))
        return str(option)

    def _selected_options(self):
        return [
            widget.text()
            for widget in self._option_widgets
            if isinstance(widget, WrappedCheckOption) and widget.isChecked()
        ]

    def _has_text_input(self):
        return bool(self._text_input.toPlainText().strip())

    def _build_answer(self):
        text = self._text_input.toPlainText().strip()

        if self._multiple:
            selected = self._selected_options()
            if selected and text:
                return f"已选：{'、'.join(selected)}；补充：{text}"
            if selected:
                return "、".join(selected)
            return text

        return text

    def _update_mode_ui(self):
        has_options = bool(self._options)
        text_visible = self._text_input_mode or not has_options

        self._options_scroll.setVisible(has_options and not text_visible)
        self._custom_hint_label.setVisible(has_options)
        self._toggle_text_mode_btn.setVisible(has_options)
        self._text_input.setVisible(text_visible)

        if not has_options:
            self._mode_hint_label.setVisible(True)
            self._mode_hint_label.setText("文本输入")
            self._selection_hint_label.setText("直接输入回答")
        elif self._multiple:
            self._mode_hint_label.setVisible(True)
            self._mode_hint_label.setText("多选")
            if text_visible:
                self._selection_hint_label.setText("可多选，也可补充说明")
                self._toggle_text_mode_btn.setText("返回选项")
            else:
                self._selection_hint_label.setText("请选择一个选项")
                self._toggle_text_mode_btn.setText("改为输入")
        else:
            self._mode_hint_label.setVisible(True)
            self._mode_hint_label.setText("单选")
            if text_visible:
                self._selection_hint_label.setText("文本输入会替代选项选择")
                self._toggle_text_mode_btn.setText("返回选项")
            else:
                self._selection_hint_label.setText("请选择一个选项")
                self._toggle_text_mode_btn.setText("改为输入")

        self._update_submit_state()

    def _update_submit_state(self):
        if not self._options:
            self._confirm_btn.setVisible(True)
            self._confirm_btn.setEnabled(self._has_text_input())
            self._confirm_btn.setText("提交")
            return

        if self._multiple:
            selected_count = len(self._selected_options())
            has_text = self._has_text_input()
            self._confirm_btn.setVisible(True)
            self._confirm_btn.setEnabled(selected_count > 0 or has_text)
            if selected_count > 0:
                self._confirm_btn.setText(f"提交 ({selected_count})")
            else:
                self._confirm_btn.setText("提交")
            return

        if self._text_input_mode:
            self._confirm_btn.setVisible(True)
            self._confirm_btn.setEnabled(self._has_text_input())
            self._confirm_btn.setText("提交")
        else:
            self._confirm_btn.setVisible(False)

    def _toggle_text_mode(self):
        if not self._options:
            return

        self._text_input_mode = not self._text_input_mode
        if self._text_input_mode:
            self._text_input.setFocus()
        self._update_mode_ui()

    def _on_cancel(self):
        self.cancelled.emit()

    def _on_confirm(self):
        answer = self._build_answer()
        if not answer:
            return
        self.answered.emit(answer)

    def _on_select(self, option):
        answer = self._option_label(option)
        if self._text_input_mode:
            return
        sender = self.sender()
        if isinstance(sender, WrappedOptionButton):
            sender.set_selected(True)
        self._emit_single_answer(str(answer))

    def _emit_single_answer(self, answer: str):
        self.answered.emit(answer)

    def _on_checkbox_toggled(self, _checked):
        self._update_submit_state()

    def _create_checkbox(self, option):
        checkbox = WrappedCheckOption(self._option_label(option), self)
        checkbox.toggled.connect(self._on_checkbox_toggled)
        return checkbox

    def _create_button(self, option):
        btn = WrappedOptionButton(self._option_label(option), self)
        btn.clicked.connect(partial(self._on_select, option))
        return btn

    def show_question(self, question: str, options: list, multiple: bool = False):
        self._question = question or ""
        self._options = options if isinstance(options, list) else []
        self._multiple = bool(multiple)
        self._text_input_mode = not self._options

        self._question_label.setText(self._question)
        self._text_input.clear()
        self._clear_options()

        if self._options:
            columns = 2 if len(self._options) > 2 else max(1, len(self._options))
            for index, option in enumerate(self._options):
                row = index // columns
                col = index % columns
                widget = (
                    self._create_checkbox(option)
                    if self._multiple
                    else self._create_button(option)
                )
                self._options_layout.addWidget(widget, row, col)
                self._option_widgets.append(widget)

        self._update_mode_ui()
        self.raise_()

    def clear(self):
        self._question = ""
        self._options = []
        self._option_widgets = []
        self._text_input_mode = False
        self._question_label.setText("")
        self._clear_options()
        self._text_input.clear()
        self._update_mode_ui()