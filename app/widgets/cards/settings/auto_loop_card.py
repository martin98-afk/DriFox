# -*- coding: utf-8 -*-
"""
AutoLoop 卡片组件 — 配置卡 + 运行卡

- AutoLoopConfigCard: 配置参数 + 任务输入 + 开始按钮（竖排布局，插入到聊天区）
- AutoLoopRunningCard: 运行状态显示 + 停止按钮（彩虹渐变边框动画）
"""
import time

from PyQt5.QtCore import (
    Qt, pyqtSignal, QTimer, QVariantAnimation,
)
from PyQt5.QtGui import (
    QPainter, QPen, QBrush, QLinearGradient, QColor,
)
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTextEdit, QFrame, QProgressBar, )
from qfluentwidgets import (
    PrimaryPushButton, PushButton, BodyLabel, StrongBodyLabel, LineEdit,
    SpinBox, FluentIcon, ToolButton, TransparentToolButton, )
from qfluentwidgets.components.widgets.card_widget import CardSeparator
from qfluentwidgets.components.widgets.flyout import IconWidget

from app.core.engines.auto_loop import AutoLoopConfig
from app.utils.utils import get_font_family_css, get_icon
from app.utils.design_tokens import font_size_css, scale_font_size

FONT_CSS = get_font_family_css()


# ============================================================
#  AutoLoop 配置卡
# ============================================================

class AutoLoopConfigCard(QFrame):
    """AutoLoop 配置卡片 — 插入到聊天区的竖排布局"""

    startRequested = pyqtSignal(AutoLoopConfig)

    closed = pyqtSignal()  # 关闭按钮通知  # 用户点击开始

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("autoLoopConfigCard")
        self._refresh_theme_style()
        self._build_ui()

    def refresh_font_size(self):
        """刷新字体大小配置"""
        self._refresh_theme_style()
        self._refresh_component_styles()

    def _refresh_component_styles(self):
        """刷新内部组件样式"""
        # 刷新 BodyLabel 字体大小
        for label in self.findChildren(BodyLabel):
            label.setStyleSheet(f"color: #B4C2D9; {FONT_CSS} font-size: {scale_font_size(14)}px;")
        # 刷新按钮
        self._start_btn.setStyleSheet(f"""
            PrimaryPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #C9A85C, stop:1 #B8956A);
                color: #1A1F2B;
                border: none;
                border-radius: 8px;
                padding: 4px 14px;
                {FONT_CSS} font-size: {scale_font_size(12)}px;
                font-weight: bold;
            }}
            PrimaryPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #D4B878, stop:1 #C9A060);
            }}
        """)
        self._iteration_spin.setStyleSheet(self._spin_style())
        self._token_spin.setStyleSheet(self._spin_style())
        self._duration_spin.setStyleSheet(self._spin_style())
        self._threshold_spin.setStyleSheet(self._spin_style())
        self._path_edit.setStyleSheet(self._line_style())
        self._prompt_edit.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(255, 255, 255, 0.05);
                color: #EAF2FF;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 8px;
                padding: 6px 10px;
                {FONT_CSS} font-size: {scale_font_size(13)}px;
            }}
            QTextEdit:focus {{
                border: 1px solid #C9A85C;
            }}
        """)
        # 刷新标题
        for label in self.findChildren(StrongBodyLabel):
            label.setStyleSheet(f"color: #EAF2FF; font-size: {scale_font_size(14)}px; {FONT_CSS}")

    def _refresh_theme_style(self):
        """刷新主题色，响应全局主题切换"""
        from app.utils.design_tokens import Colors
        Colors.refresh()
        self.setStyleSheet(f"""
            #autoLoopConfigCard {{
                background: {Colors.CARD_BG_SOLID};
                border: 1px solid {Colors.BORDER};
                border-radius: 12px;
                {FONT_CSS}
            }}
        """)

    def _build_ui(self):
        # 从配置读取默认值，避免硬编码不一致
        _default_config = AutoLoopConfig()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setSpacing(3)

        # ---- 标题栏（含开始按钮） ----
        title_layout = QHBoxLayout()
        icon_label = IconWidget(get_icon("无限"))
        icon_label.setFixedSize(28, 28)
        title_layout.addWidget(icon_label)
        title_layout.addSpacing(6)
        title = StrongBodyLabel("AutoLoop 自动循环")
        title.setStyleSheet(f"color: #EAF2FF; {font_size_css(14)} {FONT_CSS}")
        title_layout.addWidget(title)
        title_layout.addStretch()

        self._start_btn = PrimaryPushButton("▶ 开始")
        self._start_btn.setStyleSheet(f"""
            PrimaryPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #C9A85C, stop:1 #B8956A);
                color: #1A1F2B;
                border: none;
                border-radius: 8px;
                padding: 4px 14px;
                {FONT_CSS} {font_size_css(12)}
                font-weight: bold;
            }}
            PrimaryPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #D4B878, stop:1 #C9A060);
            }}
        """)
        self._start_btn.clicked.connect(self._on_start)
        title_layout.addWidget(self._start_btn)

        # 关闭按钮
        self.close_btn = TransparentToolButton(FluentIcon.CLOSE)
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.mousePressEvent = lambda e: self._on_close()
        title_layout.addWidget(self.close_btn)

        layout.addLayout(title_layout)

        layout.addWidget(CardSeparator())

        # ---- 参数配置（竖排）----
        field_layout = QVBoxLayout()
        field_layout.setSpacing(6)

        def _make_field(label_text, widget):
            row = QHBoxLayout()
            row.setSpacing(8)
            lbl = BodyLabel(label_text)
            lbl.setStyleSheet(f"color: #B4C2D9; {FONT_CSS} font-size: {scale_font_size(14)}px;")
            lbl.setFixedWidth(120)
            row.addWidget(lbl)
            row.addWidget(widget, 1)
            return row

        self._iteration_spin = SpinBox()
        self._iteration_spin.setRange(1, 10000)
        self._iteration_spin.setValue(_default_config.max_iterations)
        self._iteration_spin.setFixedHeight(28)
        self._iteration_spin.setStyleSheet(self._spin_style())
        field_layout.addLayout(_make_field("最大迭代轮数", self._iteration_spin))

        self._token_spin = SpinBox()
        self._token_spin.setRange(1000, 100000000)
        self._token_spin.setValue(_default_config.max_tokens)
        self._token_spin.setSingleStep(100000)
        self._token_spin.setFixedHeight(28)
        self._token_spin.setStyleSheet(self._spin_style())
        field_layout.addLayout(_make_field("Token 上限", self._token_spin))

        self._duration_spin = SpinBox()
        self._duration_spin.setRange(0, 14400)
        self._duration_spin.setValue(_default_config.max_duration_minutes)
        self._duration_spin.setSuffix(" 分钟")
        self._duration_spin.setSpecialValueText("不限")
        self._duration_spin.setFixedHeight(28)
        self._duration_spin.setStyleSheet(self._spin_style())
        field_layout.addLayout(_make_field("最大时长(分钟)", self._duration_spin))

        self._threshold_spin = SpinBox()
        self._threshold_spin.setRange(1, 10)
        self._threshold_spin.setValue(_default_config.completion_threshold)
        self._threshold_spin.setFixedHeight(28)
        self._threshold_spin.setStyleSheet(self._spin_style())
        field_layout.addLayout(_make_field("完成确认次数", self._threshold_spin))

        # 项目路径（带浏览按钮）
        path_container = QWidget()
        path_row = QHBoxLayout(path_container)
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(4)
        self._path_edit = LineEdit()
        self._path_edit.setPlaceholderText("默认为当前工作目录")
        self._path_edit.setFixedHeight(28)
        self._path_edit.setStyleSheet(self._line_style())
        path_row.addWidget(self._path_edit, 1)
        self._path_browse_btn = ToolButton(FluentIcon.FOLDER)
        self._path_browse_btn.setFixedSize(28, 28)
        self._path_browse_btn.clicked.connect(self._browse_folder)
        path_row.addWidget(self._path_browse_btn)
        field_layout.addLayout(_make_field("项目路径", path_container))

        layout.addLayout(field_layout)

        layout.addWidget(CardSeparator())

        # ---- 任务描述 ----
        self._prompt_edit = QTextEdit()
        self._prompt_edit.setPlaceholderText("📝 描述 AutoLoop 要完成的任务...")
        self._prompt_edit.setMinimumHeight(40)
        self._prompt_edit.setStyleSheet(f"""
            QTextEdit {{
                background: rgba(255, 255, 255, 0.05);
                color: #EAF2FF;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 8px;
                padding: 6px 10px;
                {FONT_CSS} {font_size_css(13)}
            }}
            QTextEdit:focus {{
                border: 1px solid #C9A85C;
            }}
        """)
        layout.addWidget(self._prompt_edit)

    def _on_start(self):
        config = AutoLoopConfig(
            max_iterations=self._iteration_spin.value(),
            max_tokens=self._token_spin.value(),
            max_duration_minutes=self._duration_spin.value(),
            completion_threshold=self._threshold_spin.value(),
            project_path=self._path_edit.text().strip(),
            task_prompt=self._prompt_edit.toPlainText().strip(),
        )
        self.startRequested.emit(config)

    def _browse_folder(self):
        """打开文件夹选择对话框"""
        from PyQt5.QtWidgets import QFileDialog
        folder = QFileDialog.getExistingDirectory(
            self, "选择项目文件夹",
            self._path_edit.text().strip() or "",
        )
        if folder:
            self._path_edit.setText(folder)

    def _spin_style(self) -> str:
        return f"""
            SpinBox {{
                background: rgba(255, 255, 255, 0.05);
                color: #EAF2FF;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 6px;
                padding: 4px 8px;
                {FONT_CSS} {font_size_css(13)}
            }}
            SpinBox:focus {{
                border-color: #C9A85C;
            }}
        """

    def _line_style(self) -> str:
        return f"""
            LineEdit {{
                background: rgba(255, 255, 255, 0.05);
                color: #EAF2FF;
                border: 1px solid rgba(255, 255, 255, 0.08);
                border-radius: 6px;
                padding: 6px 8px;
                {FONT_CSS} {font_size_css(13)}
            }}
            LineEdit:focus {{
                border-color: #C9A85C;
            }}
        """

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()

    def refresh_font_size(self):
        """刷新字体大小配置"""
        self._refresh_theme_style()
        self._refresh_component_styles()

    def _refresh_component_styles(self):
        """刷新内部组件样式"""
        from qfluentwidgets import StrongBodyLabel
        # 刷新 StrongBodyLabel
        for label in self.findChildren(StrongBodyLabel):
            label.setStyleSheet(f"color: #EAF2FF; font-size: {scale_font_size(14)}px; {FONT_CSS}")
        # 刷新停止按钮
        self._stop_btn.setStyleSheet(f"""
            PushButton {{
                background: rgba(255, 80, 80, 0.8);
                color: white;
                border: none;
                border-radius: 6px;
                {FONT_CSS} font-size: {scale_font_size(12)}px;
                font-weight: bold;
            }}
            PushButton:hover {{
                background: rgba(255, 60, 60, 1.0);
            }}
        """)
        # 刷新任务标签
        if hasattr(self, '_task_label'):
            self._task_label.setStyleSheet(f"""
                color: #9BB0D3;
                font-size: {scale_font_size(12)}px;
                {FONT_CSS}
                padding: 4px 8px;
                background: rgba(0,0,0,0.15);
                border-radius: 6px;
            """)
        # 刷新信息标签
        for label in [self._iter_label, self._time_label, self._token_label, 
                      self._status_label, self._phase_label]:
            if hasattr(self, label.property('objectName')) or hasattr(label, 'setStyleSheet'):
                label.setStyleSheet(f"font-size: {scale_font_size(13)}px; {FONT_CSS}")
        # 刷新 Token 百分比标签
        if self._token_percent_label:
            self._token_percent_label.setStyleSheet(f"color: #7FDBFF; font-size: {scale_font_size(12)}px; {FONT_CSS}")
        # 刷新日志标签
        if hasattr(self, '_log_label'):
            self._log_label.setStyleSheet(f"""
                color: #7A9BBF;
                font-size: {scale_font_size(11)}px;
                {FONT_CSS}
                padding: 3px 6px;
                background: rgba(0,0,0,0.1);
                border-radius: 4px;
            """)


# ============================================================
#  AutoLoop 运行卡（彩虹边框动画）
# ============================================================

class AutoLoopRunningCard(QFrame):
    """AutoLoop 运行状态卡 — 彩虹渐变边框 + 进度 + 停止按钮"""

    stopRequested = pyqtSignal()
    archiveRequested = pyqtSignal()  # 归档按钮点击

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("autoLoopRunningCard")
        self._refresh_theme_style()

        # 彩虹边框动画
        self._hue_offset = 0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(3000)
        self._anim.setStartValue(0)
        self._anim.setEndValue(360)
        self._anim.setLoopCount(-1)
        self._anim.valueChanged.connect(self._on_hue_changed)

        # 每秒更新时间
        self._start_timestamp = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh_elapsed)

        # Token 实时累加
        self._current_tokens = 0
        self._max_tokens = 0
        self._token_percent_label = None  # 在 _build_ui 中初始化

        # 当前阶段：planning / executing / completed
        self._current_phase = "preparing"

        self._build_ui()

    def _refresh_theme_style(self):
        """刷新主题色，响应全局主题切换"""
        from app.utils.design_tokens import Colors
        Colors.refresh()
        self.setStyleSheet(f"""
            #autoLoopRunningCard {{
                background: {Colors.CARD_BG_SOLID};
                border-radius: 12px;
                {FONT_CSS}
            }}
        """)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(8)

        # ---- 标题行 ----
        title_bar = QHBoxLayout()
        title_bar.setSpacing(8)
        icon_label = QLabel("🤖")
        icon_label.setStyleSheet(font_size_css(18))
        title_bar.addWidget(icon_label)
        title = QLabel("AutoLoop 运行中")
        title.setStyleSheet(f"color: #EAF2FF; {font_size_css(14)} font-weight: bold; {FONT_CSS}")
        title_bar.addWidget(title)
        title_bar.addStretch()
        self._archive_btn = PushButton("📦 归档")
        self._archive_btn.setFixedSize(70, 26)
        self._archive_btn.setStyleSheet(f"""
            PushButton {{
                background: rgba(139, 92, 246, 0.8);
                color: white;
                border: none;
                border-radius: 6px;
                {FONT_CSS} {font_size_css(12)}
                font-weight: bold;
            }}
            PushButton:hover {{
                background: rgba(139, 92, 246, 1.0);
            }}
        """)
        self._archive_btn.clicked.connect(self.archiveRequested.emit)
        title_bar.addWidget(self._archive_btn)
        self._stop_btn = PushButton("⏹ 停止")
        self._stop_btn.setFixedSize(70, 26)
        self._stop_btn.setStyleSheet(f"""
            PushButton {{
                background: rgba(255, 80, 80, 0.8);
                color: white;
                border: none;
                border-radius: 6px;
                {FONT_CSS} {font_size_css(12)}
                font-weight: bold;
            }}
            PushButton:hover {{
                background: rgba(255, 60, 60, 1.0);
            }}
        """)
        self._stop_btn.clicked.connect(self.stopRequested.emit)
        title_bar.addWidget(self._stop_btn)
        layout.addLayout(title_bar)

        # ---- 任务目标（显示任务描述前60字）----
        self._task_label = QLabel("")
        self._task_label.setStyleSheet(f"""
            color: #9BB0D3;
            {font_size_css(12)}
            {FONT_CSS}
            padding: 4px 8px;
            background: rgba(0,0,0,0.15);
            border-radius: 6px;
        """)
        self._task_label.setWordWrap(False)
        layout.addWidget(self._task_label)

        # ---- 信息区（两行布局）----
        self._status_widget = QWidget()
        self._status_widget.setStyleSheet("background: rgba(0,0,0,0.1); border-radius: 6px;")
        status_layout = QVBoxLayout(self._status_widget)
        status_layout.setContentsMargins(12, 10, 12, 10)
        status_layout.setSpacing(8)

        # 第一行：迭代 | 耗时 | Token + 进度条 + 百分比
        row1 = QHBoxLayout()
        row1.setSpacing(20)

        # 迭代
        iter_w = QWidget()
        iter_layout = QHBoxLayout(iter_w)
        iter_layout.setContentsMargins(0, 0, 0, 0)
        iter_layout.setSpacing(6)
        iter_layout.addWidget(QLabel("📚"))
        self._iter_label = QLabel("0 / 0")
        self._iter_label.setStyleSheet(f"color: #C9A85C; font-weight: bold; {font_size_css(13)} {FONT_CSS}")
        iter_layout.addWidget(self._iter_label)
        row1.addWidget(iter_w)

        # 耗时
        time_w = QWidget()
        time_layout = QHBoxLayout(time_w)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(6)
        time_layout.addWidget(QLabel("⏱"))
        self._time_label = QLabel("0秒")
        self._time_label.setStyleSheet(f"color: #7FDBFF; font-weight: bold; {font_size_css(13)} {FONT_CSS}")
        time_layout.addWidget(self._time_label)
        row1.addWidget(time_w)

        # Token 使用 + 进度条 + 百分比
        token_w = QWidget()
        token_layout = QHBoxLayout(token_w)
        token_layout.setContentsMargins(0, 0, 0, 0)
        token_layout.setSpacing(8)
        token_layout.addWidget(QLabel("🔢"))
        self._token_label = QLabel("0 / 500K")
        self._token_label.setStyleSheet(f"color: #A7F3D0; font-weight: bold; {font_size_css(13)} {FONT_CSS}")
        token_layout.addWidget(self._token_label)
        self._token_progress = QProgressBar()
        self._token_progress.setRange(0, 100)
        self._token_progress.setValue(0)
        self._token_progress.setTextVisible(False)
        self._token_progress.setFixedHeight(8)
        self._token_progress.setMinimumWidth(100)
        self._token_progress.setStyleSheet("""
            QProgressBar {
                background: rgba(255, 255, 255, 0.1);
                border-radius: 4px;
                border: none;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #10B981, stop:1 #34D399);
                border-radius: 4px;
            }
        """)
        token_layout.addWidget(self._token_progress)
        self._token_percent_label = QLabel("0%")
        self._token_percent_label.setStyleSheet(f"color: #7FDBFF; {font_size_css(12)} {FONT_CSS}")
        self._token_percent_label.setFixedWidth(32)
        token_layout.addWidget(self._token_percent_label)
        row1.addWidget(token_w, 1)

        status_layout.addLayout(row1)

        # 第二行：状态 | 阶段
        row2 = QHBoxLayout()
        row2.setSpacing(20)

        status_w = QWidget()
        status_layout2 = QHBoxLayout(status_w)
        status_layout2.setContentsMargins(0, 0, 0, 0)
        status_layout2.setSpacing(6)
        status_layout2.addWidget(QLabel("📊"))
        self._status_label = QLabel("▶ 准备中...")
        self._status_label.setStyleSheet(f"color: #E5E7EB; {font_size_css(13)} {FONT_CSS}")
        status_layout2.addWidget(self._status_label)
        row2.addWidget(status_w)

        phase_w = QWidget()
        phase_layout = QHBoxLayout(phase_w)
        phase_layout.setContentsMargins(0, 0, 0, 0)
        phase_layout.setSpacing(6)
        phase_layout.addWidget(QLabel("🎯"))
        self._phase_label = QLabel("待开始")
        self._phase_label.setStyleSheet(f"color: #C9A85C; font-weight: bold; {font_size_css(13)} {FONT_CSS}")
        phase_layout.addWidget(self._phase_label)
        row2.addWidget(phase_w)

        # 当前步骤
        step_w = QWidget()
        step_layout = QHBoxLayout(step_w)
        step_layout.setContentsMargins(0, 0, 0, 0)
        step_layout.setSpacing(6)
        step_layout.addWidget(QLabel("🔹"))
        self._step_label = QLabel("步骤 - / -")
        self._step_label.setStyleSheet(f"color: #A7F3D0; {font_size_css(13)} {FONT_CSS}")
        step_layout.addWidget(self._step_label)
        row2.addWidget(step_w)

        row2.addStretch()
        status_layout.addLayout(row2)

        layout.addWidget(self._status_widget)

        # ---- 日志行 ----
        self._log_label = QLabel("")
        self._log_label.setFixedHeight(20)
        self._log_label.setStyleSheet(f"""
            color: #7A9BBF;
            {font_size_css(11)}
            {FONT_CSS}
            padding: 3px 6px;
            background: rgba(0,0,0,0.1);
            border-radius: 4px;
        """)
        self._log_label.setWordWrap(False)
        self._log_label.setTextFormat(Qt.PlainText)
        layout.addWidget(self._log_label)

    def paintEvent(self, event):
        """绘制彩虹边框"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 彩虹渐变边框
        rect = self.rect()
        gradient = QLinearGradient(0, 0, rect.width(), rect.height())
        hue = self._hue_offset
        colors = [
            (0.0, QColor.fromHsv(hue % 360, 255, 200, 160)),
            (0.25, QColor.fromHsv((hue + 72) % 360, 255, 200, 160)),
            (0.5, QColor.fromHsv((hue + 144) % 360, 255, 200, 160)),
            (0.75, QColor.fromHsv((hue + 216) % 360, 255, 200, 160)),
            (1.0, QColor.fromHsv((hue + 288) % 360, 255, 200, 160)),
        ]
        for pos, color in colors:
            gradient.setColorAt(pos, color)

        painter.setPen(QPen(QBrush(gradient), 3))
        painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 10, 10)

        painter.end()

    def _on_hue_changed(self, value: int):
        self._hue_offset = value
        self.update()  # 触发重绘

    def start_animation(self):
        """开始彩虹动画和计时器"""
        self._start_timestamp = time.time()
        self._anim.start()
        self._timer.start()
        # 重置 token 累加（每轮新的循环从零开始）
        self._current_tokens = 0
        # 确保所有按钮可见
        self._archive_btn.show()
        self._stop_btn.show()
        self._archive_btn.update()
        self._stop_btn.update()
        self.update()

    def stop_animation(self):
        """停止彩虹动画和计时器"""
        self._anim.stop()
        self._timer.stop()

    def _refresh_elapsed(self):
        """每秒刷新已用时间"""
        if self._start_timestamp > 0:
            elapsed = time.time() - self._start_timestamp
            m, s = divmod(int(elapsed), 60)
            h, m = divmod(m, 60)
            if h > 0:
                self._time_label.setText(f"{h}时{m}分{s}秒")
            else:
                self._time_label.setText(f"{m}分{s}秒")

    def append_log(self, text: str):
        """追加一行日志到可视化区域（单行滚动）"""
        timestamp = time.strftime("%H:%M:%S")
        self._log_label.setText(f"[{timestamp}] {text}")
        self._log_label.repaint()

    def set_task(self, task: str):
        """设置任务目标显示（显示前60字）"""
        if task:
            preview = task[:60]
            if len(task) > 60:
                preview += "..."
            self._task_label.setText(f"🎯 {preview}")
        else:
            self._task_label.setText("🎯 <未设置>")

    def set_phase(self, phase: str):
        """设置当前阶段（planning / executing / completed）"""
        self._current_phase = phase
        phase_text = {
            "planning": "📋 规划中",
            "executing": "🔨 执行中",
            "archiving": "📦 归档中",
            "completed": "✅ 已完成",
        }.get(phase, "未知")
        self._phase_label.setText(phase_text)
        
        # 根据阶段调整颜色
        color_map = {
            "planning": "#7FDBFF",  # 蓝色
            "executing": "#C9A85C",  # 金色
            "archiving": "#A78BFA",  # 紫色
            "completed": "#10B981",  # 绿色
        }
        color = color_map.get(phase, "#C9A85C")
        self._phase_label.setStyleSheet(f"color: {color}; font-weight: bold; {font_size_css(13)} {FONT_CSS}")
        
        # 阶段变更时更新状态文本
        if phase == "planning":
            self._status_label.setText("▶ 拆解任务中...")
        elif phase == "executing":
            self._status_label.setText("▶ 执行中...")
        elif phase == "archiving":
            self._status_label.setText("📦 归档清理中...")
        elif phase == "completed":
            self._status_label.setText("✅ 全部完成")
        
        self.update()

    # ========== 更新方法 ==========

    def update_progress_no_token(self, progress: dict):
        """更新进度显示（不更新 token，避免与 update_tokens() 竞争）
        
        注意：token 更新由 update_tokens() 专门处理，避免竞争条件导致显示被覆盖。
        这个方法只更新迭代、时间、状态。
        """
        iteration = progress.get("iteration", 0)
        max_iter = progress.get("max_iterations", 0)
        elapsed = progress.get("elapsed_str", "0秒")
        state = progress.get("state", "")
        current_step = progress.get("current_step", 0)
        total_steps = progress.get("total_steps", 0)
        phase = progress.get("phase", "")

        self._iter_label.setText(f"{iteration} / {max_iter}")
        self._time_label.setText(elapsed)
        if total_steps > 0:
            self._step_label.setText(f"步骤 {current_step}/{total_steps}")
        phase_text = {
            "planning": "📋 规划中",
            "executing": "⚡ 执行中",
            "archiving": "📦 归档中",
            "completed": "✅ 已完成",
        }
        if phase in phase_text:
            self._phase_label.setText(phase_text[phase])

        if state == "running":
            self._status_label.setText(f"▶ 第 {iteration} 轮进行中...")
        elif state == "completed":
            self._status_label.setText("✅ 已完成")
        elif state == "stopped":
            self._status_label.setText("⏹ 已停止")
        elif state == "error":
            self._status_label.setText("❌ 出错")
        # 移除 self.update() 避免与 update_tokens() 竞争导致 token 显示被覆盖

    def update_tokens(self, total_tokens: int):
        """更新 token 显示（使用紧凑的数字格式 K/M）"""
        self._current_tokens = total_tokens
        
        # 数字格式化：使用 K/M 缩写
        def format_token(n: int) -> str:
            if n >= 1000000:
                return f"{n / 1000000:.1f}M"
            elif n >= 1000:
                return f"{n / 1000:.1f}K"
            return str(n)
        
        # Token 显示：当前使用 / 设定总数 + 百分比
        if self._max_tokens > 0:
            self._token_label.setText(f"{format_token(total_tokens)} / {format_token(self._max_tokens)}")
            percentage = min(100, int(total_tokens * 100 / self._max_tokens))
            self._token_progress.setValue(percentage)
            self._token_percent_label.setText(f"{percentage}%")
        else:
            self._token_label.setText(format_token(total_tokens))
            self._token_percent_label.setText("")
        
        self._token_label.repaint()

    def set_max_tokens(self, max_tokens: int):
        """设置最大 token 上限（启动时从 config 传入）"""
        self._max_tokens = max_tokens

    # ========== 公共接口（供 main_widget 等外部调用）==========

    def set_status(self, text: str):
        """设置状态文本（替代外部直接访问 _status_label）"""
        self._status_label.setText(text)

    def show_stop_button(self):
        """显示停止按钮（替代外部直接访问 _stop_btn）"""
        self._stop_btn.show()
        self._stop_btn.update()

    def hide_stop_button(self):
        """隐藏停止按钮"""
        self._stop_btn.hide()

    def show_archive_button(self):
        """显示归档按钮"""
        self._archive_btn.show()
        self._archive_btn.update()

    def hide_archive_button(self):
        """隐藏归档按钮"""
        self._archive_btn.hide()

    def show_completed(self, message: str):
        """显示完成状态"""
        self._status_label.setText(f"✅ {message}")
        self.stop_animation()
        self._archive_btn.hide()
        self._stop_btn.hide()
        self.update()

    def show_error(self, message: str):
        """显示错误状态"""
        self._status_label.setText(f"❌ {message}")
        self.stop_animation()
        self.update()