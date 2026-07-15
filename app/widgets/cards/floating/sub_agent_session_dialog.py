# -*- coding: utf-8 -*-
"""
子智能体会话对话框 - 以 MessageCard 同款样式渲染子智能体的运行日志/消息

点击紧凑卡片中的"进入会话"按钮时弹出，展示该子智能体的完整会话记录，
包括思考过程、AI 回复、工具调用和结果。

优化：左侧导览栏 + 右侧详细消息布局，参考会话分享 HTML 结构。
支持 logs_provider 回调实现实时更新（子智能体执行中自动轮询刷新）。
"""

import time
from typing import Any, Callable, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QCursor, QMouseEvent
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

LOG_TYPE_LABELS = {
    "progress": "进度",
    "thinking": "思考",
    "ai_response": "AI 回复",
    "tool_call": "工具调用",
    "tool_result": "工具结果",
    "tool_error": "工具错误",
    "finish": "完成",
}


class _DraggableHeader(QFrame):
    """可拖拽标题栏 - 无边框窗口通过拖动标题栏移动整体位置"""

    def __init__(self, parent_dialog, parent=None):
        super().__init__(parent)
        self._dialog = parent_dialog
        self._drag_offset = None

    def mousePressEvent(self, event: QMouseEvent):
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
    """子智能体会话对话框 - 左侧导览栏 + 右侧详细消息布局"""

    def __init__(
        self,
        task_id: str,
        agent_name: str,
        logs: List[Dict[str, Any]],
        summary: Dict[str, Any] = None,
        parent=None,
        logs_provider: Optional[Callable[[], Dict]] = None,
    ):
        """
        Args:
            task_id: 任务ID
            agent_name: 智能体名称
            logs: 初始日志列表
            summary: 任务摘要
            parent: 父窗口
            logs_provider: 可选。获取最新日志的回调，返回 get_task_logs() 的 dict。
                          提供后若任务状态为 running，自动启动定时轮询实现实时更新。
        """
        super().__init__(parent)
        self._task_id = task_id
        self._agent_name = agent_name
        self._logs = logs
        self._summary = summary or {}
        self._logs_provider = logs_provider
        self._last_log_count = len(logs)
        self._result_appended = bool(self._summary.get("result") or self._summary.get("error"))

        self.setWindowTitle(f"🤖 {agent_name} - 会话日志")
        self.setMinimumSize(800, 520)
        # 无边框：去掉系统原生标题栏，使用内部绘制的标题栏
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        # 半透明背景：让圆角在桌面背景下干净呈现
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._setup_ui()
        self._render_logs()

        # ── 如果任务还在运行且提供了 logs_provider，启动定时轮询 ──
        status = self._summary.get("status", "finished")
        if status == "running" and logs_provider is not None:
            self._poll_timer = QTimer(self)
            self._poll_timer.setInterval(2000)  # 2 秒轮询
            self._poll_timer.timeout.connect(self._poll_update)
            self._poll_timer.setSingleShot(False)
            self._poll_timer.start()
        else:
            self._poll_timer = None

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

        # ── 底部圆角封边 + 尺寸调节手柄 ──
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

    def closeEvent(self, event):
        """关闭时停止定时器"""
        self._stop_polling()
        super().closeEvent(event)

    def reject(self):
        """取消时停止定时器（ESC 按键）"""
        self._stop_polling()
        super().reject()

    def _stop_polling(self):
        """停止轮询定时器"""
        if self._poll_timer is not None:
            try:
                self._poll_timer.stop()
            except Exception:
                pass
            self._poll_timer = None

    def _poll_update(self):
        """轮询最新日志并增量更新右侧内容"""
        if self._logs_provider is None:
            self._stop_polling()
            return

        try:
            data = self._logs_provider()
            if not data or not data.get("found"):
                return

            logs = data.get("logs", [])
            summary = data.get("summary", {})
            status = data.get("status", summary.get("status", "running"))

            log_count = len(logs)
            if log_count == self._last_log_count:
                # 无新日志，检查任务是否结束
                if status != "running":
                    self._summary = summary
                    self._update_summary_bar()
                    self._stop_polling()
                return

            # ── 有更新 → 检查用户是否在底部（自动滚动） ──
            self._check_and_update(logs, summary, status)

        except Exception as e:
            import logging

            logging.warning(f"[SubAgentSessionDialog] 轮询更新失败: {e}")

    def _check_and_update(self, logs, summary, status):
        """检查用户滚动位置后执行更新"""
        # 先用 JS 检查用户是否在底部区域
        self._web_view.page().runJavaScript(
            """(function() {
                var m = document.getElementById('mainContent');
                if (!m) return true;
                return (m.scrollHeight - m.scrollTop - m.clientHeight) < 120;
            })()""",
            lambda near_bottom: self._apply_update(logs, summary, status, bool(near_bottom)),
        )

    def _apply_update(self, logs, summary, status, near_bottom: bool):
        """增量更新 — 只追加新增日志，不重建页面，导航和滚动位置不变"""
        new_logs = logs[self._last_log_count:]
        has_new_logs = len(new_logs) > 0

        # 生成新增日志的 HTML
        nav_items_html = ""
        content_sections_html = ""
        if has_new_logs:
            start_idx = self._last_log_count
            for i, log in enumerate(new_logs):
                idx = start_idx + i
                nav_items_html += self._build_nav_item(
                    log.get("type", "progress"), log.get("content", ""), log, idx
                )
                content_sections_html += self._build_content_section(
                    log.get("type", "progress"), log.get("content", ""), log, idx
                )

        # 检查是否有结果需要追加（任务刚完成）
        result = summary.get("result", "")
        error = summary.get("error", "")
        just_finished = status != "running" and not self._result_appended
        result_nav = ""
        result_content = ""
        if just_finished and (bool(result) or bool(error)):
            self._result_appended = True
            if result:
                result_nav = self._build_result_nav_item("✅ 执行结果", len(logs))
                result_content = self._build_result_section("✅ 执行结果", result, "#4CAF50", len(logs))
            elif error:
                result_nav = self._build_result_nav_item("❌ 执行失败", len(logs))
                result_content = self._build_result_section("❌ 执行失败", error, "#F44336", len(logs))

        # 更新内存状态
        self._logs = logs
        self._summary = summary
        self._last_log_count = len(logs)

        # 通过 JS 增量追加
        total_count = len(logs) + (1 if self._result_appended and just_finished else 0)
        combined_nav = nav_items_html + result_nav
        combined_content = content_sections_html + result_content
        if combined_nav or combined_content:
            import json as _json

            nav_json = _json.dumps(combined_nav)
            content_json = _json.dumps(combined_content)
            js = f"window._appendLogs({nav_json}, {content_json}, {total_count});"
            try:
                self._web_view.page().runJavaScript(js)
            except RuntimeError:
                pass  # 页面已销毁

        # 更新摘要栏
        self._update_summary_bar()

        # 如果用户在底部，自动滚动到最新内容
        if near_bottom and (has_new_logs or just_finished):
            self._auto_scroll_latest()

        # 如果任务完成，停止轮询
        if status != "running":
            self._stop_polling()

    def _auto_scroll_latest(self):
        """增量更新后自动滚动到最底部（最新日志）"""
        QTimer.singleShot(100, lambda: self._web_view.page().runJavaScript(
            """(function() {
                var m = document.getElementById('mainContent');
                if (m) m.scrollTop = m.scrollHeight;
            })()""",
        ))

    def _update_summary_bar(self):
        """更新摘要栏的状态显示（任务结束时刷新）"""
        # 在 Qt Widget 层面更新摘要栏中的状态标签
        if not hasattr(self, '_status_lbl'):
            return
        status = self._summary.get("status", "finished")
        status_text = "✅ 已完成" if status == "finished" else "⏳ 执行中"
        self._status_lbl.setText(status_text)

        # 更新工具调用次数
        tool_count = self._summary.get("tool_call_count", 0)
        if hasattr(self, '_tool_count_lbl'):
            self._tool_count_lbl.setText(f"🔧 {tool_count} 次工具调用")

        # 更新耗时
        elapsed = self._summary.get("elapsed_seconds", 0)
        mins = elapsed // 60
        secs = elapsed % 60
        if hasattr(self, '_elapsed_lbl'):
            self._elapsed_lbl.setText(f"⏱ {mins:02d}:{secs:02d}")

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
        title.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(title)

        layout.addStretch()

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
        bar.setStyleSheet(f"""
            #SessionSummary {{
                background-color: {Colors.REALTIME_TAG_BG};
                border-bottom: 1px solid {Colors.REALTIME_BORDER};
            }}
        """)

        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(16)

        # 状态（存引用供 _update_summary_bar 实时更新）
        status = self._summary.get("status", "finished")
        status_text = "✅ 已完成" if status == "finished" else "⏳ 执行中"
        self._status_lbl = QLabel(status_text, bar)
        self._status_lbl.setFont(get_unified_font(9))
        self._status_lbl.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        layout.addWidget(self._status_lbl)

        # 工具调用次数
        tool_count = self._summary.get("tool_call_count", 0)
        self._tool_count_lbl = QLabel(f"🔧 {tool_count} 次工具调用", bar)
        self._tool_count_lbl.setFont(get_unified_font(9))
        self._tool_count_lbl.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        layout.addWidget(self._tool_count_lbl)

        # 耗时
        elapsed = self._summary.get("elapsed_seconds", 0)
        mins = elapsed // 60
        secs = elapsed % 60
        self._elapsed_lbl = QLabel(f"⏱ {mins:02d}:{secs:02d}", bar)
        self._elapsed_lbl.setFont(get_unified_font(9))
        self._elapsed_lbl.setStyleSheet(f"color: {Colors.REALTIME_TEXT_SECONDARY}; background: transparent;")
        layout.addWidget(self._elapsed_lbl)

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
        """构建完整的日志 HTML — 左侧导览栏 + 右侧详细消息布局"""
        Colors.refresh()

        task_desc = self._summary.get("task_description", "")
        log_count = len(self._logs)

        # ── 生成导览项 + 内容区 ──
        nav_items_html = ""
        content_sections_html = ""

        for i, log in enumerate(self._logs):
            log_type = log.get("type", "progress")
            content = log.get("content", "")

            nav_items_html += self._build_nav_item(log_type, content, log, i)
            content_sections_html += self._build_content_section(log_type, content, log, i)

        # ── 最终结果块（作为独立导览项 + 内容区） ──
        result = self._summary.get("result", "")
        error = self._summary.get("error", "")
        has_result = bool(result) or bool(error)

        if result:
            nav_items_html += self._build_result_nav_item("✅ 执行结果", log_count)
            content_sections_html += self._build_result_section("✅ 执行结果", result, "#4CAF50", log_count)
        elif error:
            nav_items_html += self._build_result_nav_item("❌ 执行失败", log_count)
            content_sections_html += self._build_result_section("❌ 执行失败", error, "#F44336", log_count)

        # ── 主题色变量 ──
        bg_color = Colors.REALTIME_BG
        border_color = Colors.REALTIME_BORDER
        text_primary = Colors.TEXT_PRIMARY
        text_secondary = Colors.TEXT_SECONDARY
        text_muted = Colors.TEXT_MUTED
        accent_color = Colors.REALTIME_ACCENT

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{self._agent_name} - 会话日志</title>
<style>
    :root {{
        --bg: {bg_color};
        --border: {border_color};
        --text: {text_primary};
        --text-secondary: {text_secondary};
        --text-muted: {text_muted};
        --accent: {accent_color};
        --font: {get_font_family_css()}, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
        --mono-font: "Cascadia Code", "Fira Code", "Consolas", "SFMono-Regular", monospace;
        --font-size: {scale_font_size(14)}px;
    }}
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
        font-family: var(--font);
        background-color: var(--bg);
        color: var(--text);
        font-size: var(--font-size);
        line-height: 1.7;
        -webkit-font-smoothing: antialiased;
        height: 100vh;
        overflow: hidden;
    }}

    /* ── 整体双栏布局 ── */
    .layout {{
        display: flex;
        height: 100vh;
        align-items: stretch;
    }}

    /* ── 左侧导览栏 ── */
    .sidebar {{
        width: 248px;
        flex-shrink: 0;
        background: rgba(0,0,0,0.12);
        border-right: 1px solid var(--border);
        display: flex;
        flex-direction: column;
        overflow: hidden;
    }}
    .sidebar-header {{
        padding: 10px 12px 6px;
        border-bottom: 1px solid var(--border);
        flex-shrink: 0;
    }}
    .sidebar-title {{
        font-size: {scale_font_size(11)}px;
        font-weight: 700;
        color: var(--text-muted);
        letter-spacing: .04em;
    }}
    .sidebar-title .count {{
        color: var(--accent);
        font-weight: 600;
    }}
    .sidebar-nav {{
        flex: 1;
        overflow-y: auto;
        padding: 6px 8px 12px;
        scrollbar-width: thin;
    }}
    .sidebar-nav::-webkit-scrollbar {{ width: 5px; }}
    .sidebar-nav::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}

    .nav-item {{
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 10px;
        margin-bottom: 2px;
        border-radius: 6px;
        text-decoration: none;
        color: var(--text-secondary);
        cursor: pointer;
        transition: background .12s ease, color .12s ease;
        border-left: 2px solid transparent;
        min-height: 32px;
    }}
    .nav-item:hover {{
        background: rgba(255,255,255,0.04);
        color: var(--text);
    }}
    .nav-item.active {{
        background: rgba(102,198,255,0.10);
        color: var(--text);
        border-left-color: var(--accent);
    }}
    .nav-item.nav-result {{
        margin-top: 4px;
        border-top: 1px solid var(--border);
        padding-top: 8px;
        border-radius: 0 0 6px 6px;
    }}
    .nav-icon {{
        width: 24px; height: 24px; border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        font-size: 11px; flex-shrink: 0;
        background: rgba(255,255,255,0.08);
    }}
    .nav-item[data-type="progress"] .nav-icon {{ background: rgba(156,39,176,0.18); }}
    .nav-item[data-type="thinking"] .nav-icon {{ background: rgba(255,152,0,0.18); }}
    .nav-item[data-type="ai_response"] .nav-icon {{ background: rgba(76,175,80,0.18); }}
    .nav-item[data-type="tool_call"] .nav-icon {{ background: rgba(33,150,243,0.18); }}
    .nav-item[data-type="tool_result"] .nav-icon {{ background: rgba(76,175,80,0.18); }}
    .nav-item[data-type="tool_error"] .nav-icon {{ background: rgba(244,67,54,0.18); }}
    .nav-item[data-type="finish"] .nav-icon {{ background: rgba(96,125,139,0.18); }}
    .nav-text {{
        display: flex;
        flex-direction: column;
        min-width: 0;
        line-height: 1.2;
        flex: 1;
    }}
    .nav-role {{
        font-size: {scale_font_size(12)}px;
        font-weight: 600;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }}
    .nav-time {{
        font-size: {scale_font_size(10)}px;
        color: var(--text-muted);
        white-space: nowrap;
    }}
    .nav-snip {{
        font-size: {scale_font_size(11)}px;
        color: var(--text-muted);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 160px;
    }}

    /* ── 右侧主内容区 ── */
    .main {{
        flex: 1;
        min-width: 0;
        overflow-y: auto;
        padding: 16px 20px 32px;
        scrollbar-width: thin;
    }}
    .main::-webkit-scrollbar {{ width: 6px; }}
    .main::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}

    .task-description {{
        margin-bottom: 16px;
        padding: 8px 14px;
        background: rgba(255,255,255,0.03);
        border-radius: 6px;
        font-size: {scale_font_size(12)}px;
        color: var(--text-secondary);
        border: 1px solid var(--border);
    }}

    /* ── 内容区 — 全部可见，导航点击滚动定位 ── */
    .content-section {{
        display: block;
        margin-bottom: 12px;
        scroll-margin-top: 16px;
        transition: background 0.3s ease;
    }}
    .content-section.highlight {{
        background: rgba(255,255,255,0.04);
        border-radius: 8px;
    }}
    .log-entry {{
        padding: 10px 14px;
        border-radius: 8px;
        background: rgba(255,255,255,0.02);
        border-left: 3px solid #888;
        transition: background 0.15s;
    }}
    .log-entry:hover {{
        background: rgba(255,255,255,0.04);
    }}
    .log-header {{
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 6px;
        font-size: {scale_font_size(12)}px;
        font-weight: 600;
    }}
    .log-time {{
        color: var(--text-muted);
        font-size: {scale_font_size(11)}px;
        font-weight: normal;
        margin-left: auto;
    }}
    .log-content {{
        font-size: {scale_font_size(13)}px;
        line-height: 1.6;
        white-space: pre-wrap;
        word-wrap: break-word;
    }}
    .log-content code {{
        background: rgba(0,0,0,0.08);
        border-radius: 3px;
        padding: 1px 4px;
        font-family: var(--mono-font);
        font-size: {scale_font_size(12)}px;
    }}
    .log-content pre {{
        background: rgba(0,0,0,0.06);
        border-radius: 6px;
        padding: 10px 14px;
        overflow-x: auto;
        font-family: var(--mono-font);
        font-size: {scale_font_size(12)}px;
        line-height: 1.45;
        border: 1px solid rgba(255,255,255,0.06);
        margin: 6px 0;
    }}
    .log-content p {{
        margin: 6px 0;
    }}
    .result-section {{
        display: block;
        margin-top: 4px;
        padding: 12px 16px;
        border-radius: 8px;
        border: 1px solid currentColor;
        background: rgba(255,255,255,0.03);
        scroll-margin-top: 16px;
    }}
    .result-title {{
        font-weight: 700;
        font-size: {scale_font_size(13)}px;
        margin-bottom: 8px;
    }}
    .result-content {{
        font-size: {scale_font_size(13)}px;
        line-height: 1.7;
        white-space: pre-wrap;
        word-wrap: break-word;
    }}
    .result-content pre {{
        background: rgba(0,0,0,0.06);
        border-radius: 6px;
        padding: 10px 14px;
        overflow-x: auto;
        font-family: var(--mono-font);
        font-size: {scale_font_size(12)}px;
        line-height: 1.45;
        border: 1px solid rgba(255,255,255,0.06);
        margin: 6px 0;
    }}

    /* ── 空状态 ── */
    .empty-state {{
        display: flex;
        align-items: center;
        justify-content: center;
        height: 60vh;
        color: var(--text-muted);
        font-size: {scale_font_size(15)}px;
    }}

    /* ── 导航高亮脉冲动画 ── */
    @keyframes highlight-pulse {{
        0% {{ background: rgba(255,255,255,0.06); }}
        100% {{ background: rgba(255,255,255,0); }}
    }}
    .content-section.pulse {{
        animation: highlight-pulse 1.2s ease-out;
    }}

    /* ── 响应式：小屏幕时导览栏折叠到顶部 ── */
    @media (max-width: 700px) {{
        .layout {{ flex-direction: column; }}
        .sidebar {{
            width: 100%;
            max-height: 180px;
            border-right: none;
            border-bottom: 1px solid var(--border);
        }}
        .sidebar-nav {{
            display: flex;
            flex-direction: column;
        }}
    }}
</style>
</head>
<body>
<div class="layout">
    <!-- 左侧导览栏 -->
    <div class="sidebar">
        <div class="sidebar-header">
            <div class="sidebar-title">
                会话日志 · <span class="count">{log_count + (1 if has_result else 0)}</span> 条
            </div>
        </div>
        <div class="sidebar-nav">
            {nav_items_html}
        </div>
    </div>

    <!-- 右侧主内容区 -->
    <div class="main" id="mainContent">
        <div class="task-description">📋 任务：{self._escape_html(task_desc)}</div>
        {content_sections_html}
    </div>
</div>

<script>
(function() {{
    'use strict';
    var navItems = document.querySelectorAll('.nav-item');
    var sections = document.querySelectorAll('.content-section, .result-section');
    var mainContent = document.getElementById('mainContent');

    // 滚动观察器（存全局供 _appendLogs 复用）
    window._logObserver = new IntersectionObserver(function(entries) {{
        entries.forEach(function(entry) {{
            if (entry.isIntersecting) {{
                var id = entry.target.id;
                document.querySelectorAll('.nav-item').forEach(function(item) {{
                    if (item.getAttribute('data-target') === id) {{
                        item.classList.add('active');
                    }} else {{
                        item.classList.remove('active');
                    }}
                }});
            }}
        }});
    }}, {{
        root: mainContent,
        threshold: 0,
        rootMargin: '-20% 0px -60% 0px'
    }});

    // 对所有内容区启动观察
    sections.forEach(function(sec) {{ window._logObserver.observe(sec); }});

    // ── 增量追加日志（供 Python 端 _apply_update 调用）──
    window._appendLogs = function(navHtml, contentHtml, totalCount) {{
        // 追加导览项
        document.querySelector('.sidebar-nav').insertAdjacentHTML('beforeend', navHtml);
        // 追加内容区
        document.getElementById('mainContent').insertAdjacentHTML('beforeend', contentHtml);
        // 更新计数
        var countEl = document.querySelector('.sidebar-title .count');
        if (countEl) countEl.textContent = totalCount;
        // 观察新内容区
        document.querySelectorAll('.content-section, .result-section').forEach(function(el) {{
            if (!el.dataset.observed) {{
                el.dataset.observed = '1';
                window._logObserver.observe(el);
            }}
        }});
        // 新导航项绑定点击事件
        document.querySelectorAll('.nav-item:not([data-bound])').forEach(function(item) {{
            item.dataset.bound = '1';
            item.addEventListener('click', function(e) {{
                e.preventDefault();
                var tid = this.getAttribute('data-target');
                if (tid) window._scrollTo(tid);
            }});
        }});
    }};

    function scrollTo(targetId) {{
        var target = document.getElementById(targetId);
        if (target) {{
            target.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
            target.classList.remove('pulse');
            void target.offsetWidth;
            target.classList.add('pulse');
        }}
        document.querySelectorAll('.nav-item').forEach(function(item) {{
            if (item.getAttribute('data-target') === targetId) {{
                item.classList.add('active');
                var sidebar = item.closest('.sidebar-nav');
                if (sidebar) {{
                    var itemRect = item.getBoundingClientRect();
                    var sidebarRect = sidebar.getBoundingClientRect();
                    if (itemRect.top < sidebarRect.top || itemRect.bottom > sidebarRect.bottom) {{
                        item.scrollIntoView({{ block: 'nearest', behavior: 'smooth' }});
                    }}
                }}
            }} else {{
                item.classList.remove('active');
            }}
        }});
    }}
    window._scrollTo = scrollTo;  // 暴露给 _appendLogs 新追加的导航项

    // 点击导览项 → 滚动到对应内容区
    navItems.forEach(function(item) {{
        item.addEventListener('click', function(e) {{
            e.preventDefault();
            var targetId = this.getAttribute('data-target');
            if (targetId) scrollTo(targetId);
        }});
    }});

    // 初始激活
    function initActive() {{
        var firstVisible = null;
        var mainRect = mainContent.getBoundingClientRect();
        document.querySelectorAll('.content-section, .result-section').forEach(function(sec) {{
            var rect = sec.getBoundingClientRect();
            if (rect.top >= mainRect.top && rect.top < mainRect.top + 100) {{
                firstVisible = sec.id;
            }}
        }});
        if (!firstVisible && document.querySelectorAll('.content-section, .result-section').length > 0) {{
            firstVisible = document.querySelectorAll('.content-section, .result-section')[0].id;
        }}
        if (firstVisible) {{
            document.querySelectorAll('.nav-item').forEach(function(item) {{
                if (item.getAttribute('data-target') === firstVisible) {{
                    item.classList.add('active');
                }}
            }});
        }}
    }}
    setTimeout(initActive, 100);

    // 键盘上下键导航
    document.addEventListener('keydown', function(e) {{
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {{
            var currentActive = document.querySelector('.nav-item.active');
            if (!currentActive) return;
            var siblings = Array.from(document.querySelectorAll('.nav-item'));
            var idx = siblings.indexOf(currentActive);
            if (idx === -1) return;
            var nextIdx = (e.key === 'ArrowDown') ? idx + 1 : idx - 1;
            if (nextIdx >= 0 && nextIdx < siblings.length) {{
                var tid = siblings[nextIdx].getAttribute('data-target');
                if (tid) scrollTo(tid);
            }}
            e.preventDefault();
        }}
    }});
}})();
</script>
</body>
</html>"""

    def _build_nav_item(self, log_type: str, content: str, log: Dict, index: int) -> str:
        """构建左侧导览项"""
        icon = LOG_ICONS.get(log_type, "•")
        label = LOG_TYPE_LABELS.get(log_type, log_type)
        color = LOG_COLORS.get(log_type, "#888")

        # 摘要（截取首段）
        snippet = self._make_snippet(log_type, content, log)

        target_id = f"section-{index}"
        return f"""<a class="nav-item" data-target="{target_id}" data-type="{log_type}" style="border-left-color: {color};">
    <span class="nav-icon">{icon}</span>
    <span class="nav-text">
        <span class="nav-role" style="color: {color};">{label}</span>
        <span class="nav-snip">{self._escape_html(snippet)}</span>
    </span>
</a>"""

    def _build_result_nav_item(self, title: str, index: int) -> str:
        """构建最终结果的导览项"""
        color = "#888"
        target_id = "section-result"
        return f"""<a class="nav-item nav-result" data-target="{target_id}" data-type="result" style="border-left-color: {color};">
    <span class="nav-icon">📋</span>
    <span class="nav-text">
        <span class="nav-role">{title}</span>
        <span class="nav-snip">执行结果</span>
    </span>
</a>"""

    def _build_content_section(self, log_type: str, content: str, log: Dict, index: int) -> str:
        """构建右侧内容区（一条日志条目）"""
        icon = LOG_ICONS.get(log_type, "•")
        color = LOG_COLORS.get(log_type, "#888")
        label = LOG_TYPE_LABELS.get(log_type, log_type)

        # 时间戳
        ts = log.get("timestamp", 0)
        time_str = ""
        if ts:
            local_t = time.localtime(ts)
            time_str = f"{local_t.tm_hour:02d}:{local_t.tm_min:02d}:{local_t.tm_sec:02d}"

        # 格式化内容（完整显示，不截断）
        content_html = self._format_content_full(log_type, content, log)

        target_id = f"section-{index}"
        return f"""<div class="content-section" id="{target_id}">
    <div class="log-entry" style="border-left-color: {color};">
        <div class="log-header" style="color: {color};">
            <span>{icon}</span>
            <span>{label}</span>
            <span class="log-time">{time_str}</span>
        </div>
        <div class="log-content">{content_html}</div>
    </div>
</div>"""

    def _build_result_section(self, title: str, content: str, color: str, index: int) -> str:
        """构建最终结果内容区"""
        escaped = self._escape_html(content)
        # 对于结果也渲染为 pre 块以保持格式
        return f"""<div class="result-section" id="section-result">
    <div class="result-title" style="color: {color};">{title}</div>
    <div class="result-content"><pre>{escaped}</pre></div>
</div>"""

    def _make_snippet(self, log_type: str, content: str, log: Dict, max_len: int = 40) -> str:
        """生成导览项摘要文本"""
        if not content:
            return "（空）"

        if log_type == "tool_call":
            # 工具调用：显示工具名
            return f"调用工具：{content[:max_len]}"

        if log_type == "tool_result":
            # 工具结果
            success = log.get("success", True)
            result_text = str(log.get("result", content))[:max_len]
            return f"{'✅' if success else '❌'} {result_text}"

        if log_type == "progress":
            return content[:max_len]

        # 通用：纯文本截取
        text = content.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        text = " ".join(text.split())
        if len(text) > max_len:
            text = text[:max_len] + "…"
        return text.strip()

    def _format_content_full(self, log_type: str, content: str, log: Dict) -> str:
        """格式化日志内容（完整显示，供右侧内容区使用）"""
        if not content:
            return ""

        escaped = self._escape_html(content)

        if log_type == "thinking":
            # 思考内容：完整显示
            return f"<pre>{escaped}</pre>"

        elif log_type == "ai_response":
            # AI 回复：完整显示
            return f"<pre>{escaped}</pre>"

        elif log_type == "tool_call":
            # 工具调用：工具名 + 参数
            args = log.get("args")
            args_html = ""
            if args:
                import orjson as json

                try:
                    args_str = json.dumps(args, option=json.OPT_INDENT_2).decode("utf-8")
                    args_html = f"<pre>{self._escape_html(args_str)}</pre>"
                except Exception:
                    args_html = f"<pre>{self._escape_html(str(args))}</pre>"
            return f"<p><strong>工具：</strong>{escaped}</p>{args_html}"

        elif log_type == "tool_result":
            # 工具结果：完整显示
            success = log.get("success", True)
            icon = "✅" if success else "❌"
            result_text = str(log.get("result", content))
            return f"<p>{icon} <strong>{escaped}</strong></p><pre>{self._escape_html(result_text)}</pre>"

        elif log_type == "progress":
            return f"<p>{escaped}</p>"

        elif log_type == "finish":
            return f"<p>{escaped}</p>"

        return f"<p>{escaped}</p>"

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
