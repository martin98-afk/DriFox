# -*- coding: utf-8 -*-
import time
import orjson as json
from typing import Dict

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtGui import QFont, QTextCharFormat, QColor
from PyQt5.QtWidgets import (
    QVBoxLayout,
    QLabel,
    QPushButton,
    QHBoxLayout,
    QTextEdit,
    QFrame, QSizePolicy,
)
from qfluentwidgets import SegmentedWidget, BodyLabel
from qfluentwidgets import SimpleCardWidget

from app.utils.design_tokens import Colors
from app.utils.utils import get_unified_font, get_font_family_css
from app.utils.design_tokens import scale_font_size


class SubTaskLogWidget(QFrame):
    """单个子任务的日志显示组件"""

    def __init__(self, task_id: str, agent_name: str, task_desc: str, parent=None):
        super().__init__(parent)
        self.task_id = task_id
        self.agent_name = agent_name
        self.task_desc = task_desc
        self._start_time = time.time()  # 默认当前时间，运行时会被正确设置
        self._step_count = 0
        self._tool_call_count = 0
        self._is_finished = False  # 标记是否已完成
        self._setup_ui()

    def set_start_time(self, start_time: float):
        """设置开始时间（用于显示历史任务的正确时长）"""
        self._start_time = start_time

    def set_elapsed_seconds(self, elapsed_seconds: int):
        """设置已消耗的时间（用于显示历史任务的正确时长）"""
        # 计算出任务的实际开始时间
        import time
        self._start_time = time.time() - elapsed_seconds

    def mark_finished(self):
        """标记任务已完成，停止时间更新"""
        self._is_finished = True

    def _setup_ui(self):
        self.setStyleSheet("""
            QFrame {
                background-color: transparent;
                border: none;
                padding: 2px 0;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # 任务状态行：icon + [子智能体名] 描述 + 时间
        status_layout = QHBoxLayout()
        status_layout.setSpacing(6)

        self.status_icon = QLabel("⏳")
        self.status_icon.setFont(get_unified_font(10))
        status_layout.addWidget(self.status_icon)

        # 任务描述（包含子智能体名称）
        desc = self.task_desc[:35] + "..." if len(self.task_desc) > 35 else self.task_desc
        self.status_label = BodyLabel(f"[{self.agent_name}] {desc}", self)
        self.status_label.setFont(get_unified_font(9))
        self.status_label.setStyleSheet("color: #ccc;")
        self.status_label.setMinimumWidth(180)
        status_layout.addWidget(self.status_label, 1)

        self.time_label = BodyLabel("00:00", self)
        self.time_label.setFont(get_unified_font(9))
        self.time_label.setStyleSheet("color: #666;")
        status_layout.addWidget(self.time_label)

        layout.addLayout(status_layout)

        # 日志内容
        self.log_text = QTextEdit(self)
        self.log_text.setFont(get_unified_font(8))
        self.log_text.setStyleSheet(f"""
            QTextEdit {{
                background-color: transparent;
                color: {Colors.TEXT_PRIMARY};
                border: none;
                border-radius: 3px;
                padding: 4px;
            }}
        """)
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(100)
        self.log_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.log_text)

        # 初始化日志格式
        self._init_log_format()

    def _init_log_format(self):
        """初始化日志文本格式"""
        self._normal_fmt = QTextCharFormat()
        self._normal_fmt.setForeground(QColor(Colors.TEXT_PRIMARY))

        self._step_fmt = QTextCharFormat()
        self._step_fmt.setForeground(QColor(Colors.SYNTAX_STEP))
        self._step_fmt.setFontWeight(QFont.Bold)

        self._tool_fmt = QTextCharFormat()
        self._tool_fmt.setForeground(QColor(Colors.SYNTAX_TOOL))

        self._tool_success_fmt = QTextCharFormat()
        self._tool_success_fmt.setForeground(QColor(Colors.SYNTAX_SUCCESS))

        self._tool_error_fmt = QTextCharFormat()
        self._tool_error_fmt.setForeground(QColor(Colors.SYNTAX_ERROR))

        self._result_fmt = QTextCharFormat()
        self._result_fmt.setForeground(QColor(Colors.SYNTAX_RESULT))

        self._error_fmt = QTextCharFormat()
        self._error_fmt.setForeground(QColor(Colors.SYNTAX_ERROR))
        self._error_fmt.setFontWeight(QFont.Bold)

    def _append_log(self, text: str, fmt: QTextCharFormat = None):
        """追加日志（带格式）"""
        cursor = self.log_text.textCursor()
        cursor.movePosition(cursor.End)
        if fmt:
            cursor.setCharFormat(fmt)
        cursor.insertText(text + "\n")
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()

    def _update_time(self):
        """更新时间显示（已完成的任务不再更新时间）"""
        if self._is_finished:
            # 已完成的任务保持当前显示的时间，不再更新
            return
        elapsed = int(time.time() - self._start_time)
        mins = elapsed // 60
        secs = elapsed % 60
        self.time_label.setText(f"{mins:02d}:{secs:02d}")

    def update_progress(self, message: str):
        """更新进度"""
        self._step_count += 1
        self._append_log(f"📌 {message}", self._step_fmt)
        self._update_time()

    def add_thinking(self, thinking: str):
        """添加思考内容"""
        if not thinking:
            return
        preview = thinking[:150] + "..." if len(thinking) > 150 else thinking
        self._append_log(f"💭 思考: {preview}", self._result_fmt)

    def add_ai_response(self, response: str):
        """添加 AI 回复"""
        if not response:
            return
        preview = response[:200] + "..." if len(response) > 200 else response
        self._append_log(f"🤖 AI: {preview}", self._normal_fmt)

    def add_tool_call(self, tool_name: str, args: dict = None):
        """添加工具调用"""
        self._tool_call_count += 1
        tool_info = f"🔧 工具: {tool_name}"
        if args:
            args_str = json.dumps(args, option=json.OPT_INDENT_2).decode('utf-8')[:80]
            self._append_log(f"{tool_info}\n   └ {args_str}", self._tool_fmt)
        else:
            self._append_log(tool_info, self._tool_fmt)

    def add_tool_result(self, tool_name: str, result: str, success: bool = True):
        """添加工具结果"""
        status = "✅" if success else "❌"
        result_preview = str(result)[:150] if result else ""
        if len(str(result)) > 150:
            result_preview += "..."
        fmt = self._tool_success_fmt if success else self._tool_error_fmt
        self._append_log(f"{status} {tool_name}: {result_preview}", fmt)

    def finish_task(self, result: str = None, success: bool = True):
        """完成任务"""
        self._is_finished = True
        elapsed = int(time.time() - self._start_time)
        mins = elapsed // 60
        secs = elapsed % 60

        if success:
            self.status_icon.setText("✅")
            self._append_log(f"\n✓ 完成 | {mins:02d}:{secs:02d} | 工具 {self._tool_call_count} 次", self._step_fmt)
            if result:
                result_preview = result[:200] + "..." if len(result) > 200 else result
                self._append_log(f"📋 {result_preview}", self._result_fmt)
        else:
            self.status_icon.setText("❌")
            error_msg = result if result else "执行失败"
            self._append_log(f"\n✗ 失败 | {error_msg[:100]}", self._error_fmt)

    def clear(self):
        """清空日志"""
        self.log_text.clear()
        self._step_count = 0
        self._tool_call_count = 0


class SubAgentFloatingWidget(SimpleCardWidget):
    """子智能体悬浮框组件"""

    closed = pyqtSignal()
    task_selected = pyqtSignal(str)  # 发出选中的任务ID

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: Dict[str, SubTaskLogWidget] = {}  # task_id -> widget
        self._task_labels: Dict[str, str] = {}  # task_id -> label text
        self._segment_items: Dict[str, object] = {}  # task_id -> segment item (用于更新按钮文字)
        self._active_task_id: str = None
        self._batch_started: bool = False  # 当前批次是否已开始
        self._auto_hide_timer: QTimer = None  # 自动隐藏定时器（用于取消）
        self._timer: QTimer = None
        self._auto_showed: bool = False  # 是否自动弹出的面板
        self._was_auto_showed: bool = False  # 是否曾经自动弹出过（用于决定是否自动关闭）
        self._setup_ui()
        self._start_timer()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.setMinimumHeight(100)
        self.setMaximumHeight(400)
        self._apply_card_style()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 8, 12, 8)
        main_layout.setSpacing(6)

        # 顶部标题栏
        header = QHBoxLayout()
        header.setSpacing(8)

        title = QLabel("🤖 子智能体", self)
        title.setFont(get_unified_font(11, True))
        title.setStyleSheet(f"color: {Colors.REALTIME_ACCENT};")
        header.addWidget(title)

        self.task_count_label = QLabel("0 个任务", self)
        self.task_count_label.setFont(get_unified_font(10))
        self.task_count_label.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")
        header.addWidget(self.task_count_label)

        header.addStretch()

        close_btn = QPushButton("✕", self)
        close_btn.setFixedSize(20, 20)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: transparent;
                color: {Colors.TEXT_MUTED};
                border: none;
                {get_font_family_css()}
                font-size: {scale_font_size(12)}px;
            }}
            QPushButton:hover {{
                color: {Colors.TEXT_PRIMARY};
                background-color: {Colors.CONTENT_BG};
                border-radius: 3px;
            }}
        """)
        close_btn.clicked.connect(self._on_close)
        header.addWidget(close_btn)

        main_layout.addLayout(header)

        # Segment 切换栏
        self.segment_widget = SegmentedWidget(self)
        self.segment_widget.currentItemChanged.connect(self._on_segment_changed)
        main_layout.addWidget(self.segment_widget)

        # 任务日志容器
        self.log_container = QFrame(self)
        self.log_container.setStyleSheet("background-color: transparent;")
        self.log_container_layout = QVBoxLayout(self.log_container)
        self.log_container_layout.setContentsMargins(0, 4, 0, 0)
        self.log_container_layout.setSpacing(4)
        self.log_container_layout.setStretch(0, 1)  # 日志容器占据所有剩余空间
        main_layout.addWidget(self.log_container, 1)

        # 空状态提示
        self.empty_label = BodyLabel("暂无运行中的子智能体任务", self)
        self.empty_label.setFont(get_unified_font(10))
        self.empty_label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; padding: 20px;")
        self.empty_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(self.empty_label)

    def _start_timer(self):
        """启动定时器更新任务时间"""
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_all_times)
        self._timer.start(1000)

    def _update_all_times(self):
        """更新所有任务的时间显示"""
        for task_widget in self._tasks.values():
            task_widget._update_time()

    def _on_segment_changed(self, task_id: str):
        """切换任务标签"""
        if task_id and task_id in self._tasks:
            self._show_task_log(task_id)
            self._auto_showed = False  # 手动切换后不再是自动弹出的
            self.task_selected.emit(task_id)

    def _show_task_log(self, task_id: str):
        """显示指定任务的日志"""
        if task_id not in self._tasks:
            return

        # 移除所有现有的 widget
        while self.log_container_layout.count() > 0:
            item = self.log_container_layout.takeAt(0)
            if item.widget():
                item.widget().hide()

        # 添加当前任务 widget
        widget = self._tasks[task_id]
        self.log_container_layout.addWidget(widget)
        widget.show()

        self._active_task_id = task_id

    def _apply_card_style(self):
        Colors.refresh()
        self.setStyleSheet(f"""
            SimpleCardWidget {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-radius: 8px;
            }}
        """)

    def refresh_style(self):
        """响应主题切换"""
        self._apply_card_style()
        # 刷新标题颜色
        for child in self.findChildren(QLabel):
            text = child.text()
            if text == "🤖 子智能体":
                child.setStyleSheet(f"color: {Colors.REALTIME_ACCENT};")
            elif child == self.task_count_label:
                child.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY};")

    def _on_close(self):
        # 取消自动隐藏定时器
        if self._auto_hide_timer:
            self._auto_hide_timer.stop()
            self._auto_hide_timer = None
        self.setVisible(False)
        self._batch_started = False
        self._auto_showed = False
        self.closed.emit()

    def add_task(self, task_id: str, agent_name: str, task_desc: str):
        """添加新任务"""
        # 创建任务日志组件
        task_widget = SubTaskLogWidget(task_id, agent_name, task_desc, self.log_container)

        self._tasks[task_id] = task_widget

        # 更新 Segment，使用 task_id 作为唯一标识
        task_index = len(self._tasks)
        item = self.segment_widget.addItem(task_id, f"任务{task_index}")
        self._segment_items[task_id] = item

        # 同步更新 _task_labels
        self._task_labels[task_id] = f"任务{task_index}"

        # 只在添加第一个任务时设置当前项为第一个任务
        # 后续添加的任务不改变当前选中项，保持第一个任务被选中
        if len(self._tasks) == 1:
            self.segment_widget.setCurrentItem(task_id)

        # 更新计数
        self._update_task_count()

        # 标记为自动弹出的面板
        self._auto_showed = True
        self._was_auto_showed = True  # 标记曾经自动弹出过

        # 显示日志（只显示第一个任务）
        first_task_id = list(self._tasks.keys())[0]
        self._show_task_log(first_task_id)
        self.setVisible(True)

    def show_task_from_data(self, task_data: dict, clear_first: bool = True):
        """
        从外部数据显示任务日志（用于查看已完成任务的历史日志）。

        Args:
            task_data: {
                "task_id": str,
                "summary": {
                    "task_id": str,
                    "agent_name": str,
                    "task_description": str,
                    "tool_call_count": int,
                    "elapsed_seconds": int,
                    "result": str,
                    "error": str,
                },
                "logs": [
                    {"type": "progress"|"thinking"|"ai_response"|"tool_call"|"tool_result"|"finish", "content": str, ...},
                    ...
                ],
                "status": "running"|"finished"
            }
            clear_first: 是否先清空面板，默认 True
        """
        task_id = task_data.get("task_id", "unknown")
        summary = task_data.get("summary", {})
        logs = task_data.get("logs", [])
        status = task_data.get("status", "finished")

        # 清空现有面板（仅首次调用时）
        if clear_first:
            self.clear()

        # 获取任务信息
        agent_name = summary.get("agent_name", "未知")
        task_desc = summary.get("task_description", summary.get("task_id", ""))
        result = summary.get("result", "")
        error = summary.get("error", "")
        elapsed_seconds = summary.get("elapsed_seconds", 0)

        # 创建任务日志组件（复用现有样式）
        task_widget = SubTaskLogWidget(task_id, agent_name, task_desc, self.log_container)
        self._tasks[task_id] = task_widget

        # 设置历史任务的正确时长（用于显示）
        if elapsed_seconds > 0:
            task_widget.set_elapsed_seconds(elapsed_seconds)
        task_widget.mark_finished()  # 标记为已完成，停止时间更新

        # 更新 Segment（与 add_task 保持一致的命名）
        task_index = len(self._tasks)
        item = self.segment_widget.addItem(task_id, f"任务{task_index}")
        self._segment_items[task_id] = item
        self._task_labels[task_id] = f"任务{task_index}"

        # 只在添加第一个任务时设置当前项
        if len(self._tasks) == 1:
            self.segment_widget.setCurrentItem(task_id)

        # 重放日志（与运行时的实时日志格式一致）
        for log in logs:
            log_type = log.get("type", "")
            content = log.get("content", "")

            if log_type == "progress":
                task_widget.update_progress(content)
            elif log_type == "thinking":
                task_widget.add_thinking(content)
            elif log_type == "ai_response":
                task_widget.add_ai_response(content)
            elif log_type == "tool_call":
                args = log.get("args")
                task_widget.add_tool_call(content, args)
            elif log_type == "tool_result":
                result_val = log.get("result", "")
                success = log.get("success", True)
                task_widget.add_tool_result(content, result_val, success)

        # 如果有最终结果（只有已完成的任务才有）
        if result:
            task_widget.finish_task(result, success=True)
        elif error:
            task_widget.finish_task(error, success=False)

        # 调用父方法更新 Segment 标签（只调用一次，避免重复）
        # 手动查看时重置批次状态，避免与自动触发冲突
        self._batch_started = False  # 重置批次标记，允许新的自动触发清空面板
        if result:
            self.finish_task(task_id, result, True)
        elif error:
            self.finish_task(task_id, error, False)

        # 更新计数
        self._update_task_count()

        # 显示日志
        self._show_task_log(task_id)
        # 手动查看时重置自动弹出标记
        self._auto_showed = False
        # 取消自动隐藏定时器（手动查看不自动隐藏）
        if self._auto_hide_timer:
            self._auto_hide_timer.stop()
            self._auto_hide_timer = None
        # 不重置 _was_auto_showed（保持曾经自动弹出的记录）
        self.setVisible(True)

    def update_progress(self, task_id: str, message: str):
        """更新指定任务的进度"""
        if task_id in self._tasks:
            self._tasks[task_id].update_progress(message)

    def add_thinking(self, task_id: str, thinking: str):
        """添加思考内容"""
        if task_id in self._tasks:
            self._tasks[task_id].add_thinking(thinking)

    def add_tool_call(self, task_id: str, tool_name: str, args: dict = None):
        """添加工具调用"""
        if task_id in self._tasks:
            self._tasks[task_id].add_tool_call(tool_name, args)

    def add_tool_result(self, task_id: str, tool_name: str, result: str, success: bool = True):
        """添加工具结果"""
        if task_id in self._tasks:
            self._tasks[task_id].add_tool_result(tool_name, result, success)

    def finish_task(self, task_id: str, result: str = None, success: bool = True):
        """完成任务"""
        if task_id in self._tasks:
            self._tasks[task_id].finish_task(result, success)

            # 更新 Segment 标签，添加状态图标
            status_icon = "✓" if success else "✗"
            base_label = self._task_labels.get(task_id, f"任务{len(self._tasks)}")
            # 移除已有的状态图标
            clean_label = base_label.replace(" ✓", "").replace(" ✗", "")
            self._task_labels[task_id] = f"{clean_label} {status_icon}"

            # 更新 Segment 按钮文字
            if task_id in self._segment_items:
                item = self._segment_items[task_id]
                try:
                    item.setText(self._task_labels[task_id])
                except Exception:
                    pass

            # 更新计数
            self._update_task_count()

        # 如果当前显示的是这个任务，切换到下一个
        if self._active_task_id == task_id:
            self._switch_to_next_active()

        # 检查是否所有任务都完成了
        self._check_all_finished()

    def _check_all_finished(self):
        """检查是否所有任务都完成了，如果是则隐藏面板（仅曾经自动弹出的面板）"""
        if not self._tasks:
            return

        all_done = all(
            "✓" in self._task_labels.get(tid, "") or "✗" in self._task_labels.get(tid, "")
            for tid in self._tasks
        )

        if all_done and self._was_auto_showed:
            # 延迟 3 秒后隐藏（曾经自动弹出的面板）
            # 使用成员变量引用，便于手动取消
            if self._auto_hide_timer:
                self._auto_hide_timer.stop()
            self._auto_hide_timer = QTimer(self)
            self._auto_hide_timer.setSingleShot(True)
            self._auto_hide_timer.timeout.connect(self._auto_hide)
            self._auto_hide_timer.start(3000)

    def _auto_hide(self):
        """自动隐藏（所有任务完成后），重置批次标记以便下一批任务能正确清空面板"""
        self._batch_started = False
        self.hide()

    def _switch_to_next_active(self):
        """切换到下一个活跃任务"""
        for task_id in self._tasks:
            label = self._task_labels.get(task_id, "")
            if not label.startswith("✓") and not label.startswith("✗"):
                self.segment_widget.setCurrentItem(task_id)
                return

        # 所有任务都完成了，显示第一个
        if self._tasks:
            first_id = list(self._tasks.keys())[0]
            self.segment_widget.setCurrentItem(first_id)

    def _update_task_count(self):
        """更新任务计数"""
        active = sum(
            1 for tid in self._tasks
            if not self._task_labels.get(tid, "").startswith(("✓", "✗"))
        )
        total = len(self._tasks)
        self.task_count_label.setText(f"{active} 个活跃 / {total} 个任务")

        # 显示/隐藏空状态
        if total > 0:
            self.empty_label.hide()
        else:
            self.empty_label.show()

    def remove_task(self, task_id: str):
        """移除任务"""
        # 移除 widget（先移除 widget）
        if task_id in self._tasks:
            widget = self._tasks[task_id]
            widget.hide()
            widget.deleteLater()
            del self._tasks[task_id]

        # 移除 Segment - SegmentedWidget 没有 removeItem，用 clearItems 代替
        try:
            self.segment_widget.clear()
        except:
            pass

        # 清理标签和 segment items
        self._task_labels.clear()
        self._segment_items.clear()

        # 重置活跃任务
        self._active_task_id = None

        # 更新计数
        self._update_task_count()

        # 重置批次状态
        self._batch_started = False
        self.empty_label.show()

    def clear(self):
        """清空所有任务"""
        # 取消自动隐藏定时器
        if self._auto_hide_timer:
            self._auto_hide_timer.stop()
            self._auto_hide_timer = None

        # 从 layout 中移除所有 widget
        for widget in list(self._tasks.values()):
            self.log_container_layout.removeWidget(widget)
            widget.hide()
            widget.deleteLater()

        # 清空所有 segment
        try:
            self.segment_widget.clear()
        except:
            pass

        # 重置所有状态
        self._tasks.clear()
        self._task_labels.clear()
        self._segment_items.clear()
        self._active_task_id = None
        self._batch_started = False
        self._was_auto_showed = False
        self.empty_label.show()

    def clear_finished_tasks(self):
        """清空已完成的任务（用于新任务开始前清理旧状态）"""
        finished_ids = [
            tid for tid, label in self._task_labels.items()
            if label.startswith(("✓", "✗"))
        ]
        for tid in finished_ids:
            self.remove_task(tid)

    def set_opacity(self, opacity: float):
        """设置透明度，用于响应全局透明度变化"""
        Colors.refresh()
        bg = Colors.REALTIME_BG
        if bg.startswith("rgba("):
            # 最小 alpha 为 1，避免完全透明导致卡片"消失"
            alpha = max(1, int(opacity * 255))
            bg = bg.rsplit(",", 1)[0] + f", {alpha})"
        self.setStyleSheet(f"""
            CardWidget {{
                background-color: {bg};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-radius: 8px;
            }}
        """)
