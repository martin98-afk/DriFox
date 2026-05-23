# -*- coding: utf-8 -*-
"""
悬浮提问卡片 - 支持多问题、选项标题+描述

触发方式：LLM 调用 question 工具
数据格式：questions = [{ "question": str, "options": [{ "label": str, "description": str }], "multiple": bool }]
交互方式：点击选项单选/多选，分页导航，提交或忽略
"""
from functools import partial

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors
from app.utils.utils import get_unified_font, get_font_family_css


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
        self.setMinimumHeight(48)
        self._setup_ui()

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        # 单选圆圈
        self._radio_icon = QLabel("○")
        self._radio_icon.setFont(get_unified_font(13))
        self._radio_icon.setFixedWidth(18)
        self._radio_icon.setAlignment(Qt.AlignCenter)
        self._radio_icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        # 右侧文字区域
        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)

        self._title_label = QLabel(self._label_text)
        self._title_label.setFont(get_unified_font(11, True))
        self._title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._desc_label = QLabel(self._desc_text if self._desc_text else "")
        self._desc_label.setFont(get_unified_font(9))
        self._desc_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        if not self._desc_text:
            self._desc_label.setVisible(False)

        text_layout.addWidget(self._title_label)
        text_layout.addWidget(self._desc_label)

        layout.addWidget(self._radio_icon, 0, Qt.AlignTop)
        layout.addLayout(text_layout, 1)

        self._apply_style()

    def _apply_style(self):
        Colors.refresh()
        if self._selected:
            bg = Colors.REALTIME_TAG_BG
            border = Colors.REALTIME_ACCENT
            radio_fg = Colors.REALTIME_ACCENT
            title_fg = "#ffffff"
            desc_fg = "rgba(255,255,255,0.6)"
        elif self._hovered:
            bg = Colors.REALTIME_TAG_BG
            border = Colors.REALTIME_TAG_BORDER
            radio_fg = "rgba(255,255,255,0.5)"
            title_fg = Colors.REALTIME_TEXT
            desc_fg = Colors.REALTIME_TEXT_SECONDARY
        else:
            bg = "rgba(255,255,255,0.03)"
            border = Colors.REALTIME_TAG_BORDER
            radio_fg = "rgba(255,255,255,0.3)"
            title_fg = Colors.REALTIME_TEXT
            desc_fg = Colors.REALTIME_TEXT_SECONDARY

        self.setStyleSheet(f"""
            _OptionRadioCard {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
        """)
        self._radio_icon.setStyleSheet(f"color: {radio_fg}; background: transparent;")
        self._title_label.setStyleSheet(f"color: {title_fg}; background: transparent;")
        if self._desc_text:
            self._desc_label.setStyleSheet(f"color: {desc_fg}; background: transparent;")

    def set_selected(self, selected: bool):
        self._selected = selected
        self._radio_icon.setText("●" if selected else "○")
        self._apply_style()

    def enterEvent(self, event):
        self._hovered = True
        if not self._selected:
            self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        if not self._selected:
            self._apply_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _OptionCheckCard(QWidget):
    """多选选项卡片 — 标题 + 描述 + 复选框"""

    toggled = pyqtSignal(bool)

    def __init__(self, label: str, description: str = "", parent=None):
        super().__init__(parent)
        self._label_text = label
        self._desc_text = description
        self._checked = False
        self._hovered = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(48)
        self._setup_ui()

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        # 复选框图标
        self._check_icon = QLabel("□")
        self._check_icon.setFont(get_unified_font(13))
        self._check_icon.setFixedWidth(18)
        self._check_icon.setAlignment(Qt.AlignCenter)
        self._check_icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)

        self._title_label = QLabel(self._label_text)
        self._title_label.setFont(get_unified_font(11, True))
        self._title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self._desc_label = QLabel(self._desc_text if self._desc_text else "")
        self._desc_label.setFont(get_unified_font(9))
        self._desc_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        if not self._desc_text:
            self._desc_label.setVisible(False)

        text_layout.addWidget(self._title_label)
        text_layout.addWidget(self._desc_label)

        layout.addWidget(self._check_icon, 0, Qt.AlignTop)
        layout.addLayout(text_layout, 1)

        self._apply_style()

    def _apply_style(self):
        Colors.refresh()
        if self._checked:
            bg = Colors.REALTIME_TAG_BG
            border = Colors.REALTIME_ACCENT
            check_fg = Colors.REALTIME_ACCENT
            title_fg = "#ffffff"
            desc_fg = "rgba(255,255,255,0.6)"
        elif self._hovered:
            bg = Colors.REALTIME_TAG_BG
            border = Colors.REALTIME_TAG_BORDER
            check_fg = "rgba(255,255,255,0.5)"
            title_fg = Colors.REALTIME_TEXT
            desc_fg = Colors.REALTIME_TEXT_SECONDARY
        else:
            bg = "rgba(255,255,255,0.03)"
            border = Colors.REALTIME_TAG_BORDER
            check_fg = "rgba(255,255,255,0.3)"
            title_fg = Colors.REALTIME_TEXT
            desc_fg = Colors.REALTIME_TEXT_SECONDARY

        self.setStyleSheet(f"""
            _OptionCheckCard {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 8px;
            }}
        """)
        self._check_icon.setStyleSheet(f"color: {check_fg}; background: transparent;")
        self._title_label.setStyleSheet(f"color: {title_fg}; background: transparent;")
        if self._desc_text:
            self._desc_label.setStyleSheet(f"color: {desc_fg}; background: transparent;")

    def set_checked(self, checked: bool):
        self._checked = checked
        self._check_icon.setText("☑" if checked else "□")
        self._apply_style()

    def toggle(self):
        self._checked = not self._checked
        self._check_icon.setText("☑" if self._checked else "□")
        self._apply_style()
        self.toggled.emit(self._checked)

    def isChecked(self):
        return self._checked

    def enterEvent(self, event):
        self._hovered = True
        if not self._checked:
            self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovered = False
        if not self._checked:
            self._apply_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.toggle()
        super().mousePressEvent(event)


class QuestionFloatingWidget(QWidget):
    """悬浮提问卡片，支持单选、多选、多问题分页"""

    answered = pyqtSignal(str)
    cancelled = pyqtSignal()
    heightChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._questions = []         # 完整问题数据
        self._current_index = 0      # 当前问题索引
        self._answers = {}           # {index: "答案文本"}
        self._option_widgets = []    # 当前页选项 widgets
        self._setup_ui()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 12)
        main_layout.setSpacing(8)

        # ── 顶栏：页码 + 关闭 ──
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
            QPushButton {
                color: rgba(255,255,255,0.4);
                background: transparent;
                border: none;
                border-radius: 11px;
            }
            QPushButton:hover {
                color: rgba(255,255,255,0.8);
                background: rgba(255,255,255,0.1);
            }
        """)
        header.addWidget(self._close_btn)
        main_layout.addLayout(header)

        # ── 问题标题 ──
        self._question_label = QLabel("")
        self._question_label.setFont(get_unified_font(12, True))
        self._question_label.setWordWrap(True)
        self._question_label.setMinimumHeight(24)
        self._question_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        main_layout.addWidget(self._question_label)

        # ── 辅助提示 ──
        self._hint_label = QLabel("")
        self._hint_label.setFont(get_unified_font(9))
        self._hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        main_layout.addWidget(self._hint_label)

        # ── 选项区域（滚动） ──
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll.setMaximumHeight(260)
        self._scroll.setStyleSheet("""
            QScrollArea { border: none; background: transparent; }
            QScrollArea > QWidget > QWidget { background: transparent; }
            QScrollBar:vertical { width: 4px; background: transparent; }
            QScrollBar::handle:vertical { background: rgba(255,255,255,0.15); border-radius: 2px; min-height: 20px; }
            QScrollBar::handle:vertical:hover { background: rgba(255,255,255,0.25); }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: none; }
        """)

        self._scroll_content = QWidget()
        self._scroll_content.setStyleSheet("background: transparent;")
        self._options_layout = QVBoxLayout(self._scroll_content)
        self._options_layout.setContentsMargins(0, 0, 0, 0)
        self._options_layout.setSpacing(6)
        self._options_layout.addStretch()

        self._scroll.setWidget(self._scroll_content)
        main_layout.addWidget(self._scroll)

        # ── 底栏：忽略 + 提交 ──
        footer = QHBoxLayout()
        footer.setSpacing(10)

        self._ignore_btn = QPushButton("忽略")
        self._ignore_btn.setCursor(Qt.PointingHandCursor)
        self._ignore_btn.setFont(get_unified_font(10))
        self._ignore_btn.clicked.connect(self._on_ignore)
        self._ignore_btn.setStyleSheet("""
            QPushButton {
                color: rgba(255,255,255,0.4);
                background: transparent;
                border: none;
                padding: 6px 0px;
            }
            QPushButton:hover {
                color: rgba(255,255,255,0.7);
            }
        """)

        self._submit_btn = QPushButton("提交")
        self._submit_btn.setFixedHeight(32)
        self._submit_btn.setCursor(Qt.PointingHandCursor)
        self._submit_btn.setFont(get_unified_font(10, True))
        self._submit_btn.clicked.connect(self._on_submit)
        self._submit_btn.setEnabled(False)

        footer.addWidget(self._ignore_btn)
        footer.addStretch()
        footer.addWidget(self._submit_btn)

        main_layout.addLayout(footer)

        # 最后应用样式（确保所有子控件已创建）
        self._apply_card_style()
        self._apply_submit_btn_style()

    def _apply_card_style(self):
        Colors.refresh()
        self.setStyleSheet(f"""
            QuestionFloatingWidget {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
            }}
        """)
        self._question_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT}; background: transparent;")
        self._hint_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")

    def _apply_submit_btn_style(self):
        """提交按钮样式 — 白色背景 + 黑色粗体文字（参考图）"""
        self._submit_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: #ffffff;
                color: #000000;
                border: none;
                border-radius: 6px;
                padding: 0 18px;
                font-weight: bold;
                {get_font_family_css()} font-size: 11px;
            }}
            QPushButton:hover {{
                background-color: #e0e0e0;
            }}
            QPushButton:disabled {{
                background-color: rgba(255,255,255,0.15);
                color: rgba(0,0,0,0.3);
            }}
        """)

    # ────────────── 公开接口 ──────────────

    def show_question(self, questions: list):
        """显示问题列表"""
        self._questions = questions if isinstance(questions, list) else []
        self._current_index = 0
        self._answers = {}
        self._render_current()
        QTimer.singleShot(0, self.heightChanged.emit)

    def clear(self):
        self._questions = []
        self._current_index = 0
        self._answers = {}
        self._clear_options()
        self.setVisible(False)

    # ────────────── 内部 ──────────────

    def _render_current(self):
        """渲染当前问题"""
        self._clear_options()
        self._apply_card_style()

        total = len(self._questions)
        if total == 0:
            # 无问题 → 直接关闭
            self._on_cancel()
            return

        q_data = self._questions[self._current_index]
        question_text = q_data.get("question", "")
        options = q_data.get("options", [])
        multiple = q_data.get("multiple", False)

        # 页码
        self._page_label.setText(f"{self._current_index + 1}/{total} 个问题")
        self._page_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")

        # 问题标题
        self._question_label.setText(question_text)

        # 辅助提示
        if multiple:
            self._hint_label.setText("选择所有适用的选项")
        elif options:
            self._hint_label.setText("选择一个答案")
        else:
            self._hint_label.setText("")

        # 渲染选项
        for opt in options:
            label = opt.get("label", str(opt))
            description = opt.get("description", "")
            if multiple:
                card = _OptionCheckCard(label, description, self._scroll_content)
                card.toggled.connect(lambda _=None: self._update_submit_state())
                self._options_layout.insertWidget(self._options_layout.count() - 1, card)
                self._option_widgets.append(card)
            else:
                card = _OptionRadioCard(label, description, self._scroll_content)
                card.clicked.connect(partial(self._on_radio_selected, card))
                self._options_layout.insertWidget(self._options_layout.count() - 1, card)
                self._option_widgets.append(card)

        # 如果有之前保存的答案，恢复
        saved = self._answers.get(self._current_index)
        if saved:
            self._restore_answer(saved, options, multiple)

        self._update_submit_state()
        self._scroll.verticalScrollBar().setValue(0)

    def _on_radio_selected(self, card):
        """单选选中"""
        for w in self._option_widgets:
            if isinstance(w, _OptionRadioCard):
                w.set_selected(w is card)
        self._update_submit_state()

    def _clear_options(self):
        for w in self._option_widgets:
            self._options_layout.removeWidget(w)
            w.deleteLater()
        self._option_widgets = []

    def _get_selected_options(self) -> list:
        """获取当前已选中的选项 list[dict]"""
        results = []
        for w in self._option_widgets:
            if isinstance(w, _OptionRadioCard) and w._selected:
                results.append({"label": w._label_text, "description": w._desc_text})
            elif isinstance(w, _OptionCheckCard) and w.isChecked():
                results.append({"label": w._label_text, "description": w._desc_text})
        return results

    def _has_selection(self) -> bool:
        return len(self._get_selected_options()) > 0

    def _update_submit_state(self):
        has = self._has_selection()
        self._submit_btn.setEnabled(has)
        self._submit_btn.setText("提交")

    def _save_current_answer(self):
        """保存当前问题的答案"""
        selected = self._get_selected_options()
        if selected:
            # 构建可读答案
            parts = [f"【{s['label']}】" for s in selected]
            if any(s.get("description") for s in selected):
                parts = [f"【{s['label']}】{s.get('description', '')}" for s in selected]
            self._answers[self._current_index] = "\n".join(parts)
        else:
            self._answers.pop(self._current_index, None)

    def _restore_answer(self, answer: str, options: list, multiple: bool):
        """恢复已有答案到选项状态（用于切换页面后恢复）"""
        for w in self._option_widgets:
            if isinstance(w, _OptionRadioCard):
                if answer and w._label_text in answer:
                    w.set_selected(True)
                else:
                    w.set_selected(False)
            elif isinstance(w, _OptionCheckCard):
                if answer and w._label_text in answer:
                    w.set_checked(True)
                else:
                    w.set_checked(False)

    def _on_ignore(self):
        """忽略 — 关闭卡片，返回空"""
        self.cancelled.emit()

    def _on_submit(self):
        """提交答案"""
        self._save_current_answer()

        # 是否还有未回答的问题？
        if self._current_index < len(self._questions) - 1:
            # 去下一个问题
            self._current_index += 1
            self._render_current()
            QTimer.singleShot(0, self.heightChanged.emit)
        else:
            # 所有问题已回答 → 构建最终答案
            self._build_and_emit_answer()

    def _build_and_emit_answer(self):
        """构建最终答案并发射 answered 信号"""
        total = len(self._questions)
        parts = []
        for i, q in enumerate(self._questions):
            q_text = q.get("question", f"问题{i+1}")
            answer = self._answers.get(i, "")
            if answer:
                parts.append(f"问题「{q_text}」的回答：\n{answer}")

        if not parts:
            self.cancelled.emit()
            return

        final_answer = "\n---\n".join(parts)
        self.answered.emit(final_answer)

    def set_opacity(self, opacity: float):
        """设置透明度"""
        Colors.refresh()
        bg = Colors.REALTIME_BG
        if bg.startswith("rgba("):
            alpha = max(1, int(opacity * 255))
            bg = bg.rsplit(",", 1)[0] + f", {alpha})"
        self.setStyleSheet(f"""
            QuestionFloatingWidget {{
                background-color: {bg};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
            }}
        """)
