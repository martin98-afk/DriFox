# -*- coding: utf-8 -*-
"""
悬浮提问卡片 - 支持多问题、选项标题+描述、自定义输入

触发方式：LLM 调用 question 工具
交互方式：点击选项单选/多选，分页导航，可跳过、可自定答案
"""
from functools import partial

from loguru import logger
from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QEvent
from PyQt5.QtWidgets import (
    QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QScrollArea, QSizePolicy, QWidget, QTextEdit,
)

from app.utils.design_tokens import Colors
from app.utils.utils import get_unified_font, get_font_family_css


# ═══════════════════════════════════════════════════════════
# 单选选项卡片
# ═══════════════════════════════════════════════════════════

class _OptionRadioCard(QWidget):
    """单选选项卡片 — 标题 + 描述"""
    clicked = pyqtSignal()

    def __init__(self, label: str, description: str = "", parent=None):
        super().__init__(parent)
        self._label_text = label
        self._desc_text = description
        self._selected = False
        self._hovered = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(44)
        self._setup_ui()

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        self._icon = QLabel("○")
        self._icon.setFont(get_unified_font(14))
        self._icon.setFixedWidth(18)
        self._icon.setAlignment(Qt.AlignCenter)
        self._icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)

        self._title_label = QLabel(self._label_text)
        self._title_label.setFont(get_unified_font(11, True))
        self._title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._desc_label = QLabel(self._desc_text)
        self._desc_label.setFont(get_unified_font(9))
        self._desc_label.setWordWrap(True)
        self._desc_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._desc_label.setVisible(bool(self._desc_text))

        text_layout.addWidget(self._title_label)
        text_layout.addWidget(self._desc_label)

        layout.addWidget(self._icon, 0, Qt.AlignTop)
        layout.addLayout(text_layout, 1)
        self._apply_style()

    def _apply_style(self):
        Colors.refresh()
        if self._selected:
            bg, border = Colors.REALTIME_TAG_BG, Colors.REALTIME_ACCENT
            rf, tf = Colors.REALTIME_ACCENT, "#ffffff"
        elif self._hovered:
            bg, border = Colors.HOVER_BG, Colors.REALTIME_TAG_BORDER
            rf, tf = Colors.REALTIME_ACCENT, Colors.REALTIME_TEXT
        else:
            bg, border = Colors.HOVER_BG, Colors.REALTIME_TAG_BORDER
            rf, tf = Colors.REALTIME_TEXT_SECONDARY, Colors.REALTIME_TEXT

        self.setStyleSheet(f"_OptionRadioCard{{background-color:{bg};border:1px solid {border};border-radius:8px;}}")
        self._icon.setStyleSheet(f"color:{rf};background:transparent;")
        self._title_label.setStyleSheet(f"color:{tf};background:transparent;")
        if self._desc_text:
            self._desc_label.setStyleSheet(f"color:{Colors.REALTIME_TEXT_SECONDARY};background:transparent;")

    def set_selected(self, s: bool):
        self._selected = s
        self._icon.setText("●" if s else "○")
        self._apply_style()

    def reuse(self, label: str, description: str = ""):
        """复用卡片更新内容（代替销毁重建，避免幽灵窗口）"""
        self._label_text = label
        self._desc_text = description
        self._selected = False
        self._hovered = False
        self._icon.setText("○")
        self._title_label.setText(label)
        self._desc_label.setText(description)
        self._desc_label.setVisible(bool(description))
        self._apply_style()

    def enterEvent(self, e):
        self._hovered = True
        if not self._selected: self._apply_style()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False
        if not self._selected: self._apply_style()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton: self.clicked.emit()
        super().mousePressEvent(e)


# ═══════════════════════════════════════════════════════════
# 多选选项卡片
# ═══════════════════════════════════════════════════════════

class _OptionCheckCard(QWidget):
    """多选选项卡片 — 标题 + 描述"""
    toggled = pyqtSignal(bool)

    def __init__(self, label: str, description: str = "", parent=None):
        super().__init__(parent)
        self._label_text = label
        self._desc_text = description
        self._checked = False
        self._hovered = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(44)
        self._setup_ui()

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        self._icon = QLabel("□")
        self._icon.setFont(get_unified_font(14))
        self._icon.setFixedWidth(18)
        self._icon.setAlignment(Qt.AlignCenter)
        self._icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)

        self._title_label = QLabel(self._label_text)
        self._title_label.setFont(get_unified_font(11, True))
        self._title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._desc_label = QLabel(self._desc_text)
        self._desc_label.setFont(get_unified_font(9))
        self._desc_label.setWordWrap(True)
        self._desc_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._desc_label.setVisible(bool(self._desc_text))

        text_layout.addWidget(self._title_label)
        text_layout.addWidget(self._desc_label)

        layout.addWidget(self._icon, 0, Qt.AlignTop)
        layout.addLayout(text_layout, 1)
        self._apply_style()

    def _apply_style(self):
        Colors.refresh()
        if self._checked:
            bg, border = Colors.REALTIME_TAG_BG, Colors.REALTIME_ACCENT
            cf, tf = Colors.REALTIME_ACCENT, "#ffffff"
        elif self._hovered:
            bg, border = Colors.HOVER_BG, Colors.REALTIME_TAG_BORDER
            cf, tf = Colors.REALTIME_ACCENT, Colors.REALTIME_TEXT
        else:
            bg, border = Colors.HOVER_BG, Colors.REALTIME_TAG_BORDER
            cf, tf = Colors.REALTIME_TEXT_SECONDARY, Colors.REALTIME_TEXT

        self.setStyleSheet(f"_OptionCheckCard{{background-color:{bg};border:1px solid {border};border-radius:8px;}}")
        self._icon.setStyleSheet(f"color:{cf};background:transparent;")
        self._title_label.setStyleSheet(f"color:{tf};background:transparent;")
        self._desc_label.setStyleSheet(f"color:{Colors.REALTIME_TEXT_SECONDARY};background:transparent;")

    def set_checked(self, c: bool):
        self._checked = c
        self._icon.setText("☑" if c else "□")
        self._apply_style()

    def toggle(self):
        self._checked = not self._checked
        self._icon.setText("☑" if self._checked else "□")
        self._apply_style()
        self.toggled.emit(self._checked)

    def isChecked(self):
        return self._checked

    def reuse(self, label: str, description: str = ""):
        """复用卡片更新内容（代替销毁重建，避免幽灵窗口）"""
        self._label_text = label
        self._desc_text = description
        self._checked = False
        self._hovered = False
        self._icon.setText("□")
        self._title_label.setText(label)
        self._desc_label.setText(description)
        self._desc_label.setVisible(bool(description))
        self._apply_style()

    def enterEvent(self, e):
        self._hovered = True
        if not self._checked: self._apply_style()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False
        if not self._checked: self._apply_style()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton: self.toggle()
        super().mousePressEvent(e)


# ═══════════════════════════════════════════════════════════
# 自定义输入选项卡片
# ═══════════════════════════════════════════════════════════

class _CustomInputCard(QWidget):
    """输入自己的答案选项 — 默认显示描述，选中后变成文本输入框"""

    PLACEHOLDER = "输入你的答案..."
    activated = pyqtSignal()  # 用户主动点击选中时触发
    heightNeedsUpdate = pyqtSignal()  # 高度需要更新时触发

    def __init__(self, multiple: bool = False, parent=None):
        super().__init__(parent)
        self._active = False
        self._text_value = ""
        self._multiple = multiple
        self._label_text = "输入自己的答案"
        self._desc_text = self.PLACEHOLDER
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(44)
        self._setup_ui()

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(10)

        self._icon = QLabel("□" if self._multiple else "○")
        self._icon.setFont(get_unified_font(14))
        self._icon.setFixedWidth(18)
        self._icon.setAlignment(Qt.AlignCenter)
        self._icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._title_label = QLabel("输入自己的答案")
        self._title_label.setFont(get_unified_font(11, True))
        self._title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        header.addWidget(self._icon, 0, Qt.AlignTop)
        header.addWidget(self._title_label, 1)
        self._layout.addLayout(header)

        self._desc_label = QLabel(self.PLACEHOLDER)
        self._desc_label.setFont(get_unified_font(9))
        self._desc_label.setStyleSheet(f"color:{Colors.REALTIME_TEXT_SECONDARY};background:transparent;")
        self._desc_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._layout.addWidget(self._desc_label)

        self._text_edit = QTextEdit()
        self._text_edit.setPlaceholderText(self.PLACEHOLDER)
        self._text_edit.setFont(get_unified_font(10))
        self._text_edit.setMaximumHeight(80)
        self._text_edit.setVisible(False)
        self._text_edit.textChanged.connect(self._on_text_changed)
        self._layout.addWidget(self._text_edit)

        self._apply_style()

    def _on_text_changed(self):
        self._text_value = self._text_edit.toPlainText()

    def set_active(self, active: bool):
        self._active = active
        a_icon = "☑" if self._multiple else "●"
        i_icon = "□" if self._multiple else "○"
        self._icon.setText(a_icon if active else i_icon)
        self._desc_label.setVisible(not active)
        self._text_edit.setVisible(active)
        if active:
            self._text_edit.setFocus()
            if self._text_value:
                self._text_edit.setPlainText(self._text_value)
            # 延迟发出高度更新信号，确保布局完成
            QTimer.singleShot(10, self._emit_height_update)
        self._apply_style()

    def _emit_height_update(self):
        """触发高度更新，让父级重新布局"""
        self.updateGeometry()
        self.heightNeedsUpdate.emit()

    def toggle(self):
        new_state = not self._active
        self.set_active(new_state)
        if new_state:
            self.activated.emit()

    def get_text(self) -> str:
        if self._active:
            return self._text_edit.toPlainText().strip()
        return self._text_value.strip()

    def set_content(self, text: str):
        """恢复已保存的文本内容"""
        self._text_value = text
        if self._active:
            self._text_edit.setPlainText(text)

    def _apply_style(self):
        Colors.refresh()
        if self._active:
            bg, border = Colors.REALTIME_TAG_BG, Colors.REALTIME_ACCENT
            rf, tf = Colors.REALTIME_ACCENT, "#ffffff"
        else:
            bg, border = Colors.HOVER_BG, Colors.REALTIME_TAG_BORDER
            rf, tf = Colors.REALTIME_TEXT_SECONDARY, Colors.REALTIME_TEXT

        self.setStyleSheet(f"_CustomInputCard{{background-color:{bg};border:1px solid {border};border-radius:8px;}}")
        self._icon.setStyleSheet(f"color:{rf};background:transparent;")
        self._title_label.setStyleSheet(f"color:{tf};background:transparent;")

        te_border = Colors.REALTIME_ACCENT if self._active else Colors.REALTIME_TAG_BORDER
        self._text_edit.setStyleSheet(f"""
            QTextEdit {{
                background-color: {Colors.HOVER_BG};
                color: {Colors.REALTIME_TEXT};
                border: 1px solid {te_border};
                border-radius: 6px;
                padding: 8px 10px;
                {get_font_family_css()} font-size: 10pt;
            }}
            QTextEdit:focus {{ border-color: {Colors.REALTIME_ACCENT}; }}
        """)

    def enterEvent(self, e):
        if not self._active:
            self.setStyleSheet(f"_CustomInputCard{{background-color:{Colors.REALTIME_TAG_BG};border:1px solid {Colors.REALTIME_TAG_BORDER};border-radius:8px;}}")
        super().enterEvent(e)

    def leaveEvent(self, e):
        if not self._active:
            self._apply_style()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton: self.toggle()
        super().mousePressEvent(e)


# ═══════════════════════════════════════════════════════════
# 主提问卡片
# ═══════════════════════════════════════════════════════════

class QuestionFloatingWidget(QWidget):
    """悬浮提问卡片，支持多问题分页"""
    answered = pyqtSignal(str)
    cancelled = pyqtSignal()
    heightChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._questions = []
        self._current_index = 0
        self._answers = {}
        self._option_widgets = []
        self._custom_input_widget = None
        self._show_custom_input = True
        self._setup_ui()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 12)
        main_layout.setSpacing(6)

        # ── 顶栏 ──
        header = QHBoxLayout()
        header.setSpacing(8)

        self._page_label = QLabel("")
        self._page_label.setFont(get_unified_font(10))
        self._page_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        header.addWidget(self._page_label)
        header.addStretch()

        self._close_btn = QPushButton("−")
        self._close_btn.setFixedSize(22, 22)
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setFont(get_unified_font(12, True))
        self._close_btn.clicked.connect(self._on_ignore)
        self._close_btn.setStyleSheet("""
            QPushButton { color: rgba(255,255,255,0.4); background: transparent; border: none; border-radius: 11px; }
            QPushButton:hover { color: rgba(255,255,255,0.8); background: rgba(255,255,255,0.1); }
        """)
        header.addWidget(self._close_btn)
        main_layout.addLayout(header)

        # ── 问题标题（超出 160px 高度时滚动） ──
        self._question_scroll = QScrollArea()
        self._question_scroll.setWidgetResizable(True)
        self._question_scroll.setMaximumHeight(160)
        self._question_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._question_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._question_scroll.viewport().setAutoFillBackground(False)
        self._question_scroll.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
            QScrollArea > QWidget#qt_scrollarea_viewport {
                background: transparent;
            }
            QScrollBar:vertical {
                background: transparent;
                width: 4px;
                margin: 0;
            }
            QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.25);
                border-radius: 2px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: rgba(255,255,255,0.4);
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background: transparent;
            }
        """)

        self._question_label = QLabel("")
        self._question_label.setFont(get_unified_font(12, True))
        self._question_label.setWordWrap(True)
        self._question_label.setMinimumHeight(24)
        self._question_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._question_scroll.setWidget(self._question_label)
        main_layout.addWidget(self._question_scroll)

        # ── 提示 ──
        self._hint_label = QLabel("")
        self._hint_label.setFont(get_unified_font(9))
        self._hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        main_layout.addWidget(self._hint_label)

        # ── 选项区（直接布局，无滚动） ──
        self._options_container = QWidget()
        self._options_container.setStyleSheet("background: transparent;")
        self._options_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._options_layout = QVBoxLayout(self._options_container)
        self._options_layout.setContentsMargins(0, 0, 0, 0)
        self._options_layout.setSpacing(6)
        main_layout.addWidget(self._options_container)

        # ── 底栏 ──
        footer = QHBoxLayout()
        footer.setSpacing(8)

        self._ignore_btn = QPushButton("忽略")
        self._ignore_btn.setCursor(Qt.PointingHandCursor)
        self._ignore_btn.setFont(get_unified_font(10))
        self._ignore_btn.clicked.connect(self._on_ignore)
        self._ignore_btn.setStyleSheet("""
            QPushButton { color: rgba(255,255,255,0.4); background: transparent; border: none; padding: 6px 0; }
            QPushButton:hover { color: rgba(255,255,255,0.7); }
        """)

        self._back_btn = QPushButton("返回")
        self._back_btn.setFixedHeight(30)
        self._back_btn.setCursor(Qt.PointingHandCursor)
        self._back_btn.setFont(get_unified_font(10))
        self._back_btn.clicked.connect(self._on_back)
        self._back_btn.setStyleSheet("""
            QPushButton { color: rgba(255,255,255,0.6); background: rgba(255,255,255,0.08); border: none; border-radius: 6px; padding: 0 14px; }
            QPushButton:hover { color: rgba(255,255,255,0.9); background: rgba(255,255,255,0.15); }
        """)

        self._next_btn = QPushButton("下一步")
        self._next_btn.setFixedHeight(30)
        self._next_btn.setCursor(Qt.PointingHandCursor)
        self._next_btn.setFont(get_unified_font(10, True))
        self._next_btn.clicked.connect(self._on_next)
        self._next_btn.setStyleSheet(f"""
            QPushButton {{ background-color: {Colors.REALTIME_ACCENT}; color: #ffffff; border: none; border-radius: 6px; padding: 0 18px; font-weight: bold; }}
            QPushButton:hover {{ background-color: {Colors.REALTIME_BORDER}; }}
        """)

        footer.addWidget(self._ignore_btn)
        footer.addStretch()
        footer.addWidget(self._back_btn)
        footer.addWidget(self._next_btn)

        main_layout.addLayout(footer)
        self._apply_card_style()

    def _apply_card_style(self):
        Colors.refresh()
        self.setStyleSheet(f"""
            QuestionFloatingWidget {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-radius: 8px 8px 0 0;
            }}
        """)
        self._question_label.setStyleSheet(f"color:{Colors.REALTIME_TEXT};background:transparent;")
        self._hint_label.setStyleSheet(f"color:{Colors.REALTIME_TEXT_SECONDARY};background:transparent;")

    # ────────────── 公开接口 ──────────────

    def show_question(self, questions: list, show_custom_input: bool = True):
        self._questions = questions if isinstance(questions, list) else []
        self._current_index = 0
        self._answers = {}
        self._show_custom_input = show_custom_input
        self._render_current()
        QTimer.singleShot(0, self.heightChanged.emit)

    def clear(self):
        self._questions = []
        self._current_index = 0
        self._answers = {}
        self._full_clear_options()
        self.setVisible(False)

    # ────────────── 工具方法 ──────────────

    @staticmethod
    def _extract_label_desc(opt) -> tuple:
        """从选项数据中提取 (label, description)"""
        desc = opt.get("description", "") if isinstance(opt, dict) else ""
        if isinstance(opt, dict):
            # 稳健推导 label：label > name > text > value > title > description > str(opt)
            label = opt.get("label")
            if not label:
                for key in ("name", "text", "value", "title"):
                    label = opt.get(key)
                    if label:
                        break
            if not label:
                if desc and len(opt) <= 1:
                    label = desc
                    desc = ""  # 避免重复
                else:
                    desc = ""
                    for v in opt.values():
                        if isinstance(v, str):
                            label = v
                            break
                    if not label:
                        label = str(opt)
        else:
            label, desc = str(opt), ""
        return label, desc

    # ────────────── 渲染（widget 复用，避免幽灵窗口）──────────────

    def _render_current(self):
        self._recycle_options()
        self._apply_card_style()

        total = len(self._questions)
        if total == 0:
            self._on_ignore()
            return

        # 刷新按钮主题色
        Colors.refresh()
        self._next_btn.setStyleSheet(f"""
            QPushButton {{ background-color: {Colors.REALTIME_ACCENT}; color: #ffffff; border: none; border-radius: 6px; padding: 0 18px; font-weight: bold; }}
            QPushButton:hover {{ background-color: {Colors.REALTIME_BORDER}; }}
        """)

        q_data = self._questions[self._current_index]
        if not isinstance(q_data, dict):
            logger.warning(f"[QuestionWidget] q_data 不是 dict: {type(q_data)}, 跳过渲染")
            self._on_ignore()
            return
        question_text = q_data.get("question", "")
        options = q_data.get("options", [])
        multiple = q_data.get("multiple", False)
        if not isinstance(options, list):
            options = []

        self._page_label.setText(f"{self._current_index + 1}/{total} 个问题")
        self._page_label.setStyleSheet(f"color:{Colors.REALTIME_TEXT_SECONDARY};background:transparent;")

        self._question_label.setText(question_text)

        self._hint_label.setText(
            "选择所有适用的选项" if multiple and options else
            "选择一个答案" if options else ""
        )

        # ── 复用 option widgets（不销毁重建） ──
        count = len(options)
        expected_type = _OptionCheckCard if multiple else _OptionRadioCard

        # 如果类型变了（multiple 前后不一致），只能全部重建
        if self._option_widgets and not isinstance(self._option_widgets[0], expected_type):
            self._full_clear_options()

        # 确保 pool 数量足够
        while len(self._option_widgets) < count:
            if multiple:
                card = _OptionCheckCard("", "", self._options_container)
            else:
                card = _OptionRadioCard("", "", self._options_container)
                card.clicked.connect(partial(self._on_radio_selected, card))
            self._options_layout.addWidget(card)
            self._option_widgets.append(card)

        # 更新已有 widget 的内容
        for i, opt in enumerate(options):
            label, desc = self._extract_label_desc(opt)
            self._option_widgets[i].reuse(label, desc)
            self._option_widgets[i].setVisible(True)

        # 隐藏多余 widget
        for i in range(count, len(self._option_widgets)):
            self._option_widgets[i].setVisible(False)

        # ── 自定义输入 ──
        if self._show_custom_input:
            self._custom_input_widget = _CustomInputCard(multiple, self._options_container)
            if not multiple:
                self._custom_input_widget.activated.connect(self._on_custom_input_activated)
            self._custom_input_widget.heightNeedsUpdate.connect(self._on_options_height_changed)
            self._options_layout.addWidget(self._custom_input_widget)

        # 恢复已保存答案
        saved = self._answers.get(self._current_index)
        if saved:
            self._restore_answer(saved)

        # 如果自定义输入已激活，延迟触发高度更新
        if self._custom_input_widget and self._custom_input_widget._active:
            QTimer.singleShot(20, self._on_options_height_changed)

        self._update_footer(total)

    def _update_footer(self, total: int):
        is_first = self._current_index == 0
        is_last = self._current_index == total - 1
        self._back_btn.setVisible(not is_first)
        self._next_btn.setText("提交" if is_last else "下一步")

    def _on_radio_selected(self, card):
        for w in self._option_widgets:
            if isinstance(w, _OptionRadioCard):
                w.set_selected(w is card)
        if self._custom_input_widget:
            self._custom_input_widget.set_active(False)

    def _on_custom_input_activated(self):
        """单选模式下自定义输入被选中，取消其他选项"""
        for w in self._option_widgets:
            if hasattr(w, 'set_selected'):
                w.set_selected(False)

    def _on_options_height_changed(self):
        """选项区域高度变化时，更新卡片高度"""
        QTimer.singleShot(0, self.heightChanged.emit)

    def _recycle_options(self):
        """仅隐藏 old option widgets（不销毁），供下次 _render_current 复用"""
        for w in self._option_widgets:
            w.setVisible(False)
        if self._custom_input_widget:
            self._custom_input_widget.heightNeedsUpdate.disconnect()
            self._custom_input_widget.setVisible(False)  # 先隐藏，防止 ghost
            self._options_layout.removeWidget(self._custom_input_widget)
            self._custom_input_widget.deleteLater()
            self._custom_input_widget = None

    def _full_clear_options(self):
        """完全销毁所有 option widgets（类型变更时使用）"""
        for w in self._option_widgets:
            w.setVisible(False)
            self._options_layout.removeWidget(w)
            w.deleteLater()
        self._option_widgets = []
        if self._custom_input_widget:
            self._custom_input_widget.heightNeedsUpdate.disconnect()
            self._custom_input_widget.setVisible(False)
            self._options_layout.removeWidget(self._custom_input_widget)
            self._custom_input_widget.deleteLater()
            self._custom_input_widget = None

    def _get_selected_options(self) -> list:
        results = []
        for w in self._option_widgets:
            if not w.isVisible():
                continue
            if hasattr(w, '_selected') and w._selected:
                results.append({"label": w._label_text, "description": w._desc_text})
            elif hasattr(w, 'isChecked') and w.isChecked():
                results.append({"label": w._label_text, "description": w._desc_text})
        return results

    def _get_custom_input_text(self) -> str:
        if self._custom_input_widget and self._custom_input_widget._active:
            return self._custom_input_widget.get_text()
        return ""

    def _save_current_answer(self):
        selected = self._get_selected_options()
        custom = self._get_custom_input_text()
        has_custom = bool(custom)
        parts = []
        if selected:
            parts.extend(f"【{s['label']}】" for s in selected)
        if custom:
            parts.append(custom)
        if parts:
            self._answers[self._current_index] = {
                "text": "；".join(parts),
                "custom": has_custom,
                "custom_text": custom,  # 保存原始自定义输入文本，用于恢复
            }
        else:
            self._answers.pop(self._current_index, None)

    def _restore_answer(self, answer):
        if answer is None:
            for w in self._option_widgets:
                if w.isVisible():
                    if hasattr(w, 'set_selected'):
                        w.set_selected(False)
                    elif hasattr(w, 'set_checked'):
                        w.set_checked(False)
            if self._custom_input_widget:
                self._custom_input_widget.set_active(False)
            return

        text = answer["text"] if isinstance(answer, dict) else answer
        custom_used = answer.get("custom", False) if isinstance(answer, dict) else ("输入自己的答案" in text)

        for w in self._option_widgets:
            if w.isVisible():
                if hasattr(w, 'set_selected'):
                    w.set_selected(text and w._label_text in text)
                elif hasattr(w, 'set_checked'):
                    w.set_checked(text and w._label_text in text)
        if self._custom_input_widget:
            self._custom_input_widget.set_active(custom_used)
            if custom_used and isinstance(answer, dict):
                custom_text = answer.get("custom_text", "") or answer.get("text", "")
                # 如果是混合答案（选项+自定义），提取纯自定义部分
                import re
                pure = re.sub(r'【[^】]+】[；]?', '', custom_text).strip("；").strip()
                if pure:
                    self._custom_input_widget.set_content(pure)

    def _on_back(self):
        self._save_current_answer()
        if self._current_index > 0:
            self._current_index -= 1
            self.setUpdatesEnabled(False)
            self._render_current()
            self.setUpdatesEnabled(True)

    def _on_next(self):
        self._save_current_answer()
        total = len(self._questions)
        if self._current_index < total - 1:
            self._current_index += 1
            self.setUpdatesEnabled(False)
            self._render_current()
            self.setUpdatesEnabled(True)
        else:
            self._build_and_emit_answer()

    def _on_ignore(self):
        self.cancelled.emit()

    def _build_and_emit_answer(self):
        parts = []
        for i, q in enumerate(self._questions):
            q_text = q.get("question", f"问题{i+1}")
            data = self._answers.get(i)
            if data:
                answer_text = data["text"] if isinstance(data, dict) else data
                parts.append(f"问题「{q_text}」的回答：\n{answer_text}")
        if not parts:
            self.cancelled.emit()
            return
        self.answered.emit("\n---\n".join(parts))

    def set_opacity(self, opacity: float):
        Colors.refresh()
        bg = Colors.REALTIME_BG
        if bg.startswith("rgba("):
            alpha = max(1, int(opacity * 255))
            bg = bg.rsplit(",", 1)[0] + f", {alpha})"
        self.setStyleSheet(f"""
            QuestionFloatingWidget {{
                background-color: {bg};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-radius: 8px 8px 0 0;
            }}
        """)
