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

import sys
import threading
import time
import types

# 追问预测的最短回答长度（字符）：低于此长度的回答视为短确认，不追问
_ASK_MIN_CHARS = 150
# 写操作标记的保鲜期：跨轮残留（如进程长时间空闲）后不再触发收尾检查
_DIRTY_TTL_S = 1800
_DIRTY_MAX = 256

# Bash 只读命令降噪：首命令命中白名单、且无重定向/命令替换时，不标记 dirty。
# 按 ; | && 换行 切段逐段判定：所有段都只读才算只读，任一写命令即照旧打标。
# 保守原则：拿不准一律返回 False（照旧打标），宁可多提醒也不漏收尾。
import re as _re

_READONLY_COMMANDS = frozenset({
    # 文件/目录列举与查看
    "ls", "dir", "find", "cat", "type", "head", "tail", "wc", "stat", "tree",
    "grep", "egrep", "fgrep", "rg", "sed", "awk", "less", "more",
    "pwd", "which", "where", "echo", "printf", "true", "false", "test",
    "hg", "svn",
    # PowerShell 只读 cmdlet（大小写不敏感匹配）
    "get-childitem", "get-content", "get-item", "get-command", "get-location",
    "select-object", "where-object", "foreach-object", "measure-object",
    "format-list", "format-table", "write-output", "write-host", "out-string",
    "resolve-path", "test-path",
})
# git 只认只读子命令；commit/push/add 等写子命令不在集合内 → 照旧打标
_GIT_READONLY_SUBCMDS = frozenset({
    "status", "diff", "log", "show", "branch", "blame", "ls-files", "ls-tree",
    "grep", "remote", "tag", "rev-parse", "config", "describe", "cat-file", "count-objects",
})
# 命令替换/重定向视为有副作用；管道与分号是只读组合，改为分段处理
_WRITE_OPS = (">>", ">", "$(", "`")
_CMD_SEPARATORS = _re.compile(r"[;|&]+|\n")
_ENV_PREFIX = _re.compile(r"^\$?[A-Za-z_]\w*=")


def _is_readonly_command(ctx: dict) -> bool:
    """Bash 工具：命令是否为纯只读。非 Bash 或无命令返回 False。"""
    tool = str(ctx.get("tool_name") or ctx.get("tool_name_native") or "")
    if tool.lower() not in ("bash", "shell", "powershell", "pwsh"):
        return False
    cmd = str(ctx.get("command") or "")
    if not cmd.strip():
        return False
    if any(op in cmd for op in _WRITE_OPS):
        return False
    for seg in _CMD_SEPARATORS.split(cmd):
        toks = seg.strip().split()
        while toks and _ENV_PREFIX.match(toks[0]):
            toks = toks[1:]
        if not toks:
            continue
        exe = toks[0].strip("\"'").lower()
        exe = exe.replace("\\", "/").rsplit("/", 1)[-1]
        if exe.endswith(".exe"):
            exe = exe[:-4]
        if exe == "git":
            sub = toks[1].strip("\"'").lower() if len(toks) > 1 else ""
            if sub not in _GIT_READONLY_SUBCMDS:
                return False
        elif exe not in _READONLY_COMMANDS:
            return False
    return True


# 状态挂 sys.modules 固定键（见 _state 注释），不能用模块级 dict
_STATE_KEY = "_drifox_stop_guidance_state"

def _state():
    """取跨模块实例共享的状态（DIRTY / LOCK）

    HookWorker 以「函数路径」为键缓存函数对象，同一文件的 record_write /
    reset_round / hook 会被各自 exec 成独立模块，模块级 dict 互不可见。
    实测后果：PreToolUse 记了写操作，Stop 读到的却是空表 → 收尾指令永不注入。
    sys.modules.setdefault 保证并发首建只留一份。
    """
    state = sys.modules.get(_STATE_KEY)
    if state is not None:
        return state
    created = types.ModuleType(_STATE_KEY)
    created.DIRTY = {}
    created.LOCK = threading.Lock()
    return sys.modules.setdefault(_STATE_KEY, created)

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
    # 只读命令降噪：Bash 的纯查询命令不改变文件状态，不标记 dirty
    if _is_readonly_command(ctx):
        return ""
    session_id = str(ctx.get("session_id") or "")
    if not session_id:
        return ""
    state = _state()
    with state.LOCK:
        state.DIRTY[session_id] = time.time()
        while len(state.DIRTY) > _DIRTY_MAX:
            state.DIRTY.pop(next(iter(state.DIRTY)), None)
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
        state = _state()
        with state.LOCK:
            state.DIRTY.pop(session_id, None)
    return ""

def _take_dirty(session_id: str) -> bool:
    """读取并清除写操作标记（一轮一清，避免下一轮纯问答被误判）"""
    if not session_id:
        return False
    state = _state()
    with state.LOCK:
        ts = state.DIRTY.pop(session_id, 0.0)
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
    if ctx.get("is_team_member") and not ctx.get("is_team_leader"):
        parts.append("回报告：按团队指引 team_send_message 发完整结果，正文 1 行进度。")
    return "\n".join(parts)
