# -*- coding: utf-8 -*-
"""
分享卡片内容组件

包含格式选择、预览、操作按钮。
通过 BaseSettingsCard 包裹后嵌入 TopCardContainer。
"""

import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import InfoBar, InfoBarPosition

from app.utils.design_tokens import Colors, get_unified_scrollbar_style
from app.utils.utils import get_unified_font


# ── 导出工具函数 ──────────────────────────────────────────────────


def _format_timestamp(msg: Dict[str, Any]) -> str:
    ts = msg.get("timestamp", "")
    if ts:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%m-%d %H:%M")
        except ValueError, TypeError:
            return ts
    return ""


def _content_to_plain(content: Any) -> str:
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
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": str(content)}]


def _get_session_title(messages: List[Dict]) -> str:
    for msg in messages:
        if msg.get("role") == "user":
            text = _content_to_plain(msg.get("content", ""))
            if text:
                return text[:40] + ("…" if len(text) > 40 else "")
    return "对话分享"


# ── Markdown 导出 ─────────────────────────────────────────────────


def _export_markdown(messages: List[Dict]) -> str:
    title = _get_session_title(messages)
    lines = [f"# 对话分享 — {title}", ""]
    for msg in messages:
        role = msg.get("role", "unknown")
        ts = _format_timestamp(msg)
        content = _content_to_plain(msg.get("content", ""))
        if role == "user":
            lines.append(f"## 👤 User  {(' — ' + ts) if ts else ''}")
        elif role == "assistant":
            lines.append(f"## 🤖 Assistant  {(' — ' + ts) if ts else ''}")
        elif role == "tool":
            name = msg.get("name", "tool")
            lines.append(f"## 🔧 Tool: {name}")
        elif role == "system":
            lines.append(f"## ⚙️ System  {(' — ' + ts) if ts else ''}")
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
    clean = []
    for msg in messages:
        entry = {"role": msg.get("role", ""), "content": msg.get("content", "")}
        for key in ("name", "model_name", "timestamp", "tool_call_id", "tool_calls"):
            if msg.get(key):
                entry[key] = msg[key]
        clean.append(entry)
    return json.dumps(clean, ensure_ascii=False, indent=2)


# ── HTML 导出 ──────────────────────────────────────────────────────


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _export_html(messages: List[Dict]) -> str:
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
                paragraphs = str(block.get("text", "")).split("\n")
                p_html = "</p><p>".join(_escape_html(p) for p in paragraphs)
                content_html += f"<p>{p_html}</p>"
            elif bt == "reasoning":
                rc = _escape_html(str(block.get("content", "")))
                content_html += f'<details class="reasoning"><summary>思考过程</summary><pre>{rc}</pre></details>'
            elif bt in ("image_url", "input_image", "image"):
                content_html += '<div class="image-placeholder">[图片]</div>'
            elif bt == "tool_result":
                name = block.get("name", "tool")
                result = _escape_html(str(block.get("result", ""))[:500])
                content_html += (
                    f'<div class="tool-result"><strong>工具: {_escape_html(name)}</strong><pre>{result}</pre></div>'
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
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f5f5f5; color: #222; padding: 24px; max-width: 900px; margin: 0 auto; line-height: 1.7; }}
h1 {{ font-size: 22px; margin-bottom: 20px; padding-bottom: 12px; border-bottom: 1px solid #ddd; color: #333; }}
.bubble {{ margin-bottom: 14px; padding: 12px 16px; border-radius: 10px; background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
.bubble-user {{ border-left: 4px solid #3b82f6; }}
.bubble-assistant {{ border-left: 4px solid #f59e0b; }}
.bubble-tool {{ border-left: 4px solid #10b981; }}
.bubble-other {{ border-left: 4px solid #9ca3af; }}
.meta {{ font-size: 13px; font-weight: 600; color: #555; margin-bottom: 6px; }}
.ts {{ font-weight: 400; color: #999; font-size: 12px; margin-left: 8px; }}
.body p {{ margin-bottom: 6px; }}
.body pre {{ background: #f0f0f0; padding: 8px 12px; border-radius: 6px; overflow-x: auto; font-size: 13px; margin: 6px 0; }}
.tool-result {{ background: #f0fdf4; border: 1px solid #d1fae5; border-radius: 6px; padding: 8px 12px; margin: 6px 0; }}
.tool-result strong {{ display: block; margin-bottom: 4px; color: #059669; }}
.tool-result pre {{ background: transparent; padding: 0; font-size: 13px; white-space: pre-wrap; }}
details.reasoning {{ background: #fef9c3; border: 1px solid #fde68a; border-radius: 6px; padding: 6px 10px; margin: 6px 0; }}
details.reasoning summary {{ font-weight: 600; color: #a16207; cursor: pointer; }}
details.reasoning pre {{ background: transparent; padding: 6px 0 0; font-size: 13px; white-space: pre-wrap; color: #713f12; }}
.image-placeholder {{ display: inline-block; background: #f0f0f0; padding: 16px 24px; border-radius: 6px; color: #999; }}
@media (prefers-color-scheme: dark) {{
    body {{ background: #1a1a2e; color: #e0e0e0; }}
    h1 {{ color: #eee; border-bottom-color: #333; }}
    .bubble {{ background: #16213e; }}
    .meta {{ color: #aaa; }}
    .ts {{ color: #777; }}
    .body pre {{ background: #0f3460; }}
    .tool-result {{ background: #0a2e1a; border-color: #14532d; }}
    details.reasoning {{ background: #2e250a; border-color: #713f12; }}
}}
</style>
</head>
<body>
<h1>{title}</h1>
{"".join(bubbles)}
</body>
</html>"""


# ── 分享卡片内容组件 ──────────────────────────────────────────────


class ShareCardContent(QWidget):
    """分享卡片的内容（格式选择 + 预览 + 操作按钮），由 BaseSettingsCard 包裹"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._messages: List[Dict] = []
        self._selected_format = "markdown"
        self._setup_ui()

    def _setup_ui(self):
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(6)

        # ── 格式选择 ──
        self._format_btns = {}
        fmt_layout = QHBoxLayout()
        fmt_layout.setSpacing(4)
        Colors.refresh()
        for fmt_id, fmt_name in [
            ("markdown", "📝 Markdown"),
            ("json", "📊 JSON"),
            ("html", "🌐 HTML"),
        ]:
            btn = QPushButton(fmt_name, self)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(30)
            btn.setFont(get_unified_font(11))
            btn.setStyleSheet(self._btn_style(False))
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
        self._copy_btn.setFixedHeight(34)
        self._copy_btn.setFont(get_unified_font(11, bold=True))
        self._copy_btn.setStyleSheet(self._btn_style(False))
        self._copy_btn.clicked.connect(self._on_copy)

        self._save_btn = QPushButton("💾 保存文件", self)
        self._save_btn.setCursor(Qt.PointingHandCursor)
        self._save_btn.setFixedHeight(34)
        self._save_btn.setFont(get_unified_font(11, bold=True))
        self._save_btn.setStyleSheet(self._btn_style(False))
        self._save_btn.clicked.connect(self._on_save_file)

        self._upload_btn = QPushButton("🔗 生成链接", self)
        self._upload_btn.setCursor(Qt.PointingHandCursor)
        self._upload_btn.setFixedHeight(34)
        self._upload_btn.setFont(get_unified_font(11, bold=True))
        self._upload_btn.setStyleSheet(self._btn_style(False))
        self._upload_btn.clicked.connect(self._on_upload)

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

        self._select_format(self._selected_format)

    def _btn_style(self, selected: bool) -> str:
        Colors.refresh()
        if selected:
            return (
                f"QPushButton {{ background: {Colors.SELECTED_BG}; border: 1px solid {Colors.TAB_ACTIVE_BG}; "
                f"border-radius: 5px; padding: 4px 10px; color: {Colors.TEXT_PRIMARY}; }}"
            )
        return (
            f"QPushButton {{ background: {Colors.CONTENT_BG}; border: 1px solid {Colors.BORDER}; "
            f"border-radius: 5px; padding: 4px 10px; color: {Colors.TEXT_PRIMARY}; }}"
            f"QPushButton:hover {{ background: {Colors.HOVER_BG_STRONG}; border: 1px solid {Colors.TEXT_ACCENT}; }}"
        )

    def refresh_style(self):
        """主题切换时刷新按钮样式"""
        Colors.refresh()
        for fid, btn in self._format_btns.items():
            btn.setStyleSheet(self._btn_style(fid == self._selected_format))
        self._copy_btn.setStyleSheet(self._btn_style(False))
        self._save_btn.setStyleSheet(self._btn_style(False))
        self._upload_btn.setStyleSheet(self._btn_style(False))
        self._preview_area.setStyleSheet(
            f"QScrollArea {{ background: {Colors.CONTENT_BG}; border: 1px solid {Colors.BORDER}; "
            f"border-radius: 6px; }}" + get_unified_scrollbar_style(4)
        )
        self._preview_content.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; background: transparent; padding: 8px;")
        self._empty_label.setStyleSheet(f"color: {Colors.INPUT_PLACEHOLDER}; background: transparent; padding: 20px;")

    def _select_format(self, fmt_id: str):
        self._selected_format = fmt_id
        for fid, btn in self._format_btns.items():
            btn.setStyleSheet(self._btn_style(fid == fmt_id))

        if not self._messages:
            self._preview_content.setText("")
            self._empty_label.show()
            return
        self._empty_label.hide()

        try:
            text = self._get_export_text(fmt_id)
            self._preview_content.setText(text[:400] + ("…" if len(text) > 400 else ""))
        except Exception as e:
            self._preview_content.setText(f"生成预览失败: {e}")

    def _get_export_text(self, fmt: str = None) -> str:
        fmt = fmt or self._selected_format
        if fmt == "markdown":
            return _export_markdown(self._messages)
        elif fmt == "json":
            return _export_json(self._messages)
        elif fmt == "html":
            return _export_html(self._messages)
        return ""

    def set_messages(self, messages: List[Dict], title: str = ""):
        self._messages = messages
        self._select_format(self._selected_format)

    def _on_copy(self):
        if not self._messages:
            self._show_info("当前会话无消息", "warning")
            return
        text = self._get_export_text()
        if not text:
            self._show_info("内容为空", "warning")
            return
        QApplication.clipboard().setText(text)
        self._show_info("已复制到剪贴板", "success")

    def _on_save_file(self):
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
        ext = ".md" if fmt == "markdown" else f".{fmt}"
        default_name = f"对话分享_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        path, _ = QFileDialog.getSaveFileName(self, "保存文件", default_name, filter_str)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self._show_info(f"已保存到 {path}", "success")
        except Exception as e:
            self._show_info(f"保存失败: {e}", "error")

    def _on_upload(self):
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

    def _show_info(self, message: str, level: str = "info"):
        parent = self.window() or self.parent()
        kwargs = {
            "title": "",
            "content": message,
            "duration": 3000,
            "position": InfoBarPosition.TOP_RIGHT,
            "parent": parent,
        }
        if level == "success":
            InfoBar.success(**kwargs)
        elif level == "warning":
            InfoBar.warning(**kwargs)
        elif level == "error":
            InfoBar.error(**kwargs)
        else:
            InfoBar.info(**kwargs)
