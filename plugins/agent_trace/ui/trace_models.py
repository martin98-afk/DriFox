# -*- coding: utf-8 -*-
"""agent_trace 数据模型。

定义轨迹查看器用到的核心数据结构：
- ``EntryKind``：条目类型枚举（SYSTEM/USER/CONTEXT/ASSISTANT/TOOL）
- ``Lane``：时间线泳道（Input / Model / Tools，对齐 DeepSeek Harness）
- ``TraceRecord``：单条轨迹记录
- 工具函数：颜色辅助（修 QColor("rgba(...)") 解析失败变黑的坑）、时长格式化、内容提取

⚠️ 坑点 P022：QColor 不支持 "rgba(r,g,b,a)" 字符串（解析失败静默返回黑色），
所有带透明度的颜色必须用 :func:`with_alpha` 从 hex 派生。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from PyQt5.QtGui import QColor


class EntryKind(str, Enum):
    """轨迹条目类型 — 对应 DeepSeek Harness 的彩色 type 标签。"""

    SYSTEM = "SYSTEM"
    USER = "USER"
    CONTEXT = "CONTEXT"  # hook 注入（user 角色 + _hook_event）
    ASSISTANT = "ASSISTANT"
    TOOL = "TOOL"

    @property
    def label(self) -> str:
        return self.value

    @property
    def lane(self) -> "Lane":
        """该类型归属的时间线泳道。"""
        if self == EntryKind.TOOL:
            return Lane.TOOLS
        if self == EntryKind.ASSISTANT:
            return Lane.MODEL
        return Lane.INPUT


class Lane(Enum):
    """时间线泳道 — 对齐 DeepSeek Harness 的 Input / Model / Tools。"""

    INPUT = "Input"
    MODEL = "Model"
    TOOLS = "Tools"


LANE_ORDER: tuple = (Lane.INPUT, Lane.MODEL, Lane.TOOLS)

# 类型主色（hex，深浅主题通吃）
ENTRY_KIND_COLORS: Dict[EntryKind, str] = {
    EntryKind.SYSTEM: "#7AA2F7",  # 蓝灰
    EntryKind.USER: "#E0AF68",  # 金橙
    EntryKind.CONTEXT: "#9ECE6A",  # 绿
    EntryKind.ASSISTANT: "#BB9AF7",  # 紫
    EntryKind.TOOL: "#7DCFFF",  # 青
}


def kind_color(kind: EntryKind) -> QColor:
    """类型主色（不透明 QColor）。"""
    return QColor(ENTRY_KIND_COLORS.get(kind, "#888888"))


def with_alpha(color: QColor, alpha: int) -> QColor:
    """从 QColor 派生带透明度的颜色（禁止用 "rgba(...)" 字符串构造 QColor）。"""
    c = QColor(color)
    c.setAlpha(max(0, min(255, int(alpha))))
    return c


@dataclass
class TraceRecord:
    """单条轨迹记录。

    Attributes:
        kind: 类型标签（SYSTEM/USER/CONTEXT/ASSISTANT/TOOL）。
        label: 显示在 type 标签右侧的标题（hook 事件名 / tool 名 / role）。
        preview: 内容首行预览。
        raw: 完整原始内容（字符串或 JSON 化后的字符串）。
        source: 来源描述（"hook · PostToolUse" / "session.messages[5]" 等）。
        start_ts: 起始时间戳（time.time() 浮点秒）。0.0 表示未知。
        end_ts: 终止时间戳。0 / <=start 表示瞬时（消息写入类事件）。
        is_pending: 是否仍处于 in-flight（流式生成、tool 调用未返回）。
        is_error: 是否失败（仅 TOOL 适用）。
        turn_no: 所属对话轮次（从 1 起；collector 按真实 USER 消息计数填充）。
        meta: 额外结构化元数据（tool_call_id / arguments / result 等）。
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
    turn_no: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def lane(self) -> Lane:
        return self.kind.lane

    @property
    def duration_ms(self) -> int:
        """持续毫秒数。

        - 未启动（start_ts<=0）→ -1
        - in-flight → now - start（由 UI 定时器驱动重绘）
        - 已结束且 end<=start（瞬时消息写入）→ 0
        - 其余 → end - start（**固定值**，不随时间增长）
        """
        if self.start_ts <= 0:
            return -1
        if self.is_pending:
            return max(0, int((time.time() - self.start_ts) * 1000))
        if self.end_ts <= self.start_ts:
            return 0
        return int((self.end_ts - self.start_ts) * 1000)

    @property
    def absolute_time(self) -> str:
        """绝对时间戳（HH:MM:SS），未知返回 '-'。"""
        if self.start_ts <= 0:
            return "-"
        return _format_hms(self.start_ts)


def _format_hms(epoch: float) -> str:
    """把浮点时间戳格式化为 HH:MM:SS。"""
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def format_duration(ms: int) -> str:
    """人类可读时长：<1000ms → ``830 ms``；<60s → ``2.45 s``；其余 → ``1m 23s``。"""
    if ms < 0:
        return "-"
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.2f} s"
    minutes, rem = divmod(ms, 60_000)
    seconds = rem // 1000
    return f"{minutes}m {seconds}s"


def format_duration_compact(ms: int) -> str:
    """压缩版时长（时间线条内文字用）。"""
    if ms < 0:
        return "-"
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    minutes, rem = divmod(ms, 60_000)
    seconds = rem // 1000
    return f"{minutes}m{seconds}s"


def truncate(text: str, max_chars: int = 120) -> str:
    """首行截断。空内容返回 '（空）'。"""
    if not text:
        return "（空）"
    head = text.strip().split("\n", 1)[0].strip()
    if len(head) <= max_chars:
        return head
    return head[: max_chars - 1] + "…"


def content_to_text(content: Any) -> str:
    """把 message.content（str | list[dict] | 其它）统一转成可读文本。

    多模态 list 优先提取 ``text`` 段；tool_use / tool_result 段 JSON 化，
    避免 ``str(list)`` 那种 python-repr 噪音。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for seg in content:
            if isinstance(seg, dict):
                if isinstance(seg.get("text"), str):
                    parts.append(seg["text"])
                else:
                    try:
                        import json as _json

                        parts.append(_json.dumps(seg, ensure_ascii=False, indent=2))
                    except Exception:
                        parts.append(str(seg))
            else:
                parts.append(str(seg))
        return "\n".join(p for p in parts if p)
    try:
        import json as _json

        return _json.dumps(content, ensure_ascii=False, indent=2)
    except Exception:
        return str(content)


def infer_message_kind(message: Dict[str, Any]) -> EntryKind:
    """把 ``session.messages`` 中的 dict 映射到 ``EntryKind``。"""
    role = message.get("role", "")
    hook_event = message.get("_hook_event")
    if role == "system":
        return EntryKind.SYSTEM
    if hook_event:
        # UserPromptSubmit 是真实用户输入的增强，归 USER；其余 hook 注入归 CONTEXT
        if hook_event == "UserPromptSubmit":
            return EntryKind.USER
        return EntryKind.CONTEXT
    if role == "user":
        return EntryKind.USER
    if role == "tool":
        return EntryKind.TOOL
    if role == "assistant":
        return EntryKind.ASSISTANT
    return EntryKind.CONTEXT


def is_real_user_message(message: Dict[str, Any]) -> bool:
    """是否真实用户输入（非 hook 注入）— 用于 turn 计数。"""
    return message.get("role") == "user" and not message.get("_hook_event")


def message_label(message: Dict[str, Any]) -> str:
    """条目展示标题（type 标签右侧那行字）。"""
    hook_event = message.get("_hook_event")
    if hook_event and hook_event != "UserPromptSubmit":
        return hook_event
    role = message.get("role", "")
    if role == "tool":
        return message.get("name") or message.get("tool_name") or "tool"
    if role == "system":
        return "System Prompt"
    if role == "user":
        return "User"
    if role == "assistant":
        return "Assistant"
    return role or "Entry"


def message_source(idx: int, extra: str = "") -> str:
    """把内部来源信息格式化为可读字符串。"""
    base = f"messages[{idx}]"
    return f"{base}{(' · ' + extra) if extra else ''}"
