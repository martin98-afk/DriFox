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
        assert ns["json"]["dumps"] and ns["math"]["sqrt"] and ns["re"]["match"]


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
