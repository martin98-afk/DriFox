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
from PyQt5.QtGui import QPainter, QPixmap
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
from app.utils.utils import get_font_family_css, get_unified_font

# 卡片最大高度（超出时出现滚动条）
_MAX_CARD_HEIGHT = 320


class _RotatingIcon(QWidget):
    """用 QPainter 原地旋转 SVG，消除 QPixmap.transform 的 bounding-box 抖动"""

    def __init__(self, svg_path: str, size: int = 16, parent=None):
        super().__init__(parent)
        self._renderer = QSvgRenderer(svg_path)
        self._size = size
        self._angle = 0
        self.setFixedSize(size, size)
        self._last_pixmap = QPixmap(size, size)
        self._last_pixmap.fill(Qt.transparent)

    def set_angle(self, degrees: float):
        self._angle = degrees
        self.update()
        self._redraw()

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

    def current_pixmap(self) -> QPixmap:
        return self._last_pixmap

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._last_pixmap)
        p.end()


class _AgentTaskRow(QFrame):
    """单行子智能体任务 - 点击可展开显示详情"""

    toggled = pyqtSignal(str)  # task_id, 当展开/收起时发出

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

        # 旋转图标
        self._rotating_icon = _RotatingIcon(":/icons/执行中.svg", size=16, parent=self)
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
        self.setStyleSheet(f"""
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

        # Row 0: ● 状态      | 🧠 模型
        # Row 1: 🔧 工具调用  | 📊 上下文
        # Row 2: ⏱ 耗时      |（占位）

        sta_icon, self._status_label_detail = _make_item("●", "运行中", f"{Colors.SYNTAX_STEP}")
        detail_layout.addWidget(sta_icon, 0, 0)
        detail_layout.addWidget(self._status_label_detail, 0, 1)

        mdl_icon, self._model_label_detail = _make_item("🧠", self.model_name or "未指定")
        detail_layout.addWidget(mdl_icon, 0, 2)
        detail_layout.addWidget(self._model_label_detail, 0, 3)

        tool_icon, self._tool_label_detail = _make_item("🔧", "0 次调用")
        detail_layout.addWidget(tool_icon, 1, 0)
        detail_layout.addWidget(self._tool_label_detail, 1, 1)

        ctx_icon, self._ctx_label_detail = _make_item("📊", "—")
        detail_layout.addWidget(ctx_icon, 1, 2)
        detail_layout.addWidget(self._ctx_label_detail, 1, 3)

        elp_icon, self._elapsed_label_detail = _make_item("⏱", "00:00")
        detail_layout.addWidget(elp_icon, 2, 0)
        detail_layout.addWidget(self._elapsed_label_detail, 2, 1)

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
        """标记任务完成"""
        self.is_running = False
        self._is_finished = True
        self.update_elapsed()
        self._rotating_icon.setVisible(False)

        # 更新详情面板的状态
        if success:
            self._status_label_detail.setText("✅ 完成")
            self._status_label_detail.setStyleSheet(f"color: {Colors.SYNTAX_SUCCESS}; background: transparent;")
        else:
            self._status_label_detail.setText("❌ 失败")
            self._status_label_detail.setStyleSheet(f"color: {Colors.SYNTAX_ERROR}; background: transparent;")

        # 替换旋转图标
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
        self.setStyleSheet(f"""
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
        self._status_label_detail.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self._model_label_detail.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self._tool_label_detail.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self._ctx_label_detail.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self._elapsed_label_detail.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")


class SubAgentCompactFloatingWidget(QWidget):
    """子智能体紧凑型悬浮框 - 自动弹出显示运行状态

    特性：
    - 内容自适应高度，最多撑到 _MAX_CARD_HEIGHT px，超出时滚动
    - 每行可点击展开详情（模型、状态、工具调用、上下文用量）
    """

    closed = pyqtSignal()

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

        Args:
            task_id: 任务唯一标识
            agent_name: 子智能体名称
            task_description: 任务描述
            model_name: 使用的模型名称（可选）
        """
        row = _AgentTaskRow(task_id, agent_name, task_description, model_name, self._scroll_content)
        row.toggled.connect(self._on_row_toggled)
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

    def add_tool_call(self, task_id: str, tool_name: str, args: dict = None):
        """记录一次工具调用（更新对应行的工具计数）"""
        row = self._task_rows.get(task_id)
        if row:
            row.increment_tool_count()

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
        content_height = 0
        for _, row in self._task_rows.items():
            sh = row.sizeHint()
            if sh.isValid():
                content_height += sh.height()
            else:
                content_height += 28  # 默认一行高度
        if len(self._task_rows) > 1:
            content_height += self._body_layout.spacing() * (len(self._task_rows) - 1)

        # 卡片总高度 ≈ top margin(6) + header(~24) + spacing(2) + content + bottom margin(6)
        overhead = 6 + 24 + 2 + 6  # = 38
        total_height = overhead + content_height

        if total_height > _MAX_CARD_HEIGHT:
            self.setFixedHeight(_MAX_CARD_HEIGHT)
        else:
            self.setFixedHeight(max(36, total_height))

    # ── 显示/隐藏 ──────────────────────────────────────

    def _start_hide_timer(self):
        """所有任务完成后，延迟自动隐藏"""
        self._hide_timer.start()

    def _auto_hide(self):
        """自动隐藏"""
        self.setVisible(False)
        self.closed.emit()

    def _on_close(self):
        """手动关闭"""
        self._hide_timer.stop()
        self.setVisible(False)
        self.closed.emit()

    def showEvent(self, event):
        super().showEvent(event)
        if self._has_running and not self._rotation_timer.isActive():
            self._start_rotation()
