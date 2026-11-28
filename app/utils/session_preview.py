# -*- coding: utf-8 -*-
"""会话列表展示辅助（相对时间 / 消息预览）

原本定义在 ``app/widgets/cards/settings/history_card.py`` 内，被历史卡片之外的
模块（消息卡片时间戳、归档预览）复用。历史卡片已迁移到 ``history-manager`` 插件，
故这两个**通用**辅助上移到 ``app/utils/``，避免主程序或插件反向依赖某个具体卡片的
实现模块。

约定：不依赖任何 Qt / 卡片模块，纯函数，便于单测。
"""

import datetime
import re
from typing import Any, Dict, List

# ======== 回复预览清洗（通知正文用） ========
# 通知正文若直接截 raw content，会把协议块当正文：回复以 `<plan>` 开头时，
# toast 里只剩「目标/路径/风险」三行。下面这套规则把协议块与 markdown 标记
# 剥掉，只留可读正文。
# 与 card_render_core 的协议块正则刻意分开：那个模块依赖 Qt/插件注册表，
# 本模块的约定是纯函数、可单独单测。
_REPLY_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_REPLY_TILDE_FENCE_RE = re.compile(r"~~~.*?(?:~~~|\Z)", re.DOTALL)
# think/tool/hook/system-reminder/plan/mood 块：整块丢弃（含未闭合到文末的流式残留）
_REPLY_PROTO_BLOCK_RE = re.compile(
    r"<(think|tool|hook|system-reminder|plan|mood)\b[^>]*>.*?(?:</\1>|\Z)",
    re.DOTALL | re.IGNORECASE,
)
# 追问块同属非正文：通知里展示追问没有意义
_REPLY_ASK_BLOCK_RE = re.compile(r"<ask\b[^>]*>.*?(?:</ask>|\Z)", re.DOTALL | re.IGNORECASE)
# 兜底用：只去标签、保留块内文本
_REPLY_ANY_TAG_RE = re.compile(r"</?[a-zA-Z][\w-]*(?:\s[^<>]{0,300})?/?>")
_REPLY_HEADING_RE = re.compile(r"^#{1,6}\s+")
_REPLY_QUOTE_RE = re.compile(r"^>\s?")
_REPLY_BULLET_RE = re.compile(r"^[-*+]\s+")
_REPLY_NUMBERED_RE = re.compile(r"^\d+[.)]\s+")
_REPLY_RULE_RE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_REPLY_TABLE_SEP_RE = re.compile(r"^\|?(?:\s*:?-{2,}:?\s*\|)+[\s:|-]*$")
_REPLY_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_REPLY_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_REPLY_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_REPLY_ITALIC_RE = re.compile(r"\*([^*\n]+)\*")
_REPLY_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_REPLY_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
# 行内代码遮罩：先挖成占位符，避免代码里字面写出的 <plan>/<tool> 被当成协议块
# 起点（会一路吞到文末，后面的正文全部丢失）。剥完再还原。
_REPLY_TOKEN_RE = re.compile(r"\x00(\d+)\x00")
_REPLY_TABLE_PIPE_RE = re.compile(r"\s*\|\s*")
_CLIP_BOUNDARY_CHARS = "。！？!?；;…"


def _flatten_markdown(text: str) -> str:
    """把 markdown 压成单行纯文本（行级前缀剥除 + 行内标记剥离 + 空白折叠）"""
    lines: List[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or _REPLY_RULE_RE.match(s) or _REPLY_TABLE_SEP_RE.match(s):
            continue
        s = _REPLY_HEADING_RE.sub("", s)
        s = _REPLY_QUOTE_RE.sub("", s)
        s = _REPLY_BULLET_RE.sub("", s)
        s = _REPLY_NUMBERED_RE.sub("", s)
        lines.append(s)

    flat = " ".join(lines)
    flat = _REPLY_IMAGE_RE.sub(" ", flat)
    flat = _REPLY_LINK_RE.sub(r"\1", flat)
    flat = _REPLY_BOLD_RE.sub(r"\1", flat)
    flat = _REPLY_ITALIC_RE.sub(r"\1", flat)
    flat = _REPLY_STRIKE_RE.sub(r"\1", flat)
    flat = _REPLY_INLINE_CODE_RE.sub(r"\1", flat)
    flat = _REPLY_ANY_TAG_RE.sub(" ", flat)
    flat = _REPLY_TABLE_PIPE_RE.sub(" ", flat)
    return " ".join(flat.split())


def _clip_preview(text: str, max_len: int) -> str:
    """截断到 max_len：优先收在句末标点，否则硬截并补省略号"""
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    for i in range(len(cut), 0, -1):
        if cut[i - 1] in _CLIP_BOUNDARY_CHARS:
            return cut[:i]
    return cut + "..."


def get_reply_preview(content: Any, max_len: int = 100) -> str:
    """把 assistant 回复原文清洗成通知正文预览（单行纯文本）。

    剥除顺序：代码围栏 → 协议块（think/tool/hook/system-reminder/plan/mood）
    → 追问块 → markdown 标记 → 折叠空白 → 截断。
    先剥围栏再剥协议块：代码块里字面写出的 ``<plan>`` 不能当协议块起点，
    否则会一路吞到文末，把真正的正文一起丢掉。
    整条回复只有协议块时返回空串（plan/mood 是内部计划，不该当通知正文），
    由调用方决定兜底文案。
    """
    raw = str(content or "")
    if not raw.strip():
        return ""

    # 1) 行内代码先遮罩（里面可能有 <plan> 字面量）
    stash: List[str] = []

    def _stash(match: "re.Match[str]") -> str:
        stash.append(match.group(1))
        return f"\x00{len(stash) - 1}\x00"

    body = _REPLY_INLINE_CODE_RE.sub(_stash, raw)
    # 2) 剥围栏代码块与协议块
    body = _REPLY_FENCE_RE.sub(" ", body)
    body = _REPLY_TILDE_FENCE_RE.sub(" ", body)
    body = _REPLY_PROTO_BLOCK_RE.sub(" ", body)
    body = _REPLY_ASK_BLOCK_RE.sub(" ", body)
    # 3) 压平 markdown 后还原行内代码
    plain = _flatten_markdown(body)
    if stash:
        plain = _REPLY_TOKEN_RE.sub(lambda m: stash[int(m.group(1))], plain)
    return _clip_preview(plain, max_len)


def format_relative_time(time_str: str) -> str:
    """将时间字符串（``%Y-%m-%d %H:%M:%S``）转换为相对时间显示"""
    if not time_str or time_str == "未知":
        return "更早"
    try:
        session_time = datetime.datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
        now = datetime.datetime.now()
        diff = now - session_time

        if diff.total_seconds() < 60:
            return "刚刚"
        elif diff.total_seconds() < 3600:
            minutes = int(diff.total_seconds() / 60)
            return f"{minutes}分钟前"
        elif diff.total_seconds() < 86400:
            hours = int(diff.total_seconds() / 3600)
            return f"{hours}小时前"
        elif diff.days == 1:
            return "昨天"
        elif diff.days < 7:
            return f"{diff.days}天前"
        else:
            return time_str[5:10] if len(time_str) >= 10 else time_str
    except (ValueError, TypeError):
        return time_str[5:10] if time_str and len(time_str) >= 10 else "更早"


def get_message_preview(messages: List[Dict], max_len: int = 50) -> str:
    """从消息列表中提取预览文本（取最后一条真实 user 消息）

    跳过 hook 注入消息（``role="user"`` 但带 ``_hook_event`` 标记），避免预览
    显示 hook 内容。
    """
    if not messages:
        return ""
    for msg in reversed(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        # 跳过 hook 消息（role=user 但带 _hook_event 标记），避免预览显示 hook 内容
        if msg.get("_hook_event"):
            continue
        if role == "user" and content:
            if isinstance(content, list):
                content = " ".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
            return content[:max_len].strip() + ("..." if len(content) > max_len else "")
    return ""
