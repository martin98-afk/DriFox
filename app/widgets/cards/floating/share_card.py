# -*- coding: utf-8 -*-
"""
分享卡片

将当前对话导出为 Markdown / JSON / HTML 格式，
支持复制到剪贴板或上传至 Gitee 生成公网链接。

以卡片形式嵌入 TopCardContainer，与历史会话卡片一致。
"""

import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import FluentIcon, InfoBar, InfoBarPosition, TransparentToolButton

from app.utils.design_tokens import Colors, font_size_css, get_unified_scrollbar_style
from app.utils.utils import get_unified_font


# ── 导出工具函数 ──────────────────────────────────────────────────


def _format_timestamp(msg: Dict[str, Any]) -> str:
    """提取消息时间戳，返回友好字符串"""
    ts = msg.get("timestamp", "")
    if ts:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%m-%d %H:%M")
        except ValueError, TypeError:
            return ts
    return ""


def _content_to_plain(content: Any) -> str:
    """将多种格式的 content 转为纯文本"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("type", "")
                if t == "text":
                    texts.append(str(block.get("text", "")))
                elif t in ("image_url", "input_image", "image"):
                    texts.append("[图片]")
                elif t == "reasoning":
                    texts.append(f"【思考过程】\n{block.get('content', '')}")
                elif t == "tool_result":
                    texts.append(f"[工具: {block.get('name', 'tool')}] {block.get('result', '')[:300]}")
                else:
                    texts.append(str(block.get("text", block.get("content", ""))))
            else:
                texts.append(str(block))
        return "\n\n".join(t.strip() for t in texts if t.strip())
    return str(content)


def _ensure_content_blocks(content: Any) -> List[Dict]:
    """规范化 content 为 blocks 列表"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": str(content)}]


def _get_session_title(messages: List[Dict]) -> str:
    """从消息中推断对话标题（取首条用户消息前 40 字）"""
    for msg in messages:
        if msg.get("role") == "user":
            text = _content_to_plain(msg.get("content", ""))
            if text:
                return text[:40] + ("…" if len(text) > 40 else "")
    return "对话分享"


# ── Markdown 导出 ─────────────────────────────────────────────────


def _export_markdown(messages: List[Dict]) -> str:
    """导出为 Markdown"""
    title = _get_session_title(messages)
    lines = [f"# 对话分享 — {title}", ""]
    for msg in messages:
        role = msg.get("role", "unknown")
        ts = _format_timestamp(msg)
        content = _content_to_plain(msg.get("content", ""))

        if role == "user":
            lines.append(f"## 👤 User  {(' — ' + ts) if ts else ''}")
            lines.append("")
            lines.append(content)
        elif role == "assistant":
            lines.append(f"## 🤖 Assistant  {(' — ' + ts) if ts else ''}")
            lines.append("")
            lines.append(content)
        elif role == "tool":
            name = msg.get("name", "tool")
            lines.append(f"## 🔧 Tool: {name}")
            lines.append("")
            lines.append(content)
        elif role == "system":
            lines.append(f"## ⚙️ System  {(' — ' + ts) if ts else ''}")
            lines.append("")
            lines.append(content)
        else:
            lines.append(f"## {role}")
            lines.append("")
            lines.append(content)
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines).strip()


# ── JSON 导出 ──────────────────────────────────────────────────────


def _export_json(messages: List[Dict]) -> str:
    """导出为 JSON"""
    clean = []
    for msg in messages:
        entry = {
            "role": msg.get("role", ""),
            "content": msg.get("content", ""),
        }
        if msg.get("name"):
            entry["name"] = msg["name"]
        if msg.get("model_name"):
            entry["model_name"] = msg["model_name"]
        if msg.get("timestamp"):
            entry["timestamp"] = msg["timestamp"]
        if msg.get("tool_call_id"):
            entry["tool_call_id"] = msg["tool_call_id"]
        if msg.get("tool_calls"):
            entry["tool_calls"] = msg["tool_calls"]
        clean.append(entry)
    return json.dumps(clean, ensure_ascii=False, indent=2)


# ── HTML 导出 ──────────────────────────────────────────────────────


def _escape_html(text: str) -> str:
    """HTML 转义"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _export_html(messages: List[Dict]) -> str:
    """导出为自包含 HTML 文档"""
    title = _escape_html(_get_session_title(messages))
    bubbles = []
    for msg in messages:
        role = msg.get("role", "unknown")
        ts = _format_timestamp(msg)
        blocks = _ensure_content_blocks(msg.get("content", ""))
        content_html = ""
        for block in blocks:
            bt = block.get("type", "text")
            if bt == "text":
                text = str(block.get("text", ""))
                paragraphs = text.split("\n")
                p_html = "</p><p>".join(_escape_html(p) for p in paragraphs)
                content_html += f"<p>{p_html}</p>"
            elif bt == "reasoning":
                rc = str(block.get("content", ""))
                rc_escaped = _escape_html(rc)
                content_html += (
                    f'<details class="reasoning"><summary>思考过程</summary><pre>{rc_escaped}</pre></details>'
                )
            elif bt in ("image_url", "input_image", "image"):
                content_html += '<div class="image-placeholder">[图片]</div>'
            elif bt == "tool_result":
                name = block.get("name", "tool")
                result = str(block.get("result", ""))[:500]
                result_escaped = _escape_html(result)
                content_html += (
                    f'<div class="tool-result">'
                    f"<strong>工具: {_escape_html(name)}</strong>"
                    f"<pre>{result_escaped}</pre></div>"
                )
            else:
                content_html += f"<p>{_escape_html(str(block.get('text', block.get('content', ''))))}</p>"

        role_icons = {"user": "User", "assistant": "Assistant", "tool": "Tool"}
        role_label = role_icons.get(role, role)
        bubble_class = f"bubble-{role}" if role in ("user", "assistant", "tool") else "bubble-other"
        ts_html = f'<span class="ts">{_escape_html(ts)}</span>' if ts else ""
        bubbles.append(
            f'<div class="bubble {bubble_class}">'
            f'<div class="meta">{_escape_html(role_label)} {ts_html}</div>'
            f'<div class="body">{content_html}</div>'
            f"</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f5f5f5;
    color: #222;
    padding: 24px;
    max-width: 900px;
    margin: 0 auto;
    line-height: 1.7;
}}
h1 {{ font-size: 22px; margin-bottom: 20px; padding-bottom: 12px; border-bottom: 1px solid #ddd; color: #333; }}
.bubble {{ margin-bottom: 14px; padding: 12px 16px; border-radius: 10px; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
.bubble-user {{ border-left: 4px solid #3b82f6; }}
.bubble-assistant {{ border-left: 4px solid #f59e0b; }}
.bubble-tool {{ border-left: 4px solid #10b981; }}
.bubble-other {{ border-left: 4px solid #9ca3af; }}
.meta {{ font-size: 13px; font-weight: 600; color: #555; margin-bottom: 6px; }}
.ts {{ font-weight: 400; color: #999; font-size: 12px; margin-left: 8px; }}
.body p {{ margin-bottom: 6px; }}
.body pre {{ background: #f0f0f0; padding: 8px 12px; border-radius: 6px; overflow-x: auto; font-size: 13px; line-height: 1.5; margin: 6px 0; }}
.body code {{ background: #f0f0f0; padding: 1px 4px; border-radius: 3px; font-size: 13px; }}
.tool-result {{ background: #f0fdf4; border: 1px solid #d1fae5; border-radius: 6px; padding: 8px 12px; margin: 6px 0; }}
.tool-result strong {{ display: block; margin-bottom: 4px; color: #059669; }}
.tool-result pre {{ background: transparent; padding: 0; margin: 0; font-size: 13px; white-space: pre-wrap; }}
details.reasoning {{ background: #fef9c3; border: 1px solid #fde68a; border-radius: 6px; padding: 6px 10px; margin: 6px 0; }}
details.reasoning summary {{ font-weight: 600; color: #a16207; cursor: pointer; }}
details.reasoning pre {{ background: transparent; padding: 6px 0 0; margin: 0; font-size: 13px; white-space: pre-wrap; color: #713f12; }}
.image-placeholder {{ display: inline-block; background: #f0f0f0; padding: 16px 24px; border-radius: 6px; color: #999; font-size: 13px; margin: 6px 0; }}
@media (prefers-color-scheme: dark) {{
    body {{ background: #1a1a2e; color: #e0e0e0; }}
    h1 {{ color: #eee; border-bottom-color: #333; }}
    .bubble {{ background: #16213e; box-shadow: 0 1px 3px rgba(0,0,0,0.3); }}
    .meta {{ color: #aaa; }}
    .ts {{ color: #777; }}
    .body pre, .body code {{ background: #0f3460; }}
    .tool-result {{ background: #0a2e1a; border-color: #14532d; }}
    .tool-result strong {{ color: #34d399; }}
    details.reasoning {{ background: #2e250a; border-color: #713f12; }}
    details.reasoning summary {{ color: #fbbf24; }}
    details.reasoning pre {{ color: #fde68a; }}
    .image-placeholder {{ background: #1e293b; color: #666; }}
}}
</style>
</head>
<body>
<h1>{title}</h1>
{"".join(bubbles)}
</body>
</html>"""


# ── 分享卡片 UI ────────────────────────────────────────────────────


class ShareCard(QFrame):
    """分享卡片：选择格式和操作，嵌入 TopCardContainer"""

    closed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._messages: List[Dict] = []
        self._title: str = "对话分享"
        self._selected_format = "markdown"
        self._setup_ui()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self._apply_card_style()

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 8, 10, 10)
        main_layout.setSpacing(6)

        # ── 标题栏 ──
        header = QHBoxLayout()
        header.setSpacing(6)

        title_icon = QLabel("📤")
        title_icon.setFont(get_unified_font(13))

        self._title_label = QLabel("分享当前对话")
        self._title_label.setFont(get_unified_font(11, bold=True))
        Colors.refresh()
        self._title_label.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; background: transparent;")

        self._count_label = QLabel("")
        self._count_label.setFont(get_unified_font(10))
        self._count_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; background: transparent;")

        header.addWidget(title_icon)
        header.addWidget(self._title_label)
        header.addSpacing(4)
        header.addWidget(self._count_label)
        header.addStretch()

        self.close_btn = TransparentToolButton(FluentIcon.CLOSE, self)
        self.close_btn.setFixedSize(22, 22)
        self.close_btn.clicked.connect(lambda: self.closed.emit())
        header.addWidget(self.close_btn)

        main_layout.addLayout(header)

        # ── 格式选择 ──
        self._format_btns = {}
        fmt_layout = QHBoxLayout()
        fmt_layout.setSpacing(4)
        for fmt_id, fmt_name in [
            ("markdown", "📝 Markdown"),
            ("json", "📊 JSON"),
            ("html", "🌐 HTML"),
        ]:
            btn = QPushButton(fmt_name, self)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(32)
            btn.setFont(get_unified_font(11))
            btn.clicked.connect(lambda checked, f=fmt_id: self._select_format(f))
            self._format_btns[fmt_id] = btn
            fmt_layout.addWidget(btn)
        main_layout.addLayout(fmt_layout)

        # ── 预览区 ──
        self._preview_area = QScrollArea(self)
        self._preview_area.setWidgetResizable(True)
        self._preview_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._preview_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._preview_area.setFrameShape(QScrollArea.NoFrame)
        self._preview_area.setFixedHeight(160)
        self._preview_area.setStyleSheet(
            f"QScrollArea {{ background: {Colors.CONTENT_BG}; border: 1px solid {Colors.BORDER}; "
            f"border-radius: 6px; }}" + get_unified_scrollbar_style(4)
        )

        self._preview_content = QLabel()
        self._preview_content.setWordWrap(True)
        self._preview_content.setFont(get_unified_font(10))
        self._preview_content.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; background: transparent; padding: 8px;")
        self._preview_content.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._preview_area.setWidget(self._preview_content)
        main_layout.addWidget(self._preview_area)

        # ── 操作按钮 ──
        actions_layout = QHBoxLayout()
        actions_layout.setSpacing(6)

        self._copy_btn = QPushButton("📋 复制内容", self)
        self._copy_btn.setCursor(Qt.PointingHandCursor)
        self._copy_btn.setFixedHeight(36)
        self._copy_btn.setFont(get_unified_font(11, bold=True))
        self._copy_btn.clicked.connect(self._on_copy)

        self._upload_btn = QPushButton("🔗 生成链接", self)
        self._upload_btn.setCursor(Qt.PointingHandCursor)
        self._upload_btn.setFixedHeight(36)
        self._upload_btn.setFont(get_unified_font(11, bold=True))
        self._upload_btn.clicked.connect(self._on_upload)

        self._save_btn = QPushButton("💾 保存文件", self)
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.setFixedHeight(36)
        self._save_btn.setFont(get_unified_font(11, bold=True))
        self._save_btn.clicked.connect(self._on_save_file)

        actions_layout.addWidget(self._copy_btn)
        actions_layout.addWidget(self._save_btn)
        actions_layout.addWidget(self._upload_btn)
        main_layout.addLayout(actions_layout)

        # 无数据状态
        self._empty_label = QLabel("当前会话暂无消息", self)
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setFont(get_unified_font(11))
        self._empty_label.setStyleSheet(f"color: {Colors.INPUT_PLACEHOLDER}; background: transparent; padding: 20px;")
        main_layout.addWidget(self._empty_label)
        self._empty_label.hide()

        # 初始选中
        self._select_format(self._selected_format)

    def _apply_card_style(self):
        Colors.refresh()
        self.setStyleSheet(
            f"""
            ShareCard {{
                background-color: {Colors.REALTIME_BG};
                border: 1px solid {Colors.REALTIME_BORDER};
                border-radius: 10px;
            }}
            QPushButton {{
                background: {Colors.CONTENT_BG};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 5px;
                padding: 4px 10px;
            }}
            QPushButton:hover {{
                background: {Colors.HOVER_BG_STRONG};
                border: 1px solid {Colors.REALTIME_ACCENT};
            }}
            QPushButton:pressed {{
                background: {Colors.SELECTED_BG};
            }}
            QPushButton[selected="true"] {{
                background: {Colors.SELECTED_BG};
                border: 1px solid {Colors.TAB_ACTIVE_BG};
            }}
            """
        )

    def refresh_style(self):
        """响应主题切换"""
        self._apply_card_style()

    def _select_format(self, fmt_id: str):
        """切换选中格式"""
        self._selected_format = fmt_id
        for fid, btn in self._format_btns.items():
            btn.setProperty("selected", fid == fmt_id)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

        if not self._messages:
            self._preview_content.setText("")
            self._empty_label.show()
            return
        self._empty_label.hide()

        try:
            text = self._get_export_text(fmt_id)
            if len(text) > 400:
                text = text[:400] + "…"
            self._preview_content.setText(text)
        except Exception as e:
            self._preview_content.setText(f"生成预览失败: {e}")

    def _get_export_text(self, fmt: str = None) -> str:
        """获取指定格式的导出文本"""
        fmt = fmt or self._selected_format
        if fmt == "markdown":
            return _export_markdown(self._messages)
        elif fmt == "json":
            return _export_json(self._messages)
        elif fmt == "html":
            return _export_html(self._messages)
        return ""

    def set_messages(self, messages: List[Dict], title: str = ""):
        """设置要分享的消息数据"""
        self._messages = messages
        self._title = title or _get_session_title(messages)
        self._count_label.setText(f"{len(messages)} 条消息" if messages else "")
        self._select_format(self._selected_format)

    def _on_copy(self):
        """复制到剪贴板"""
        if not self._messages:
            self._show_info("当前会话无消息", "warning")
            return
        text = self._get_export_text()
        if not text:
            self._show_info("内容为空", "warning")
            return
        QApplication.clipboard().setText(text)
        self._show_info("已复制到剪贴板", "success")

    def _on_upload(self):
        """上传并生成公网链接"""
        if not self._messages:
            self._show_info("当前会话无消息", "warning")
            return

        text = self._get_export_text()
        if not text:
            self._show_info("内容为空，无法上传", "warning")
            return

        fmt = self._selected_format
        ext_map = {"markdown": ".md", "json": ".json", "html": ".html"}
        ext = ext_map.get(fmt, ".txt")

        # 写临时文件
        try:
            tmp = tempfile.NamedTemporaryFile(
                suffix=ext, prefix="drifox_share_", delete=False, mode="w", encoding="utf-8"
            )
            tmp.write(text)
            tmp_path = tmp.name
            tmp.close()
        except Exception as e:
            self._show_info(f"写入临时文件失败: {e}", "error")
            return

        # 上传
        try:
            from app.gateway.utils.gitee_uploader import GiteeUploader

            uploader = GiteeUploader.get_instance()
            if not uploader.is_configured():
                self._show_info("Gitee 未配置（缺少 token/owner/repo）", "warning")
                Path(tmp_path).unlink(missing_ok=True)
                return

            self._upload_btn.setEnabled(False)
            self._upload_btn.setText("⏳ 上传中…")

            url, err = uploader.upload_file(tmp_path)
            Path(tmp_path).unlink(missing_ok=True)

            self._upload_btn.setEnabled(True)
            self._upload_btn.setText("🔗 生成链接")

            if err:
                self._show_info(f"上传失败: {err}", "error")
                return

            QApplication.clipboard().setText(url)
            self._show_info("链接已复制到剪贴板", "success")

        except Exception as e:
            self._upload_btn.setEnabled(True)
            self._upload_btn.setText("🔗 生成链接")
            Path(tmp_path).unlink(missing_ok=True)
            self._show_info(f"上传异常: {e}", "error")

    def _on_save_file(self):
        """保存到本地文件"""
        if not self._messages:
            self._show_info("当前会话无消息", "warning")
            return

        text = self._get_export_text()
        if not text:
            self._show_info("内容为空", "warning")
            return

        fmt = self._selected_format
        ext_map = {"markdown": "Markdown (*.md)", "json": "JSON (*.json)", "html": "HTML (*.html)"}
        filter_str = ext_map.get(fmt, "All Files (*)")
        ext = "." + fmt if fmt in ("markdown", "json", "html") else ".txt"
        if fmt == "markdown":
            ext = ".md"

        default_name = f"对话分享_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存文件",
            default_name,
            filter_str,
        )
        if not path:
            return

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self._show_info(f"已保存到 {path}", "success")
        except Exception as e:
            self._show_info(f"保存失败: {e}", "error")

    def _show_info(self, message: str, level: str = "info"):
        """显示浮动通知"""
        parent = self.window() or self.parent()
        if level == "success":
            InfoBar.success(
                title="",
                content=message,
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=parent,
            )
        elif level == "warning":
            InfoBar.warning(
                title="",
                content=message,
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=parent,
            )
        elif level == "error":
            InfoBar.error(
                title="",
                content=message,
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=parent,
            )
        else:
            InfoBar.info(
                title="",
                content=message,
                duration=3000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=parent,
            )
