# -*- coding: utf-8 -*-
"""
分享卡片内容组件

包含格式选择、预览、操作按钮。
通过 BaseSettingsCard 包裹后嵌入 TopCardContainer。
"""

import json
import markdown
import os
import subprocess
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

from app.utils.design_tokens import Colors, current_theme, get_unified_scrollbar_style
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_unified_font

# 延迟导入：share-history 插件（模块加载时 plugins/ 可能未入 path）


def _insert_share_record(**kwargs):
    """延迟导入并插入分享记录，插件不存在则静默跳过"""
    try:
        from plugins.share_history.ui.db import insert_record
        insert_record(**kwargs)
    except ImportError:
        pass
    except Exception as e:
        from loguru import logger
        logger.debug(f"[ShareCard] share-history 记录写入失败: {e}")


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


def _export_markdown(messages: List[Dict], record: Dict = None) -> str:
    record = record or {}
    title = record.get("title") or _get_session_title(messages)
    lines = [f"# 对话分享 — {title}", ""]
    if record.get("project") or record.get("last_time"):
        meta_bits = []
        if record.get("project"):
            meta_bits.append(f"项目：{record['project']}")
        if record.get("last_time"):
            meta_bits.append(f"时间：{record['last_time']}")
        if record.get("message_count") is not None:
            meta_bits.append(f"共 {record['message_count']} 轮")
        lines.append("> " + "　".join(meta_bits))
        lines.append("")
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


def _export_json(record: Dict) -> str:
    """导出与归档（archive）一致的完整 session 记录（含全部元信息），而非仅消息列表"""
    return json.dumps(record, ensure_ascii=False, indent=2)


# ── HTML 导出 ──────────────────────────────────────────────────────


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _md_to_html(text: str) -> str:
    """用与 in-app 一致的 markdown 扩展渲染正文"""
    try:
        md = markdown.Markdown(extensions=["fenced_code", "nl2br", "tables"])
        return md.convert(text or "")
    except Exception:
        return f"<p>{_escape_html(text or '')}</p>"


def _role_meta(role: str, msg: Dict) -> tuple:
    if role == "user":
        return "👤", "User", "avatar-user"
    if role == "assistant":
        return "🤖", "Assistant", "avatar-assistant"
    if role == "tool":
        name = msg.get("name", "tool")
        return "🔧", f"Tool · {name}", "avatar-tool"
    if role == "system":
        return "⚙️", "System", "avatar-system"
    return "💬", role, "avatar-other"


def _message_snippet(msg: Dict, limit: int = 28) -> str:
    """左侧导航用：取消息首段纯文本作为预览摘要"""
    text = _content_to_plain(msg.get("content", "")).replace("\n", " ").strip()
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return text or "（空消息）"


def _render_message_body(blocks: List[Dict]) -> str:
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            parts.append(f'<div class="md">{_md_to_html(_escape_html(str(block)))}</div>')
            continue
        bt = block.get("type", "text")
        if bt == "text":
            parts.append(f'<div class="md">{_md_to_html(block.get("text", ""))}</div>')
        elif bt == "reasoning":
            content = block.get("content", "") or block.get("text", "")
            parts.append(
                '<details class="reasoning" open>'
                "<summary>💭 思考过程</summary>"
                f'<div class="reasoning-body md">{_md_to_html(content)}</div>'
                "</details>"
            )
        elif bt in ("image_url", "input_image", "image"):
            parts.append('<div class="image-block">🖼️ 图片内容</div>')
        elif bt == "tool_result":
            name = block.get("name", "tool")
            result = block.get("result", "") or block.get("content", "")
            parts.append(
                '<div class="tool-block">'
                f'<div class="tool-head">🔧 工具调用 · {_escape_html(name)}</div>'
                f'<div class="tool-body md">{_md_to_html(str(result))}</div>'
                "</div>"
            )
        else:
            parts.append(f'<div class="md">{_md_to_html(str(block.get("text", block.get("content", ""))))}</div>')
    return "".join(parts)


def _render_message_card(msg: Dict, index: int) -> str:
    role = msg.get("role", "unknown")
    ts = _format_timestamp(msg)
    blocks = _ensure_content_blocks(msg.get("content", ""))
    body = _render_message_body(blocks)
    icon, label, avatar_cls = _role_meta(role, msg)
    ts_html = f'<span class="ts">{_escape_html(ts)}</span>' if ts else ""
    return (
        f'<div class="msg-card msg-{role}" id="msg-{index}">'
        f'<div class="msg-head">'
        f'<div class="avatar {avatar_cls}">{icon}</div>'
        f'<div class="role-name">{_escape_html(label)}</div>'
        f"{ts_html}"
        f"</div>"
        f'<div class="msg-body">{body}</div>'
        f"</div>"
    )


def _export_html(messages: List[Dict], record: Dict = None) -> str:
    record = record or {}
    title = _escape_html(record.get("title") or _get_session_title(messages))

    # ── 主题色（与 in-app 消息卡片一致）──
    theme = current_theme()
    c = {
        "panel": theme.get("card_bg_solid", "rgba(33, 33, 38, 0.96)"),
        "panel_soft": theme.get("content_bg", "#2a2a2e"),
        "border": theme.get("border", "#3d3d3d"),
        "border_strong": theme.get("border_accent", "#f59e0b"),
        "text": theme.get("text_primary", "#ffffff"),
        "text_secondary": theme.get("text_secondary", "rgba(255, 255, 255, 0.5)"),
        "text_muted": theme.get("text_muted", "#888888"),
        "accent": theme.get("accent", "#66c6ff"),
        "accent_warm": theme.get("accent_warm", "#f59e0b"),
    }
    win = {}
    try:
        win = theme_manager.get_current_theme().get("window", {})
    except Exception:
        win = {}
    grad_start = win.get("gradient_start", "rgba(10, 14, 22, 255)")
    grad_end = win.get("gradient_end", "rgba(15, 20, 30, 255)")
    page_bg = f"linear-gradient(135deg, {grad_start} 0%, {grad_end} 100%)"

    # ── 会话元信息头部 ──
    meta_chips = []
    if record.get("project"):
        meta_chips.append(("项目", str(record["project"])))
    if record.get("last_time"):
        meta_chips.append(("时间", str(record["last_time"])))
    if record.get("message_count") is not None:
        meta_chips.append(("消息", f"{record['message_count']} 轮"))
    model_name = next((m.get("model_name") for m in messages if m.get("model_name")), None)
    if model_name:
        meta_chips.append(("模型", str(model_name)))
    chips_html = "".join(
        f'<span class="chip"><span class="chip-k">{_escape_html(k)}</span>{_escape_html(v)}</span>'
        for k, v in meta_chips
    )
    header_html = (
        f'<div class="session-header">'
        f'<h1 class="session-title">{title}</h1>'
        f'<div class="session-meta">{chips_html}</div>'
        f"</div>"
    )

    cards_html = "".join(_render_message_card(m, i) for i, m in enumerate(messages))

    # ── 左侧导航栏 ──
    nav_items = []
    for i, m in enumerate(messages):
        role = m.get("role", "unknown")
        icon, label, _ = _role_meta(role, m)
        snip = _escape_html(_message_snippet(m))
        nav_items.append(
            f'<a class="nav-item nav-{role}" href="#msg-{i}" data-target="msg-{i}">'
            f'<span class="nav-icon">{icon}</span>'
            f'<span class="nav-text">'
            f'<span class="nav-role">{_escape_html(label)}</span>'
            f'<span class="nav-snip">{snip}</span>'
            f"</span>"
            f"</a>"
        )
    nav_html = "".join(nav_items)
    msg_count = len(messages)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
:root {{
    --panel: {c["panel"]};
    --panel-soft: {c["panel_soft"]};
    --border: {c["border"]};
    --border-strong: {c["border_strong"]};
    --text: {c["text"]};
    --text-secondary: {c["text_secondary"]};
    --text-muted: {c["text_muted"]};
    --accent: {c["accent"]};
    --accent-warm: {c["accent_warm"]};
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", "Hiragino Sans GB", Roboto, sans-serif;
    background: {page_bg};
    background-attachment: fixed;
    color: var(--text);
    line-height: 1.7;
    -webkit-font-smoothing: antialiased;
}}
.container {{ max-width: 1100px; margin: 0 auto; padding: 28px 16px 60px; }}

/* ── 整体布局：左导航 + 右正文 ── */
.layout {{ display: flex; gap: 24px; align-items: flex-start; }}
.sidebar {{
    width: 248px;
    flex-shrink: 0;
    position: sticky;
    top: 16px;
    max-height: calc(100vh - 32px);
    overflow-y: auto;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 8px;
    scrollbar-width: thin;
}}
.sidebar::-webkit-scrollbar {{ width: 6px; }}
.sidebar::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 3px; }}
.sidebar-title {{
    font-size: 12px;
    font-weight: 700;
    color: var(--text-muted);
    letter-spacing: .06em;
    text-transform: uppercase;
    padding: 4px 10px 8px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}}
.sidebar-count {{ color: var(--accent); font-weight: 600; }}
.nav-item {{
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 7px 10px;
    border-radius: 8px;
    text-decoration: none;
    color: var(--text-secondary);
    transition: background .12s ease, color .12s ease;
    border-left: 2px solid transparent;
}}
.nav-item:hover {{ background: rgba(255, 255, 255, 0.04); color: var(--text); }}
.nav-item.active {{
    background: rgba(102, 198, 255, 0.10);
    color: var(--text);
    border-left-color: var(--accent);
}}
.nav-icon {{
    width: 22px; height: 22px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; flex-shrink: 0;
    background: rgba(255, 255, 255, 0.10);
}}
.nav-item.nav-user .nav-icon {{ background: rgba(102, 198, 255, 0.16); }}
.nav-item.nav-assistant .nav-icon {{ background: rgba(245, 158, 11, 0.16); }}
.nav-item.nav-tool .nav-icon {{ background: rgba(95, 209, 140, 0.16); }}
.nav-item.nav-system .nav-icon {{ background: rgba(255, 255, 255, 0.16); }}
.nav-text {{ display: flex; flex-direction: column; min-width: 0; line-height: 1.25; }}
.nav-role {{ font-size: 13px; font-weight: 600; white-space: nowrap; }}
.nav-snip {{
    font-size: 11px; color: var(--text-muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 170px;
}}
.main {{ flex: 1; min-width: 0; }}

/* ── 会话头部 ── */
.session-header {{ margin-bottom: 22px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
.session-title {{ font-size: 22px; font-weight: 700; color: #fff; letter-spacing: .01em; }}
.session-meta {{ margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px; }}
.chip {{
    display: inline-flex; align-items: center; gap: 6px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 3px 12px;
    font-size: 12px;
    color: var(--text-secondary);
}}
.chip-k {{ color: var(--text-muted); }}

/* ── 移动端：导航转为顶部横向滚动条 ── */
@media (max-width: 760px) {{
    .layout {{ flex-direction: column; gap: 14px; }}
    .sidebar {{
        width: 100%; position: static; max-height: 160px;
        display: flex; flex-direction: column;
    }}
    .sidebar-nav {{ display: flex; flex-direction: column; gap: 2px; }}
}}

/* ── 消息卡片（对齐 in-app CardWidget）── */
.msg-card {{
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 16px;
    margin-bottom: 14px;
}}
.msg-card.msg-user {{ border-left: 3px solid var(--accent); }}
.msg-card.msg-assistant {{ border-left: 3px solid var(--accent-warm); }}
.msg-card.msg-tool {{ border-left: 3px solid #5fd18c; }}
.msg-card.msg-system {{ border-left: 3px solid var(--text-muted); }}
.msg-head {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
.avatar {{
    width: 28px; height: 28px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; flex-shrink: 0;
}}
.avatar-user {{ background: rgba(102, 198, 255, 0.16); }}
.avatar-assistant {{ background: rgba(245, 158, 11, 0.16); }}
.avatar-tool {{ background: rgba(95, 209, 140, 0.16); }}
.avatar-system {{ background: rgba(255, 255, 255, 0.10); }}
.avatar-other {{ background: rgba(255, 255, 255, 0.10); }}
.role-name {{ font-weight: 600; color: var(--text); font-size: 14px; }}
.ts {{ color: var(--text-muted); font-size: 12px; margin-left: auto; }}

/* ── markdown 正文 ── */
.msg-body .md > :first-child {{ margin-top: 0; }}
.msg-body .md > :last-child {{ margin-bottom: 0; }}
.msg-body p {{ margin: 8px 0; color: var(--text-secondary); }}
.msg-body h1, .msg-body h2, .msg-body h3, .msg-body h4 {{ color: #fff; font-weight: 700; margin: 12px 0 6px; }}
.msg-body h1 {{ font-size: 1.35em; }}
.msg-body h2 {{ font-size: 1.2em; }}
.msg-body h3 {{ font-size: 1.08em; }}
.msg-body a {{ color: var(--accent); text-decoration: none; }}
.msg-body a:hover {{ text-decoration: underline; }}
.msg-body ul, .msg-body ol {{ margin: 8px 0; padding-left: 24px; }}
.msg-body li {{ margin: 4px 0; color: var(--text-secondary); }}
.msg-body strong {{ color: #fff; font-weight: 600; }}
.msg-body em {{ color: #c4cedd; }}
.msg-body code {{
    background: rgba(102, 198, 255, 0.12);
    color: #9bddff;
    padding: 2px 6px;
    border-radius: 5px;
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 0.88em;
}}
.msg-body pre {{
    background: rgba(0, 0, 0, 0.28);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 14px;
    overflow-x: auto;
    margin: 10px 0;
}}
.msg-body pre code {{
    background: transparent;
    color: var(--text-secondary);
    padding: 0;
    font-size: 0.85em;
    line-height: 1.5;
}}
.msg-body blockquote {{
    border-left: 3px solid var(--border-strong);
    margin: 10px 0;
    padding: 4px 14px;
    color: var(--text-muted);
}}
.msg-body hr {{ border: none; border-top: 1px solid var(--border); margin: 14px 0; }}
.msg-body table {{
    width: 100%;
    border-collapse: collapse;
    margin: 10px 0;
    font-size: 0.92em;
    border: 1px solid var(--border);
    border-radius: 10px;
    overflow: hidden;
}}
.msg-body th {{
    background: rgba(255, 255, 255, 0.04);
    padding: 8px 12px;
    text-align: left;
    font-weight: 600;
    color: #fff;
    border-bottom: 1px solid var(--border-strong);
}}
.msg-body td {{
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    color: var(--text-secondary);
}}
.msg-body tr:nth-child(even) {{ background: rgba(255, 255, 255, 0.02); }}

/* ── 思考过程 ── */
details.reasoning {{
    background: rgba(255, 255, 255, 0.03);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 12px;
    margin: 8px 0;
}}
details.reasoning summary {{ font-weight: 600; color: var(--text-muted); cursor: pointer; }}
.reasoning-body {{ margin-top: 6px; color: var(--text-muted); }}

/* ── 工具调用 ── */
.tool-block {{
    background: rgba(95, 209, 140, 0.06);
    border: 1px solid rgba(95, 209, 140, 0.25);
    border-radius: 8px;
    padding: 8px 12px;
    margin: 8px 0;
}}
.tool-head {{ font-weight: 600; color: #5fd18c; margin-bottom: 4px; font-size: 13px; }}
.tool-body {{ font-size: 0.92em; }}

/* ── 图片占位 ── */
.image-block {{
    display: inline-block;
    background: rgba(255, 255, 255, 0.04);
    border: 1px dashed var(--border);
    border-radius: 8px;
    padding: 18px 26px;
    color: var(--text-muted);
}}
</style>
</head>
<body>
<div class="container">
<div class="layout">
<aside class="sidebar">
<div class="sidebar-title">对话导航 · <span class="sidebar-count">{msg_count} 条</span></div>
<nav class="sidebar-nav">
{nav_html}
</nav>
</aside>
<main class="main">
{header_html}
{cards_html}
</main>
</div>
</div>
<script>
(function() {{
    var items = Array.prototype.slice.call(document.querySelectorAll('.nav-item'));
    var map = {{}};
    items.forEach(function(a) {{
        var id = a.getAttribute('data-target');
        var el = document.getElementById(id);
        if (el) map[id] = a;
    }});
    var targets = Object.keys(map).map(function(id) {{ return document.getElementById(id); }});
    if (!('IntersectionObserver' in window) || !targets.length) return;
    var current = null;
    function setActive(id) {{
        if (current === id) return;
        current = id;
        items.forEach(function(a) {{ a.classList.remove('active'); }});
        if (map[id]) map[id].classList.add('active');
    }}
    var observer = new IntersectionObserver(function(entries) {{
        // 取当前视口内最靠上的可见消息
        var visible = entries.filter(function(e) {{ return e.isIntersecting; }})
            .sort(function(a, b) {{ return a.boundingClientRect.top - b.boundingClientRect.top; }});
        if (visible.length) setActive(visible[0].target.id);
    }}, {{ rootMargin: '-15% 0px -70% 0px', threshold: 0 }});
    targets.forEach(function(t) {{ observer.observe(t); }});
}})();
</script>
</body>
</html>"""


# ── 分享卡片内容组件 ──────────────────────────────────────────────


class ShareCardContent(QWidget):
    """分享卡片的内容（格式选择 + 预览 + 操作按钮），由 BaseSettingsCard 包裹"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._messages: List[Dict] = []
        self._record: Dict = {}
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
            if fmt_id == "html":
                self._preview_content.setText(
                    f"🌐 HTML 文档　·　{len(self._messages)} 条消息\n"
                    f"标题：{self._record.get('title') or _get_session_title(self._messages)}\n"
                    f"已套用当前主题卡片样式，复制/保存后在浏览器查看完整效果。"
                )
            else:
                text = self._get_export_text(fmt_id)
                self._preview_content.setText(text[:400] + ("…" if len(text) > 400 else ""))
        except Exception as e:
            self._preview_content.setText(f"生成预览失败: {e}")

    def _get_export_text(self, fmt: str = None) -> str:
        fmt = fmt or self._selected_format
        if fmt == "markdown":
            return _export_markdown(self._messages, self._record)
        elif fmt == "json":
            return _export_json(self._record)
        elif fmt == "html":
            return _export_html(self._messages, self._record)
        return ""

    def set_messages(self, record: Dict, title: str = ""):
        self._record = record or {}
        self._messages = self._record.get("messages", []) or []
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
        title = (self._record.get("title") or _get_session_title(self._messages) or "对话分享").strip()
        safe_title = "".join(c for c in title if c not in r'<>:"/\|?*').rstrip(". ") or "对话分享"
        shared_dir = Path.home() / ".drifox" / "shared"
        shared_dir.mkdir(parents=True, exist_ok=True)
        default_path = str(shared_dir / f"{safe_title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}")
        path, _ = QFileDialog.getSaveFileName(self, "保存文件", default_path, filter_str)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self._show_info(f"已保存到 {path}", "success")
            # ── 写入分享记录 ──
            fmt_name = {"markdown": "md", "json": "json", "html": "html"}.get(fmt, fmt)
            _insert_share_record(
                type_="session",
                title=self._record.get("title") or _get_session_title(self._messages) or "对话分享",
                format_=fmt_name,
                file_path=path,
                ref_id=self._record.get("session_id", ""),
                extra_info={
                    "msg_count": len(self._messages),
                    "project": self._record.get("project", ""),
                },
            )
            # ── 自动打开文件夹并选中文件 ──
            try:
                if os.name == "nt":
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                else:
                    folder = os.path.dirname(path)
                    if folder:
                        subprocess.Popen(["xdg-open", folder])
            except Exception as open_err:
                from loguru import logger

                logger.debug(f"[ShareCard] 打开文件夹失败: {open_err}")
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

        # 自动保存到 ~/.drifox/shared/
        shared_dir = Path.home() / ".drifox" / "shared"
        try:
            shared_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._show_info(f"创建分享目录失败: {e}", "error")
            return

        title = (self._record.get("title") or _get_session_title(self._messages) or "对话分享").strip()
        safe_title = "".join(c for c in title if c not in r'<>:"/\|?*').rstrip(". ") or "对话分享"
        filename = f"{safe_title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        save_path = shared_dir / filename

        try:
            save_path.write_text(text, encoding="utf-8")
        except Exception as e:
            self._show_info(f"保存分享文件失败: {e}", "error")
            return

        try:
            from app.gateway.utils.gitee_uploader import GiteeUploader

            uploader = GiteeUploader.get_instance()
            if not uploader.is_configured():
                self._show_info("Gitee 未配置（缺少 token/owner/repo）", "warning")
                # 已保存到本地，写入记录
                fmt_name = {"markdown": "md", "json": "json", "html": "html"}.get(fmt, fmt)
                _insert_share_record(
                    type_="session",
                    title=title,
                    format_=fmt_name,
                    file_path=str(save_path),
                    ref_id=self._record.get("session_id", ""),
                    extra_info={
                        "msg_count": len(self._messages),
                        "project": self._record.get("project", ""),
                    },
                )
                return
            self._upload_btn.setEnabled(False)
            self._upload_btn.setText("⏳ 上传中…")
            url, err = uploader.upload_file(str(save_path))
            self._upload_btn.setEnabled(True)
            self._upload_btn.setText("🔗 生成链接")
            if err:
                self._show_info(f"上传失败: {err}（文件已保存到本地）", "warning")
                fmt_name = {"markdown": "md", "json": "json", "html": "html"}.get(fmt, fmt)
                _insert_share_record(
                    type_="session",
                    title=title,
                    format_=fmt_name,
                    file_path=str(save_path),
                    ref_id=self._record.get("session_id", ""),
                    extra_info={
                        "msg_count": len(self._messages),
                        "project": self._record.get("project", ""),
                    },
                )
                return
            QApplication.clipboard().setText(url)
            self._show_info(f"链接已复制到剪贴板\n本地备份: {save_path.name}", "success")
            # ── 写入分享记录（含上传链接） ──
            fmt_name = {"markdown": "md", "json": "json", "html": "html"}.get(fmt, fmt)
            _insert_share_record(
                type_="session",
                title=title,
                format_=fmt_name,
                file_path=str(save_path),
                upload_url=url,
                ref_id=self._record.get("session_id", ""),
                extra_info={
                    "msg_count": len(self._messages),
                    "project": self._record.get("project", ""),
                },
            )
        except Exception as e:
            self._upload_btn.setEnabled(True)
            self._upload_btn.setText("🔗 生成链接")
            self._show_info(f"上传异常: {e}（文件已保存到本地）", "warning")

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
