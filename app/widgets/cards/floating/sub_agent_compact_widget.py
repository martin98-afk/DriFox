# -*- coding: utf-8 -*-
"""
子智能体紧凑型悬浮框 - task_batch 触发时自动弹出
类似 ToolFloatingWidget 的风格：每行一个子智能体，显示旋转图标 + agent名 + 任务描述
与 SubAgentFloatingWidget（详细日志面板）完全独立
"""
import time
from typing import Dict

from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QRectF
from PyQt5.QtGui import QPixmap, QPainter
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QApplication, QFrame,
)

from app.utils.design_tokens import Colors
from app.utils.utils import get_unified_font, get_font_family_css


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
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        cx, cy = self._size / 2, self._size / 2
        p.translate(cx, cy)
        p.rotate(self._angle)
        p.translate(-cx, -cy)
        self._renderer.render(p, QRectF(0, 0, self._size, self._size))
        p.end()

    def current_pixmap(self) -> QPixmap:
        return self._last_pixmap

    def paintEvent(self, event):
        p = QPainter(self)
        p.drawPixmap(0, 0, self._last_pixmap)
        p.end()


class _AgentTaskRow(QFrame):
    """单行子智能体任务"""

    def __init__(self, task_id: str, agent_name: str, task_desc: str, parent=None):
        super().__init__(parent)
        self.task_id = task_id
        self.agent_name = agent_name
        self.task_desc = task_desc
        self.is_running = True
        self._tool_count = 0
        self._start_time = time.time()
        self._is_finished = False

        # 旋转图标
        self._rotating_icon = _RotatingIcon(":/icons/执行中new.svg", size=16, parent=self)
        # 成功后显示的静态图标
        self._success_pixmap = QPixmap(16, 16)
        self._success_pixmap.fill(Qt.transparent)
        _svg_success = QSvgRenderer(":/icons/成功.svg")
        _p = QPainter(self._success_pixmap)
        _svg_success.render(_p, QRectF(0, 0, 16, 16))
        _p.end()

        self._setup_ui()

    def _setup_ui(self):
        self.setFixedHeight(28)
        self.setStyleSheet("QFrame { background: transparent; border: none; }")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 旋转图标（直接作为子控件，paintEvent 自己画）
        self._rotating_icon.setFixedSize(16, 16)
        self._rotating_icon.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self._rotating_icon)

        # Agent 名称标签
        self.agent_label = QLabel(f" {self.agent_name} ", self)
        self.agent_label.setFont(get_unified_font(9))
        Colors.refresh()
        self.agent_label.setStyleSheet(
            f"color: {Colors.REALTIME_ACCENT}; background-color: {Colors.REALTIME_TAG_BG}; "
            f"padding: 0px 0px; border-radius: 4px;"
        )
        layout.addWidget(self.agent_label)

        # 任务描述
        desc = self.task_desc
        if len(desc) > 60:
            desc = desc[:60] + "..."
        self.desc_label = QLabel(desc, self)
        self.desc_label.setFont(get_unified_font(9))
        self.desc_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        self.desc_label.setWordWrap(False)
        self.desc_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.desc_label, 1)

        # 工具调用次数
        self.tool_count_label = QLabel("🔧0", self)
        self.tool_count_label.setFont(get_unified_font(9))
        self.tool_count_label.setStyleSheet(
            f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;"
        )
        layout.addWidget(self.tool_count_label)

        # 耗时
        self.time_label = QLabel("⏱00:00", self)
        self.time_label.setFont(get_unified_font(9))
        self.time_label.setStyleSheet(
            f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;"
        )
        layout.addWidget(self.time_label)

    def set_rotation_angle(self, angle: float):
        """设置旋转角度（由父组件统一驱动）"""
        if self.is_running:
            self._rotating_icon.set_angle(angle)

    def finish(self, success: bool = True):
        """标记任务完成"""
        self.is_running = False
        self._is_finished = True
        # 冻结最终耗时
        self.update_elapsed()
        self._rotating_icon.setVisible(False)
        if success:
            self._success_label = QLabel(self)
            self._success_label.setFixedSize(16, 16)
            self._success_label.setPixmap(self._success_pixmap)
            self._success_label.setStyleSheet("background: transparent; border: none;")
            # 替换旋转图标位置
            idx = self.layout().indexOf(self._rotating_icon)
            self.layout().removeWidget(self._rotating_icon)
            self.layout().insertWidget(idx, self._success_label)
        else:
            self._error_label = QLabel("❌", self)
            self._error_label.setFixedSize(16, 16)
            self._error_label.setStyleSheet("font-size: 14px; background: transparent; border: none;")
            idx = self.layout().indexOf(self._rotating_icon)
            self.layout().removeWidget(self._rotating_icon)
            self.layout().insertWidget(idx, self._error_label)

    def increment_tool_count(self):
        """工具调用次数 +1"""
        self._tool_count += 1
        self.tool_count_label.setText(f"🔧{self._tool_count}")

    def update_elapsed(self):
        """更新已用时间显示（每秒由父组件定时器驱动）"""
        if self._is_finished:
            return
        elapsed = int(time.time() - self._start_time)
        mins = elapsed // 60
        secs = elapsed % 60
        self.time_label.setText(f"⏱{mins:02d}:{secs:02d}")

    def clear_icon(self):
        """清空图标"""
        self._rotating_icon.setVisible(False)
        if hasattr(self, '_success_label'):
            self._success_label.setVisible(False)
        if hasattr(self, '_error_label'):
            self._error_label.setVisible(False)


class SubAgentCompactFloatingWidget(QWidget):
    """子智能体紧凑型悬浮框 - 自动弹出显示运行状态"""

    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task_rows: Dict[str, _AgentTaskRow] = {}  # task_id -> row widget
        self._rotation_angle = 0
        self._rotation_timer = QTimer(self)
        self._rotation_timer.timeout.connect(self._update_all_rotations)
        self._has_running = False
        self._batch_started: bool = False  # 当前批次是否已开始（由 main_widget 管理）
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(2000)
        self._hide_timer.timeout.connect(self._auto_hide)

        # 每秒更新一次各行的已用时间
        self._time_timer = QTimer(self)
        self._time_timer.timeout.connect(self._update_all_times)
        self._time_timer.setInterval(1000)

        self._setup_ui()

    # ── UI 初始化 ──────────────────────────────────────

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
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

        # ── 任务列表容器 ──
        self._body_layout = QVBoxLayout()
        self._body_layout.setContentsMargins(24, 0, 0, 0)  # 缩进，与标题对齐
        self._body_layout.setSpacing(2)
        main_layout.addLayout(self._body_layout)

        # 初始化固定高度
        self._update_height()

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
        running = self._has_running
        self._apply_style(running if running else None)
        # 刷新每行的 agent 标签样式
        for row in self._task_rows.values():
            row.agent_label.setStyleSheet(
                f"color: {Colors.REALTIME_ACCENT}; background-color: {Colors.REALTIME_TAG_BG}; "
                f"padding: 0px 0px; border-radius: 4px;"
            )
            row.desc_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
            row.tool_count_label.setStyleSheet(
                f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
            row.time_label.setStyleSheet(
                f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")

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

    def add_task(self, task_id: str, agent_name: str, task_description: str):
        """添加一个新的子智能体任务行"""
        row = _AgentTaskRow(task_id, agent_name, task_description, self)
        self._task_rows[task_id] = row
        self._body_layout.addWidget(row)

        self._has_running = True
        self._start_rotation()
        self._apply_style(True)
        self._update_status_text()
        self._update_height()

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
        self._update_height()

    def add_tool_call(self, task_id: str, tool_name: str, args: dict = None):
        """记录一次工具调用（更新对应行的工具计数）"""
        row = self._task_rows.get(task_id)
        if row:
            row.increment_tool_count()

    def clear(self):
        """清空所有任务"""
        self._stop_rotation()
        self._hide_timer.stop()

        # 移除所有行
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self._task_rows.clear()
        self._has_running = False
        self._rotation_angle = 0
        self._apply_style(None)
        self._update_status_text()
        self._update_height()
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

    def _update_height(self):
        """根据行数动态计算高度"""
        row_count = len(self._task_rows)
        # 标题行 ~28px + 每行 ~28px + padding 12px + spacing
        height = 28 + 12 + row_count * 28 + max(0, row_count - 1) * 2
        self.setFixedHeight(height)

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
