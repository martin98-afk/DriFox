# -*- coding: utf-8 -*-
import time

import orjson as json
from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QRectF
from PyQt5.QtGui import QPixmap, QPainter
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import (
    QVBoxLayout,
    QLabel,
    QPushButton,
    QHBoxLayout,
    QWidget, QApplication,
)
from qfluentwidgets import SimpleCardWidget

from app.utils.design_tokens import Colors
from app.utils.utils import get_unified_font


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


class ToolFloatingWidget(SimpleCardWidget):
    """工具执行悬浮框组件 - 当工具执行时间过长时显示"""

    cancelled = pyqtSignal()
    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._task_start_time = None
        self._is_running = False
        self._current_tool = None
        self._current_process = None
        self._suppress_visible = False  # 被系统卡片压制，工具调用期间不自行显示
        self._needs_show_after_unsuppress = False  # 工具完成但被压制，解除压制后需要显示
        self._rotation_angle = 0
        self._rotation_timer = QTimer(self)
        self._rotation_timer.timeout.connect(self._update_rotation)
        self._rotating = False
        self._svg_renderer = _RotatingIcon(":/icons/执行中.svg", size=18)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(2000)
        self._hide_timer.timeout.connect(self.hide)
        self._setup_ui()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self._update_style(False)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 12, 6, 12)
        main_layout.setSpacing(8)

        header = QHBoxLayout()
        header.setSpacing(10)

        self.icon_label = QLabel(self)
        self.icon_label.setFixedSize(22, 22)
        self.icon_label.setStyleSheet("background: transparent; border: none;")
        # 初始显示静态 SVG
        self.icon_label.setPixmap(self._svg_renderer.current_pixmap())

        self.tool_name_label = QLabel("", self)
        self.tool_name_label.setFont(get_unified_font(10))
        self._apply_tool_name_style()

        self.title_label = QLabel("正在执行工具", self)
        self.title_label.setFont(get_unified_font(11, True))
        self.title_label.setStyleSheet(f"color: {Colors.REALTIME_ACCENT_WARM};")

        header.addWidget(self.icon_label)
        header.addWidget(self.tool_name_label)
        header.addWidget(self.title_label)
        header.addStretch()

        self.cancel_btn = QPushButton("中止", self)
        self.cancel_btn.setFixedSize(52, 26)
        self.cancel_btn.setCursor(Qt.PointingHandCursor)
        self.cancel_btn.setFont(get_unified_font(9, True))
        self.cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(239, 83, 80, 0.9);
                color: white;
                border: 1px solid rgba(239, 83, 80, 0.3);
                border-radius: 6px;
            }}
            QPushButton:hover {{
                background-color: rgba(239, 83, 80, 1);
                border: 1px solid rgba(239, 83, 80, 0.6);
            }}
            QPushButton:disabled {{
                background-color: rgba(117, 117, 117, 0.6);
                border: 1px solid rgba(117, 117, 117, 0.3);
            }}
        """)
        self.cancel_btn.clicked.connect(self._on_cancel)
        header.addWidget(self.cancel_btn)

        main_layout.addLayout(header)

        self.task_label = QLabel("等待执行...", self)
        self.task_label.setFont(get_unified_font(10))
        self.task_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")
        self.task_label.setWordWrap(True)
        self.task_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        main_layout.addWidget(self.task_label)

    def _apply_tool_name_style(self):
        Colors.refresh()
        self.tool_name_label.setStyleSheet(
            f"color: {Colors.REALTIME_ACCENT}; background-color: {Colors.REALTIME_TAG_BG}; "
            f"padding: 2px 8px; border-radius: 6px;"
        )

    # ── 旋转图标 ──────────────────────────────────────────

    def _start_rotation(self):
        if not self._rotating:
            self._rotating = True
            self._rotation_timer.start(30)  # 30ms ≈ 33fps，流畅旋转

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
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("已中止")
        self.title_label.setText("执行已中止")
        self.title_label.setStyleSheet(f"color: {Colors.REALTIME_ERROR};")
        self._update_style(False)
        self.cancelled.emit()
        self._hide_timer.start()

    def set_suppress_visible(self, suppressed: bool):
        """设置压制状态：系统卡片打开时压制工具卡片显示"""
        old_suppressed = self._suppress_visible
        self._suppress_visible = suppressed
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

    def start_tool(self, tool_name: str, args: dict = None):
        """开始执行工具"""
        # mcp工具去除mcp头
        if tool_name.startswith("mcp__"):
            tool_name = "__".join(tool_name.split("__")[2:])
        # 取消之前的自动隐藏定时器，防止上一个工具的隐藏影响当前工具
        self._hide_timer.stop()

        self._task_start_time = time.time()
        self._is_running = True
        self._current_tool = tool_name
        self._current_process = None

        self.title_label.setText("正在执行工具")
        self.title_label.setStyleSheet(f"color: {Colors.REALTIME_ACCENT_WARM};")
        self._update_style(None)

        self._start_rotation()

        self.tool_name_label.setText(f" {tool_name} ")

        args_preview = ""
        if args:
            # 过滤掉内部字段（_status、_preview_hint 等），只显示实际参数
            display_args = {k: v for k, v in args.items() if not k.startswith("_")}
            if display_args:
                args_str = json.dumps(display_args).decode('utf-8')
                if len(args_str) > 60:
                    args_preview = f"{args_str[:60]}..."
                else:
                    args_preview = f"{args_str}"
            else:
                # 只有内部字段（预览阶段），显示友好的等待消息
                args_preview = "正在准备参数..."

        self.task_label.setText(f"⏳ {args_preview}")

        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("中止")
        self.cancel_btn.setVisible(True)

        if self._suppress_visible:
            # 压制状态下记录任务但不显示
            self.setVisible(False)
        else:
            self.setVisible(True)
            self.raise_()
        QApplication.processEvents()

    def _append_progress(self, text: str):
        self.task_label.setText(text)

    def update_progress(self, message: str):
        """更新进度"""
        self.task_label.setText(f"⏳ {message}")

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

        # 清除旋转 pixmap，显示结果表情
        self.icon_label.setPixmap(QPixmap())
        self.icon_label.setFixedSize(24, 24)
        self.icon_label.setFont(get_unified_font(14))
        self.icon_label.setStyleSheet("background: transparent; border: none;")
        self.icon_label.setText("✅" if success else "❌")

        self._update_style(success)

        if success:
            self.title_label.setText("执行完成")
            self.title_label.setStyleSheet(f"color: {Colors.REALTIME_SUCCESS};")
            self.task_label.setText("✓ 工具执行成功")
            self.cancel_btn.setVisible(False)  # 成功时立即隐藏中止按钮
        else:
            self.title_label.setText("执行失败")
            self.title_label.setStyleSheet(f"color: {Colors.REALTIME_ERROR};")
            error_msg = result if result else "执行失败"
            self.task_label.setText(f"✗ {error_msg[:50]}")

        # 工具完成时根据压制状态决定是否显示
        if self._suppress_visible:
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
        self._needs_show_after_unsuppress = False  # 重置待显示标志
        self.setVisible(False)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setText("中止")
        self.icon_label.setPixmap(self._svg_renderer.current_pixmap())
        self.icon_label.setText("")
        self.icon_label.setFixedSize(22, 22)
        self.icon_label.setStyleSheet("background: transparent; border: none;")
        self.title_label.setText("正在执行工具")
        self.title_label.setStyleSheet(f"color: {Colors.REALTIME_ACCENT_WARM};")
        self._update_style(None)

    def show_if_needed(self, elapsed: float):
        """根据耗时决定是否显示（不考虑压制状态）"""
        if elapsed > 3:
            self.setVisible(True)

    def show_when_ready(self):
        """统一控制显示时机（考虑压制状态）"""
        if not self._suppress_visible:
            self.setVisible(True)

    def _update_style(self, success: bool = None):
        """更新卡片样式，根据状态改变边框颜色"""
        Colors.refresh()
        if success is None:
            # 运行中
            border_color = Colors.REALTIME_ACCENT_WARM
        elif success:
            # 成功
            border_color = Colors.REALTIME_SUCCESS
        else:
            # 失败
            border_color = Colors.REALTIME_ERROR

        self.setStyleSheet(f"""
            CardWidget {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {border_color};
                border-radius: 10px;
            }}
        """)

    def refresh_style(self):
        """响应主题切换"""
        Colors.refresh()
        self._apply_tool_name_style()
        # 刷新标题颜色（根据当前状态）
        if self._is_running:
            self.title_label.setStyleSheet(f"color: {Colors.REALTIME_ACCENT_WARM};")
        elif self.title_label.text() == "执行完成":
            self.title_label.setStyleSheet(f"color: {Colors.REALTIME_SUCCESS};")
        elif self.title_label.text() == "执行失败":
            self.title_label.setStyleSheet(f"color: {Colors.REALTIME_ERROR};")
        elif self.title_label.text() == "执行已中止":
            self.title_label.setStyleSheet(f"color: {Colors.REALTIME_ERROR};")
        else:
            self.title_label.setStyleSheet(f"color: {Colors.REALTIME_ACCENT_WARM};")
        self.task_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")
        # 刷新边框样式
        if self._is_running:
            self._update_style(None)
        elif self.title_label.text() == "执行完成":
            self._update_style(True)
        elif self.title_label.text() in ("执行失败", "执行已中止"):
            self._update_style(False)
        else:
            self._update_style(None)

    def set_opacity(self, opacity: float):
        """设置透明度，用于响应全局透明度变化"""
        Colors.refresh()
        bg = Colors.REALTIME_BG
        if bg.startswith("rgba("):
            # 最小 alpha 为 1，避免完全透明导致卡片"消失"
            alpha = max(1, int(opacity * 255))
            bg = bg.rsplit(",", 1)[0] + f", {alpha})"
        # 根据当前状态保持边框颜色
        if self._is_running:
            border_color = Colors.REALTIME_ACCENT_WARM
        elif self.title_label.text() == "执行完成":
            border_color = Colors.REALTIME_SUCCESS
        else:
            border_color = Colors.REALTIME_ERROR
        self.setStyleSheet(f"""
            CardWidget {{
                background-color: {bg};
                border: 1px solid {border_color};
                border-radius: 10px;
            }}
        """)
