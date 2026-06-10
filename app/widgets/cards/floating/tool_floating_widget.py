# -*- coding: utf-8 -*-
import re
import time

import orjson as json
from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QRectF
from PyQt5.QtGui import QPixmap, QPainter
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QLabel,
    QPushButton,
    QHBoxLayout,
    QWidget, QApplication, QSizePolicy,
)

from app.utils.design_tokens import Colors
from app.utils.utils import get_unified_font
from app.widgets.cards.floating.command_card import _ElidedLabel


class _RotatingIcon(QWidget):
    """用 QPainter 原地旋转 SVG，消除 QPixmap.transform 的 bounding-box 抖动"""

    def __init__(self, svg_path: str, size: int = 18, parent=None):
        super().__init__(parent)
        self._renderer = QSvgRenderer(svg_path)
        self._size = size
        self._angle = 0
        self.setFixedSize(size, size)
        # 预渲染一帧到 QPixmap，用于 QLabel 显示
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


class ToolFloatingWidget(QWidget):
    """工具执行悬浮框组件 - 当工具执行时间过长时显示"""

    cancelled = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task_start_time = None
        self._is_running = False
        self._current_tool = None
        self._current_process = None
        self._is_hide_suppressed = False  # 被其他卡片压制，工具调用期间不自行显示
        self._needs_show_after_unsuppress = False  # 工具完成但被压制，解除压制后需要显示
        self._rotation_angle = 0
        self._rotation_timer = QTimer(self)
        self._rotation_timer.timeout.connect(self._update_rotation)
        self._rotating = False
        self._svg_renderer = _RotatingIcon(":/icons/执行中.svg", size=18)
        self._svg_success_pixmap = QPixmap(18, 18)
        self._svg_success_pixmap.fill(Qt.transparent)
        _svg_success_renderer = QSvgRenderer(":/icons/成功.svg")
        p = QPainter(self._svg_success_pixmap)
        _svg_success_renderer.render(p, QRectF(0, 0, 18, 18))
        p.end()
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(2000)
        self._hide_timer.timeout.connect(self.hide)
        self._setup_ui()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(36)  # 一行高度
        self._update_style(None)

        main_layout = QHBoxLayout(self)  # 横向布局
        main_layout.setContentsMargins(12, 4, 12, 4)
        main_layout.setSpacing(8)

        # 状态图标
        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(18, 18)
        self.icon_label.setStyleSheet("background: transparent; border: none;")
        self.icon_label.setPixmap(self._svg_renderer.current_pixmap())
        main_layout.addWidget(self.icon_label)

        # 工具名称标签
        self.tool_name_label = QLabel("", self)
        self.tool_name_label.setFont(get_unified_font(10))
        self._apply_tool_name_style()
        main_layout.addWidget(self.tool_name_label)

        # 参数/结果内容（一行显示）
        self.task_label = _ElidedLabel("", self)
        self.task_label.setFont(get_unified_font(10))
        self.task_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")
        self.task_label.setWordWrap(False)
        self.task_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.task_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        main_layout.addWidget(self.task_label, 1)  # 弹性拉伸

        # 隐藏 title_label 和 cancel_btn（不再使用）
        self.title_label = QLabel("", self)
        self.cancel_btn = QPushButton("", self)
        self.cancel_btn.setVisible(False)

    def _apply_tool_name_style(self):
        Colors.refresh()
        self.tool_name_label.setStyleSheet(
            f"color: {Colors.REALTIME_ACCENT}; background-color: {Colors.REALTIME_TAG_BG}; "
            f"padding: 0px 0px; border-radius: 5px;"
        )

    # ── 旋转图标 ──────────────────────────────────────────

    def _start_rotation(self):
        if not self._rotating:
            self._rotating = True
            self._rotation_timer.start(30)

    def _stop_rotation(self):
        self._rotating = False
        self._rotation_timer.stop()

    def _update_rotation(self):
        self._rotation_angle = (self._rotation_angle + 12) % 360
        self._svg_renderer.set_angle(self._rotation_angle)
        self.icon_label.setPixmap(self._svg_renderer.current_pixmap())

    def _on_close(self):
        self.setVisible(False)
        self.closed.emit()

    def _on_cancel(self):
        self._is_running = False
        self._stop_rotation()
        self.icon_label.setText("⏹")
        self._update_style(False)
        self.task_label.setText("已中止")
        self.cancelled.emit()
        self._hide_timer.start()

    def set_suppress_visible(self, suppressed: bool):
        """设置压制状态：其他卡片打开时压制工具卡片显示"""
        old_suppressed = self._is_hide_suppressed
        self._is_hide_suppressed = suppressed
        if suppressed and not old_suppressed:
            if self.isVisible() or self._is_running:
                self._needs_show_after_unsuppress = True
        elif old_suppressed and not suppressed:
            if self._needs_show_after_unsuppress or self._is_running:
                self.setVisible(True)
                self.raise_()
                self._needs_show_after_unsuppress = False
                if self.title_label.text() in ("执行完成", "执行失败"):
                    self._hide_timer.start()

    def set_process(self, process):
        """设置当前进程以便中止"""
        self._current_process = process

    def _flatten_text(self, text: str) -> str:
        """将换行符替换为空格，用于单行显示"""
        return re.sub(r"[\n\r]+", " ", text)

    def start_tool(self, tool_name: str, args: dict = None):
        """开始执行工具"""
        if tool_name.startswith("mcp__"):
            tool_name = "__".join(tool_name.split("__")[2:])
        self._hide_timer.stop()

        self._task_start_time = time.time()
        self._is_running = True
        self._current_tool = tool_name
        self._current_process = None

        self._update_style(None)
        self._start_rotation()

        self.tool_name_label.setText(f" {tool_name} ")

        # 参数预览
        args_preview = ""
        if args:
            display_args = {k: v for k, v in args.items() if not k.startswith("_")}
            if display_args:
                args_str = json.dumps(display_args).decode('utf-8')
                if len(args_str) > 80:
                    args_preview = f"{args_str[:80]}..."
                else:
                    args_preview = f"{args_str}"
            else:
                args_preview = "..."

        self.task_label.setText(self._flatten_text(args_preview))

        if self._is_hide_suppressed:
            self.setVisible(False)
        else:
            self.setVisible(True)
            self.raise_()
        QApplication.processEvents()

    def _append_progress(self, text: str):
        self.task_label.setText(self._flatten_text(text))

    def update_progress(self, message: str):
        """更新进度"""
        self.task_label.setText(self._flatten_text(message))

    def add_tool_call(self, tool_name: str, args: dict = None):
        """添加工具调用"""
        self.tool_name_label.setText(f" {tool_name} ")

    def add_tool_result(self, result: str, success: bool = True):
        """添加工具结果"""
        pass

    def finish_tool(self, result: str = None, success: bool = True):
        """完成工具执行"""
        self._is_running = False
        self._current_process = None
        self._stop_rotation()

        self.icon_label.setFixedSize(18, 18)
        self.icon_label.setStyleSheet("background: transparent; border: none;")
        self.icon_label.setPixmap(self._svg_success_pixmap)

        self._update_style(success)

        if success:
            self.task_label.setText(self._flatten_text(result) or "")
        else:
            error_msg = result if result else ""
            self.task_label.setText(self._flatten_text(error_msg[:80]))

        if self._is_hide_suppressed:
            self._needs_show_after_unsuppress = True
            self.setVisible(False)
        else:
            self.setVisible(True)
            self.raise_()
            self._hide_timer.start()

    def is_cancelled(self) -> bool:
        """检查是否已被中止"""
        return not self._is_running

    def clear(self):
        """清空显示"""
        self._hide_timer.stop()
        self._task_start_time = None
        self._is_running = False
        self._current_tool = None
        self._current_process = None
        self._stop_rotation()
        self._rotation_angle = 0
        self._needs_show_after_unsuppress = False
        self.setVisible(False)
        self.icon_label.setPixmap(self._svg_renderer.current_pixmap())
        self.icon_label.setText("")
        self.icon_label.setFixedSize(18, 18)
        self.icon_label.setStyleSheet("background: transparent; border: none;")
        self.tool_name_label.setText("")
        self.task_label.setText("")
        self._update_style(None)

    def show_if_needed(self, elapsed: float):
        """根据耗时决定是否显示（不考虑压制状态）"""
        if elapsed > 3:
            self.setVisible(True)

    def show_when_ready(self):
        """统一控制显示时机（考虑压制状态）"""
        if not self._is_hide_suppressed:
            self.setVisible(True)

    def _update_style(self, success: bool = None):
        """更新卡片样式，根据状态改变边框颜色"""
        Colors.refresh()
        if success is None:
            border_color = Colors.REALTIME_BORDER if not self._is_running else Colors.REALTIME_ACCENT_WARM
        elif success:
            border_color = Colors.REALTIME_SUCCESS
        else:
            border_color = Colors.REALTIME_ERROR

        self.setStyleSheet(f"""
            ToolFloatingWidget {{
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
        self._apply_tool_name_style()
        self.task_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")
        if self._is_running:
            self._update_style(None)
        else:
            self._update_style(True)

    def set_opacity(self, opacity: float):
        """设置透明度，用于响应全局透明度变化"""
        Colors.refresh()
        bg = Colors.REALTIME_BG
        if bg.startswith("rgba("):
            alpha = max(1, int(opacity * 255))
            bg = bg.rsplit(",", 1)[0] + f", {alpha})"
        if self._is_running:
            border_color = Colors.REALTIME_ACCENT_WARM
        else:
            border_color = Colors.REALTIME_SUCCESS
        self.setStyleSheet(f"""
            ToolFloatingWidget {{
                background-color: {bg};
                border: 1px solid {border_color};
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
            }}
        """)
