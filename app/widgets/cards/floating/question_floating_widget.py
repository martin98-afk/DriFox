# -*- coding: utf-8 -*-
"""
悬浮提问卡片 - 支持多问题、选项标题+描述、自定义输入

触发方式：LLM 调用 question 工具
交互方式：点击选项单选/多选，分页导航，可跳过、可自定答案
"""

from functools import partial

from loguru import logger
from PySide6.QtCore import QEvent, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors, font_size_css
from app.utils.utils import get_font_family_css, get_icon, get_unified_font
from app.widgets.cards.card_container import CardContainer

# ═══════════════════════════════════════════════════════════
# 自适应高度滚动区
# ═══════════════════════════════════════════════════════════


class _AutoHeightScrollArea(QScrollArea):
    """高度跟随内容的自适应滚动区

    短内容 → 高度 = 内容高度（不产生空白）；
    长内容 → 高度 = maximumHeight，内部滚动。
    解决普通 QScrollArea sizeHint 固定（~192px）导致短文本也占大片空白的问题。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # 垂直改为 Preferred：布局尊重 sizeHint，不再把滚动区拉伸占满剩余空间
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def minimumSizeHint(self):
        # QAbstractScrollArea 默认 minimumSizeHint 含滚动条尺寸（~42px），
        # 会把短内容强行垫高，因此不复用默认值。改为返回"内容高度下限"：
        # 容器高度动画滞后 / 布局空间不足时，问题标题区是布局中唯一可被
        # 压到 0 的成员，minimumSizeHint=(0,0) 会被优先压没（标题完全不可见）。
        # 下限 = 内容高度（封顶 maximumHeight），保证问题标题区始终可见。
        base = super().sizeHint()
        w = self.widget()
        if w is None:
            return QSize(0, 0)
        frame = 2 * self.frameWidth()
        vw = self.viewport().width()
        if vw <= 0:
            vw = max(1, base.width() - frame)
        else:
            # 预留垂直滚动条宽度：内容高度临界时滚动条出现会使视口宽骤减，
            # wordWrap 内容重折行 → 高度变化 → 滚动条消失 → 宽度反馈环抖动。
            # 按“含滚动条”的最窄视口测高，滚动条出现后测量基准不变，环闭合。
            vw = max(1, vw - self.verticalScrollBar().sizeHint().width())
        if w.hasHeightForWidth():
            content_h = w.heightForWidth(vw)
        else:
            content_h = w.sizeHint().height()
        h = max(self.minimumHeight(), min(content_h + frame, self.maximumHeight()))
        return QSize(base.width(), h)

    def sizeHint(self):
        base = super().sizeHint()
        w = self.widget()
        if w is None:
            return base
        frame = 2 * self.frameWidth()
        # 用当前视口宽度估算换行后内容高度（QLabel wordWrap 时 heightForWidth 最准）
        vw = self.viewport().width()
        if vw <= 0:
            vw = max(1, base.width() - frame)
        else:
            # 同 minimumSizeHint：预留垂直滚动条宽度，消除滚动条出现/消失的宽度反馈环
            vw = max(1, vw - self.verticalScrollBar().sizeHint().width())
        if w.hasHeightForWidth():
            content_h = w.heightForWidth(vw)
        else:
            content_h = w.sizeHint().height()
        h = max(self.minimumHeight(), min(content_h + frame, self.maximumHeight()))
        return QSize(base.width(), h)


# ═══════════════════════════════════════════════════════════
# 单选选项卡片
# ═══════════════════════════════════════════════════════════


class _OptionRadioCard(QWidget):
    """单选选项卡片 — 标题 + 描述"""

    clicked = Signal()

    def __init__(self, label: str, description: str = "", parent=None):
        super().__init__(parent)
        self._label_text = label
        self._desc_text = description
        self._selected = False
        self._hovered = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(44)
        self._setup_ui()

    def sizeHint(self):
        """高度跟随当前宽度下的真实换行高度

        wordWrap QLabel 的 C++ sizeHint() 用固定宽度估算换行（窄宽度），
        在卡片实际较宽时严重高估（如 desc 单行却按 2-3 行算），逐级传递到
        提问卡片 sizeHint → CardContainer 锁高 → 「下一步」按钮下方大段空白。
        布局的 heightForWidth(当前宽度) 才是真实换行高度。
        """
        base = super().sizeHint()
        lay = self.layout()
        if lay is not None and lay.hasHeightForWidth():
            w = self.width()
            if w > 0:
                return QSize(base.width(), lay.heightForWidth(w))
        return base

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        self._icon = QLabel("○")
        self._icon.setFont(get_unified_font(13))
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
        if not self._selected:
            self._apply_style()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False
        if not self._selected:
            self._apply_style()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


# ═══════════════════════════════════════════════════════════
# 多选选项卡片
# ═══════════════════════════════════════════════════════════


class _OptionCheckCard(QWidget):
    """多选选项卡片 — 标题 + 描述"""

    toggled = Signal(bool)

    def __init__(self, label: str, description: str = "", parent=None):
        super().__init__(parent)
        self._label_text = label
        self._desc_text = description
        self._checked = False
        self._hovered = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(44)
        self._setup_ui()

    def sizeHint(self):
        """高度跟随当前宽度下的真实换行高度（同 _OptionRadioCard）"""
        base = super().sizeHint()
        lay = self.layout()
        if lay is not None and lay.hasHeightForWidth():
            w = self.width()
            if w > 0:
                return QSize(base.width(), lay.heightForWidth(w))
        return base

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)

        self._icon = QLabel("□")
        self._icon.setFont(get_unified_font(13))
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
        if not self._checked:
            self._apply_style()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False
        if not self._checked:
            self._apply_style()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.toggle()
        super().mousePressEvent(e)


# ═══════════════════════════════════════════════════════════
# 自定义输入选项卡片
# ═══════════════════════════════════════════════════════════


class _CustomInputCard(QWidget):
    """输入自己的答案选项 — 默认显示描述，选中后变成文本输入框"""

    PLACEHOLDER = "输入你的答案..."
    activated = Signal()  # 用户主动点击选中时触发
    heightNeedsUpdate = Signal()  # 高度需要更新时触发

    MAX_INPUT_HEIGHT = 220  # 输入框最大高度
    MIN_INPUT_HEIGHT = 32  # 输入框初始单行高度（一行文字 + 内边距）

    def __init__(self, multiple: bool = False, parent=None):
        super().__init__(parent)
        self._active = False
        self._text_value = ""
        self._multiple = multiple
        self._label_text = "输入自己的答案"
        self._desc_text = self.PLACEHOLDER
        self._adjusting_height = False
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(44)
        self._setup_ui()

    def sizeHint(self):
        """高度跟随当前宽度下的真实换行高度（同 _OptionRadioCard）"""
        base = super().sizeHint()
        lay = self.layout()
        if lay is not None and lay.hasHeightForWidth():
            w = self.width()
            if w > 0:
                return QSize(base.width(), lay.heightForWidth(w))
        return base

    def showEvent(self, event):
        """控件变为可见时自动聚焦到文本输入框（如果处于激活态）"""
        super().showEvent(event)
        if self._active and event.isAccepted():
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, lambda: self._text_edit.setFocus() if self.isVisible() else None)

    def _setup_ui(self):
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        # 关键：与选项卡片 _OptionRadioCard / _OptionCheckCard 完全相同的横向布局结构
        # 让输入框的文字起始 x 坐标跟选项标题文字（12+18+10=40px）对齐
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(12, 10, 12, 10)
        self._layout.setSpacing(10)

        self._icon = QLabel("□" if self._multiple else "○")
        self._icon.setFont(get_unified_font(13))
        self._icon.setFixedWidth(18)
        self._icon.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self._icon.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._layout.addWidget(self._icon, 0)

        # 右侧：标题 + 描述/输入框（垂直布局）
        self._right_layout = QVBoxLayout()
        self._right_layout.setContentsMargins(0, 0, 0, 0)
        self._right_layout.setSpacing(4)

        self._title_label = QLabel("输入自己的答案")
        self._title_label.setFont(get_unified_font(11, True))
        self._title_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._right_layout.addWidget(self._title_label)

        self._desc_label = QLabel(self.PLACEHOLDER)
        self._desc_label.setFont(get_unified_font(9))
        self._desc_label.setStyleSheet(f"color:{Colors.REALTIME_TEXT_SECONDARY};background:transparent;")
        self._desc_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._right_layout.addWidget(self._desc_label)

        self._text_edit = QTextEdit()
        self._text_edit.setPlaceholderText(self.PLACEHOLDER)
        self._text_edit.setFont(get_unified_font(10))
        self._text_edit.setMaximumHeight(self.MAX_INPUT_HEIGHT)
        self._text_edit.setMinimumHeight(self.MIN_INPUT_HEIGHT)
        self._text_edit.setFixedHeight(self.MIN_INPUT_HEIGHT)
        self._text_edit.setLineWrapMode(QTextEdit.WidgetWidth)
        # 兜底：即使 auto-grow 临时失效，垂直滚动条也能让用户看到溢出内容
        self._text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._text_edit.setVisible(False)
        self._text_edit.textChanged.connect(self._on_text_changed)
        self._text_edit.installEventFilter(self)  # 监听 Resize/Show，等布局完成后再算高度
        # 强制白色文字：Qt 样式表 color 对 QTextEdit 经常不生效，需用 QPalette
        pal = self._text_edit.palette()
        pal.setColor(QPalette.Text, QColor("#ffffff"))
        self._text_edit.setPalette(pal)
        self._right_layout.addWidget(self._text_edit)

        self._layout.addLayout(self._right_layout, 1)
        self._apply_style()

    def eventFilter(self, obj, event):
        """监听 _text_edit 的 Show 和 Resize 事件：布局完成后才计算高度

        必须延迟到下一轮事件循环（QTimer.singleShot(0, ...)），
        因为 Resize 事件触发时 viewport().width() 还没更新好。
        """
        if obj is self._text_edit and self._active:
            if event.type() in (QEvent.Resize, QEvent.Show):
                QTimer.singleShot(0, self._adjust_height_to_content)
        return super().eventFilter(obj, event)

    def _on_text_changed(self):
        self._text_value = self._text_edit.toPlainText()
        if self._active:
            QTimer.singleShot(0, self._adjust_height_to_content)

    def _adjust_height_to_content(self):
        """根据内容自动调整输入框高度

        双重计算策略：
        1. 优先用 QTextDocument.size()（最准确）
        2. 如果 document 还没准备好，用 fontMetrics 估算（兜底）
        3. 如果 viewport 宽度还没好，延迟 20ms 重试
        """
        if self._adjusting_height:
            return
        if not self._text_edit.isVisible():
            return

        viewport_width = self._text_edit.viewport().width()
        if viewport_width <= 0:
            # 布局未完成，延迟重试
            QTimer.singleShot(20, self._adjust_height_to_content)
            return

        # ── 策略 1：QTextDocument.size() ──
        doc = self._text_edit.document()
        doc.setTextWidth(viewport_width)
        doc_height = int(doc.size().height())

        # ── 策略 2：fontMetrics 兜底估算 ──
        # 如果 size() 返回 0（比如刚 setTextWidth 后还没重排），
        # 用 fontMetrics 根据行数和字符宽度估算
        if doc_height <= 0:
            fm = self._text_edit.fontMetrics()
            line_height = fm.lineSpacing()
            avg_char_w = max(1, fm.averageCharWidth())
            chars_per_line = max(1, viewport_width // avg_char_w)
            text = self._text_edit.toPlainText()
            if not text:
                # 空文本：一行高度（光标占位）
                doc_height = line_height
            else:
                total_lines = 0
                for line in text.split("\n"):
                    n = len(line)
                    if n == 0:
                        total_lines += 1
                    else:
                        total_lines += max(1, -(-n // chars_per_line))  # 向上取整
                doc_height = total_lines * line_height

        # padding：上下各 6px
        total_height = doc_height + 12
        new_height = max(self.MIN_INPUT_HEIGHT, min(self.MAX_INPUT_HEIGHT, total_height))

        if self._text_edit.height() != new_height:
            self._adjusting_height = True
            try:
                self._text_edit.setFixedHeight(new_height)
                self._emit_height_update()
            finally:
                self._adjusting_height = False

    def set_active(self, active: bool):
        self._active = active
        a_icon = "☑" if self._multiple else "●"
        i_icon = "□" if self._multiple else "○"
        self._icon.setText(a_icon if active else i_icon)
        self._desc_label.setVisible(not active)
        self._text_edit.setVisible(active)
        if active:
            self._text_edit.setFixedHeight(self.MIN_INPUT_HEIGHT)
            # ★ 仅在可见时聚焦——防止 QStackedWidget 隐藏页窃取焦点
            if self.isVisible():
                self._text_edit.setFocus()
            if self._text_value:
                self._text_edit.setPlainText(self._text_value)
            # 延迟到下一轮事件循环，等布局完成（viewport().width() > 0）后再算高度
            # _adjust_height_to_content 内部已在高度变化时调用 _emit_height_update
            # 不需要额外的 10ms 兜底 timer（避免与导航路径的 heightChanged 重复触发）
            QTimer.singleShot(0, self._adjust_height_to_content)
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
                /* 左右 padding 设为 0：让文字从 x=40px 开始，跟选项标题对齐 */
                padding-top: 6px;
                padding-bottom: 6px;
                padding-left: 0px;
                padding-right: 0px;
                {get_font_family_css()} font-size: {font_size_css(10)};
            }}
            QTextEdit:focus {{ border-color: {Colors.REALTIME_ACCENT}; }}
        """)
        # 样式表 color 对 QTextEdit 不稳定，用 QPalette 兜底
        pal = self._text_edit.palette()
        pal.setColor(QPalette.Text, QColor(Colors.REALTIME_TEXT))
        pal.setColor(QPalette.Base, QColor(Colors.HOVER_BG))
        self._text_edit.setPalette(pal)

    def enterEvent(self, e):
        if not self._active:
            self.setStyleSheet(
                f"_CustomInputCard{{background-color:{Colors.REALTIME_TAG_BG};border:1px solid {Colors.REALTIME_TAG_BORDER};border-radius:8px;}}"
            )
        super().enterEvent(e)

    def leaveEvent(self, e):
        if not self._active:
            self._apply_style()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.toggle()
        super().mousePressEvent(e)


# ═══════════════════════════════════════════════════════════
# 主提问卡片
# ═══════════════════════════════════════════════════════════


class QuestionFloatingWidget(QWidget):
    """悬浮提问卡片，支持多问题分页"""

    answered = Signal(str)
    cancelled = Signal()
    previewRequested = Signal(object)
    heightChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._questions = []
        self._current_index = 0
        self._answers = {}
        self._option_widgets = []
        self._custom_input_widget = None
        self._show_custom_input = True
        self._preview_payload = None
        self._collapsed = False
        # 高度严格跟随内容：即使容器处于 dock 模式（高度由 QSplitter 分配、
        # 默认不随内容收缩），也锁定容器高度 = 卡片 sizeHint，
        # 避免"容器比内容高 → 卡片内部/底部出现空白"。
        self.setProperty("followContent", True)
        # 声明跳过容器 200ms 展开/折叠动画：自定义输入框增高时高度变化经
        # heightChanged 触发容器 _do_expand，动画期间容器高度滞后于卡片实际
        # 高度，QVBoxLayout 空间不足会优先把问题标题区压没。跳过动画让容器
        # 高度直接 snap 到目标值，消除滞后窗口期。
        self.setProperty(CardContainer.NO_ANIMATION_PROP, True)
        self._setup_ui()

    def showEvent(self, event):
        """控件变为可见时自动聚焦到下一步按钮，并在首帧布局完成后重算高度

        卡片首次显示时容器/卡片宽度可能尚未稳定（dock splitter 动画分配尺寸中），
        导致 sizeHint/heightForWidth 用错误宽度估算高度、被 CardContainer 锁高，
        出现内容抖动；resize 后宽度稳定才修正。此处于布局完成后（width 已定）
        主动重算一次，纠正首帧错锁。
        """
        super().showEvent(event)
        if event.isAccepted() and self._questions:
            from PySide6.QtCore import QTimer

            # 延迟到布局完成后聚焦，确保按钮在正确位置
            QTimer.singleShot(0, lambda: self._next_btn.setFocus() if self.isVisible() else None)
            # 首帧布局完成后强制重算高度，纠正因宽度未定导致的锁高抖动
            QTimer.singleShot(0, self._resync_height_on_show)

    def _resync_height_on_show(self):
        """首帧显示后、布局稳定时强制重算高度（消除显示瞬间抖动）

        与 resizeEvent / show_question 的重算互补：本方法在 showEvent 之后
        （此时控件已可见、width 已确定）触发，纠正 show_question 早于 show
        调用时首帧宽度未定造成的错锁。
        """
        if not self.isVisible():
            return
        self.updateGeometry()
        self.heightChanged.emit()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 2, 10, 2)
        main_layout.setSpacing(2)

        # ── 顶栏 ──
        self._header_widget = QWidget()
        self._header_widget.setCursor(Qt.PointingHandCursor)
        self._header_widget.setFixedHeight(24)
        self._header_widget.installEventFilter(self)
        header = QHBoxLayout(self._header_widget)
        header.setSpacing(4)
        header.setContentsMargins(0, 0, 0, 0)

        self._collapse_btn = QPushButton()
        self._collapse_btn.setIcon(get_icon("折叠"))
        self._collapse_btn.setIconSize(QSize(16, 16))
        self._collapse_btn.setFixedSize(24, 24)
        self._collapse_btn.setCursor(Qt.PointingHandCursor)
        self._collapse_btn.setToolTip("折叠问题")
        self._collapse_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background: rgba(255,255,255,0.1);
            }
        """)
        self._collapse_btn.clicked.connect(self._toggle_collapse)
        header.addWidget(self._collapse_btn)

        self._page_label = QLabel("")
        self._page_label.setFont(get_unified_font(10))
        self._page_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        header.addWidget(self._page_label)
        header.addStretch()

        main_layout.addWidget(self._header_widget)

        # ── 问题标题（按内容动态高度，上限自适应） ──
        self._question_scroll = _AutoHeightScrollArea()
        self._question_scroll.setWidgetResizable(True)
        self._question_scroll.setMaximumHeight(280)
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
        self._question_label.installEventFilter(self)  # 内容/宽度变化 → 重算自适应高度
        self._question_scroll.setWidget(self._question_label)
        main_layout.addWidget(self._question_scroll)

        # ── 提示 ──
        self._hint_label = QLabel("")
        self._hint_label.setFont(get_unified_font(9))
        self._hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        main_layout.addWidget(self._hint_label)

        # ── 选项区（直接布局，随内容自然展开） ──
        self._options_container = QWidget()
        self._options_container.setStyleSheet("background: transparent;")
        self._options_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._options_layout = QVBoxLayout(self._options_container)
        self._options_layout.setContentsMargins(0, 0, 0, 0)
        self._options_layout.setSpacing(6)
        main_layout.addWidget(self._options_container)

        # ── 底栏 ──
        self._footer_widget = QWidget()
        self._footer_widget.setStyleSheet("background: transparent;")
        footer = QHBoxLayout(self._footer_widget)
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)

        self._ignore_btn = QPushButton("忽略")
        self._ignore_btn.setCursor(Qt.PointingHandCursor)
        self._ignore_btn.setFont(get_unified_font(10))
        self._ignore_btn.clicked.connect(self._on_ignore)
        self._ignore_btn.setStyleSheet(f"""
            QPushButton {{ color: {Colors.TEXT_SECONDARY}; background: transparent; border: none; padding: 6px 0; }}
            QPushButton:hover {{ color: {Colors.TEXT_SECONDARY_HOVER}; }}
        """)

        self._preview_btn = QPushButton("预览参数")
        self._preview_btn.setFixedHeight(26)
        self._preview_btn.setCursor(Qt.PointingHandCursor)
        self._preview_btn.setFont(get_unified_font(10))
        self._preview_btn.clicked.connect(self._on_preview)
        self._preview_btn.setVisible(False)
        # 🛠️ 跟随主题：原本硬编码 rgba(255,255,255,...) 在浅色主题（crema 等
        # realtime_bg 偏浅 + REALTIME_TAG_BG 浅黄）下变成"白字淡黄底"不可读。
        # 改用 REALTIME_TEXT，深色主题=浅色字，浅色主题=深色字，与卡片整体配色一致。
        self._preview_btn.setStyleSheet(f"""
            QPushButton {{ color: {Colors.REALTIME_TEXT}; background: {Colors.REALTIME_TAG_BG}; border: none; border-radius: 6px; padding: 0 14px; }}
            QPushButton:hover {{ color: {Colors.REALTIME_TEXT}; background: {Colors.CARD_BG_SOLID}; }}
        """)

        self._back_btn = QPushButton("返回")
        self._back_btn.setFixedHeight(26)
        self._back_btn.setCursor(Qt.PointingHandCursor)
        self._back_btn.setFont(get_unified_font(10))
        self._back_btn.clicked.connect(self._on_back)
        self._back_btn.setStyleSheet(f"""
            QPushButton {{ color: {Colors.TEXT_SECONDARY}; background: {Colors.HOVER_BG}; border: none; border-radius: 6px; padding: 0 14px; }}
            QPushButton:hover {{ color: {Colors.TEXT_PRIMARY}; background: {Colors.SELECTED_BG}; }}
        """)

        self._next_btn = QPushButton("下一步")
        self._next_btn.setFixedHeight(26)
        self._next_btn.setCursor(Qt.PointingHandCursor)
        self._next_btn.setFont(get_unified_font(10, True))
        self._next_btn.clicked.connect(self._on_next)
        self._next_btn.setStyleSheet(f"""
            QPushButton {{ background-color: {Colors.CARD_BG}; color: {Colors.TEXT_PRIMARY}; border: none; border-radius: 6px; padding: 0 18px; font-weight: bold; }}
            QPushButton:hover {{ background-color: {Colors.CARD_BG_SOLID}; }}
        """)

        footer.addWidget(self._ignore_btn)
        footer.addStretch()
        footer.addWidget(self._back_btn)
        footer.addWidget(self._preview_btn)
        footer.addWidget(self._next_btn)

        main_layout.addWidget(self._footer_widget)

        # 尾部占位（stretch 因子 = 0，不吸收剩余空间）：
        # 与 follow_content 协议协同保证容器高度 = 卡片 sizeHint → 不再产生
        # 「下一步」按钮下方的空白。CardContainer 在 follow_content 分支同时锁
        # min/max 到 natural_h 并显式 _restore_dock_size 让 splitter 实际分配
        # 也匹配；这里不再用 addStretch(1) 兜底，避免锁定失败时拉伸内容样式错乱
        # 被掩盖。占位仅作为视觉收尾，sizeHint 仍 = 内容累加（不含此 stretch）。
        main_layout.addStretch(0)

        self._apply_card_style()
        self._setup_shortcuts()

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
        if hasattr(self, "_ignore_btn") and self._ignore_btn:
            self._ignore_btn.setStyleSheet(f"""
                QPushButton {{ color: {Colors.TEXT_SECONDARY}; background: transparent; border: none; padding: 6px 0; }}
                QPushButton:hover {{ color: {Colors.TEXT_SECONDARY_HOVER}; }}
            """)
        if hasattr(self, "_back_btn") and self._back_btn:
            self._back_btn.setStyleSheet(f"""
                QPushButton {{ color: {Colors.TEXT_SECONDARY}; background: {Colors.HOVER_BG}; border: none; border-radius: 6px; padding: 0 14px; }}
                QPushButton:hover {{ color: {Colors.TEXT_PRIMARY}; background: {Colors.SELECTED_BG}; }}
            """)
        if hasattr(self, "_next_btn") and self._next_btn:
            self._next_btn.setStyleSheet(f"""
                QPushButton {{ background-color: {Colors.CARD_BG}; color: {Colors.TEXT_PRIMARY}; border: none; border-radius: 6px; padding: 0 18px; font-weight: bold; }}
                QPushButton:hover {{ background-color: {Colors.CARD_BG_SOLID}; }}
            """)

    def refresh_style(self):
        """刷新样式（主题/深浅切换时调用）"""
        self._apply_card_style()

    def eventFilter(self, obj, event):
        # 窗口 resize → 动态更新各区域最大高度
        if event.type() == QEvent.Resize and obj is self.window() and obj is not None:
            self._update_dynamic_heights()
            return False
        # 问题文本内容 / 宽度变化 → 重算滚动区自适应高度，通知容器重排
        # （getattr 防御：eventFilter 安装早于 _question_label 创建）
        if getattr(self, "_question_label", None) is obj and event.type() in (
            QEvent.Resize,
            QEvent.LayoutRequest,
        ):
            self._sync_question_area()
            return False
        if obj is self._header_widget and event.type() == QEvent.MouseButtonPress:
            self._toggle_collapse()
            return True
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        """卡片自身宽度变化 → 通知容器重算高度

        选项描述（wordWrap）在宽度变化后换行数改变 → 卡片真实高度改变，
        但布局 sizeHint 缓存不会自动失效、也没有信号通知 CardContainer。
        不处理会导致容器锁在旧高度：变宽后底部留白 / 变窄后内容被压。
        仅宽度变化时触发（高度变化由 heightChanged 其它路径处理，避免
        容器动画与自身 resize 互相干扰）。
        """
        super().resizeEvent(event)
        new_w = event.size().width()
        if new_w != getattr(self, "_last_layout_width", -1):
            self._last_layout_width = new_w
            self.updateGeometry()
            QTimer.singleShot(0, self.heightChanged.emit)

    def heightForWidth(self, w):
        """按宽度 w 计算真实内容高度

        CardContainer 在 follow_content 分支显式调用此方法（而非布局 sizeHint）
        来锁定容器高度：C++ 布局 sizeHint 受 wordWrap QLabel 高估影响，
        heightForWidth 按真实宽度换行，返回准确高度。
        """
        lay = self.layout()
        if lay is not None:
            return lay.heightForWidth(w)
        return super().heightForWidth(w)

    def keyPressEvent(self, event):
        """键盘快捷键：Enter/数字键选选项（不在文本输入框时生效）"""
        key = event.key()
        mods = event.modifiers()
        # Ctrl+Enter → 下一步（已在 QShortcut 中捕获，此处兜底）
        if mods & Qt.ControlModifier and key in (Qt.Key_Return, Qt.Key_Enter):
            self._on_next()
            return
        # 纯 Enter → 下一步（焦点不在 QTextEdit 时才到达此处）
        if not mods and key in (Qt.Key_Return, Qt.Key_Enter):
            self._on_next()
            return
        # 数字键 1-9 → 选择对应序号选项
        if not mods and Qt.Key_1 <= key <= Qt.Key_9:
            self._select_option_by_digit(key - Qt.Key_0)
            return
        super().keyPressEvent(event)

    def _select_option_by_digit(self, digit: int):
        """按数字键选择对应序号的可见选项（1 → 第一个选项）"""
        idx = digit - 1
        if idx < 0 or idx >= len(self._option_widgets):
            return
        w = self._option_widgets[idx]
        if not w.isVisible():
            return
        if isinstance(w, _OptionCheckCard):
            w.toggle()
        else:
            self._on_radio_selected(w)

    def _update_dynamic_heights(self):
        """根据窗口高度动态调整问题标题区的最大高度

        短内容自然展开（≤ sizeHint），长内容在窗口比例的合理阈值内滚动，
        避免硬编码值在小窗口溢出 / 大窗口浪费。
        """
        win = self.window()
        win_h = win.height() if win is not None else 800
        # 问题标题：窗口 30%，夹紧 [40, 320]
        q_max = max(40, min(int(win_h * 0.30), 320))
        self._question_scroll.setMaximumHeight(q_max)

    def _sync_question_area(self):
        """问题区高度跟随内容：短内容收缩、长内容限高滚动

        仅在期望高度（sizeHint）与实际高度不一致时触发重排，
        避免 QLabel Resize 级联 → 容器 _do_expand → 再 Resize 的无限循环。
        """
        sc = self._question_scroll
        want_h = sc.sizeHint().height()
        if want_h != sc.height():
            sc.updateGeometry()
            QTimer.singleShot(0, self.heightChanged.emit)

    def _toggle_collapse(self):
        """折叠/展开提问卡片，仅保留顶栏"""
        self._collapsed = not self._collapsed
        visible = not self._collapsed
        self._question_scroll.setVisible(visible)
        self._hint_label.setVisible(visible)
        self._options_container.setVisible(visible)
        self._footer_widget.setVisible(visible)
        self._collapse_btn.setIcon(get_icon("展开" if self._collapsed else "折叠"))
        self._collapse_btn.setToolTip("展开问题" if self._collapsed else "折叠问题")
        QTimer.singleShot(0, self.heightChanged.emit)

    def _setup_shortcuts(self):
        """设置键盘快捷键"""
        # Esc → 忽略（WidgetWithChildrenShortcut 确保在子控件聚焦时也生效）
        sc_esc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        sc_esc.setContext(Qt.WidgetWithChildrenShortcut)
        sc_esc.activated.connect(self._on_ignore)

        # Ctrl+Enter / Ctrl+Return → 下一步/提交（文本输入框中也可用）
        sc_ctrl_ret = QShortcut(QKeySequence("Ctrl+Return"), self)
        sc_ctrl_ret.setContext(Qt.WidgetWithChildrenShortcut)
        sc_ctrl_ret.activated.connect(self._on_next)
        sc_ctrl_ent = QShortcut(QKeySequence("Ctrl+Enter"), self)
        sc_ctrl_ent.setContext(Qt.WidgetWithChildrenShortcut)
        sc_ctrl_ent.activated.connect(self._on_next)

    # ────────────── 公开接口 ──────────────

    def show_question(self, questions: list, show_custom_input: bool = True, preview_payload=None):
        self._questions = questions if isinstance(questions, list) else []
        self._current_index = 0
        self._answers = {}
        self._show_custom_input = show_custom_input
        self._preview_payload = preview_payload
        # 新问题进来时自动展开
        if self._collapsed:
            self._toggle_collapse()
        # 安装窗口 resize 监听（只一次）
        win = self.window()
        if win is not None and win is not self and not getattr(self, "_win_filtered", False):
            win.installEventFilter(self)
            self._win_filtered = True
        self._update_dynamic_heights()
        self._render_current()
        # 强制几何重新计算，确保 _options_container 的 sizeHint 反映最新内容
        # 避免 CardContainer._do_expand 读到过期的 sizeHint 而跳过展开
        self.updateGeometry()
        QTimer.singleShot(0, self.heightChanged.emit)

    def clear(self):
        self._questions = []
        self._current_index = 0
        self._answers = {}
        self._preview_payload = None
        self._preview_btn.setVisible(False)
        self._full_clear_options()
        self.setVisible(False)

    def _on_preview(self):
        if self._preview_payload is not None:
            self.previewRequested.emit(self._preview_payload)

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

        self._page_label.setText(f" {self._current_index + 1} / {total} ")
        self._page_label.setStyleSheet(f"""
            color:{Colors.REALTIME_ACCENT};
            background:{Colors.REALTIME_TAG_BG};
            border:1px solid {Colors.REALTIME_ACCENT};
            border-radius:10px;
            padding:1px 8px;
        """)

        self._question_label.setText(question_text)

        self._hint_label.setText(
            "☑ 选择所有适用的选项（可多选）" if multiple and options else "👆 选择一个答案" if options else ""
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

        self._update_footer(total)
        # 自动聚焦到下一步按钮，键盘操作立即可用
        # ★ 仅在可见时聚焦——否则 QStackedWidget 隐藏页的 setFocus()
        #   会从当前活动 Tab 的输入框窃取焦点（Tab 模式 bug）。
        if self.isVisible():
            self._next_btn.setFocus()

    def _update_footer(self, total: int):
        is_first = self._current_index == 0
        is_last = self._current_index == total - 1
        self._back_btn.setVisible(not is_first)
        self._preview_btn.setVisible(self._preview_payload is not None)
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
            if hasattr(w, "set_selected"):
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
            if hasattr(w, "_selected") and w._selected:
                results.append({"label": w._label_text, "description": w._desc_text})
            elif hasattr(w, "isChecked") and w.isChecked():
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
                    if hasattr(w, "set_selected"):
                        w.set_selected(False)
                    elif hasattr(w, "set_checked"):
                        w.set_checked(False)
            if self._custom_input_widget:
                self._custom_input_widget.set_active(False)
            return

        text = answer["text"] if isinstance(answer, dict) else answer
        custom_used = answer.get("custom", False) if isinstance(answer, dict) else ("输入自己的答案" in text)

        for w in self._option_widgets:
            if w.isVisible():
                if hasattr(w, "set_selected"):
                    w.set_selected(text and w._label_text in text)
                elif hasattr(w, "set_checked"):
                    w.set_checked(text and w._label_text in text)
        if self._custom_input_widget:
            self._custom_input_widget.set_active(custom_used)
            if custom_used and isinstance(answer, dict):
                custom_text = answer.get("custom_text", "") or answer.get("text", "")
                # 如果是混合答案（选项+自定义），提取纯自定义部分
                import re

                pure = re.sub(r"【[^】]+】[；]?", "", custom_text).strip("；").strip()
                if pure:
                    self._custom_input_widget.set_content(pure)

    def _on_back(self):
        self._save_current_answer()
        if self._current_index > 0:
            self._current_index -= 1
            self.setUpdatesEnabled(False)
            self._render_current()
            self.setUpdatesEnabled(True)
            # 内容变更后强制几何重新计算，确保容器高度同步更新
            self.updateGeometry()
            QTimer.singleShot(0, self.heightChanged.emit)

    def _on_next(self):
        self._save_current_answer()
        total = len(self._questions)
        if self._current_index < total - 1:
            self._current_index += 1
            self.setUpdatesEnabled(False)
            self._render_current()
            self.setUpdatesEnabled(True)
            # 内容变更后强制几何重新计算，确保容器高度同步更新
            self.updateGeometry()
            QTimer.singleShot(0, self.heightChanged.emit)
        else:
            self._build_and_emit_answer()

    def _on_ignore(self):
        self.cancelled.emit()

    def _build_and_emit_answer(self):
        parts = []
        for i, q in enumerate(self._questions):
            q_text = q.get("question", f"问题{i + 1}")
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
