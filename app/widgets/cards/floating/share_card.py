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
from typing import Any, Dict, List

from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import InfoBar, InfoBarPosition, ScrollArea

from app.utils.design_tokens import Colors, current_theme, get_unified_scrollbar_style
from app.utils.theme_manager import theme_manager
from app.utils.utils import get_unified_font

from app.utils.share_records import ensure_dirs, get_sessions_dir, insert_record


# ── 导出工具函数 ──────────────────────────────────────────────────


def _format_timestamp(msg: Dict[str, Any]) -> str:
    ts = msg.get("timestamp", "")
    if ts:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%m-%d %H:%M")
        except (ValueError, TypeError):
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
    """用与 in-app 一致的 markdown 扩展渲染正文（含语法高亮）"""
    try:
        md = markdown.Markdown(
            extensions=["fenced_code", "codehilite", "nl2br", "tables"],
            extension_configs={
                "codehilite": {
                    "noclasses": True,
                    "pygments_style": _pygments_style_for_theme(),
                    "guess_lang": False,
                }
            },
        )
        return md.convert(text or "")
    except Exception:
        return f"<p>{_escape_html(text or '')}</p>"


def _pygments_style_for_theme() -> str:
    """按当前主题明暗选 pygments 高亮配色（与 in-app 保持同一对）"""
    try:
        from app.utils.theme_manager import theme_manager

        return "friendly" if theme_manager.is_light_theme() else "dracula"
    except Exception:
        return "dracula"


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
    try:
        from app.utils.theme_manager import theme_manager as _tm

        is_light = bool(_tm.is_light_theme())
    except Exception:
        is_light = False

    def _pick(key: str, fallback: str) -> str:
        v = theme.get(key)
        return str(v) if v else fallback

    c = {
        "panel": _pick("card_bg_solid", "rgba(33, 33, 38, 0.96)"),
        "panel_soft": _pick("content_bg", "#2a2a2e"),
        "border": _pick("border", "#3d3d3d"),
        "border_strong": _pick("border_accent", "#f59e0b"),
        "text": _pick("text_primary", "#ffffff"),
        "text_secondary": _pick("text_secondary", "rgba(255, 255, 255, 0.5)"),
        "text_muted": _pick("text_muted", "#888888"),
        "accent": _pick("accent", "#66c6ff"),
        "accent_warm": _pick("accent_warm", "#f59e0b"),
        # 角色配色（对齐 in-app 用户卡 / 助手卡）
        "user_bg": _pick("user_card_bg", "rgba(102, 198, 255, 0.10)"),
        "user_accent": _pick("user_card_accent", "#66c6ff"),
        "assistant_bg": _pick("assistant_card_bg", "rgba(245, 158, 11, 0.08)"),
        "assistant_accent": _pick("assistant_card_accent", "#f59e0b"),
        # 工具 / 状态色
        "tool_accent": _pick("syntax_step", "#5fd18c"),
        "tool_color": _pick("syntax_tool", "#b45309"),
        # 代码块
        "code_bg": _pick("card_bg_dim", "rgba(255, 255, 255, 0.04)"),
        "inline_code_bg": _pick("hover_bg", "rgba(102, 198, 255, 0.12)"),
        "inline_code_fg": _pick("tag_accent_text", "#9bddff"),
        # 分隔与引用
        "divider": _pick("divider_color", "rgba(255, 255, 255, 0.08)"),
        "quote_fg": _pick("text_muted", "#888888"),
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
    --user-bg: {c["user_bg"]};
    --user-accent: {c["user_accent"]};
    --assistant-bg: {c["assistant_bg"]};
    --assistant-accent: {c["assistant_accent"]};
    --tool-accent: {c["tool_accent"]};
    --tool-color: {c["tool_color"]};
    --code-bg: {c["code_bg"]};
    --inline-code-bg: {c["inline_code_bg"]};
    --inline-code-fg: {c["inline_code_fg"]};
    --divider: {c["divider"]};
    --quote-fg: {c["quote_fg"]};
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
.container {{ max-width: 1180px; margin: 0 auto; padding: 32px 20px 72px; }}

/* ── 整体布局：左导航 + 右正文 ── */
.layout {{ display: flex; gap: 24px; align-items: flex-start; }}
.sidebar {{
    width: 262px;
    flex-shrink: 0;
    position: sticky;
    top: 20px;
    max-height: calc(100vh - 40px);
    overflow-y: auto;
    overscroll-behavior: contain;
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
.nav-item:hover {{ background: var(--code-bg); color: var(--text); }}
.nav-item.active {{
    background: var(--inline-code-bg);
    color: var(--text);
    border-left-color: var(--accent);
}}
.nav-icon {{
    width: 22px; height: 22px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; flex-shrink: 0;
    background: var(--code-bg);
}}
.nav-item.nav-user .nav-icon {{ background: var(--user-bg); }}
.nav-item.nav-assistant .nav-icon {{ background: var(--assistant-bg); }}
.nav-item.nav-tool .nav-icon {{ background: var(--code-bg); }}
.nav-item.nav-system .nav-icon {{ background: var(--code-bg); }}
.nav-text {{ display: flex; flex-direction: column; min-width: 0; line-height: 1.25; }}
.nav-role {{ font-size: 13px; font-weight: 600; white-space: nowrap; }}
.nav-snip {{
    font-size: 11px; color: var(--text-muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 170px;
}}
.main {{ flex: 1; min-width: 0; }}

/* ── 会话头部 ── */
.session-header {{ margin-bottom: 22px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
.session-title {{ font-size: 22px; font-weight: 700; color: var(--text); letter-spacing: .01em; }}
.session-meta {{ margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px; }}
.chip {{
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--code-bg);
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
    border-radius: 12px;
    padding: 14px 18px;
    margin-bottom: 14px;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.06);
}}
.msg-card.msg-user {{ background: var(--user-bg); border-left: 3px solid var(--user-accent); }}
.msg-card.msg-assistant {{ background: var(--assistant-bg); border-left: 3px solid var(--assistant-accent); }}
.msg-card.msg-tool {{ border-left: 3px solid var(--tool-accent); }}
.msg-card.msg-system {{ border-left: 3px solid var(--text-muted); }}
.msg-head {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
.avatar {{
    width: 28px; height: 28px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 14px; flex-shrink: 0;
    background: var(--code-bg);
}}
.avatar-user {{ background: var(--user-bg); }}
.avatar-assistant {{ background: var(--assistant-bg); }}
.avatar-tool {{ background: var(--code-bg); }}
.avatar-system {{ background: var(--code-bg); }}
.avatar-other {{ background: var(--code-bg); }}
.role-name {{ font-weight: 600; color: var(--text); font-size: 14px; }}
.ts {{ color: var(--text-muted); font-size: 12px; margin-left: auto; }}

/* ── markdown 正文 ── */
.msg-body .md > :first-child {{ margin-top: 0; }}
.msg-body .md > :last-child {{ margin-bottom: 0; }}
.msg-body p {{ margin: 8px 0; color: var(--text-secondary); }}
.msg-body h1, .msg-body h2, .msg-body h3, .msg-body h4 {{ color: var(--text); font-weight: 700; margin: 14px 0 8px; }}
.msg-body h1 {{ font-size: 1.35em; }}
.msg-body h2 {{ font-size: 1.2em; }}
.msg-body h3 {{ font-size: 1.08em; }}
.msg-body a {{ color: var(--accent); text-decoration: none; }}
.msg-body a:hover {{ text-decoration: underline; }}
.msg-body ul, .msg-body ol {{ margin: 8px 0; padding-left: 24px; }}
.msg-body li {{ margin: 4px 0; color: var(--text-secondary); }}
.msg-body strong {{ color: var(--text); font-weight: 600; }}
.msg-body em {{ color: var(--text-secondary); font-style: italic; }}
.msg-body code {{
    background: var(--inline-code-bg);
    color: var(--inline-code-fg);
    padding: 2px 6px;
    border-radius: 5px;
    font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    font-size: 0.88em;
}}
.msg-body pre {{
    background: var(--code-bg);
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
/* pygments 高亮产物（codehilite）：保留行内颜色，去掉自带的背景与内边距 */
.msg-body .codehilite {{ background: transparent; margin: 0; }}
.msg-body .codehilite pre {{ margin: 0; }}
.msg-body .codehilite pre span {{ background: transparent !important; }}
.msg-body blockquote {{
    border-left: 3px solid var(--border-strong);
    margin: 10px 0;
    padding: 4px 14px;
    color: var(--quote-fg);
}}
.msg-body hr {{ border: none; border-top: 1px solid var(--divider); margin: 14px 0; }}
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
    background: var(--code-bg);
    padding: 8px 12px;
    text-align: left;
    font-weight: 600;
    color: var(--text);
    border-bottom: 1px solid var(--border-strong);
}}
.msg-body td {{
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    color: var(--text-secondary);
}}
.msg-body tr:nth-child(even) {{ background: var(--code-bg); }}

/* ── 思考过程 ── */
details.reasoning {{
    background: var(--code-bg);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 6px 12px;
    margin: 8px 0;
}}
details.reasoning summary {{ font-weight: 600; color: var(--text-muted); cursor: pointer; }}
.reasoning-body {{ margin-top: 6px; color: var(--text-muted); }}

/* ── 工具调用 ── */
.tool-block {{
    background: var(--code-bg);
    border: 1px solid var(--border);
    border-left: 3px solid var(--tool-accent);
    border-radius: 8px;
    padding: 8px 12px;
    margin: 8px 0;
}}
.tool-head {{ font-weight: 600; color: var(--tool-color); margin-bottom: 4px; font-size: 13px; }}
.tool-body {{ font-size: 0.92em; }}

/* ── 图片占位 ── */
.image-block {{
    display: inline-block;
    background: var(--code-bg);
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
    // 按 DOM 顺序建立条目列表，不依赖对象键顺序
    var entries = [];
    items.forEach(function(a) {{
        var id = a.getAttribute('data-target');
        var el = id ? document.getElementById(id) : null;
        if (el) entries.push({{ id: id, el: el, link: a }});
    }});
    if (!entries.length) return;

    var sidebar = document.querySelector('.sidebar');
    var current = null;

    function setActive(id) {{
        if (current === id) return;
        current = id;
        var link = null;
        entries.forEach(function(e) {{
            e.link.classList.remove('active');
            if (e.id === id) link = e.link;
        }});
        if (!link) return;
        link.classList.add('active');
        // 高亮项滚出侧栏可视区时，把侧栏滚到能看见它的位置
        if (!sidebar) return;
        var linkTop = link.offsetTop;
        var linkBottom = linkTop + link.offsetHeight;
        var viewTop = sidebar.scrollTop;
        var viewBottom = viewTop + sidebar.clientHeight;
        if (linkTop < viewTop) {{
            sidebar.scrollTop = Math.max(0, linkTop - 8);
        }} else if (linkBottom > viewBottom) {{
            sidebar.scrollTop = linkBottom - sidebar.clientHeight + 8;
        }}
    }}

    // 以视口上方 15% 处为基准线，取基准线上方最靠下的一条消息作为「当前读到」
    // 不用 IntersectionObserver：它只在元素跨越观察边界时回调，滚动中大量位置
    // 没有任何元素落在判定带内，高亮会停在旧值或跳变。
    function pickActive() {{
        var baseY = window.innerHeight * 0.15;
        var best = entries[0];
        for (var i = 0; i < entries.length; i++) {{
            if (entries[i].el.getBoundingClientRect().top <= baseY) best = entries[i];
        }}
        setActive(best.id);
    }}

    var ticking = false;
    window.addEventListener('scroll', function() {{
        if (ticking) return;
        ticking = true;
        window.requestAnimationFrame(function() {{
            ticking = false;
            pickActive();
        }});
    }}, {{ passive: true }});
    window.addEventListener('resize', pickActive, {{ passive: true }});
    pickActive();
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
        self._selected_format = "json"
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
            ("json", "📊 JSON"),
            ("markdown", "📝 Markdown"),
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
        self._preview_area = ScrollArea(self)
        self._preview_area.setWidgetResizable(True)
        self._preview_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._preview_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._preview_area.setFrameShape(ScrollArea.NoFrame)
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

        # HTML 走 EdgeOne 在线渲染，按钮语义与其它格式不同
        self._upload_btn.setText("🌐 发布网页" if fmt_id == "html" else "🔗 生成链接")

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
        shared_dir = get_sessions_dir()
        ensure_dirs()
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
            insert_record(
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

        # 自动保存到 ~/.drifox/share/sessions/
        try:
            ensure_dirs()
        except Exception as e:
            self._show_info(f"创建分享目录失败: {e}", "error")
            return
        shared_dir = get_sessions_dir()

        title = (self._record.get("title") or _get_session_title(self._messages) or "对话分享").strip()
        safe_title = "".join(c for c in title if c not in r'<>:"/\|?*').rstrip(". ") or "对话分享"
        filename = f"{safe_title}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        save_path = shared_dir / filename

        try:
            save_path.write_text(text, encoding="utf-8")
        except Exception as e:
            self._show_info(f"保存分享文件失败: {e}", "error")
            return

        # HTML 走 EdgeOne 匿名部署（Gitee raw 对 HTML 返回 text/plain，浏览器只显示源码）
        if fmt == "html":
            self._on_deploy_html(text, save_path, title)
            return

        try:
            from app.gateway.utils.gitee_uploader import GiteeUploader

            uploader = GiteeUploader.get_instance()
            if not uploader.is_configured():
                self._show_info("Gitee 未配置（缺少 token/owner/repo）", "warning")
                # 已保存到本地，写入记录
                fmt_name = {"markdown": "md", "json": "json", "html": "html"}.get(fmt, fmt)
                insert_record(
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
            # ── M3：异步上传，避免点击"生成链接"后 UI 冻结（后台线程）──
            self._upload_btn.setEnabled(False)
            self._upload_btn.setText("⏳ 上传中…")
            self._pending_upload_path = save_path
            self._pending_title = title
            self._pending_fmt = fmt
            try:
                self._upload_thread = _ShareUploadThread(uploader, str(save_path))
                self._upload_thread.finished_signal.connect(self._on_upload_finished)
                self._upload_thread.finished.connect(self._upload_thread.deleteLater)
                self._upload_thread.start()
            except Exception as e:
                self._upload_btn.setEnabled(True)
                self._upload_btn.setText("🔗 生成链接")
                self._show_info(f"上传启动失败: {e}（文件已保存到本地）", "warning")
        except Exception as e:
            self._upload_btn.setEnabled(True)
            self._upload_btn.setText("🔗 生成链接")
            self._show_info(f"上传异常: {e}（文件已保存到本地）", "warning")

    def _on_deploy_html(self, html_text: str, save_path, title: str):
        """把 HTML 部署到 EdgeOne 匿名站点（后台线程，链 30 分钟内有效）"""
        self._upload_btn.setEnabled(False)
        self._upload_btn.setText("⏳ 发布中…")
        self._pending_upload_path = save_path
        self._pending_title = title
        self._pending_fmt = "html"
        try:
            self._deploy_thread = _EdgeOneDeployThread(html_text, title)
            self._deploy_thread.finished_signal.connect(self._on_deploy_finished)
            self._deploy_thread.finished.connect(self._deploy_thread.deleteLater)
            self._deploy_thread.start()
        except Exception as e:
            self._upload_btn.setEnabled(True)
            self._upload_btn.setText("🌐 发布网页")
            self._show_info(f"发布启动失败: {e}（文件已保存到本地）", "warning")

    def _on_deploy_finished(self, result):
        url, err = result
        save_path = self._pending_upload_path
        title = self._pending_title
        self._upload_btn.setEnabled(True)
        self._upload_btn.setText("🌐 发布网页")
        if err:
            self._show_info(f"发布失败: {err}（文件已保存到本地）", "warning")
            insert_record(
                type_="session",
                title=title,
                format_="html",
                file_path=str(save_path),
                ref_id=self._record.get("session_id", ""),
                extra_info={
                    "msg_count": len(self._messages),
                    "project": self._record.get("project", ""),
                },
            )
            return
        QApplication.clipboard().setText(url)
        self._show_info(f"链接已复制到剪贴板（30 分钟内有效）\n本地备份: {save_path.name}", "success")
        insert_record(
            type_="session",
            title=title,
            format_="html",
            file_path=str(save_path),
            upload_url=url,
            ref_id=self._record.get("session_id", ""),
            extra_info={
                "msg_count": len(self._messages),
                "project": self._record.get("project", ""),
            },
        )

    def _on_upload_finished(self, result):
        url, err = result
        save_path = self._pending_upload_path
        title = self._pending_title
        fmt = self._pending_fmt
        fmt_name = {"markdown": "md", "json": "json", "html": "html"}.get(fmt, fmt)
        self._upload_btn.setEnabled(True)
        self._upload_btn.setText("🔗 生成链接")
        if err:
            self._show_info(f"上传失败: {err}（文件已保存到本地）", "warning")
            insert_record(
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
        insert_record(
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

    def _show_info(self, message: str, level: str = "info"):
        from app.widgets.tab_manager_window import TabManagerWindow

        parent = TabManagerWindow.get_instance() or self.window() or self.parent()
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


class _ShareUploadThread(QThread):
    """后台线程：上传分享文件到 Gitee，避免点击"生成链接"后 UI 冻结。

    finished_signal 携带 upload_file 返回值 (url, err)，二者有且仅有一个非空。
    """

    finished_signal = pyqtSignal(object)

    def __init__(self, uploader, file_path, parent=None):
        super().__init__(parent)
        self._uploader = uploader
        self._file_path = file_path

    def run(self):
        try:
            self._result = self._uploader.upload_file(self._file_path)
        except Exception as e:
            self._result = ("", str(e))
        self.finished_signal.emit(self._result)


class _EdgeOneDeployThread(QThread):
    """后台线程：把 HTML 部署到 EdgeOne 匿名站点，避免点击"发布网页"后 UI 冻结。

    finished_signal 携带 deploy_html 返回值 (url, err)，二者有且仅有一个非空。
    """

    finished_signal = pyqtSignal(object)

    def __init__(self, html_text, title, parent=None):
        super().__init__(parent)
        self._html_text = html_text
        self._title = title

    def run(self):
        try:
            from app.gateway.utils.edgeone_deployer import EdgeOneDeployer

            self._result = EdgeOneDeployer.get_instance().deploy_html(self._html_text, self._title)
        except Exception as e:
            self._result = (None, str(e))
        self.finished_signal.emit(self._result)
