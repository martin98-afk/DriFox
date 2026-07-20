# -*- coding: utf-8 -*-
"""
子智能体紧凑型悬浮框 - subagent_para 触发时自动弹出
类似 ToolFloatingWidget 的风格：每行一个子智能体，显示旋转图标 + agent名 + 任务描述
支持点击展开详情（状态、模型、上下文用量等），内容超最大高度时支持滚动
与 SubAgentFloatingWidget（详细日志面板）完全独立
"""

import time
from typing import Dict

from PyQt5.QtCore import QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors
from app.utils.utils import _is_current_theme_light, get_font_family_css, get_unified_font

# 卡片最大高度（超出时出现滚动条）
_MAX_CARD_HEIGHT = 320


class _RotatingIcon(QWidget):
    """用 QPainter 原地旋转 SVG，消除 QPixmap.transform 的 bounding-box 抖动"""

    def __init__(self, svg_path: str, size: int = 16, parent=None):
        super().__init__(parent)
        self._svg_path = svg_path
        self._renderer = QSvgRenderer(svg_path)
        self._size = size
        self._angle = 0
        self._tint = None  # 浅色主题叠加色 (None=不叠加)
        self.setFixedSize(size, size)
        self._last_pixmap = QPixmap(size, size)
        self._last_pixmap.fill(Qt.transparent)

    def set_angle(self, degrees: float):
        self._angle = degrees
        self.update()
        self._redraw()

    def set_svg_path(self, svg_path: str):
        """切换 SVG 资源（用于主题切换）"""
        if svg_path == self._svg_path:
            return
        self._svg_path = svg_path
        self._renderer = QSvgRenderer(svg_path)
        self._redraw()
        self.update()

    def set_tint(self, color: str = None):
        """设置主题叠加色（用于浅色主题下加深图标）"""
        self._tint = color
        self._redraw()
        self.update()

    def _redraw(self):
        self._last_pixmap.fill(Qt.transparent)
        p = QPainter(self._last_pixmap)
        try:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            cx, cy = self._size / 2, self._size / 2
            p.translate(cx, cy)
            p.rotate(self._angle)
            p.translate(-cx, -cy)
            self._renderer.render(p, QRectF(0, 0, self._size, self._size))
        finally:
            p.end()
        # 浅色主题叠加：在 SVG 形状上叠加半透明黑色，使浅色图标在亮背景下可见
        if self._tint is not None:
            tp = QPainter(self._last_pixmap)
            try:
                tp.setCompositionMode(QPainter.CompositionMode_SourceAtop)
                tp.fillRect(self._last_pixmap.rect(), QColor(self._tint))
            finally:
                tp.end()

    def current_pixmap(self) -> QPixmap:
        return self._last_pixmap

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._last_pixmap)
        p.end()


class _AgentTaskRow(QFrame):
    """单行子智能体任务 - 点击可展开显示详情"""

    toggled = pyqtSignal(str)  # task_id, 当展开/收起时发出
    enter_session_requested = pyqtSignal(str)  # task_id, 当用户点击"进入会话"时发出
    stop_requested = pyqtSignal(str)  # task_id, 当用户点击停止按钮时发出

    def __init__(self, task_id: str, agent_name: str, task_desc: str, model_name: str = "", parent=None):
        super().__init__(parent)
        self.task_id = task_id
        self.agent_name = agent_name
        self.task_desc = task_desc
        self.model_name = model_name
        self.is_running = True
        self._tool_count = 0
        self._start_time = time.time()
        self._is_finished = False
        self._is_expanded = False
        self._context_info = ""  # 上下文用量信息（如 token 数）

        # 旋转图标（浅色主题时叠加半透明黑色以适配亮背景）
        self._rotating_icon = _RotatingIcon(":/icons/执行中.svg", size=16, parent=self)
        if _is_current_theme_light():
            self._rotating_icon.set_tint("#88000000")
        # 成功后显示的静态图标
        self._success_pixmap = QPixmap(16, 16)
        self._success_pixmap.fill(Qt.transparent)
        _svg_success = QSvgRenderer(":/icons/成功.svg")
        _p = QPainter(self._success_pixmap)
        _svg_success.render(_p, QRectF(0, 0, 16, 16))
        _p.end()

        # 详情面板
        self._detail_panel = None

        self._setup_ui()
        self.setCursor(Qt.PointingHandCursor)

    # ── UI 构建 ──────────────────────────────────────

    def _setup_ui(self):
        self.setObjectName("AgentTaskRow")
        self.setStyleSheet("""
            #AgentTaskRow {{
                background: rgba(255,255,255,0.03);
                border: none;
                border-radius: 6px;
            }}
            #AgentTaskRow:hover {{
                background: rgba(255,255,255,0.06);
            }}
        """)

        self._main_layout = QVBoxLayout(self)
        self._main_layout.setContentsMargins(0, 0, 0, 0)
        self._main_layout.setSpacing(0)

        # ── 顶栏行 ──
        self._header_widget = QFrame(self)
        self._header_widget.setObjectName("AgentTaskHeader")
        self._header_widget.setStyleSheet("QFrame#AgentTaskHeader { background: transparent; border: none; }")
        header_layout = QHBoxLayout(self._header_widget)
        header_layout.setContentsMargins(3, 5, 6, 5)
        header_layout.setSpacing(6)

        # 【展开指示器 → 放到最前面】
        self._expand_indicator = QLabel("▾", self._header_widget)
        self._expand_indicator.setFont(get_unified_font(10))
        self._expand_indicator.setFixedWidth(14)
        Colors.refresh()
        self._expand_indicator.setStyleSheet(f"color: {Colors.REALTIME_ACCENT}; background: transparent;")
        self._expand_indicator.setAlignment(Qt.AlignCenter)
        header_layout.addWidget(self._expand_indicator)

        # 旋转图标
        self._rotating_icon.setFixedSize(16, 16)
        self._rotating_icon.setStyleSheet("background: transparent; border: none;")
        header_layout.addWidget(self._rotating_icon)

        # Agent 名称标签
        self.agent_label = QLabel(f" {self.agent_name} ", self._header_widget)
        self.agent_label.setFont(get_unified_font(9))
        Colors.refresh()
        self.agent_label.setStyleSheet(
            f"color: {Colors.REALTIME_ACCENT}; background-color: {Colors.REALTIME_TAG_BG}; "
            f"padding: 1px 4px; border-radius: 4px;"
        )
        header_layout.addWidget(self.agent_label)

        # 任务描述
        # 清洗描述：换行符替换为空格，截断过长文本
        desc = self.task_desc.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        self._full_desc = desc
        if len(desc) > 55:
            desc = desc[:55] + "..."
        self.desc_label = QLabel(desc, self._header_widget)
        self.desc_label.setFont(get_unified_font(9))
        self.desc_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self.desc_label.setWordWrap(False)
        self.desc_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.desc_label.setMinimumWidth(20)
        header_layout.addWidget(self.desc_label, 1)

        # 工具调用次数
        self.tool_count_label = QLabel("🔧0", self._header_widget)
        self.tool_count_label.setFont(get_unified_font(9))
        self.tool_count_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        header_layout.addWidget(self.tool_count_label)

        # 耗时
        self.time_label = QLabel("⏱00:00", self._header_widget)
        self.time_label.setFont(get_unified_font(9))
        self.time_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        header_layout.addWidget(self.time_label)

        self._main_layout.addWidget(self._header_widget)

        # ── 详情面板（默认隐藏） ──
        self._detail_panel = QFrame(self)
        self._detail_panel.setObjectName("AgentTaskDetail")
        self._detail_panel.setStyleSheet("QFrame#AgentTaskDetail { background: transparent; border: none; }")
        self._detail_panel.setVisible(False)
        self._setup_detail_panel()
        self._main_layout.addWidget(self._detail_panel)

    def _setup_detail_panel(self):
        """构建详情面板内容 - 横向网格布局，宽度不够自动换行"""
        Colors.refresh()
        # 使用 2 列网格布局，每个格子内含 icon+label
        detail_layout = QGridLayout(self._detail_panel)
        detail_layout.setContentsMargins(28, 4, 8, 4)
        detail_layout.setSpacing(6)

        def _make_item(icon_char: str, text: str, text_color: str = None) -> tuple:
            """返回 (icon_widget, label_widget)"""
            icon = QLabel(icon_char, self._detail_panel)
            icon.setFont(get_unified_font(9))
            label = QLabel(text, self._detail_panel)
            label.setFont(get_unified_font(8))
            c = text_color if text_color else f"{Colors.REALTIME_TEXT_SECONDARY}"
            label.setStyleSheet(f"color: {c}; background: transparent;")
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
            return icon, label

        # Row 0: 🧠 模型      | 🔧 工具调用
        # Row 1: 📊 上下文    | ⏱ 耗时
        # Row 2: 🔗 进入会话  | ⏹ 停止

        mdl_icon, self._model_label_detail = _make_item("🧠", self.model_name or "未指定")
        detail_layout.addWidget(mdl_icon, 0, 0)
        detail_layout.addWidget(self._model_label_detail, 0, 1)

        tool_icon, self._tool_label_detail = _make_item("🔧", "0 次调用")
        detail_layout.addWidget(tool_icon, 0, 2)
        detail_layout.addWidget(self._tool_label_detail, 0, 3)

        ctx_icon, self._ctx_label_detail = _make_item("📊", "—")
        detail_layout.addWidget(ctx_icon, 1, 0)
        detail_layout.addWidget(self._ctx_label_detail, 1, 1)

        elp_icon, self._elapsed_label_detail = _make_item("⏱", "00:00")
        detail_layout.addWidget(elp_icon, 1, 2)
        detail_layout.addWidget(self._elapsed_label_detail, 1, 3)

        # 进入会话按钮
        self._enter_session_btn = QPushButton("🔗 进入会话", self._detail_panel)
        self._enter_session_btn.setFont(get_unified_font(8))
        self._enter_session_btn.setFixedHeight(22)
        Colors.refresh()
        self._enter_session_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Colors.REALTIME_ACCENT};
                color: #ffffff;
                border: none;
                border-radius: 4px;
                padding: 0 8px;
                {get_font_family_css()}
            }}
            QPushButton:hover {{
                background-color: {Colors.REALTIME_ACCENT_WARM};
            }}
            QPushButton:pressed {{
                background-color: {Colors.REALTIME_ACCENT};
            }}
        """)
        self._enter_session_btn.setCursor(Qt.PointingHandCursor)
        self._enter_session_btn.clicked.connect(self._on_enter_session_clicked)
        detail_layout.addWidget(self._enter_session_btn, 2, 0, 1, 2)

        # 停止按钮（运行中可见）
        self._stop_btn_detail = QPushButton("⏹ 停止", self._detail_panel)
        self._stop_btn_detail.setFont(get_unified_font(8))
        self._stop_btn_detail.setFixedHeight(22)
        Colors.refresh()
        self._stop_btn_detail.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {Colors.SYNTAX_ERROR};
                border: 1px solid {Colors.SYNTAX_ERROR};
                border-radius: 4px;
                padding: 0 8px;
                {get_font_family_css()}
            }}
            QPushButton:hover {{
                background-color: {Colors.SYNTAX_ERROR}33;
                color: {Colors.SYNTAX_ERROR};
            }}
            QPushButton:pressed {{
                background-color: {Colors.SYNTAX_ERROR}55;
            }}
        """)
        self._stop_btn_detail.setCursor(Qt.PointingHandCursor)
        self._stop_btn_detail.clicked.connect(self._on_stop_clicked)
        detail_layout.addWidget(self._stop_btn_detail, 2, 2, 1, 2)

    # ── 交互 ──────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._toggle_expand()
        super().mousePressEvent(event)

    def _toggle_expand(self):
        """切换展开/收起"""
        self._is_expanded = not self._is_expanded
        self._detail_panel.setVisible(self._is_expanded)
        self._expand_indicator.setText("▴" if self._is_expanded else "▾")
        self.toggled.emit(self.task_id)

        # 通知父组件重新计算尺寸
        parent_widget = self.parentWidget()
        while parent_widget:
            if hasattr(parent_widget, "_on_row_toggled"):
                parent_widget._on_row_toggled()
                break
            parent_widget = parent_widget.parentWidget()

    def collapse(self):
        """收起详情"""
        if self._is_expanded:
            self._is_expanded = False
            self._detail_panel.setVisible(False)
            self._expand_indicator.setText("▾")

    def _on_enter_session_clicked(self):
        """进入会话按钮点击 - 发出信号由父组件处理弹出"""
        self.enter_session_requested.emit(self.task_id)

    def _on_stop_clicked(self):
        """停止按钮点击 - 发出信号由父组件处理中止逻辑"""
        self.stop_requested.emit(self.task_id)

    # ── 公共方法 ──────────────────────────────────────

    def sizeHint(self):
        """只返回可见部分的高度：header + 展开后的详情面板"""
        h = self._header_widget.sizeHint().height()
        if self._is_expanded:
            h += self._detail_panel.sizeHint().height()
        return QSize(self._main_layout.sizeHint().width(), h)

    def set_rotation_angle(self, angle: float):
        if self.is_running:
            self._rotating_icon.set_angle(angle)

    def set_model_name(self, model_name: str):
        """设置模型名称（运行时更新）"""
        self.model_name = model_name
        if self._model_label_detail:
            self._model_label_detail.setText(model_name or "未指定")

    def set_context_info(self, info: str):
        """设置上下文用量信息"""
        self._context_info = info
        if self._ctx_label_detail:
            self._ctx_label_detail.setText(info)

    def finish(self, success: bool = True):
        """标记任务完成（正常完成或中止）"""
        if self._is_finished:
            return
        self.is_running = False
        self._is_finished = True
        self.update_elapsed()
        self._rotating_icon.setVisible(False)

        # 隐藏详情面板中的停止按钮
        if hasattr(self, "_stop_btn_detail"):
            self._stop_btn_detail.setVisible(False)

        # 替换旋转图标为完成状态图标
        if success:
            self._success_label = QLabel(self._header_widget)
            self._success_label.setFixedSize(16, 16)
            self._success_label.setPixmap(self._success_pixmap)
            self._success_label.setStyleSheet("background: transparent; border: none;")
            idx = self._header_widget.layout().indexOf(self._rotating_icon)
            self._header_widget.layout().removeWidget(self._rotating_icon)
            self._header_widget.layout().insertWidget(idx, self._success_label)
        else:
            self._error_label = QLabel("❌", self._header_widget)
            self._error_label.setFixedSize(16, 16)
            self._error_label.setStyleSheet("font-size: 14px; background: transparent; border: none;")
            idx = self._header_widget.layout().indexOf(self._rotating_icon)
            self._header_widget.layout().removeWidget(self._rotating_icon)
            self._header_widget.layout().insertWidget(idx, self._error_label)

    def increment_tool_count(self):
        """工具调用次数 +1"""
        self._tool_count += 1
        self.tool_count_label.setText(f"🔧{self._tool_count}")
        if self._tool_label_detail:
            self._tool_label_detail.setText(f"{self._tool_count} 次调用")

    def update_elapsed(self):
        """更新已用时间显示（每秒由父组件定时器驱动）"""
        if self._is_finished:
            return
        elapsed = int(time.time() - self._start_time)
        mins = elapsed // 60
        secs = elapsed % 60
        time_str = f"{mins:02d}:{secs:02d}"
        self.time_label.setText(f"⏱{time_str}")
        if self._elapsed_label_detail:
            self._elapsed_label_detail.setText(time_str)

    def clear_icon(self):
        """清空图标"""
        self._rotating_icon.setVisible(False)
        if hasattr(self, "_success_label"):
            self._success_label.setVisible(False)
        if hasattr(self, "_error_label"):
            self._error_label.setVisible(False)

    # ── 样式刷新 ──────────────────────────────────────

    def refresh_row_style(self):
        """响应主题切换，刷新所有标签颜色"""
        Colors.refresh()
        # 更新旋转图标叠加色（浅色主题下加深图标）
        if self.is_running and hasattr(self, "_rotating_icon"):
            self._rotating_icon.set_tint("#88000000" if _is_current_theme_light() else None)
        self.setStyleSheet("""
            #AgentTaskRow {{
                background: rgba(255,255,255,0.03);
                border: none;
                border-radius: 6px;
            }}
            #AgentTaskRow:hover {{
                background: rgba(255,255,255,0.06);
            }}
        """)
        self.agent_label.setStyleSheet(
            f"color: {Colors.REALTIME_ACCENT}; background-color: {Colors.REALTIME_TAG_BG}; "
            f"padding: 1px 4px; border-radius: 4px;"
        )
        self.desc_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self.tool_count_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self.time_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self._expand_indicator.setStyleSheet(f"color: {Colors.REALTIME_ACCENT}; background: transparent;")
        self._model_label_detail.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self._tool_label_detail.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self._ctx_label_detail.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self._elapsed_label_detail.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        if hasattr(self, "_enter_session_btn"):
            self._enter_session_btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {Colors.REALTIME_ACCENT};
                    color: #ffffff;
                    border: none;
                    border-radius: 4px;
                    padding: 0 8px;
                    {get_font_family_css()}
                }}
                QPushButton:hover {{
                    background-color: {Colors.REALTIME_ACCENT_WARM};
                }}
                QPushButton:pressed {{
                    background-color: {Colors.REALTIME_ACCENT};
                }}
            """)
        if hasattr(self, "_stop_btn_detail"):
            self._stop_btn_detail.setStyleSheet(f"""
                QPushButton {{
                    background-color: transparent;
                    color: {Colors.SYNTAX_ERROR};
                    border: 1px solid {Colors.SYNTAX_ERROR};
                    border-radius: 4px;
                    padding: 0 8px;
                    {get_font_family_css()}
                }}
                QPushButton:hover {{
                    background-color: {Colors.SYNTAX_ERROR}33;
                    color: {Colors.SYNTAX_ERROR};
                }}
                QPushButton:pressed {{
                    background-color: {Colors.SYNTAX_ERROR}55;
                }}
            """)


class SubAgentCompactFloatingWidget(QWidget):
    """子智能体紧凑型悬浮框 - 自动弹出显示运行状态

    特性：
    - 内容自适应高度，最多撑到 _MAX_CARD_HEIGHT px，超出时滚动
    - 每行可点击展开详情（模型、状态、工具调用、上下文用量）
    - 每行详情中提供"进入会话"按钮，弹出对应子智能体会话窗口
    """

    closed = pyqtSignal()
    enter_session_requested = pyqtSignal(str, str)  # task_id, agent_name
    stop_subagent_requested = pyqtSignal(str)  # task_id, 用户点击停止按钮时发出

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task_rows: Dict[str, _AgentTaskRow] = {}
        self._rotation_angle = 0
        self._rotation_timer = QTimer(self)
        self._rotation_timer.timeout.connect(self._update_all_rotations)
        self._has_running = False
        self._batch_started: bool = False
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(2000)
        self._hide_timer.timeout.connect(self._auto_hide)

        self._time_timer = QTimer(self)
        self._time_timer.timeout.connect(self._update_all_times)
        self._time_timer.setInterval(1000)

        self._reflow_deferred_guard = False  # 防止 deferred reflow 无限循环
        self._setup_ui()

    # ── UI 初始化 ──────────────────────────────────────

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMaximumHeight(_MAX_CARD_HEIGHT)
        Colors.refresh()
        self._apply_style(None)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 6, 12, 6)
        main_layout.setSpacing(2)

        # ── 顶部标题栏 ──
        header = QHBoxLayout()
        header.setSpacing(6)

        self.header_icon = QLabel("🤖", self)
        self.header_icon.setFont(get_unified_font(11))
        self.header_icon.setStyleSheet("background: transparent; border: none;")
        header.addWidget(self.header_icon)

        self.title_label = QLabel("子智能体", self)
        self.title_label.setFont(get_unified_font(10, True))
        Colors.refresh()
        self.title_label.setStyleSheet(f"color: {Colors.REALTIME_ACCENT}; background: transparent;")
        header.addWidget(self.title_label)

        self.status_label = QLabel("", self)
        self.status_label.setFont(get_unified_font(9))
        self.status_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        header.addWidget(self.status_label)

        header.addStretch()

        self.close_btn = QPushButton("✕", self)
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {Colors.TEXT_MUTED};
                border: none;
                {get_font_family_css()}
                font-size: 12px;
            }}
            QPushButton:hover {{
                color: {Colors.TEXT_PRIMARY};
                background-color: {Colors.CONTENT_BG};
                border-radius: 3px;
            }}
        """)
        self.close_btn.clicked.connect(self._on_close)
        header.addWidget(self.close_btn)

        main_layout.addLayout(header)

        # ── 任务列表滚动容器 ──
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._scroll_area.setFrameShape(QFrame.NoFrame)
        Colors.refresh()
        self._scroll_area.setStyleSheet(f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 4px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {Colors.REALTIME_BORDER};
                border-radius: 2px;
                min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {Colors.REALTIME_ACCENT};
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
        """)

        # 滚动区域内的内容容器（无 stretch）
        self._scroll_content = QWidget(self._scroll_area)
        self._scroll_content.setStyleSheet("background: transparent;")
        self._body_layout = QVBoxLayout(self._scroll_content)
        self._body_layout.setContentsMargins(0, 0, 8, 0)
        self._body_layout.setSpacing(4)

        self._scroll_area.setWidget(self._scroll_content)
        main_layout.addWidget(self._scroll_area, 1)

        # 初始高度
        self._reflow()

    def _apply_style(self, running: bool = None):
        """更新卡片样式"""
        Colors.refresh()
        if running is None:
            border_color = Colors.REALTIME_BORDER
        elif running:
            border_color = Colors.REALTIME_ACCENT_WARM
        else:
            border_color = Colors.REALTIME_SUCCESS

        self.setStyleSheet(f"""
            SubAgentCompactFloatingWidget {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {border_color};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
            }}
        """)

    def refresh_style(self):
        """响应主题切换"""
        Colors.refresh()
        self.title_label.setStyleSheet(f"color: {Colors.REALTIME_ACCENT}; background: transparent;")
        self.status_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")

        # 刷新滚动条样式
        self._scroll_area.setStyleSheet(f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 4px;
                margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {Colors.REALTIME_BORDER};
                border-radius: 2px;
                min-height: 20px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {Colors.REALTIME_ACCENT};
            }}
            QScrollBar::add-line:vertical,
            QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical,
            QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
        """)

        running = self._has_running
        self._apply_style(running if running else None)
        # 刷新每行样式
        for row in self._task_rows.values():
            row.refresh_row_style()

    def set_opacity(self, opacity: float):
        """设置透明度"""
        Colors.refresh()
        bg = Colors.REALTIME_BG
        if bg.startswith("rgba("):
            alpha = max(1, int(opacity * 255))
            bg = bg.rsplit(",", 1)[0] + f", {alpha})"
        running = self._has_running
        border_color = Colors.REALTIME_ACCENT_WARM if running else Colors.REALTIME_SUCCESS
        self.setStyleSheet(f"""
            SubAgentCompactFloatingWidget {{
                background-color: {bg};
                border: 1px solid {border_color};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
            }}
        """)

    # ── 旋转动画 ──────────────────────────────────────

    def _start_rotation(self):
        if not self._rotation_timer.isActive():
            self._rotation_timer.start(30)
        if not self._time_timer.isActive():
            self._time_timer.start(1000)

    def _stop_rotation(self):
        self._rotation_timer.stop()
        self._time_timer.stop()

    def _update_all_rotations(self):
        self._rotation_angle = (self._rotation_angle + 12) % 360
        for row in self._task_rows.values():
            if row.is_running:
                row.set_rotation_angle(self._rotation_angle)

    def _update_all_times(self):
        """更新所有行的已用时间"""
        for row in self._task_rows.values():
            row.update_elapsed()

    # ── 任务管理 ──────────────────────────────────────

    def add_task(self, task_id: str, agent_name: str, task_description: str, model_name: str = ""):
        """添加一个新的子智能体任务行

        如果 task_id 已存在则跳过（保持已有的统计数据）。用于卡片关闭后重新打开时
        避免工具计数、运行时间等统计被重置。

        Args:
            task_id: 任务唯一标识
            agent_name: 子智能体名称
            task_description: 任务描述
            model_name: 使用的模型名称（可选）
        """
        # 如果任务已存在，仅更新模型名称并保持已有统计数据
        existing = self._task_rows.get(task_id)
        if existing is not None:
            if model_name:
                existing.set_model_name(model_name)
            self._has_running = True
            self._start_rotation()
            self._apply_style(True)
            self._update_status_text()
            self._reflow()
            return

        row = _AgentTaskRow(task_id, agent_name, task_description, model_name, self._scroll_content)
        row.toggled.connect(self._on_row_toggled)
        row.enter_session_requested.connect(self._on_enter_session_requested)
        row.stop_requested.connect(self._on_row_stop_requested)
        self._task_rows[task_id] = row
        self._body_layout.addWidget(row)

        # 从模型名推断上下文窗口信息
        ctx_info = self._resolve_context_info(model_name)
        if ctx_info:
            row.set_context_info(ctx_info)

        self._has_running = True
        self._start_rotation()
        self._apply_style(True)
        self._update_status_text()
        self._reflow()

    def _on_enter_session_requested(self, task_id: str):
        """处理进入会话请求 - 转发信号给主窗口"""
        row = self._task_rows.get(task_id)
        agent_name = row.agent_name if row else ""
        self.enter_session_requested.emit(task_id, agent_name)

    def _on_row_stop_requested(self, task_id: str):
        """处理行停止请求 - 转发给主窗口处理中止逻辑"""
        self.stop_subagent_requested.emit(task_id)

    @staticmethod
    def _resolve_context_info(model_name: str) -> str:
        """从模型名推断上下文窗口信息"""
        if not model_name:
            return ""
        try:
            from app.core.model_capabilities import get_model_capabilities

            caps = get_model_capabilities(model_name)
            ctx_limit = caps.get("context_limit", 0) or caps.get("max_context_tokens", 0) or 0
            if ctx_limit:
                if ctx_limit >= 1000000:
                    return f"{ctx_limit // 1000000}M 上下文"
                elif ctx_limit >= 1000:
                    return f"{ctx_limit // 1000}K 上下文"
                else:
                    return f"{ctx_limit} 上下文"
        except Exception:
            pass
        # 从常见模型名推断
        name_lower = model_name.lower()
        if "128k" in name_lower or "claude-3" in name_lower:
            return "200K 上下文"
        if "gemini" in name_lower:
            return "1M 上下文"
        if "gpt-4" in name_lower or "gpt4" in name_lower:
            return "128K 上下文"
        return ""

    def _on_row_toggled(self, task_id: str = ""):
        """行展开/收起时重新计算卡片高度"""
        self._reflow()

    def finish_task(self, task_id: str, success: bool = True):
        """标记任务完成"""
        row = self._task_rows.get(task_id)
        if not row:
            return
        row.finish(success)

        self._update_status_text()

        # 检查是否所有任务都完成
        all_done = all(not r.is_running for r in self._task_rows.values())
        if all_done:
            self._has_running = False
            self._stop_rotation()
            self._apply_style(False)
            self._start_hide_timer()
        self._reflow()

    def show_completed_task(
        self,
        task_id: str,
        agent_name: str,
        task_description: str,
        model_name: str = "",
        tool_call_count: int = 0,
        elapsed_seconds: int = 0,
        context_usage: str = "",
    ):
        """显示一个已完成的任务（从历史数据加载，无旋转动画）

        Args:
            task_id: 任务ID
            agent_name: 智能体名称
            task_description: 任务描述
            model_name: 模型名（可选）
            tool_call_count: 工具调用次数
            elapsed_seconds: 已用秒数
            context_usage: 上下文用量文字（如 "12.5K tokens"）
        """
        if task_id in self._task_rows:
            return

        row = _AgentTaskRow(task_id, agent_name, task_description, model_name, self._scroll_content)
        row.toggled.connect(self._on_row_toggled)
        row.enter_session_requested.connect(self._on_enter_session_requested)
        self._task_rows[task_id] = row
        self._body_layout.addWidget(row)

        # 设置统计数据
        if tool_call_count > 0:
            for _ in range(tool_call_count):
                row.increment_tool_count()
        # 设置已用时间（直接设置 label，因为 update_elapsed 在 _is_finished 后是空操作）
        if elapsed_seconds > 0:
            import time as _time

            row._start_time = _time.time() - elapsed_seconds
            mins = elapsed_seconds // 60
            secs = elapsed_seconds % 60
            time_str = f"{mins:02d}:{secs:02d}"
            row.time_label.setText(f"⏱{time_str}")
            if hasattr(row, "_elapsed_label_detail") and row._elapsed_label_detail:
                row._elapsed_label_detail.setText(time_str)

        # 设置模型名称（详情面板显示）
        if model_name:
            row.set_model_name(model_name)
        # 设置上下文用量
        if context_usage:
            row.set_context_info(context_usage)

        # 标记为已完成（替换旋转图标为对号，停止计时器更新）
        row.finish(success=True)

        # 确保卡片可见
        self.setVisible(True)
        self._update_status_text()
        self._reflow()

    def add_tool_call(self, task_id: str, tool_name: str, args: dict = None):
        """记录一次工具调用（更新对应行的工具计数）"""
        row = self._task_rows.get(task_id)
        if row:
            row.increment_tool_count()

    def remove_task(self, task_id: str):
        """移除指定任务行

        用于新批次开始时清理已完成的任务，同时保留运行中的任务统计数据。
        """
        row = self._task_rows.pop(task_id, None)
        if row is None:
            return
        self._body_layout.removeWidget(row)
        row.deleteLater()
        self._reflow()

    def set_task_model(self, task_id: str, model_name: str):
        """设置任务的模型名称（运行时更新）"""
        row = self._task_rows.get(task_id)
        if row:
            row.set_model_name(model_name)

    def set_task_context(self, task_id: str, info: str):
        """设置任务的上下文用量信息"""
        row = self._task_rows.get(task_id)
        if row:
            row.set_context_info(info)

    def clear(self):
        """清空所有任务"""
        self._stop_rotation()
        self._hide_timer.stop()

        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._task_rows.clear()
        self._has_running = False
        self._rotation_angle = 0
        self._apply_style(None)
        self._update_status_text()
        self._reflow()
        self.setVisible(False)

    def _update_status_text(self):
        """更新状态文字"""
        running = sum(1 for r in self._task_rows.values() if r.is_running)
        total = len(self._task_rows)
        if running > 0:
            self.status_label.setText(f"{running} 个执行中 / {total} 个任务")
        elif total > 0:
            self.status_label.setText("全部完成")
        else:
            self.status_label.setText("")

    def _reflow(self):
        """根据内容重新计算卡片高度。

        策略：
        - 内容总高度 = 所有任务行高度之和 + 间距
        - 内容总高度 <= _MAX_CARD_HEIGHT → 卡片固定为内容高度（无滚动）
        - 内容总高度 > _MAX_CARD_HEIGHT → 卡片固定为最大高度（出现滚动条）

        widgetResizable=True 时内容 widget 由 scroll area 自动缩放，
        任务行自然高度小于视口则无滚动条，大于视口则出现滚动条。
        """
        # 计算内容自然高度（所有任务行的 sizeHint 之和 + 间距）
        # 注意：当 widget 未显示（隐藏状态）时，Qt 尚未处理子 widget 的布局，
        # sizeHint() 可能返回极小值（如 15-20px）。为确保卡片不会在首次显示时
        # 因 height 过小而被锁死在矮小尺寸，对每个未展开行强制最低 28px。
        _MIN_ROW_HEIGHT = 28
        content_height = 0
        for _, row in self._task_rows.items():
            sh = row.sizeHint()
            row_h = 0
            if sh.isValid():
                row_h = sh.height()
            if row_h < _MIN_ROW_HEIGHT:
                row_h = _MIN_ROW_HEIGHT
            content_height += row_h
        if len(self._task_rows) > 1:
            content_height += self._body_layout.spacing() * (len(self._task_rows) - 1)

        # 从实际 layout 动态计算 overhead（替代硬编码 38px）
        # header 的实际高度取决于字体渲染、DPI、status_label 内容等，写死 24px 会
        # 在 header 实际 > 24px 时让 scroll area 可用高度 < content_height，
        # 导致底部行被裁切隐藏（又因未达 _MAX_CARD_HEIGHT 而不出现滚动条）。
        margins = self.layout().contentsMargins()
        spacing = self.layout().spacing()
        header_item = self.layout().itemAt(0)  # QVBoxLayout 索引 0 是 header QHBoxLayout
        header_h = 24  # fallback（隐藏状态时 sizeHint 可能为 0）
        if header_item and header_item.layout():
            hint = header_item.layout().sizeHint()
            if hint.isValid() and hint.height() > 0:
                header_h = hint.height()
        overhead = margins.top() + header_h + spacing + margins.bottom()
        total_height = overhead + content_height

        if total_height > _MAX_CARD_HEIGHT:
            self.setFixedHeight(_MAX_CARD_HEIGHT)
        else:
            self.setFixedHeight(max(36, total_height))

        # 显式激活布局，确保 scroll area 及其内容 widget 立即响应新高度
        self.layout().activate()
        self._scroll_area.updateGeometry()

        # ⚡ 若当前 widget 不可见（未显示），Qt 布局系统尚未给子 widget 分配有效高度，
        # 上述 sizeHint 可能不可靠。调度一次延迟重算，确保在 widget 显示后、
        # 布局就绪时纠正高度。
        if not self.isVisible() and not self._reflow_deferred_guard:
            self._reflow_deferred_guard = True
            QTimer.singleShot(0, self._reflow_after_layout)
        elif self.isVisible() and not self._reflow_deferred_guard:
            # 即使 widget 可见，Qt 布局传播可能未完全同步（尤其首次显示时），
            # 调度一次延迟重算以确保 scroll area 内部 content widget 尺寸正确。
            self._reflow_deferred_guard = True
            QTimer.singleShot(0, self._reflow_after_layout)

    # ── 显示/隐藏 ──────────────────────────────────────

    def _start_hide_timer(self):
        """所有任务完成后，延迟自动隐藏"""
        self._hide_timer.start()

    def _auto_hide(self):
        """自动隐藏"""
        self.setVisible(False)
        self.closed.emit()

    def _reflow_after_layout(self):
        """延迟重算：由 _reflow 或 showEvent 调度，在 Qt 事件循环处理完布局后执行"""
        self._reflow_deferred_guard = False
        self._reflow()

    def _on_close(self):
        """手动关闭"""
        self._hide_timer.stop()
        self.setVisible(False)
        self.closed.emit()

    def showEvent(self, event):
        super().showEvent(event)
        if self._has_running and not self._rotation_timer.isActive():
            self._start_rotation()
        # widget 变为可见后，子 widget 布局才被 Qt 真正处理，
        # 调度延迟重算以纠正之前隐藏状态下计算的过小高度
        if not self._reflow_deferred_guard:
            self._reflow_deferred_guard = True
            QTimer.singleShot(0, self._reflow_after_layout)
