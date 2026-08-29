# -*- coding: utf-8 -*-
"""agent_trace 数据模型。

定义轨迹查看器用到的核心数据结构：
- ``EntryKind``：条目类型枚举（SYSTEM/USER/CONTEXT/ASSISTANT/TOOL）
- ``TraceRecord``：单条轨迹记录（一条 history message 或一条实时事件）
- 工具函数：格式化时间、截断摘要、推断类型
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class EntryKind(str, Enum):
    """轨迹条目类型 — 对应 DeepSeek Harness 的彩色左侧 type 标签。"""

    SYSTEM = "SYSTEM"
    USER = "USER"
    CONTEXT = "CONTEXT"  # hook 注入（user 角色 + _hook_event）
    ASSISTANT = "ASSISTANT"
    TOOL = "TOOL"

    @property
    def label(self) -> str:
        return self.value


# 不同类型对应的「左色块」颜色（与深/浅主题兼容）。
# 设计参考 DeepSeek Harness：每条记录左侧有彩色竖条 + 大写 type 标签。
# 颜色取自 qfluentwidgets 主题色 token，浅色模式单独提供 hex。
ENTRY_KIND_COLORS = {
    EntryKind.SYSTEM: "#7AA2F7",  # 蓝
    EntryKind.USER: "#E0AF68",  # 金
    EntryKind.CONTEXT: "#9ECE6A",  # 绿
    EntryKind.ASSISTANT: "#F7768E",  # 红
    EntryKind.TOOL: "#7DCFFF",  # 青
}


@dataclass
class TraceRecord:
    """单条轨迹记录。

    Attributes:
        kind: 类型标签（SYSTEM/USER/CONTEXT/ASSISTANT/TOOL）。
        label: 显示在 type 标签右侧的标题（agent-instructions / skill / 自定义）。
        preview: 内容首行预览（用户点击右侧详情面板时仍看 raw）。
        raw: 完整原始内容（字符串或 JSON 化后的字符串）。
        source: 来源描述（"SessionStart hook" / "session.messages[5]" /
                "tool_call_started" 等）。
        start_ts: 起始时间戳（time.time()）。允许 0.0 表示未知。
        end_ts: 终止时间戳；None 或 0 表示尚未结束（流式中）。
        is_pending: 是否仍处于 in-flight（流式生成、tool 调用未返回）。
        is_error: 是否失败（仅 TOOL 适用）。
        meta: 额外结构化元数据（tool_name/arguments/result/error 等）。
    """

    kind: EntryKind
    label: str
    preview: str
    raw: str
    source: str = ""
    start_ts: float = 0.0
    end_ts: float = 0.0
    is_pending: bool = False
    is_error: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        """已完成毫秒数（in-flight 返回瞬时毫秒）。-1 表示未启动。"""
        if self.start_ts <= 0:
            return -1
        end = self.end_ts if self.end_ts > 0 else time.time()
        return max(0, int((end - self.start_ts) * 1000))

    @property
    def absolute_time(self) -> str:
        """绝对时间戳（HH:MM:SS.mmm），未启动返回 '-'。"""
        if self.start_ts <= 0:
            return "-"
        return _format_hms_ms(self.start_ts)


def _format_hms_ms(epoch: float) -> str:
    """把浮点时间戳格式化为 HH:MM:SS.mmm。"""
    import datetime as _dt

    dt = _dt.datetime.fromtimestamp(epoch)
    return dt.strftime("%H:%M:%S") + f".{int((epoch - int(epoch)) * 1000):03d}"


def format_duration(ms: int) -> str:
    """人类可读时长。

    - <1000ms：``830 ms``
    - <60s：``2.45 s``
    - 其余：``1m 23s``
    """
    if ms < 0:
        return "-"
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f} s"
    minutes, ms = divmod(ms, 60_000)
    seconds = ms // 1000
    return f"{minutes}m {seconds}s"


def format_duration_compact(ms: int) -> str:
    """压缩版时长（TimelinePanel 用）。"""
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes, ms = divmod(ms, 60_000)
    seconds = ms // 1000
    return f"{minutes}m{seconds}s"


def truncate(text: str, max_chars: int = 120) -> str:
    """首行截断。空内容返回 '（空）'。"""
    if not text:
        return "（空）"
    head = text.strip().split("\n", 1)[0].strip()
    if len(head) <= max_chars:
        return head
    return head[: max_chars - 1] + "…"


def infer_message_kind(message: Dict[str, Any]) -> EntryKind:
    """把 ``session.messages`` 中的 dict 映射到 ``EntryKind``。"""
    role = message.get("role", "")
    hook_event = message.get("_hook_event")
    if role == "system":
        return EntryKind.SYSTEM
    if hook_event:
        # 用户 prompt 也会带 _hook_event，但 UserPromptSubmit 是 USER。
        if hook_event == "UserPromptSubmit":
            return EntryKind.USER
        # PreToolUse / PostToolUse / SessionStart / Stop → CONTEXT
        return EntryKind.CONTEXT
    if role == "user":
        return EntryKind.USER
    if role == "tool":
        return EntryKind.TOOL
    if role == "assistant":
        return EntryKind.ASSISTANT
    return EntryKind.CONTEXT


def message_label(message: Dict[str, Any]) -> str:
    """条目展示标题（type 标签右侧的那行字）。

    优先级：
    1. Hook 事件名（SessionStart / PreToolUse / …）
    2. tool name （role=tool 时取 tool 名）
    3. role 首字母大写
    """
    hook_event = message.get("_hook_event")
    if hook_event:
        # UserPromptSubmit 不带 hook 时仍是 USER，所以这里跳过 UserPromptSubmit
        if hook_event != "UserPromptSubmit":
            return hook_event
    role = message.get("role", "")
    if role == "tool":
        name = message.get("name") or message.get("tool_name") or "tool"
        return f"{name}"
    if role == "system":
        return "System Prompt"
    if role == "user":
        return "User"
    if role == "assistant":
        return "Assistant"
    return role or "Entry"


def message_source(idx: int, extra: str = "") -> str:
    """把内部来源信息格式化为可读字符串。"""
    base = f"session.messages[{idx}]"
    return f"{base}{(' · ' + extra) if extra else ''}"


def content_to_text(content: Any) -> str:
    """统一把 content（str | list[dict]）转成字符串。

    多模态 list 走 str()，调用方按需要单独美化。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        import json

        return json.dumps(content, ensure_ascii=False, indent=2)
    except Exception:
        return str(content)
