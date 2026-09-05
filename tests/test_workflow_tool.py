# -*- coding: utf-8 -*-
"""workflow 工具单测：受限命名空间 / 钩子语义 / 上限 / impl 组装。"""
import time

import pytest

from plugins.workflow.tools.workflow_tool import (
    WorkflowError,
    WorkflowTimeoutError,
    _build_sandbox,
    _RunState,
)


class _FakeManager:
    """同步回调的假 SubAgentManager（execute_task 契约：成功 True + on_finished(result)；失败 False + on_error）"""

    def __init__(self, routes=None, fail_agents=()):
        self.routes = routes or {}
        self.fail_agents = set(fail_agents)
        self.calls = []

    def execute_task(self, task_id, agent_name, task_description,
                     on_finished=None, on_error=None, **kw):
        self.calls.append((agent_name, task_description, kw))
        if agent_name in self.fail_agents:
            if on_error:
                on_error(f"Agent not found: {agent_name}")
            return False
        if on_finished:
            on_finished(self.routes.get(agent_name, f"done:{agent_name}"))
        return True


class TestSandbox:
    def test_denies_dangerous_builtins(self):
        ns = _build_sandbox(args={})
        for name in ("open", "eval", "exec", "compile", "__import__", "input", "breakpoint"):
            with pytest.raises(NameError):
                exec(f"{name}('x')", ns)

    def test_common_builtins_available(self):
        ns = _build_sandbox(args={})
        exec("xs = sorted([3, 1, 2]); n = len(xs); s = sum(xs)", ns)
        assert ns["xs"] == [1, 2, 3]
        assert ns["n"] == 3 and ns["s"] == 6

    def test_preset_modules_and_args(self):
        ns = _build_sandbox(args={"files": ["a"]})
        assert ns["args"] == {"files": ["a"]}
        assert ns["json"].dumps and ns["math"].sqrt and ns["re"].match


class TestRunState:
    def test_reserve_counts(self):
        st = _RunState(max_total_agents=2, deadline=time.monotonic() + 60)
        st.reserve()
        st.reserve()
        assert st.started == 2

    def test_total_cap_raises(self):
        st = _RunState(max_total_agents=1, deadline=time.monotonic() + 60)
        st.reserve()
        with pytest.raises(WorkflowError):
            st.reserve()

    def test_deadline_raises(self):
        st = _RunState(max_total_agents=10, deadline=time.monotonic() - 1)
        with pytest.raises(WorkflowTimeoutError):
            st.reserve()

    def test_check_only_time(self):
        st = _RunState(max_total_agents=1, deadline=time.monotonic() + 60)
        st.check()
        st.check()  # 不计数，不抛
        assert st.started == 0


class TestAgentHook:
    def _make(self, manager, default_agent="build", max_total=50, deadline=None):
        from plugins.workflow.tools.workflow_tool import _make_agent_hook

        st = _RunState(max_total, deadline or time.monotonic() + 60)
        return _make_agent_hook(manager, "sess-1", st, default_agent), st

    def test_returns_child_final_text(self):
        hook, st = self._make(_FakeManager(routes={"build": "审计完成"}))
        assert hook("检查 src/a.py") == "审计完成"
        assert st.started == 1

    def test_failure_returns_none(self):
        hook, _ = self._make(_FakeManager(fail_agents={"build"}))
        assert hook("x") is None

    def test_default_agent_used_when_unspecified(self):
        mgr = _FakeManager()
        hook, _ = self._make(mgr, default_agent="explore")
        hook("x")
        assert mgr.calls[0][0] == "explore"

    def test_explicit_agent_overrides(self):
        mgr = _FakeManager()
        hook, _ = self._make(mgr)
        hook("x", agent="review")
        assert mgr.calls[0][0] == "review"

    def test_blank_prompt_raises(self):
        hook, _ = self._make(_FakeManager())
        with pytest.raises(WorkflowError):
            hook("  ")

    def test_unknown_kwarg_raises(self):
        hook, _ = self._make(_FakeManager())
        with pytest.raises(TypeError):
            hook("x", effort="high")

    def test_total_cap_kills_script(self):
        hook, _ = self._make(_FakeManager(), max_total=1)
        hook("a")
        with pytest.raises(WorkflowError):
            hook("b")

    def test_share_context_forwarded(self):
        mgr = _FakeManager()
        hook, _ = self._make(mgr)
        hook("x", share_context=True)
        assert mgr.calls[0][2].get("share_context") is True
