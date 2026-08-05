# -*- coding: utf-8 -*-
"""F1 回归测试：W1 异步化波及的 hook 语义修复

覆盖：
1. W1-C1：engine.py PostAssistantMessage 调用必须显式 trigger_async=False
   （异步会让 command hook 输出经 _hook_message_queue 注入 LLM 对话，
    而旧行为是同步执行、输出仅在返回值中 → 静默丢弃）
2. W1-R2：异步回补注入前校验 session_id，会话切换后输出被丢弃
   （hook_manager 事件名编码 session_id + backend 解析校验）
"""

import ast
import textwrap
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parent.parent / "app"


def _read_src(rel: str) -> str:
    return (SRC_ROOT / rel).read_text(encoding="utf-8")


# ──────────────────────────────────────────────
# W1-C1：PostAssistantMessage 显式同步
# ──────────────────────────────────────────────


def test_engine_post_assistant_message_trigger_async_false():
    """engine.py PostAssistantMessage 必须显式 trigger_async=False。

    回归来源：W1 给 trigger_event 默认 trigger_async=True 后，engine.py:803
    未显式传参 → command hook 走后台异步 → 完成回调 `__async__:PostAssistantMessage`
    → on_hook_finished 不匹配 _PRE_DIALOG_EVENTS → 入 _hook_message_queue
    → chat_worker._inject_pending_hook_messages 无事件名过滤 → 注入 LLM。
    旧行为：同步执行、输出只在返回值（engine 不消费）→ 不注入。
    """
    src = _read_src("core/engines/ui/engine.py")
    # 直接检查原始源码（比 ast.unparse 更可靠，避免引号规范化干扰）
    method_src = src[src.index("def _trigger_post_assistant_message") :]
    # 截取到方法体结束（下一个顶层 def 或类）
    end = method_src.find("\n    def ", 10)
    if end > 0:
        method_src = method_src[:end]
    assert "PostAssistantMessage" in method_src, "方法内应触发 PostAssistantMessage"
    assert "trigger_async=False" in method_src, (
        "PostAssistantMessage 必须显式 trigger_async=False："
        "否则 W1 默认 True 会把 command hook 输出注入 LLM 对话（C1 阻塞回归）"
    )


# ──────────────────────────────────────────────
# W1-R2：异步回补注入的 session_id 校验
# ──────────────────────────────────────────────


def test_hook_manager_async_event_encodes_session_id():
    """hook_manager 异步事件名必须编码触发时的 session_id。

    格式 `__async__:<event>:<sid>`，供 backend 回补注入前校验。
    """
    src = _read_src("core/hook_manager.py")
    lines = src.splitlines()
    tree = ast.parse(src)
    # 用 AST 拿 _execute_hook 的精确行号范围，再对原始源码切片
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_execute_hook":
            target = node
            break
    assert target is not None, "未找到 _execute_hook"
    method_src = "\n".join(lines[target.lineno - 1 : target.end_lineno])
    assert "async_event" in method_src, "异步路径必须构造 async_event"
    assert "__async__:" in method_src, "异步事件名必须带 __async__ 前缀"
    assert "session_id" in method_src, "异步事件名必须编码触发时的 session_id（F1/W1-R2）"


def test_backend_on_hook_finished_parses_and_checks_session_id():
    """backend on_hook_finished 必须解析异步 session_id 并在预对话注入前校验。

    会话切换（当前 session != 触发时 session）→ 丢弃输出不注入。
    """
    src = _read_src("core/backend.py")
    # on_hook_finished 是 __init__ 内的闭包：直接检查 _PRE_DIALOG_EVENTS 定义段
    idx = src.index("_PRE_DIALOG_EVENTS =")
    # 从该定义向前回溯到 on_hook_finished 闭包起点，向后取足够文本覆盖校验逻辑
    seg_start = max(0, idx - 400)
    seg_end = min(len(src), idx + 2600)
    seg = src[seg_start:seg_end]
    assert "async_session_id" in seg, "必须解析异步事件名携带的 session_id"
    assert "session.session_id == async_session_id" in seg, (
        "预对话异步注入前必须校验当前会话 == 触发时会话（不一致丢弃）"
    )


# ──────────────────────────────────────────────
# 行为级：on_hook_finished 校验逻辑（独立模拟）
# ──────────────────────────────────────────────

from app.core.backend import _inject_hook_to_session  # noqa: E402
from app.core.chat_session import ChatSession  # noqa: E402


def _make_session(sid: str) -> ChatSession:
    s = ChatSession()
    s.session_id = sid
    return s


def test_async_inject_skips_wrong_session():
    """行为模拟：异步 hook 输出不应注入到切换后的 session。"""
    session_a = _make_session("sid-A")
    session_b = _make_session("sid-B")

    # 模拟 backend 校验逻辑：触发时为 sid-A，当前已切到 sid-B → 不注入
    async_session_id = "sid-A"
    current = session_b
    if current is not None and (not async_session_id or current.session_id == async_session_id):
        _inject_hook_to_session(current, "SessionStart", "output", "")
    assert len(session_b.messages) == 0, "会话切换后不得注入到错误 session"
    assert len(session_a.messages) == 0


def test_async_inject_accepts_matching_session():
    """行为模拟：当前 session 与触发时一致 → 正常注入。"""
    session_a = _make_session("sid-A")
    async_session_id = "sid-A"
    current = session_a
    if current is not None and (not async_session_id or current.session_id == async_session_id):
        _inject_hook_to_session(current, "SessionStart", "output", "")
    assert len(session_a.messages) == 1, "session 一致时应注入 hook 输出"
    assert session_a.messages[0]["_hook_event"] == "SessionStart"


def test_async_inject_accepts_no_sid_backward_compat():
    """行为模拟：事件名未携带 session_id（旧格式兼容）→ 允许注入当前 session。"""
    session_a = _make_session("sid-A")
    async_session_id = ""
    current = session_a
    if current is not None and (not async_session_id or current.session_id == async_session_id):
        _inject_hook_to_session(current, "SessionStart", "output", "")
    assert len(session_a.messages) == 1, "无 session_id 时保持向后兼容注入"
