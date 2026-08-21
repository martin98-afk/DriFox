# -*- coding: utf-8 -*-
"""
AutoLoop 卡片组件 — 配置卡 + 运行卡

- AutoLoopConfigCard: 配置参数 + 任务输入 + 开始按钮（竖排布局，插入到聊天区）
- AutoLoopRunningCard: 运行状态显示 + 停止按钮（彩虹渐变边框动画）
"""

import time

from PyQt5.QtCore import (
    Qt,
    QTimer,
    QVariantAnimation,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QPainter,
    QPen,
)
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    LineEdit,
    PrimaryPushButton,
    PushButton,
    SpinBox,
    StrongBodyLabel,
    ToolButton,
    TransparentToolButton,
)
from qfluentwidgets.components.widgets.card_widget import CardSeparator
from qfluentwidgets.components.widgets.flyout import IconWidget

from autoloop_core.config import AutoLoopConfig
from app.utils.design_tokens import Colors, font_size_css, scale_font_size
from app.utils.utils import get_font_family_css, get_icon

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
        self._ctx_provider = None
        self._last_ctx = {}
        # 注意：先 _build_ui 创建控件，再 _refresh_theme_style 刷新样式
        # 确保 _refresh_component_styles 访问控件时它们已存在
        self._build_ui()
        self._refresh_theme_style()

    # ── 插件上下文（拉模型，showEvent 时取最新）──

    def set_context_provider(self, provider):
        self._ctx_provider = provider

    def showEvent(self, event):
        super().showEvent(event)
        if self._ctx_provider is not None:
            try:
                self._last_ctx = self._ctx_provider() or {}
                # 项目路径预填（未手动填过时）
                if not self._path_edit.text().strip():
                    root = self._last_ctx.get("project_root") or ""
                    if root:
                        self._path_edit.setText(root)
            except Exception:
                pass

    def refresh_font_size(self):
        """刷新字体大小配置"""
        self._refresh_theme_style()
        self._refresh_component_styles()

    def _refresh_component_styles(self):
        """刷新内部组件样式"""
        Colors.refresh()
        # 刷新 BodyLabel 字体大小
        for label in self.findChildren(BodyLabel):
            label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; {FONT_CSS} font-size: {scale_font_size(14)}px;")
        # 刷新按钮
        radius = Colors.SEND_BTN_RADIUS
        self._start_btn.setStyleSheet(f"""
            PrimaryPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.SEND_BTN_START}, stop:1 {Colors.SEND_BTN_END});
                color: {Colors.BUTTON_TEXT_ON_ACCENT};
                border: none;
                border-radius: {radius}px;
                padding: 4px 14px;
                {FONT_CSS} font-size: {scale_font_size(12)}px;
                font-weight: bold;
            }}
            PrimaryPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.SEND_BTN_HOVER_START}, stop:1 {Colors.SEND_BTN_HOVER_END});
            }}
        """)
        self._iteration_spin.setStyleSheet(self._spin_style())
        self._token_spin.setStyleSheet(self._spin_style())
        self._duration_spin.setStyleSheet(self._spin_style())
        self._threshold_spin.setStyleSheet(self._spin_style())
        self._path_edit.setStyleSheet(self._line_style())
        self._prompt_edit.setStyleSheet(f"""
            QTextEdit {{
                background: {Colors.TOOLBAR_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 6px 10px;
                {FONT_CSS} font-size: {scale_font_size(13)}px;
            }}
            QTextEdit:focus {{
                border: 1px solid {Colors.INPUT_FOCUS_BORDER};
            }}
        """)
        # 刷新标题
        Colors.refresh()
        for label in self.findChildren(StrongBodyLabel):
            label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; font-size: {scale_font_size(14)}px; {FONT_CSS}")

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
        self._refresh_component_styles()

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
        Colors.refresh()
        title = StrongBodyLabel("AutoLoop 自动循环")
        title.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {font_size_css(14)} {FONT_CSS}")
        title_layout.addWidget(title)
        title_layout.addStretch()

        self._start_btn = PrimaryPushButton("▶ 开始")
        self._start_btn.setStyleSheet(f"""
            PrimaryPushButton {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.SEND_BTN_START}, stop:1 {Colors.SEND_BTN_END});
                color: {Colors.BUTTON_TEXT_ON_ACCENT};
                border: none;
                border-radius: 8px;
                padding: 4px 14px;
                {FONT_CSS} {font_size_css(12)}
                font-weight: bold;
            }}
            PrimaryPushButton:hover {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 {Colors.SEND_BTN_HOVER_START}, stop:1 {Colors.SEND_BTN_HOVER_END});
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
            Colors.refresh()
            lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; {FONT_CSS} font-size: {scale_font_size(14)}px;")
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
        Colors.refresh()
        self._prompt_edit.setStyleSheet(f"""
            QTextEdit {{
                background: {Colors.TOOLBAR_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
                padding: 6px 10px;
                {FONT_CSS} {font_size_css(13)}
            }}
            QTextEdit:focus {{
                border: 1px solid {Colors.INPUT_FOCUS_BORDER};
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
        # 刷新最新上下文后交给控制器启动
        if self._ctx_provider is not None:
            try:
                self._last_ctx = self._ctx_provider() or {}
            except Exception:
                pass
        from .controller import AutoLoopController

        AutoLoopController.get_instance().request_start(config, self._last_ctx)

    def _browse_folder(self):
        """打开文件夹选择对话框"""
        from PyQt5.QtWidgets import QFileDialog

        folder = QFileDialog.getExistingDirectory(
            self,
            "选择项目文件夹",
            self._path_edit.text().strip() or "",
        )
        if folder:
            self._path_edit.setText(folder)

    def _spin_style(self) -> str:
        Colors.refresh()
        return f"""
            SpinBox {{
                background: {Colors.TOOLBAR_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 4px 8px;
                {FONT_CSS} {font_size_css(13)}
            }}
            SpinBox:focus {{
                border-color: {Colors.INPUT_FOCUS_BORDER};
            }}
        """

    def _line_style(self) -> str:
        Colors.refresh()
        return f"""
            LineEdit {{
                background: {Colors.TOOLBAR_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 6px 8px;
                {FONT_CSS} {font_size_css(13)}
            }}
            LineEdit:focus {{
                border-color: {Colors.INPUT_FOCUS_BORDER};
            }}
        """

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()


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
        self._ctx_provider = None

        # 彩虹边框动画 — 60 帧/3秒 (≈20fps)，平衡流畅度与避免无谓重绘
        self._hue_offset = 0
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(3000)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(360.0)
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

        # 声明跳过容器展开/折叠动画（自带彩虹动画，避免容器动画抖动）
        self.setProperty("noContainerAnimation", True)

        # 水平填充容器宽度，垂直方向由内容决定
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

        # 注意：先 _build_ui 创建控件，再 _refresh_theme_style 刷新样式
        # 确保 _refresh_component_styles 访问控件时它们已存在
        self._build_ui()
        self._refresh_theme_style()

    # ── 插件上下文（拉模型，showEvent 时绑定控制器）──

    def set_context_provider(self, provider):
        self._ctx_provider = provider

    def showEvent(self, event):
        super().showEvent(event)
        if self._ctx_provider is None:
            return
        try:
            ctx = self._ctx_provider() or {}
        except Exception:
            ctx = {}
        window_id = str(ctx.get("window_id") or "")
        if not window_id:
            return
        from .controller import AutoLoopController

        controller = AutoLoopController.get_instance()
        controller.bind_running_card(self, ctx)
        # 停止/归档按钮 → 控制器（幂等：断开旧连接再连）
        for sig, slot in (
            (self.stopRequested, lambda wid=window_id: controller.request_stop(wid)),
            (self.archiveRequested, lambda wid=window_id: controller.request_archive(wid)),
        ):
            try:
                sig.disconnect()
            except TypeError, RuntimeError:
                pass
            sig.connect(slot)

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
        self._refresh_component_styles()

    def _refresh_component_styles(self):
        """刷新内部组件样式 — 主题切换时刷新所有内嵌控件的颜色/字体"""
        Colors.refresh()
        # 标题
        self._task_label.setStyleSheet(f"""
            color: {Colors.TEXT_MUTED};
            {font_size_css(12)}
            {FONT_CSS}
            padding: 4px 8px;
            background: {Colors.TOOLBAR_BG};
            border-radius: 6px;
        """)
        # 状态卡片背景
        self._status_widget.setStyleSheet(f"background: {Colors.TOOLBAR_BG}; border-radius: 6px;")
        # 信息标签
        self._time_label.setStyleSheet(
            f"color: {Colors.STATUS_INFO}; font-weight: bold; {font_size_css(13)} {FONT_CSS}"
        )
        self._token_label.setStyleSheet(
            f"color: {Colors.REALTIME_SUCCESS}; font-weight: bold; {font_size_css(13)} {FONT_CSS}"
        )
        self._status_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {font_size_css(13)} {FONT_CSS}")
        self._step_label.setStyleSheet(f"color: {Colors.REALTIME_SUCCESS}; {font_size_css(13)} {FONT_CSS}")
        self._phase_label.setStyleSheet(
            f"color: {Colors.SEND_BTN_START}; font-weight: bold; {font_size_css(13)} {FONT_CSS}"
        )
        self._token_percent_label.setStyleSheet(f"color: {Colors.STATUS_INFO}; {font_size_css(12)} {FONT_CSS}")
        # 日志标签
        self._log_label.setStyleSheet(f"""
            color: {Colors.TEXT_MUTED};
            {font_size_css(11)}
            {FONT_CSS}
            padding: 3px 6px;
            background: {Colors.TOOLBAR_BG};
            border-radius: 4px;
        """)
        # 进度条
        self._token_progress.setStyleSheet(f"""
            QProgressBar {{
                background: {Colors.TOOLBAR_BG};
                border-radius: 4px;
                border: none;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {Colors.REALTIME_SUCCESS}, stop:1 {Colors.REALTIME_SUCCESS});
                border-radius: 4px;
            }}
        """)

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(8)

        # ---- 标题行 ----
        title_bar = QHBoxLayout()
        title_bar.setSpacing(8)
        icon_label = QLabel()
        icon_label.setPixmap(get_icon("设置-subagent").pixmap(20, 20))
        icon_label.setFixedSize(20, 20)
        title_bar.addWidget(icon_label)
        Colors.refresh()
        title = QLabel("AutoLoop 运行中")
        title.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {font_size_css(14)} font-weight: bold; {FONT_CSS}")
        title_bar.addWidget(title)
        title_bar.addStretch()
        self._archive_btn = PushButton("📦 归档")
        self._archive_btn.setFixedSize(70, 26)
        Colors.refresh()
        self._archive_btn.setStyleSheet(f"""
            PushButton {{
                background: {Colors.STATUS_ARCHIVE_BG};
                color: white;
                border: none;
                border-radius: 6px;
                {FONT_CSS} {font_size_css(12)}
                font-weight: bold;
            }}
            PushButton:hover {{
                background: {Colors.STATUS_ARCHIVE_BG};
            }}
        """)
        self._archive_btn.clicked.connect(self.archiveRequested.emit)
        title_bar.addWidget(self._archive_btn)
        self._stop_btn = PushButton("⏹ 停止")
        self._stop_btn.setFixedSize(70, 26)
        Colors.refresh()
        self._stop_btn.setStyleSheet(f"""
            PushButton {{
                background: {Colors.STATUS_DANGER_BG};
                color: white;
                border: none;
                border-radius: 6px;
                {FONT_CSS} {font_size_css(12)}
                font-weight: bold;
            }}
            PushButton:hover {{
                background: {Colors.ERROR};
            }}
        """)
        self._stop_btn.clicked.connect(self.stopRequested.emit)
        title_bar.addWidget(self._stop_btn)
        layout.addLayout(title_bar)

        # ---- 任务目标（显示任务描述前60字）----
        self._task_label = QLabel("")
        Colors.refresh()
        self._task_label.setStyleSheet(f"""
            color: {Colors.TEXT_MUTED};
            {font_size_css(12)}
            {FONT_CSS}
            padding: 4px 8px;
            background: {Colors.TOOLBAR_BG};
            border-radius: 6px;
        """)
        self._task_label.setWordWrap(True)
        layout.addWidget(self._task_label)

        # ---- 信息区（两行布局）----
        self._status_widget = QWidget()
        self._status_widget.setStyleSheet(f"background: {Colors.TOOLBAR_BG}; border-radius: 6px;")
        status_layout = QVBoxLayout(self._status_widget)
        status_layout.setContentsMargins(12, 10, 12, 10)
        status_layout.setSpacing(8)

        # 第一行：耗时 | Token + 进度条 + 百分比
        row1 = QHBoxLayout()
        row1.setSpacing(20)

        # 耗时
        time_w = QWidget()
        time_layout = QHBoxLayout(time_w)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(6)
        time_layout.addWidget(QLabel("⏱"))
        self._time_label = QLabel("0秒")
        Colors.refresh()
        self._time_label.setStyleSheet(
            f"color: {Colors.STATUS_INFO}; font-weight: bold; {font_size_css(13)} {FONT_CSS}"
        )
        time_layout.addWidget(self._time_label)
        row1.addWidget(time_w)

        # Token 使用 + 进度条 + 百分比
        token_w = QWidget()
        token_layout = QHBoxLayout(token_w)
        token_layout.setContentsMargins(0, 0, 0, 0)
        token_layout.setSpacing(8)
        token_layout.addWidget(QLabel("🔢"))
        self._token_label = QLabel("0 / 500K")
        Colors.refresh()
        self._token_label.setStyleSheet(
            f"color: {Colors.REALTIME_SUCCESS}; font-weight: bold; {font_size_css(13)} {FONT_CSS}"
        )
        token_layout.addWidget(self._token_label)
        self._token_progress = QProgressBar()
        self._token_progress.setRange(0, 100)
        self._token_progress.setValue(0)
        self._token_progress.setTextVisible(False)
        self._token_progress.setFixedHeight(8)
        self._token_progress.setMinimumWidth(100)
        Colors.refresh()
        self._token_progress.setStyleSheet(f"""
            QProgressBar {{
                background: {Colors.TOOLBAR_BG};
                border-radius: 4px;
                border: none;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {Colors.REALTIME_SUCCESS}, stop:1 {Colors.REALTIME_SUCCESS});
                border-radius: 4px;
            }}
        """)
        token_layout.addWidget(self._token_progress)
        self._token_percent_label = QLabel("0%")
        self._token_percent_label.setStyleSheet(f"color: {Colors.STATUS_INFO}; {font_size_css(12)} {FONT_CSS}")
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
        Colors.refresh()
        self._status_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; {font_size_css(13)} {FONT_CSS}")
        status_layout2.addWidget(self._status_label)
        row2.addWidget(status_w)

        phase_w = QWidget()
        phase_layout = QHBoxLayout(phase_w)
        phase_layout.setContentsMargins(0, 0, 0, 0)
        phase_layout.setSpacing(6)
        phase_layout.addWidget(QLabel("🎯"))
        self._phase_label = QLabel("待开始")
        Colors.refresh()
        self._phase_label.setStyleSheet(
            f"color: {Colors.SEND_BTN_START}; font-weight: bold; {font_size_css(13)} {FONT_CSS}"
        )
        phase_layout.addWidget(self._phase_label)
        row2.addWidget(phase_w)

        # 当前步骤
        step_w = QWidget()
        step_layout = QHBoxLayout(step_w)
        step_layout.setContentsMargins(0, 0, 0, 0)
        step_layout.setSpacing(6)
        step_layout.addWidget(QLabel("🔹"))
        self._step_label = QLabel("步骤 - / -")
        Colors.refresh()
        self._step_label.setStyleSheet(f"color: {Colors.REALTIME_SUCCESS}; {font_size_css(13)} {FONT_CSS}")
        step_layout.addWidget(self._step_label)
        row2.addWidget(step_w)

        row2.addStretch()
        status_layout.addLayout(row2)

        layout.addWidget(self._status_widget)

        # ---- 日志行 ----
        # 参考设置卡片（_task_label 等）：宽度由父布局约束，按需自动换行，
        # 避免长日志撑开卡片宽度。水平 Expanding 确保占满可用宽度，
        # 垂直 Preferred 让高度根据行数自适应。
        self._log_label = QLabel("")
        self._log_label.setStyleSheet(f"""
            color: {Colors.TEXT_MUTED};
            {font_size_css(11)}
            {FONT_CSS}
            padding: 3px 6px;
            background: {Colors.TOOLBAR_BG};
            border-radius: 4px;
        """)
        self._log_label.setWordWrap(True)
        self._log_label.setTextFormat(Qt.PlainText)
        self._log_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout.addWidget(self._log_label)

        # 底部弹性空间：当卡片在 BottomCardContainer 中展开时，
        # stretch 确保卡片内容贴顶、底部留出弹性空隙，
        # 避免容器高度计算偏差导致上层 session_bar 布局偏移。
        layout.addStretch()

    def paintEvent(self, event):
        """绘制彩虹边框 — 使用主题感知的透明度，浅色模式降低饱和度"""
        if not self.isVisible():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # 浅色模式降饱和、降透明度，避免彩虹边框过于突兀
        is_light = False
        try:
            from app.utils.theme_manager import theme_manager

            is_light = theme_manager.is_light_theme()
        except Exception:
            pass

        alpha = 100 if is_light else 160  # 浅色模式用更透的边框
        saturation = 180 if is_light else 255
        value = 180 if is_light else 200

        rect = self.rect()
        gradient = QLinearGradient(0, 0, rect.width(), rect.height())
        hue = self._hue_offset
        colors = [
            (0.0, QColor.fromHsv(int(hue % 360), saturation, value, alpha)),
            (0.25, QColor.fromHsv(int((hue + 72) % 360), saturation, value, alpha)),
            (0.5, QColor.fromHsv(int((hue + 144) % 360), saturation, value, alpha)),
            (0.75, QColor.fromHsv(int((hue + 216) % 360), saturation, value, alpha)),
            (1.0, QColor.fromHsv(int((hue + 288) % 360), saturation, value, alpha)),
        ]
        for pos, color in colors:
            gradient.setColorAt(pos, color)

        painter.setPen(QPen(QBrush(gradient), 3))
        painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 10, 10)

        painter.end()

    def _on_hue_changed(self, value: float):
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
        """追加一行日志到可视化区域（单行滚动，带时间戳）"""
        timestamp = time.strftime("%H:%M:%S")
        self._log_label.setText(f"[{timestamp}] {text}")
        # repaint 改为 update（延迟合并绘制，减少不必要的重绘竞争）
        self._log_label.update()

    def update_log(self, text: str):
        """流式更新日志内容（不添加时间戳，用于实时内容预览）"""
        self._log_label.setText(text)
        self._log_label.update()

    def set_task(self, task: str):
        """设置任务目标显示（完整显示，自动换行）"""
        if task:
            self._task_label.setText(f"🎯 {task}")
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
        Colors.refresh()
        color_map = {
            "planning": Colors.STATUS_INFO,
            "executing": Colors.SEND_BTN_START,
            "archiving": Colors.STATUS_ARCHIVE_BG,
            "completed": Colors.REALTIME_SUCCESS,
        }
        color = color_map.get(phase, Colors.SEND_BTN_START)
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

    # ========== 更新方法 ==========

    def update_progress_no_token(self, progress: dict):
        """更新进度显示（不更新 token，避免与 update_tokens() 竞争）

        注意：token 更新由 update_tokens() 专门处理，避免竞争条件导致显示被覆盖。
        这个方法只更新迭代、时间、状态。
        """
        elapsed = progress.get("elapsed_str", "0秒")
        state = progress.get("state", "")
        current_step = progress.get("current_step", 0)
        total_steps = progress.get("total_steps", 0)
        phase = progress.get("phase", "")

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
            self._status_label.setText("▶ 进行中...")
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

        self._token_label.update()

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


# ============================================================
#  AutoLoop 全屏运行页面（QStackedWidget 中的 Page 1）
# ============================================================


class AutoLoopFullPage(QFrame):
    """AutoLoop 全屏运行页面 — 撑满 chat 区域，替换消息列表+输入框

    布局结构：
    ┌─ TopBar (标题 + 耗时 + 停止按钮) ───────────────────┐
    ├─ TaskSummary ───────────────────────────────────────┤
    ├─ StatsBar (迭代 + Token 进度) ──────────────────────┤
    ├─ PhaseStepper (规划 → 执行 → 归档) ────────────────┤
    ├─ StepList (步骤清单) ───────────────────────────────┤
    ├─ SplitArea (LLM思考 | 工具调用)  (stretch=1) ──────┤
    └─ SystemLog ─────────────────────────────────────────┘
    """

    stopRequested = pyqtSignal()
    forceArchiveRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("autoLoopFullPage")
        self._refresh_theme_style()
        self._start_timestamp = 0.0
        self._current_tokens = 0
        self._max_tokens = 0
        self._build_ui()

    def _refresh_theme_style(self):
        """刷新主题色"""
        from app.utils.design_tokens import Colors

        Colors.refresh()
        self.setStyleSheet(f"""
            #autoLoopFullPage {{
                background: {Colors.CARD_BG_SOLID};
                border: 1px solid {Colors.BORDER};
                border-radius: 12px;
                {FONT_CSS}
            }}
        """)

    def _build_ui(self):
        """构建完整 UI 布局"""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(16, 12, 16, 12)
        main_layout.setSpacing(8)

        # ===== TopBar: 标题 + 耗时 + 停止按钮 =====
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        icon_label = QLabel()
        icon_label.setPixmap(get_icon("设置-subagent").pixmap(24, 24))
        icon_label.setFixedSize(24, 24)
        top_bar.addWidget(icon_label)

        title = StrongBodyLabel("AutoLoop 运行中")
        title.setStyleSheet(f"color: #EAF2FF; {font_size_css(14)} font-weight: bold; {FONT_CSS}")
        top_bar.addWidget(title)
        top_bar.addStretch()

        # 耗时
        self._elapsed_label = QLabel("00:00:00")
        self._elapsed_label.setStyleSheet(f"color: #7FDBFF; {font_size_css(13)} {FONT_CSS}")
        top_bar.addWidget(self._elapsed_label)
        top_bar.addSpacing(12)

        # 停止按钮
        self._stop_btn = PushButton("⏹ 停止")
        self._stop_btn.setFixedSize(80, 30)
        self._stop_btn.setStyleSheet(f"""
            PushButton {{
                background: rgba(255, 80, 80, 0.85);
                color: white;
                border: none;
                border-radius: 8px;
                {FONT_CSS} {font_size_css(13)}
                font-weight: bold;
            }}
            PushButton:hover {{
                background: rgba(255, 60, 60, 1.0);
            }}
        """)
        self._stop_btn.clicked.connect(self.stopRequested.emit)
        top_bar.addWidget(self._stop_btn)

        # 强制归档按钮
        self._archive_btn = PushButton("📦 归档")
        self._archive_btn.setFixedSize(80, 30)
        self._archive_btn.setStyleSheet(f"""
            PushButton {{
                background: rgba(245, 158, 11, 0.8);
                color: white;
                border: none;
                border-radius: 8px;
                {FONT_CSS} {font_size_css(13)}
                font-weight: bold;
            }}
            PushButton:hover {{
                background: rgba(245, 158, 11, 1.0);
            }}
        """)
        self._archive_btn.clicked.connect(self.forceArchiveRequested.emit)
        top_bar.addWidget(self._archive_btn)

        main_layout.addLayout(top_bar)

        # ===== 任务描述 =====
        self._task_label = QLabel("")
        self._task_label.setWordWrap(True)
        self._task_label.setStyleSheet(f"""
            color: #9BB0D3;
            {font_size_css(12)}
            {FONT_CSS}
            padding: 6px 10px;
            background: rgba(0,0,0,0.15);
            border-radius: 6px;
        """)
        main_layout.addWidget(self._task_label)

        # ===== StatsBar: 迭代 + Token 进度 =====
        stats_widget = QWidget()
        stats_widget.setStyleSheet("background: rgba(0,0,0,0.1); border-radius: 8px;")
        stats_layout = QHBoxLayout(stats_widget)
        stats_layout.setContentsMargins(12, 8, 12, 8)
        stats_layout.setSpacing(20)

        # 迭代
        iter_w = QWidget()
        iter_l = QHBoxLayout(iter_w)
        iter_l.setContentsMargins(0, 0, 0, 0)
        iter_l.setSpacing(6)
        iter_l.addWidget(QLabel("📚"))
        self._iter_label = QLabel("0 / 0")
        self._iter_label.setStyleSheet(f"color: #C9A85C; font-weight: bold; {font_size_css(14)} {FONT_CSS}")
        iter_l.addWidget(self._iter_label)
        stats_layout.addWidget(iter_w)

        stats_layout.addStretch()

        # Token
        token_w = QWidget()
        token_l = QHBoxLayout(token_w)
        token_l.setContentsMargins(0, 0, 0, 0)
        token_l.setSpacing(6)
        token_l.addWidget(QLabel("🔢"))
        self._token_label = QLabel("0 / 0")
        self._token_label.setStyleSheet(f"color: #A7F3D0; font-weight: bold; {font_size_css(14)} {FONT_CSS}")
        token_l.addWidget(self._token_label)

        self._token_bar = QProgressBar()
        self._token_bar.setRange(0, 100)
        self._token_bar.setValue(0)
        self._token_bar.setTextVisible(False)
        self._token_bar.setFixedHeight(8)
        self._token_bar.setFixedWidth(120)
        self._token_bar.setStyleSheet("""
            QProgressBar {
                background: rgba(255,255,255,0.1);
                border-radius: 4px;
                border: none;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                    stop:0 #10B981, stop:1 #34D399);
                border-radius: 4px;
            }
        """)
        token_l.addWidget(self._token_bar)

        self._token_pct = QLabel("0%")
        self._token_pct.setStyleSheet(f"color: #7FDBFF; {font_size_css(12)} {FONT_CSS}")
        self._token_pct.setFixedWidth(36)
        token_l.addWidget(self._token_pct)
        stats_layout.addWidget(token_w)

        main_layout.addWidget(stats_widget)

        # ===== 阶段标签（简洁一行，替换原来的大 PhaseStepper）=====
        self._phase_label = QLabel("📋 规划阶段")
        self._phase_label.setStyleSheet(f"""
            color: #7FDBFF; {font_size_css(11)} font-weight: bold; {FONT_CSS}
            padding: 2px 10px; background: rgba(127,219,255,0.1);
            border-radius: 4px; border: 1px solid rgba(127,219,255,0.2);
        """)
        main_layout.addWidget(self._phase_label)

        # ===== 步骤清单（全宽，stretch=1）=====
        step_header = QLabel("📋 步骤清单")
        step_header.setStyleSheet(f"color: #E5E7EB; {font_size_css(12)} font-weight: bold; {FONT_CSS}")
        main_layout.addWidget(step_header)

        self._step_scroll = QScrollArea()
        self._step_scroll.setWidgetResizable(True)
        self._step_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._step_scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._step_container = QWidget()
        self._step_container.setStyleSheet("background: transparent;")
        self._step_layout = QVBoxLayout(self._step_container)
        self._step_layout.setContentsMargins(4, 2, 4, 2)
        self._step_layout.setSpacing(4)
        self._step_placeholder = QLabel("⏳ 等待规划阶段解析步骤...")
        self._step_placeholder.setStyleSheet(f"color: #7A9BBF; {font_size_css(12)} {FONT_CSS}")
        self._step_layout.addWidget(self._step_placeholder)
        self._step_layout.addStretch()
        self._step_scroll.setWidget(self._step_container)
        main_layout.addWidget(self._step_scroll, 1)

        # ===== 底部系统日志 =====
        syslog_widget = QWidget()
        syslog_widget.setStyleSheet("background: rgba(0,0,0,0.08); border-radius: 6px;")
        syslog_layout = QVBoxLayout(syslog_widget)
        syslog_layout.setContentsMargins(8, 4, 8, 4)
        syslog_layout.setSpacing(2)
        syslog_header = QLabel("📝 系统日志")
        syslog_header.setStyleSheet(f"color: #7A9BBF; {font_size_css(10)} font-weight: bold; {FONT_CSS}")
        syslog_layout.addWidget(syslog_header)
        self._sys_log = QLabel("")
        self._sys_log.setStyleSheet(f"""
            color: #7A9BBF;
            {font_size_css(12)}
            {FONT_CSS}
            padding: 2px 8px;
            background: rgba(0,0,0,0.05);
            border-radius: 4px;
        """)
        # 参考设置卡片：宽度由父布局约束，自动换行，避免长日志撑开布局
        self._sys_log.setWordWrap(True)
        self._sys_log.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        syslog_layout.addWidget(self._sys_log)
        main_layout.addWidget(syslog_widget)

    # ========== 生命周期 ==========

    def start(self, task: str = "", max_tokens: int = 0):
        """启动显示：重置状态、启动计时器"""
        self._start_timestamp = time.time()
        self._max_tokens = max_tokens
        self._current_tokens = 0

        if task:
            preview = task[:80]
            if len(task) > 80:
                preview += "..."
            self._task_label.setText(f"🎯 {preview}")

        # 启动计时器
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._update_elapsed)
        self._timer.start()

        # 重置 UI
        self._iter_label.setText("0 / 0")
        self._token_label.setText("0 / 0")
        self._token_bar.setValue(0)
        self._token_pct.setText("0%")
        self._sys_log.setText("")
        self._clear_steps()
        self._step_placeholder.show()

        # 重置阶段高亮
        self.set_phase("planning")

    def stop(self):
        """停止计时器"""
        if hasattr(self, "_timer") and self._timer:
            self._timer.stop()

    def _update_elapsed(self):
        """每秒更新耗时显示"""
        if self._start_timestamp > 0:
            elapsed = time.time() - self._start_timestamp
            h, m = divmod(int(elapsed), 60)
            h, m = divmod(h, 60) if h >= 60 else (0, h)
            s = int(elapsed) % 60
            self._elapsed_label.setText(f"{h:02d}:{m:02d}:{s:02d}")

    # ========== UI 更新方法 ==========

    def update_stats(self, progress: dict):
        """更新统计信息（迭代数等）"""
        iteration = progress.get("iteration", 0)
        max_iter = progress.get("max_iterations", 0)
        self._iter_label.setText(f"{iteration} / {max_iter}")

    def update_tokens(self, total: int):
        """更新 Token 显示"""
        self._current_tokens = total

        def fmt(n: int) -> str:
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n / 1_000:.1f}K"
            return str(n)

        if self._max_tokens > 0:
            pct = min(100, int(total * 100 / self._max_tokens))
            self._token_bar.setValue(pct)
            self._token_pct.setText(f"{pct}%")
            self._token_label.setText(f"{fmt(total)} / {fmt(self._max_tokens)}")
        else:
            self._token_label.setText(fmt(total))
            self._token_pct.setText("")

    def set_phase(self, phase: str):
        """设置阶段标签"""
        phase_icons = {"planning": "📋", "executing": "🔨", "archiving": "📦", "completed": "✅"}
        phase_names = {"planning": "规划中", "executing": "执行中", "archiving": "归档中", "completed": "已完成"}
        icon = phase_icons.get(phase, "📋")
        name = phase_names.get(phase, "规划中")
        self._phase_label.setText(f"{icon} {name}")

    def update_steps(self, steps: list, current: int):
        """更新步骤清单

        Args:
            steps: 步骤名称列表 ["步骤1标题", "步骤2标题", ...]
            current: 当前步骤序号（从1开始）
        """
        self._clear_steps()
        self._step_placeholder.hide()

        for i, step_name in enumerate(steps, 1):
            step_num = i
            is_done = step_num < current
            is_current = step_num == current

            if is_done:
                icon = "✅"
                color = "#10B981"
                bg = "rgba(16,185,129,0.08)"
            elif is_current:
                icon = "▶"
                color = "#C9A85C"
                bg = "rgba(201,168,92,0.08)"
            else:
                icon = "⬜"
                color = "rgba(255,255,255,0.3)"
                bg = "transparent"

            lbl = QLabel(f"{icon}  {step_name}")
            lbl.setStyleSheet(f"""
                color: {color};
                {font_size_css(11)}
                {FONT_CSS}
                padding: 3px 8px;
                background: {bg};
                border-radius: 4px;
            """)
            self._step_layout.addWidget(lbl)

    def _clear_steps(self):
        """清空步骤列表（保留 placeholder）"""
        # 从布局中移除 placeholder（保留引用）
        self._step_layout.removeWidget(self._step_placeholder)
        self._step_placeholder.setParent(None)

        # 删除其他所有 widget
        while self._step_layout.count():
            item = self._step_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 重新添加 placeholder
        self._step_layout.addWidget(self._step_placeholder)
        self._step_layout.addStretch()

    def append_thinking(self, text: str):
        """追加 LLM 思考内容（目前不显示，保留方法避免信号连接报错）"""
        pass

    def append_tool_call(self, info: dict):
        """追加工具调用日志（目前不显示，保留方法避免信号连接报错）"""
        pass

    def append_sys_log(self, text: str):
        """追加系统日志"""
        ts = time.strftime("%H:%M:%S")
        self._sys_log.setText(f"[{ts}] {text}")
