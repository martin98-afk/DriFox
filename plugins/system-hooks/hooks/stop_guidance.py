# -*- coding: utf-8 -*-
"""stop_guidance.py — Stop 阶段指令（条件注入）

替代原先三条静态 prompt hook（``builtin_stop_cleanup`` / ``builtin_stop_followup``
/ ``team_member_notify``）。规则正文已上移到 BuildSystemPrompt（编码契约），
这里只负责在 Stop 时刻发出极短的「现在该做什么」信号，并按条件裁剪：

- **收尾检查**：仅当本轮有过写/命令操作（PreToolUse 记的标记）才发出。
  纯问答轮不该提醒「检查未提交改动」。
- **追问预测**：仅当回答长度达到 ``_ASK_MIN_CHARS`` 才发出。短确认不需要追问，
  也顺带省掉一次续命请求（Stop 有注入 → chat_worker 续命一轮）。
- **团队回报告**：仅团队成员发出（matcher 仍是 completed，取消/出错轮不催报告）。

注入本身就是续命信号（``chat_worker`` 靠「Stop 有注入」决定是否重跑一轮），
所以三条都不命中时不返回任何内容 → 本轮直接结束，不再多花一次 API 请求。
"""

from __future__ import annotations

import threading
import time

# 追问预测的最短回答长度（字符）：低于此长度的回答视为短确认，不追问
_ASK_MIN_CHARS = 150
# 写操作标记的保鲜期：跨轮残留（如进程长时间空闲）后不再触发收尾检查
_DIRTY_TTL_S = 1800
_DIRTY_MAX = 256

_DIRTY_LOCK = threading.Lock()
_DIRTY: dict[str, float] = {}  # session_id -> 最近一次写操作时间

def record_write(event: str, context: dict) -> str:
    """PreToolUse hook：记录本会话发生过写/命令操作

    matcher 侧已限定 Bash|Edit|Write|MultiEdit，本函数只负责落标记，不注入任何
    内容（add_output_to_context=false）。子智能体的操作不计入主会话标记。
    """
    if event != "PreToolUse":
        return ""
    ctx = context or {}
    if ctx.get("current_role", "primary") != "primary":
        return ""
    session_id = str(ctx.get("session_id") or "")
    if not session_id:
        return ""
    with _DIRTY_LOCK:
        _DIRTY[session_id] = time.time()
        while len(_DIRTY) > _DIRTY_MAX:
            _DIRTY.pop(next(iter(_DIRTY)), None)
    return ""

def reset_round(event: str, context: dict) -> str:
    """PreUserMessage hook：新一轮用户消息开始时清掉上一轮残留的写操作标记

    缺这一步时，上一轮（如被取消）没被 Stop 消费的标记会在 TTL 内残留，
    下一轮纯问答也被误发「收尾」指令。
    """
    if event != "PreUserMessage":
        return ""
    session_id = str((context or {}).get("session_id") or "")
    if session_id:
        with _DIRTY_LOCK:
            _DIRTY.pop(session_id, None)
    return ""

def _take_dirty(session_id: str) -> bool:
    """读取并清除写操作标记（一轮一清，避免下一轮纯问答被误判）"""
    if not session_id:
        return False
    with _DIRTY_LOCK:
        ts = _DIRTY.pop(session_id, 0.0)
    return bool(ts) and (time.time() - ts) <= _DIRTY_TTL_S

def hook(event: str, context: dict) -> str:
    """Stop hook：按条件拼出本轮收尾指令，空串表示不注入、不续命"""
    if event != "Stop":
        return ""
    ctx = context or {}
    # 与原先 cleanup/followup 的 matcher="completed" 对齐：取消/出错轮不注入
    if ctx.get("reason", "completed") != "completed":
        return ""
    # 续命轮不再注入：chat_worker 侧已在 _stop_hook_active 时跳过本 hook，此处防御
    if ctx.get("stop_hook_active"):
        return ""

    parts: list[str] = []
    if _take_dirty(str(ctx.get("session_id") or "")):
        parts.append("收尾：按契约检查未提交改动与未完成项，清理自己制造的临时垃圾。")
    if len(str(ctx.get("last_assistant_message") or "")) >= _ASK_MIN_CHARS:
        parts.append("追问预测：按契约在回复末尾输出（或「无需追问」）。")
    if ctx.get("is_team_member"):
        parts.append("回报告：非 leader 按团队指引 team_send_message 发完整结果，正文 1 行进度。")
    return "\n".join(parts)
