# -*- coding: utf-8 -*-
"""结构化审批决策回传：decide_tool_permission 映射 + deny reason 回填

背景：审批原走 question 卡的文本标签反解析（`if "【允许】" in answer`），
标签文案一旦改字 → 4 个 `in` 全不命中 → 落入 else → **静默 deny**（无异常无日志）。
本链路改为结构化回传，彻底摆脱文本反解析。

覆盖：
1. allow / allow+round / allow+session 三种 remember 语义映射到 worker 对应参数
2. deny 携带 reason → worker 拒绝文案拼上理由
3. 不传 reason → 文案与改造前逐字一致（向后兼容）
4. 非法 decision → 按 deny 处理（安全默认）
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


class _FakeWorker:
    """记录 approve/deny 调用的假 worker（替代真实 QThread）"""

    def __init__(self):
        self.calls = []

    def approve_permission(self, tool_call_id, auto_allow=False, session_allow=False):
        self.calls.append(("approve", tool_call_id, auto_allow, session_allow))

    def deny_permission(self, tool_call_id, reason=""):
        self.calls.append(("deny", tool_call_id, reason))


class _FakeExecutor:
    """只暴露 _current_worker 的最小 executor 替身"""

    def __init__(self, worker):
        self._current_worker = worker


def _make_engine(worker):
    """构造带假 executor 的 UIEngine（绕过 __init__ 的重依赖）"""
    from app.core.engines.ui.engine import UIEngine

    engine = UIEngine.__new__(UIEngine)
    engine._conversation_executor = _FakeExecutor(worker)
    return engine


def _make_backend(engine):
    """构造只带 _chat_engine 的 backend 替身（验证门面转发）"""
    from app.core.conversation.backend import ChatBackend

    backend = ChatBackend.__new__(ChatBackend)
    backend._chat_engine = engine
    return backend


# ── 一、engine.decide_tool_permission 的四条映射 ──


def test_decide_allow_maps_to_approve():
    """allow + remember="" → approve_permission，auto/session 均 False"""
    worker = _FakeWorker()
    _make_engine(worker).decide_tool_permission("call_1", "allow")
    assert worker.calls == [("approve", "call_1", False, False)]


def test_decide_allow_round_maps_to_auto_allow():
    """allow + remember="round" → auto_allow=True"""
    worker = _FakeWorker()
    _make_engine(worker).decide_tool_permission("call_2", "allow", "round")
    assert worker.calls == [("approve", "call_2", True, False)]


def test_decide_allow_session_maps_to_session_allow():
    """allow + remember="session" → session_allow=True"""
    worker = _FakeWorker()
    _make_engine(worker).decide_tool_permission("call_3", "allow", "session")
    assert worker.calls == [("approve", "call_3", False, True)]


def test_decide_deny_carries_reason():
    """deny + reason → deny_permission 收到 reason"""
    worker = _FakeWorker()
    _make_engine(worker).decide_tool_permission("call_4", "deny", "", "别删这个目录")
    assert worker.calls == [("deny", "call_4", "别删这个目录")]


def test_unknown_decision_falls_back_to_deny():
    """非法 decision → deny（安全默认：不因参数异常而放行）"""
    worker = _FakeWorker()
    _make_engine(worker).decide_tool_permission("call_5", "maybe", "round")
    assert worker.calls[0][0] == "deny"


# ── 二、backend 门面转发 ──


def test_backend_facade_forwards_decide():
    worker = _FakeWorker()
    backend = _make_backend(_make_engine(worker))
    backend.decide_tool_permission("call_6", "allow", "session")
    assert worker.calls == [("approve", "call_6", False, True)]


def test_backend_facade_deny_passes_reason():
    worker = _FakeWorker()
    backend = _make_backend(_make_engine(worker))
    backend.deny_tool_permission("call_7", "换个路径")
    assert worker.calls == [("deny", "call_7", "换个路径")]


def test_backend_facade_no_engine_is_noop():
    """engine 未就绪（None）时门面静默返回，不抛异常"""
    from app.core.conversation.backend import ChatBackend

    backend = ChatBackend.__new__(ChatBackend)
    backend._chat_engine = None
    backend.decide_tool_permission("call_8", "allow")
    backend.deny_tool_permission("call_9", "x")


# ── 三、worker 侧：deny reason 写入与拒绝文案拼接 ──


def _make_worker_with_pending(tool_call_id="call_p", tool_name="bash"):
    """构造 worker 并预置一条待审批请求（不启动线程）"""
    from app.core.workers.chat_worker import OpenAIChatWorker

    w = OpenAIChatWorker(
        messages=[],
        session_messages=[],
        llm_config={"模型名称": "gpt-4", "API_KEY": "k", "API_URL": "https://x/v1", "温度": 0},
    )
    w._permission_pending = {"tool_call_id": tool_call_id, "tool_name": tool_name, "arguments": {}}
    w._permission_approved = False
    w._permission_deny_reason = ""
    return w


def test_deny_permission_stores_reason():
    """deny_permission 写入 reason，供拒绝文案读取"""
    w = _make_worker_with_pending()
    w.deny_permission("call_p", "命令写错了")
    assert w._permission_deny_reason == "命令写错了"
    assert w._permission_pending is None
    assert w._permission_approved is False


def test_deny_without_reason_backward_compatible():
    """不传 reason → 拒绝理由为空（文案走原文案分支）"""
    w = _make_worker_with_pending()
    w.deny_permission("call_p")
    assert w._permission_deny_reason == ""


def test_approve_ignores_reason_field():
    """approve 路径不受 deny reason 影响"""
    w = _make_worker_with_pending()
    w.approve_permission("call_p", auto_allow=True)
    assert w._permission_approved is True
    assert w._permission_pending is None
    assert w._permission_cache.is_allowed("bash") is True


def test_deny_reason_produces_feedback_text():
    """拒绝理由写进 tool 结果文案（模型可见）——直测生产实现"""
    from app.core.workers.chat_worker import _build_deny_message

    assert _build_deny_message("别删这个目录") == "Error: Permission denied by user. User feedback: 别删这个目录"


def test_deny_without_reason_text_unchanged():
    """无 reason → 结果文案与改造前逐字一致（向后兼容）"""
    from app.core.workers.chat_worker import _build_deny_message

    assert _build_deny_message("") == "Error: Permission denied by user"


def test_cleanup_resets_deny_reason():
    """cleanup() 清空失败理由，防止跨轮次串味"""
    w = _make_worker_with_pending()
    w._permission_deny_reason = "残留理由"
    w.cleanup()
    assert w._permission_deny_reason == ""
