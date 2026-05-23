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
from qfluentwidgets import PrimaryPushButton, TransparentToolButton, FluentIcon

from app.utils.design_tokens import Colors
from app.utils.utils import get_unified_font, get_font_family_css
from app.widgets.cards.settings.base_settings_card import BaseSettingsCard


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


class QuestionFloatingWidget(BaseSettingsCard):
    """悬浮提问卡片 — 使用 BaseSettingsCard 基类，行为与系统卡片一致"""

    answered = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("等待你的选择", "❓", parent)
        self._question = ""
        self._options = []
        self._multiple = False
        self._text_input_mode = False
        self._option_widgets = []
        self._setup_content()

        # 基类的关闭按钮 → 发射 cancelled 信号
        self.closed.connect(self._on_cancel)

    def _setup_content(self):
        # 使用基类的 content_layout，移除 scroll_area（选项自己带滚动）
        self.scroll_area.setParent(None)
        self.scroll_area.deleteLater()

        # ── Question text ──
        self.question_label = QLabel("", self)
        self.question_label.setFont(get_unified_font(10))
        self.question_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")
        self.question_label.setWordWrap(True)
        self.question_label.setMinimumHeight(28)

        # ── Options in ScrollArea ──
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
        self.options_layout = QGridLayout(self._options_scroll_content)
        self.options_layout.setContentsMargins(0, 0, 0, 0)
        self.options_layout.setHorizontalSpacing(10)
        self.options_layout.setVerticalSpacing(10)
        self._options_scroll.setWidget(self._options_scroll_content)

        # ── Text Input (hidden initially) ──
        self.text_input = QTextEdit(self)
        self.text_input.setPlaceholderText("输入你想补充的内容")
        self.text_input.setFont(get_unified_font(10))
        self.text_input.setMaximumHeight(104)
        self.text_input.setVisible(False)
        self.text_input.textChanged.connect(self._update_submit_state)
        self._apply_text_input_style()

        # ── Bottom bar: hint + toggle on same line ──
        self.bottom_bar = QHBoxLayout()
        self.bottom_bar.setSpacing(8)

        self.custom_hint_label = QLabel("没有合适的选项？", self)
        self.custom_hint_label.setFont(get_unified_font(9))
        self.custom_hint_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")

        self.toggle_text_mode_btn = QPushButton("改为输入", self)
        self.toggle_text_mode_btn.setCursor(Qt.PointingHandCursor)
        self._apply_toggle_btn_style()
        self.toggle_text_mode_btn.clicked.connect(self._toggle_text_mode)

        self.bottom_bar.addWidget(self.custom_hint_label)
        self.bottom_bar.addStretch()
        self.bottom_bar.addWidget(self.toggle_text_mode_btn)

        # ── Footer ──
        self.footer_layout = QHBoxLayout()
        self.footer_layout.setSpacing(8)

        self.selection_hint_label = QLabel("", self)
        self.selection_hint_label.setFont(get_unified_font(9))
        self.selection_hint_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")

        self.confirm_btn = PrimaryPushButton("提交", self)
        self.confirm_btn.setCursor(Qt.PointingHandCursor)
        self.confirm_btn.clicked.connect(self._on_confirm)
        self._apply_confirm_btn_style()

        self.footer_layout.addWidget(self.selection_hint_label)
        self.footer_layout.addStretch()
        self.footer_layout.addWidget(self.confirm_btn)

        # ── Assemble into content_layout ──
        self.content_layout.setContentsMargins(0, 2, 0, 2)
        self.content_layout.setSpacing(8)
        self.content_layout.addWidget(self.question_label)
        self.content_layout.addWidget(self._options_scroll)
        self.content_layout.addWidget(self.text_input)
        self.content_layout.addLayout(self.bottom_bar)
        self.content_layout.addLayout(self.footer_layout)

        self._update_mode_ui()

    def _apply_text_input_style(self):
        Colors.refresh()
        self.text_input.setStyleSheet(
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

    def _apply_toggle_btn_style(self):
        Colors.refresh()
        self.toggle_text_mode_btn.setStyleSheet(
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

    def _apply_confirm_btn_style(self):
        Colors.refresh()
        self.confirm_btn.setStyleSheet(
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

    def refresh_style(self):
        """响应主题切换"""
        Colors.refresh()
        super().refresh_style()
        self.question_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")
        self.custom_hint_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")
        self.selection_hint_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")
        self._apply_toggle_btn_style()
        self._apply_text_input_style()
        self._apply_confirm_btn_style()

    def _clear_options(self):
        self._option_widgets = []
        while self.options_layout.count():
            item = self.options_layout.takeAt(0)
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
        return bool(self.text_input.toPlainText().strip())

    def _build_answer(self):
        text = self.text_input.toPlainText().strip()

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
        self.custom_hint_label.setVisible(has_options)
        self.toggle_text_mode_btn.setVisible(has_options)
        self.text_input.setVisible(text_visible)

        if not has_options:
            self.selection_hint_label.setText("直接输入回答")
        elif self._multiple:
            if text_visible:
                self.selection_hint_label.setText("可多选，也可补充说明")
                self.toggle_text_mode_btn.setText("返回选项")
            else:
                self.selection_hint_label.setText("请选择一个选项")
                self.toggle_text_mode_btn.setText("改为输入")
        else:
            if text_visible:
                self.selection_hint_label.setText("文本输入会替代选项选择")
                self.toggle_text_mode_btn.setText("返回选项")
            else:
                self.selection_hint_label.setText("请选择一个选项")
                self.toggle_text_mode_btn.setText("改为输入")

        self._update_submit_state()

    def _update_submit_state(self):
        if not self._options:
            self.confirm_btn.setVisible(True)
            self.confirm_btn.setEnabled(self._has_text_input())
            self.confirm_btn.setText("提交")
            return

        if self._multiple:
            selected_count = len(self._selected_options())
            has_text = self._has_text_input()
            self.confirm_btn.setVisible(True)
            self.confirm_btn.setEnabled(selected_count > 0 or has_text)
            if selected_count > 0:
                self.confirm_btn.setText(f"提交 ({selected_count})")
            else:
                self.confirm_btn.setText("提交")
            return

        if self._text_input_mode:
            self.confirm_btn.setVisible(True)
            self.confirm_btn.setEnabled(self._has_text_input())
            self.confirm_btn.setText("提交")
        else:
            self.confirm_btn.setVisible(False)

    def _toggle_text_mode(self):
        if not self._options:
            return

        self._text_input_mode = not self._text_input_mode
        if self._text_input_mode:
            self.text_input.setFocus()
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

        self.question_label.setText(self._question)
        self.text_input.clear()
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
                self.options_layout.addWidget(widget, row, col)
                self._option_widgets.append(widget)

        self._update_mode_ui()
        self.raise_()

    def clear(self):
        self._question = ""
        self._options = []
        self._option_widgets = []
        self._clear_options()
        self.question_label.clear()
        self.text_input.clear()
