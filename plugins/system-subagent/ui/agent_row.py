# -*- coding: utf-8 -*-
"""子智能体任务行（插件侧实现）

自 ``app/widgets/cards/floating/sub_agent_compact_widget.py`` 的 ``_RotatingIcon`` /
``_AgentTaskRow`` 移植：行 UI、旋转图标与三路信号（toggled / enter_session_requested /
stop_requested）原样保留，供消息卡内嵌运行面板复用。主程序对应的浮动卡已停用，
本文件是该实现的唯一使用方。
"""

import time

from PyQt5.QtCore import QRectF, QSize, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QPainter, QPixmap
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors
from app.utils.utils import _is_current_theme_light, get_font_family_css, get_unified_font

class RotatingIcon(QWidget):
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

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._last_pixmap)
        p.end()


class AgentTaskRow(QFrame):
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
        self._rotating_icon = RotatingIcon(":/icons/执行中.svg", size=16, parent=self)
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

    def _build_row_qss(self) -> str:
        """[PERF T35] Row 级聚合 QSS（构建与主题刷新两处共用）。

        原先 8 处独立 setStyleSheet（header/detail 容器透明 + 5 个 label
        文字色/背景）合并为一条；硬纪律：容器级一律 objectName 选择器。
        主题敏感的动态项（agent_label 标签底色、_expand_indicator 强调色、
        两个按钮）仍由各自 setStyleSheet 单控件重设。
        """
        return f"""
            #AgentTaskRow {{
                background: {Colors.HOVER_BG};
                border: none;
                border-radius: 6px;
            }}
            #AgentTaskRow:hover {{
                background: {Colors.HOVER_BG_STRONG};
            }}
            QFrame#AgentTaskHeader {{
                background: transparent;
                border: none;
            }}
            QFrame#AgentTaskDetail {{
                background: transparent;
                border: none;
            }}
            QLabel#agentRotatingIcon {{
                background: transparent;
                border: none;
            }}
            QLabel#agentDescLabel, QLabel#agentToolCountLabel, QLabel#agentTimeLabel {{
                color: {Colors.REALTIME_TEXT_SECONDARY};
                background: transparent;
            }}
            QLabel#agentTaskSuccessLabel {{
                background: transparent;
                border: none;
            }}
            QLabel#agentTaskErrorLabel {{
                font-size: 14px;
                background: transparent;
                border: none;
            }}
        """

    def _setup_ui(self):
        self.setObjectName("AgentTaskRow")
        # 自定义 QWidget 子类不设 WA_StyledBackground 时，QSS 的 background 一行
        # 都不绘制（见 CardStyles.floating 说明）；原先还写成 {{ 双花括号，
        # 非 f-string 下是字面量，选择器本身也不合法。两处一起修。
        self.setAttribute(Qt.WA_StyledBackground, True)
        Colors.refresh()
        self.setStyleSheet(self._build_row_qss())

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
        self._rotating_icon.setObjectName("agentRotatingIcon")
        self._rotating_icon.setFixedSize(16, 16)
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
        self.desc_label.setObjectName("agentDescLabel")
        self.desc_label.setFont(get_unified_font(9))
        self.desc_label.setWordWrap(False)
        self.desc_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.desc_label.setMinimumWidth(20)
        header_layout.addWidget(self.desc_label, 1)

        # 工具调用次数
        self.tool_count_label = QLabel("🔧0", self._header_widget)
        self.tool_count_label.setObjectName("agentToolCountLabel")
        self.tool_count_label.setFont(get_unified_font(9))
        header_layout.addWidget(self.tool_count_label)

        # 耗时
        self.time_label = QLabel("⏱00:00", self._header_widget)
        self.time_label.setObjectName("agentTimeLabel")
        self.time_label.setFont(get_unified_font(9))
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
            # [PERF T35] 动态色（调用方可传 text_color）整表保留；默认色路径
            # 走 Row 级聚合 QSS 的 QLabel 文字色规则（此处仅当显式传色时才设）
            if text_color:
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
            self._success_label.setObjectName("agentTaskSuccessLabel")
            self._success_label.setFixedSize(16, 16)
            self._success_label.setPixmap(self._success_pixmap)
            idx = self._header_widget.layout().indexOf(self._rotating_icon)
            self._header_widget.layout().removeWidget(self._rotating_icon)
            self._header_widget.layout().insertWidget(idx, self._success_label)
        else:
            self._error_label = QLabel("❌", self._header_widget)
            self._error_label.setObjectName("agentTaskErrorLabel")
            self._error_label.setFixedSize(16, 16)
            idx = self._header_widget.layout().indexOf(self._rotating_icon)
            self._header_widget.layout().removeWidget(self._rotating_icon)
            self._header_widget.layout().insertWidget(idx, self._error_label)

    def increment_tool_count(self):
        """工具调用次数 +1"""
        self._tool_count += 1
        self.tool_count_label.setText(f"🔧{self._tool_count}")
        if self._tool_label_detail:
            self._tool_label_detail.setText(f"{self._tool_count} 次调用")

    def set_tool_count(self, count: int):
        """直接设定工具调用次数（迁卡对齐用，不做增量）

        与 ``increment_tool_count`` 的区别：迁卡/补投影时同一个任务的累计次数要从
        桥里的记录一次写回，逐次 +1 会把历史计数重复累加。
        """
        self._tool_count = max(0, int(count))
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

    # ── 样式刷新 ──────────────────────────────────────

    def refresh_row_style(self):
        """响应主题切换，刷新所有标签颜色"""
        Colors.refresh()
        # 更新旋转图标叠加色（浅色主题下加深图标）
        if self.is_running and hasattr(self, "_rotating_icon"):
            self._rotating_icon.set_tint("#88000000" if _is_current_theme_light() else None)
        # [PERF T35] 保留：动态项单控件重设（作用域限自身，不影响聚合表）；
        # Row 级聚合规则必须随主题重建 → 复用 _build_row_qss()。
        self.setStyleSheet(self._build_row_qss())
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
