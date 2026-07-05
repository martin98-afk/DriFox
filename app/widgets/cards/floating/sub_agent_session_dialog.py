# -*- coding: utf-8 -*-
"""
子智能体会话对话框 - 以 MessageCard 同款样式渲染子智能体的运行日志/消息

点击紧凑卡片中的"进入会话"按钮时弹出，展示该子智能体的完整会话记录，
包括思考过程、AI 回复、工具调用和结果。
"""

import time
from typing import Any, Dict, List

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QCursor, QMouseEvent
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.utils.design_tokens import Colors, scale_font_size
from app.utils.utils import get_font_family_css, get_unified_font

# 日志类型到图标/样式的映射
LOG_ICONS = {
    "progress": "📌",
    "thinking": "💭",
    "ai_response": "🤖",
    "tool_call": "🔧",
    "tool_result": "✅",
    "tool_error": "❌",
    "finish": "🏁",
}

LOG_COLORS = {
    "progress": "#9C27B0",
    "thinking": "#FF9800",
    "ai_response": "#4CAF50",
    "tool_call": "#2196F3",
    "tool_result": "#4CAF50",
    "tool_error": "#F44336",
    "finish": "#607D8B",
}


class _DraggableHeader(QFrame):
    """可拖拽标题栏 - 无边框窗口通过拖动标题栏移动整体位置"""

    def __init__(self, parent_dialog, parent=None):
        super().__init__(parent)
        self._dialog = parent_dialog
        self._drag_offset = None

    def mousePressEvent(self, event: QMouseEvent):
        # 左键按下时记录全局偏移量，用于后续拖动
        if event.button() == Qt.LeftButton:
            self._drag_offset = event.globalPos() - self._dialog.pos()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_offset is not None and (event.buttons() & Qt.LeftButton):
            self._dialog.move(event.globalPos() - self._drag_offset)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._drag_offset = None
        super().mouseReleaseEvent(event)


class SubAgentSessionDialog(QDialog):
    """子智能体会话对话框 - MessageCard 同款样式渲染"""

    def __init__(
        self,
        task_id: str,
        agent_name: str,
        logs: List[Dict[str, Any]],
        summary: Dict[str, Any] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._task_id = task_id
        self._agent_name = agent_name
        self._logs = logs
        self._summary = summary or {}

        self.setWindowTitle(f"🤖 {agent_name} - 会话日志")
        self.setMinimumSize(640, 480)
        # 无边框：去掉系统原生标题栏，使用内部绘制的标题栏（已含标题与关闭按钮）
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        # 半透明背景：让圆角在桌面背景下干净呈现
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._setup_ui()
        self._render_logs()

    def _setup_ui(self):
        """初始化 UI"""
        Colors.refresh()
        self.setStyleSheet(f"""
            SubAgentSessionDialog {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-radius: 8px;
            }}
        """)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── 顶部标题栏 ──
        header = self._build_header()
        main_layout.addWidget(header)

        # ── 摘要信息栏 ──
        summary_bar = self._build_summary_bar()
        main_layout.addWidget(summary_bar)

        # ── 日志内容 WebView ──
        self._web_view = QWebEngineView(self)
        self._web_view.setMinimumHeight(200)
        self._web_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        Colors.refresh()
        self._web_view.setStyleSheet(f"background-color: {Colors.REALTIME_BG}; border: none;")
        main_layout.addWidget(self._web_view, 1)

        # ── 底部圆角封边 + 尺寸调节手柄（无边框窗口需自行提供缩放入口）──
        footer = QFrame(self)
        footer.setObjectName("SessionFooter")
        footer.setFixedHeight(16)
        Colors.refresh()
        footer.setStyleSheet(f"""
            #SessionFooter {{
                background-color: {Colors.REALTIME_BG};
                border-bottom-left-radius: 8px;
                border-bottom-right-radius: 8px;
            }}
        """)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.addStretch()
        grip = QSizeGrip(footer)
        grip.setStyleSheet("background: transparent;")
        footer_layout.addWidget(grip, 0, Qt.AlignBottom | Qt.AlignRight)
        main_layout.addWidget(footer)

    def _build_header(self) -> QWidget:
        """构建标题栏"""
        header = _DraggableHeader(self, self)
        header.setObjectName("SessionHeader")
        header.setFixedHeight(44)
        Colors.refresh()
        header.setStyleSheet(f"""
            #SessionHeader {{
                background-color: {Colors.REALTIME_ACCENT};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }}
        """)

        layout = QHBoxLayout(header)
        layout.setContentsMargins(16, 0, 12, 0)

        title = QLabel(f"🤖  {self._agent_name}  —  会话日志", header)
        title.setFont(get_unified_font(12, True))
        title.setStyleSheet("color: #ffffff; background: transparent;")
        # 标题文字对鼠标透明，确保点击标题区域可触发标题栏拖动
        title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(title)

        layout.addStretch()

        # 关闭按钮
        close_btn = QPushButton("✕", header)
        close_btn.setFixedSize(28, 28)
        close_btn.setCursor(QCursor(Qt.PointingHandCursor))
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(255,255,255,0.15);
                color: #ffffff;
                border: none;
                border-radius: 14px;
                font-size: 14px;
                {get_font_family_css()}
            }}
            QPushButton:hover {{
                background-color: rgba(255,255,255,0.3);
            }}
        """)
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)

        return header

    def _build_summary_bar(self) -> QWidget:
        """构建摘要信息栏"""
        bar = QFrame(self)
        bar.setObjectName("SessionSummary")
        bar.setAutoFillBackground(True)
        Colors.refresh()
        # 无边框透明窗口下，REALTIME_TAG_BG(0.15 alpha) 会透出桌面，
        # 这里改用基于 REALTIME_BG 的不透明纯色，保持层次感同时不漏底
        bar.setStyleSheet(f"""
            #SessionSummary {{
                background-color: rgb(28, 40, 66);
                border-bottom: 1px solid {Colors.REALTIME_BORDER};
            }}
        """)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(16)

        # 状态
        status = self._summary.get("status", "finished")
        status_text = "✅ 已完成" if status == "finished" else "⏳ 执行中"
        status_lbl = QLabel(status_text, bar)
        status_lbl.setFont(get_unified_font(9))
        status_lbl.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        layout.addWidget(status_lbl)

        # 工具调用次数
        tool_count = self._summary.get("tool_call_count", 0)
        tool_lbl = QLabel(f"🔧 {tool_count} 次工具调用", bar)
        tool_lbl.setFont(get_unified_font(9))
        tool_lbl.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        layout.addWidget(tool_lbl)

        # 耗时
        elapsed = self._summary.get("elapsed_seconds", 0)
        mins = elapsed // 60
        secs = elapsed % 60
        elapsed_lbl = QLabel(f"⏱ {mins:02d}:{secs:02d}", bar)
        elapsed_lbl.setFont(get_unified_font(9))
        elapsed_lbl.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        layout.addWidget(elapsed_lbl)

        # 任务描述
        desc = self._summary.get("task_description", "")
        if desc:
            if len(desc) > 40:
                desc = desc[:40] + "..."
            desc_lbl = QLabel(f"📋 {desc}", bar)
            desc_lbl.setFont(get_unified_font(9))
            desc_lbl.setStyleSheet(f"color: {Colors.TEXT_MUTED}; background: transparent;")
            layout.addWidget(desc_lbl, 1)

        layout.addStretch()

        return bar

    def _render_logs(self):
        """以 MessageCard 同款样式渲染日志"""
        if not self._logs:
            self._web_view.setHtml(self._build_empty_html())
            return

        html = self._build_html()
        self._web_view.setHtml(html)

    def _build_empty_html(self) -> str:
        """构建无日志时的 HTML"""
        Colors.refresh()
        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    body {{
        font-family: {get_font_family_css()};
        background-color: {Colors.REALTIME_BG};
        color: {Colors.TEXT_MUTED};
        margin: 0;
        padding: 40px 20px;
        text-align: center;
    }}
</style>
</head>
<body>
    <p>暂无会话日志</p>
</body>
</html>"""

    def _build_html(self) -> str:
        """构建完整的日志 HTML（MessageCard 同款样式）"""
        Colors.refresh()

        # 任务描述作为上下文
        task_desc = self._summary.get("task_description", "")

        entries_html = ""
        for i, log in enumerate(self._logs):
            log_type = log.get("type", "progress")
            content = log.get("content", "")

            entries_html += self._render_log_entry(log_type, content, log, i)

        # 最终结果
        result = self._summary.get("result", "")
        error = self._summary.get("error", "")
        if result:
            entries_html += self._render_result_block("✅ 执行结果", result, "#4CAF50")
        elif error:
            entries_html += self._render_result_block("❌ 执行失败", error, "#F44336")

        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    * {{
        box-sizing: border-box;
    }}
    body {{
        font-family: {get_font_family_css()}, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
        background-color: {Colors.REALTIME_BG};
        color: {Colors.TEXT_PRIMARY};
        margin: 0;
        padding: 12px 16px;
        line-height: 1.6;
        font-size: {scale_font_size(14)}px;
    }}
    .log-entry {{
        margin-bottom: 8px;
        padding: 8px 12px;
        border-radius: 6px;
        background: rgba(255,255,255,0.02);
        border-left: 3px solid #888;
        transition: background 0.15s;
    }}
    .log-entry:hover {{
        background: rgba(255,255,255,0.05);
    }}
    .log-header {{
        display: flex;
        align-items: center;
        gap: 6px;
        margin-bottom: 4px;
        font-size: {scale_font_size(12)}px;
        font-weight: 600;
    }}
    .log-time {{
        color: {Colors.TEXT_MUTED};
        font-size: {scale_font_size(11)}px;
        font-weight: normal;
        margin-left: auto;
    }}
    .log-content {{
        font-size: {scale_font_size(13)}px;
        line-height: 1.5;
        white-space: pre-wrap;
        word-wrap: break-word;
    }}
    .log-content code {{
        background: rgba(0,0,0,0.08);
        border-radius: 3px;
        padding: 1px 4px;
        font-family: "Cascadia Code", "Fira Code", "Consolas", monospace;
        font-size: {scale_font_size(12)}px;
    }}
    .log-content pre {{
        background: rgba(0,0,0,0.06);
        border-radius: 4px;
        padding: 8px 12px;
        overflow-x: auto;
        font-family: "Cascadia Code", "Fira Code", "Consolas", monospace;
        font-size: {scale_font_size(12)}px;
        line-height: 1.4;
        border: 1px solid rgba(255,255,255,0.06);
    }}
    .result-block {{
        margin-top: 12px;
        padding: 10px 14px;
        border-radius: 8px;
        border: 1px solid currentColor;
        background: rgba(255,255,255,0.03);
    }}
    .result-title {{
        font-weight: 700;
        font-size: {scale_font_size(13)}px;
        margin-bottom: 6px;
    }}
    .result-content {{
        font-size: {scale_font_size(13)}px;
        line-height: 1.6;
        white-space: pre-wrap;
        word-wrap: break-word;
    }}
    .separator {{
        border: none;
        border-top: 1px solid {Colors.REALTIME_BORDER};
        margin: 12px 0;
    }}
    .task-description {{
        margin-bottom: 12px;
        padding: 8px 12px;
        background: rgba(255,255,255,0.03);
        border-radius: 6px;
        font-size: {scale_font_size(12)}px;
        color: {Colors.TEXT_SECONDARY};
    }}
</style>
</head>
<body>
    <div class="task-description">📋 任务：{self._escape_html(task_desc)}</div>
    {entries_html}
</body>
</html>"""

    def _render_log_entry(self, log_type: str, content: str, log: Dict, index: int) -> str:
        """渲染单条日志条目"""
        icon = LOG_ICONS.get(log_type, "•")
        color = LOG_COLORS.get(log_type, "#888")

        # 时间戳
        ts = log.get("timestamp", 0)
        time_str = ""
        if ts:
            local_t = time.localtime(ts)
            time_str = f"{local_t.tm_hour:02d}:{local_t.tm_min:02d}:{local_t.tm_sec:02d}"

        # 根据日志类型定制内容显示
        content_html = self._format_content(log_type, content, log)

        return f"""<div class="log-entry" style="border-left-color: {color};">
    <div class="log-header" style="color: {color};">
        <span>{icon}</span>
        <span>{log_type}</span>
        <span class="log-time">{time_str}</span>
    </div>
    <div class="log-content">{content_html}</div>
</div>"""

    def _format_content(self, log_type: str, content: str, log: Dict) -> str:
        """格式化日志内容"""
        if not content:
            return ""

        escaped = self._escape_html(content)

        if log_type == "thinking":
            # 思考内容折叠显示，较长时用省略
            if len(content) > 300:
                preview = escaped[:300] + "..."
                return f"<pre>{preview}</pre>"
            return f"<pre>{escaped}</pre>"

        elif log_type == "ai_response":
            # AI 回复渲染较完整
            if len(content) > 500:
                preview = escaped[:500] + "..."
                return f"<pre>{preview}</pre>"
            return f"<pre>{escaped}</pre>"

        elif log_type == "tool_call":
            # 工具调用：显示工具名和参数
            args = log.get("args")
            args_html = ""
            if args:
                import orjson as json

                try:
                    args_str = json.dumps(args, option=json.OPT_INDENT_2).decode("utf-8")
                    args_html = f"<pre>{self._escape_html(args_str)}</pre>"
                except Exception:
                    args_html = f"<pre>{self._escape_html(str(args))}</pre>"
            return f"<strong>工具：{escaped}</strong>{args_html}"

        elif log_type == "tool_result":
            # 工具结果
            success = log.get("success", True)
            icon = "✅" if success else "❌"
            return f"{icon} <strong>{escaped}</strong><br>{self._escape_html(str(log.get('result', ''))[:300])}"

        elif log_type == "progress":
            return f"<span>{escaped}</span>"

        elif log_type == "finish":
            return f"<span>{escaped}</span>"

        return f"<span>{escaped}</span>"

    def _render_result_block(self, title: str, content: str, color: str) -> str:
        """渲染最终结果块"""
        escaped = self._escape_html(content)
        return f"""<div class="result-block" style="border-color: {color};">
    <div class="result-title" style="color: {color};">{title}</div>
    <div class="result-content">{escaped}</div>
</div>"""

    @staticmethod
    def _escape_html(text: str) -> str:
        """转义 HTML 特殊字符"""
        if not text:
            return ""
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")
        text = text.replace('"', "&quot;")
        text = text.replace("'", "&#39;")
        return text
