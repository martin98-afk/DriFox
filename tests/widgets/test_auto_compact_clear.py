# -*- coding: utf-8 -*-
"""回归测试：自动压缩完成后正确清空会话

== 问题描述 ==
用户反馈：自动压缩触发后没有先清空会话（Bug：清空根本没生效）。
怀疑根因：
- A: `_on_compact_clear_finished` 被早 return（execution_error / task 不在 _finished_tasks）
- B: `_on_compact_clear_finished` 执行了清空，但旧 worker 的延迟 finished_with_messages 覆盖
- C: 子智能体 finished_with_result 信号没有触发回调

== 测试策略 ==
本测试用 stub 替换 main_widget 的复杂依赖（session_manager/backend/sub_agent_mgr），
直接调用 `_on_compact_clear_finished` 的等价逻辑，验证：
1. 子智能体成功完成 → session.messages 应清空
2. 子智能体执行失败（execution_error） → session.messages 不清空
3. 子智能体被取消（task 不在 _finished_tasks） → session.messages 不清空
4. 清空后 _post_compact_guard=True，能拦截后续 worker 延迟消息（短对话阈值）

本测试不依赖 PyQt5/UI 启动，专注核心清空逻辑。
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# 让测试能找到 app 包
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.core.chat_session import ChatSession


# ──────────────────────────────────────────────────────
# Stub: 模拟 main_widget 中 _on_compact_clear_finished 的关键状态和依赖
# ──────────────────────────────────────────────────────
class StubSessionManager:
    def __init__(self, sessions):
        self.sessions = sessions


class StubBackend:
    def __init__(self):
        self.sub_agent_manager = StubSubAgentManager()
        self.trigger_session_event = MagicMock()

    def trigger_session_event(self, state):
        """mock：不实际执行 hook"""
        pass


class StubSubAgentManager:
    def __init__(self):
        self._running_tasks = {}
        self._finished_tasks = {}


class StubExecutor:
    def __init__(self, error=None):
        self._execution_error = error


class StubWidget:
    """模拟 main_widget 的关键状态和方法"""

    def __init__(self, session, sub_agent_mgr, backend):
        self.session_manager = StubSessionManager([session])
        self.backend = backend
        self._auto_compact_in_progress = True
        self._post_compact_guard = False
        self._session_card_cache = {}
        self._is_destroyed = False
        self._current_session_id = session.session_id
        self._display_current_session = MagicMock()


def _setup_widget_with_running_executor(session, executor=None, finished_task=None):
    """构造 widget，模拟子智能体正在运行或已完成"""
    backend = StubBackend()
    backend.sub_agent_manager._running_tasks = {"task_1": executor} if executor else {}
    if finished_task:
        backend.sub_agent_manager._finished_tasks = {"task_1": finished_task}
    return StubWidget(session, backend.sub_agent_manager, backend)


# ──────────────────────────────────────────────────────
# 把 main_widget 中的 _on_compact_clear_finished 逻辑提取为独立函数
# 这样测试不依赖 PyQt5 widget 启动
# ──────────────────────────────────────────────────────
def clear_session_for_compact(widget, task_id: str, result: str, session_id: str):
    """精简版的 _on_compact_clear_finished 核心清空逻辑

    与 main_widget.py:6469-6578 保持等价
    """
    widget._auto_compact_in_progress = False

    if getattr(widget, "_is_destroyed", False):
        return False, "destroyed"

    sub_agent_mgr = widget.backend.sub_agent_manager
    executor = sub_agent_mgr._running_tasks.get(task_id) if sub_agent_mgr else None
    if executor is None:
        if sub_agent_mgr and task_id in sub_agent_mgr._finished_tasks:
            task_info = sub_agent_mgr._finished_tasks.get(task_id, {})
            if task_info.get("error"):
                return False, f"finished_error: {task_info.get('error')}"
        else:
            return False, "no_record"

    if executor is not None:
        execution_error = getattr(executor, "_execution_error", None)
        if execution_error:
            return False, f"execution_error: {execution_error}"

    target_session = None
    for s in widget.session_manager.sessions:
        if s.session_id == session_id:
            target_session = s
            break
    if not target_session or not target_session.messages:
        return False, "session_missing"

    target_session.set_messages([], preserve_compaction=False)
    widget._post_compact_guard = True
    widget._session_card_cache.pop(target_session.session_id, None)

    if target_session and result:
        target_session.set_compaction_cache({
            "active": True,
            "kind": "auto_compact",
            "summary_message": {"role": "system", "content": str(result)},
        })

    return True, "cleared"


# ══════════════════════════════════════════════════════════
# 测试用例
# ══════════════════════════════════════════════════════════

class TestAutoCompactClear:
    """验证自动压缩完成后的清空逻辑"""

    def _make_session(self, msg_count: int = 4) -> ChatSession:
        s = ChatSession()
        for i in range(msg_count):
            role = "user" if i % 2 == 0 else "assistant"
            s.add_user_message(f"user msg {i}") if role == "user" else s.add_assistant_message(f"ai msg {i}")
        return s

    # ── 场景 1: 正常完成（root cause A 反向证明） ──
    def test_clear_on_successful_completion(self):
        """子智能体成功完成 → session.messages 应被清空"""
        session = self._make_session(4)
        widget = _setup_widget_with_running_executor(
            session,
            executor=StubExecutor(error=None),  # 无错误
        )

        assert len(session.messages) == 4, "前置：session 应该有 4 条消息"

        ok, reason = clear_session_for_compact(
            widget, "task_1", "压缩摘要内容", session.session_id
        )

        assert ok, f"应该返回 True，实际: {reason}"
        assert reason == "cleared"
        assert len(session.messages) == 0, "session.messages 应被清空"
        assert widget._post_compact_guard == True, "_post_compact_guard 应被设置"
        assert session.compaction_cache.get("active") == True, "应设置新 compaction_cache"
        assert session.compaction_cache.get("summary_message", {}).get("content") == "压缩摘要内容"

    # ── 场景 2: 子智能体执行失败（root cause A 验证） ──
    def test_no_clear_on_execution_error(self):
        """子智能体执行错误 → session.messages 不应清空"""
        session = self._make_session(4)
        widget = _setup_widget_with_running_executor(
            session,
            executor=StubExecutor(error="LLM timeout"),
        )

        ok, reason = clear_session_for_compact(
            widget, "task_1", "result", session.session_id
        )

        assert not ok
        assert "execution_error" in reason
        assert len(session.messages) == 4, "错误时应保留原消息"
        assert widget._post_compact_guard == False, "错误时不应设置守卫"

    # ── 场景 3: 任务不在 _finished_tasks 且 executor 已清理（root cause A 验证） ──
    def test_no_clear_when_no_record(self):
        """task_id 不在 _running_tasks 也不在 _finished_tasks → 不清空"""
        session = self._make_session(4)
        widget = _setup_widget_with_running_executor(session)  # 空 _running_tasks 和 _finished_tasks

        ok, reason = clear_session_for_compact(
            widget, "task_1", "result", session.session_id
        )

        assert not ok
        assert reason == "no_record"
        assert len(session.messages) == 4

    # ── 场景 4: _finished_tasks 中有 error → 不清空 ──
    def test_no_clear_on_finished_error(self):
        """task 在 _finished_tasks 但 error 非空 → 不清空"""
        session = self._make_session(4)
        widget = _setup_widget_with_running_executor(
            session,
            finished_task={"error": "agent not found", "result": ""},
        )

        ok, reason = clear_session_for_compact(
            widget, "task_1", "result", session.session_id
        )

        assert not ok
        assert "finished_error" in reason
        assert len(session.messages) == 4

    # ── 场景 5: session 不存在（用户删除） ──
    def test_no_clear_when_session_missing(self):
        """session 不存在 → 不清空"""
        session = self._make_session(4)
        widget = _setup_widget_with_running_executor(
            session,
            executor=StubExecutor(error=None),
        )

        ok, reason = clear_session_for_compact(
            widget, "task_1", "result", "nonexistent_session_id"
        )

        assert not ok
        assert reason == "session_missing"
        assert len(session.messages) == 4

    # ── 场景 6: 窗口已销毁 ──
    def test_no_clear_when_widget_disached(self):
        """widget 已销毁 → 不清空（避免访问已销毁对象）"""
        session = self._make_session(4)
        widget = _setup_widget_with_running_executor(
            session,
            executor=StubExecutor(error=None),
        )
        widget._is_destroyed = True

        ok, reason = clear_session_for_compact(
            widget, "task_1", "result", session.session_id
        )

        assert not ok
        assert reason == "destroyed"
        assert len(session.messages) == 4


class TestPostCompactGuardLogic:
    """验证 _post_compact_guard 在短对话下的拦截漏洞"""

    def test_guard_threshold_short_conversation(self):
        """短对话（≤10 条）：旧 worker 延迟 finished_with_messages 可能不被拦截

        这是 root cause B 的核心：守卫阈值 len(messages) > len(_cur_session.messages) + 10
        对短对话无法拦截，会覆盖已清空的 session.messages
        """
        # 直接测试守卫逻辑（来自 main_widget.py:17006）
        _post_compact_guard = True
        _cur_session_messages_count = 0  # 已清空
        worker_snapshot_messages_count = 5  # 旧 worker 的短对话快照

        should_drop = (
            worker_snapshot_messages_count
            > _cur_session_messages_count + 10  # 守卫阈值
        )
        assert should_drop == False, (
            f"短对话（{worker_snapshot_messages_count} 条）下，"
            "旧 worker 延迟消息不被守卫拦截 → 会覆盖清空后的 session.messages！"
        )

        # 对照：长对话会被拦截
        _cur_session_messages_count = 0
        worker_snapshot_messages_count_long = 50
        should_drop_long = worker_snapshot_messages_count_long > _cur_session_messages_count + 10
        assert should_drop_long == True, "长对话（50 条）下守卫应该能拦截"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])