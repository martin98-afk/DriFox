# -*- coding: utf-8 -*-
"""workflow 工具单测：受限命名空间 / 钩子语义 / 上限 / impl 组装。"""
import threading
import time

from PyQt5.QtCore import QThread, pyqtSignal
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
        self.cancelled = []

    def execute_task(self, task_id, agent_name, task_description,
                     on_finished=None, on_error=None, **kw):
        self.calls.append((agent_name, task_description, kw))
        if agent_name in self.fail_agents:
            if on_error:
                on_error(task_id, f"Agent not found: {agent_name}")
            return False
        if on_finished:
            on_finished(task_id, self.routes.get(agent_name, f"done:{agent_name}"))
        return True

    def cancel_task(self, task_id):
        self.cancelled.append(task_id)
        return True


class _SilentManager(_FakeManager):
    """子任务既不 on_finished 也不 on_error（模拟 executor 静默死亡 / 被 stall 检测器摘除）。"""

    def execute_task(self, task_id, agent_name, task_description,
                     on_finished=None, on_error=None, **kw):
        self.calls.append((agent_name, task_description, kw))
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


class TestAgentHookWaitContract:
    """★ 回归：agent() 的「回调能收到 + 等待有上界」双保险。

    缺 connection_type=DirectConnection 时，回调会被排进发起连接那个线程的事件循环，
    而该线程正阻塞在 wait() 上（线程池 worker 还是无 Qt 事件循环的普通线程）
    → 子任务跑完了、回调永不投递 → 脚本永久挂起。
    """

    def _make(self, manager, max_total=50, deadline=None, max_agent_wait=900.0):
        from plugins.workflow.tools.workflow_tool import _make_agent_hook

        st = _RunState(max_total, deadline or time.monotonic() + 60)
        return _make_agent_hook(manager, "sess-1", st, "build", max_agent_wait), st

    def test_forwards_direct_connection(self):
        mgr = _FakeManager()
        hook, _ = self._make(mgr)
        hook("x")
        # 1 == Qt.DirectConnection：回调在 executor 线程里直接执行，等待方才能被唤醒
        assert mgr.calls[0][2].get("connection_type") == 1

    def test_silent_child_degrades_to_none_within_cap(self):
        mgr = _SilentManager()
        hook, _ = self._make(mgr, max_agent_wait=0.3)
        t0 = time.monotonic()
        assert hook("x") is None
        assert time.monotonic() - t0 < 3.0  # 有上界，不可能是无限等待
        assert len(mgr.cancelled) == 1  # 挂起的子任务被尽力取消

    def test_abort_unblocks_waiting_agent(self):
        mgr = _SilentManager()
        hook, st = self._make(mgr, max_agent_wait=60.0)
        box = {}

        def runner():
            box["v"] = hook("x")

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        time.sleep(0.2)
        st.abort()
        t.join(timeout=3)
        assert not t.is_alive(), "abort 后 agent() 仍被阻塞"
        assert box.get("v") is None
        assert len(mgr.cancelled) == 1

    def test_deadline_passed_raises_timeout(self):
        hook, _ = self._make(_SilentManager(), deadline=time.monotonic() + 0.3, max_agent_wait=60.0)
        with pytest.raises(WorkflowTimeoutError):
            hook("x")

    def test_blank_agent_name_raises(self):
        hook, _ = self._make(_FakeManager())
        with pytest.raises(WorkflowError):
            hook("x", agent="   ")

    def test_legacy_signature_does_not_raise_type_error(self):
        """宿主核心是旧签名时不能抛 TypeError（抛了就整段脚本挂掉，而不是降级）。"""

        class _Legacy:
            def execute_task(self, task_id=None, agent_name=None, task_description=None,
                             parent_context="", on_finished=None, on_error=None,
                             on_progress=None, executor_ref=None, share_context=False,
                             session_id="", llm_config=None):
                if on_finished:
                    on_finished(task_id, "ok")
                if executor_ref is not None:
                    executor_ref["executor"] = None  # 同步回调场景没有 executor
                return True

            def cancel_task(self, task_id):
                return True

        hook, _ = self._make(_Legacy())
        assert hook("x") == "ok"

    def test_abort_skips_new_dispatch(self):
        mgr = _FakeManager()
        hook, st = self._make(mgr)
        st.abort()
        assert hook("x") is None
        assert mgr.calls == []  # 已中止：不再派发新的子智能体


class TestRunStateAbort:
    def test_abort_wakes_tracked_waiters(self):
        st = _RunState(10, time.monotonic() + 60)
        ev = threading.Event()
        st.track_waiter(ev)
        st.abort()
        assert st.aborted() is True
        assert ev.is_set() is True

    def test_untracked_waiter_not_touched(self):
        st = _RunState(10, time.monotonic() + 60)
        ev = threading.Event()
        st.track_waiter(ev)
        st.untrack_waiter(ev)
        st.abort()
        assert ev.is_set() is False


class TestQtCallbackDelivery:
    """★ 真实 QThread + 真实 pyqtSignal 下的回调投递验证（根因层面回归）。

    卡死根因：不指定 connection_type 时是 AutoConnection，回调被排进「发起连接那个线程」
    的事件循环，而该线程正阻塞在 wait() 上（线程池 worker 还是无 Qt 事件循环的普通线程）
    → 子任务早已跑完，回调却永不投递。
    """

    class _MgrBase:
        """共享：真实 QThread 发射 + 连接语义；子类决定 execute_task 的签名。"""

        class _Exec(QThread):
            finished_with_result = pyqtSignal(str, str)
            error_occurred = pyqtSignal(str, str)

            def run(self):
                time.sleep(0.05)
                self.finished_with_result.emit(self._tid, "payload")

        def __init__(self, honor=True):
            self.honor = honor
            self.cancelled = []
            self._execs = []  # 持有引用，避免 QThread 运行中被 GC

        def _dispatch(self, task_id, on_finished, on_error, connection_type, executor_ref):
            ex = self._Exec()
            ex._tid = task_id
            if connection_type is not None:
                if on_finished:
                    ex.finished_with_result.connect(on_finished, connection_type)
                if on_error:
                    ex.error_occurred.connect(on_error, connection_type)
            else:  # AutoConnection：回调排进发起连接那个线程的事件循环
                if on_finished:
                    ex.finished_with_result.connect(on_finished)
                if on_error:
                    ex.error_occurred.connect(on_error)
            self._execs.append(ex)
            if executor_ref is not None:
                executor_ref["executor"] = ex  # 与真实核心一致：start() 之前写入
            ex.start()
            return True

        def cancel_task(self, task_id):
            self.cancelled.append(task_id)
            return True

    class _NewCoreMgr(_MgrBase):
        """新核心：execute_task 支持 connection_type。honor=False 模拟「收了但没照做」。"""

        def execute_task(self, task_id=None, agent_name=None, task_description=None,
                         on_finished=None, on_error=None, connection_type=None,
                         executor_ref=None, **kw):
            return self._dispatch(task_id, on_finished, on_error,
                                  connection_type if self.honor else None, executor_ref)

    class _LegacyMgr(_MgrBase):
        """旧核心（宿主未重启时就是这样）：无 connection_type，但有 executor_ref。"""

        def execute_task(self, task_id=None, agent_name=None, task_description=None,
                         parent_context="", on_finished=None, on_error=None,
                         on_progress=None, executor_ref=None, share_context=False,
                         session_id="", llm_config=None):
            return self._dispatch(task_id, on_finished, on_error, None, executor_ref)

    class _BareMgr(_MgrBase):
        """最旧核心：connection_type / executor_ref 都没有，无任何直连手段。"""

        def execute_task(self, task_id=None, agent_name=None, task_description=None,
                         parent_context="", on_finished=None, on_error=None,
                         on_progress=None, share_context=False, session_id="", llm_config=None):
            return self._dispatch(task_id, on_finished, on_error, None, None)

    def _run(self, manager, agent_wait):
        """在普通 Python 线程里调 agent()（模拟 chat worker / 线程池 worker）。"""
        from plugins.workflow.tools.workflow_tool import _make_agent_hook

        st = _RunState(10, time.monotonic() + 60)
        hook = _make_agent_hook(manager, "sess", st, "build", agent_wait)
        box = {}
        t = threading.Thread(target=lambda: box.update(v=hook("x")), daemon=True)
        t.start()
        t.join(timeout=10)
        return t.is_alive(), box.get("v")

    def test_direct_connection_delivers_without_event_loop(self, qapp):
        alive, val = self._run(self._NewCoreMgr(honor=True), 5.0)
        assert not alive, "DirectConnection 下 agent() 仍被阻塞"
        assert val == "payload"

    def test_legacy_core_falls_back_to_executor_ref(self, qapp):
        """宿主核心没有 connection_type（未重启的老进程就是这种）时，靠 executor_ref 补挂直连。"""
        alive, val = self._run(self._LegacyMgr(), 5.0)
        assert not alive, "旧核心兜底路径仍被阻塞"
        assert val == "payload"

    def test_no_fallback_path_still_capped(self, qapp):
        """反例：既无 connection_type 又无 executor_ref 时回调永不投递。

        只剩等待上界兜底（返回 None）——修复前连上界都没有，
        正是「子任务跑完了但脚本永远不动」的现象。
        """
        alive, val = self._run(self._BareMgr(), 1.0)
        assert not alive, "退化路径应被等待上界兜住，不应挂死"
        assert val is None


class TestCombinators:
    def _make(self, max_items=100, max_total=50):
        from concurrent.futures import ThreadPoolExecutor

        from plugins.workflow.tools.workflow_tool import (
            _make_combinators,
            _pool_initializer,
        )

        st = _RunState(max_total, time.monotonic() + 60)
        pool = ThreadPoolExecutor(max_workers=4, initializer=_pool_initializer)
        return (*_make_combinators(st, pool, max_items), st)

    def test_parallel_returns_all(self):
        parallel, _, _ = self._make()
        out = parallel([lambda: 1, lambda: 2, lambda: 3])
        assert out == [1, 2, 3]

    def test_parallel_thunk_exception_drops_to_none(self):
        parallel, _, _ = self._make()

        def boom():
            raise ValueError("x")

        out = parallel([lambda: "ok", boom, lambda: "ok2"])
        assert out == ["ok", None, "ok2"]

    def test_parallel_nested_call_raises(self):
        parallel, _, _ = self._make()

        def nested():
            parallel([lambda: 1])

        with pytest.raises(WorkflowError):
            parallel([nested])

    def test_parallel_items_cap(self):
        parallel, _, _ = self._make(max_items=2)
        with pytest.raises(WorkflowError):
            parallel([lambda: 1, lambda: 2, lambda: 3])

    def test_pipeline_no_barrier_stage_signature(self):
        _, pipeline, _ = self._make()
        order = []

        def s1(prev, item, idx):
            time.sleep(0.05 if item == 0 else 0)
            order.append(("s1", item))
            return f"{item}-a"

        def s2(prev, item, idx):
            order.append(("s2", item))
            return f"{prev}-b"

        out = pipeline([0, 1], s1, s2)
        assert out == ["0-a-b", "1-a-b"]
        # 无屏障断言：item=1 的 s2 早于 item=0 的 s1（慢项不阻塞快项）
        assert order.index(("s2", 1)) < order.index(("s1", 0))

    def test_pipeline_stage_failure_skips_rest(self):
        _, pipeline, _ = self._make()
        reached = []

        def bad(prev, item, idx):
            raise ValueError("stage boom")

        def after(prev, item, idx):
            reached.append(item)
            return prev

        out = pipeline([1, 2], bad, after)
        assert out == [None, None]
        assert reached == []

    def test_pipeline_workflow_error_propagates(self):
        _, pipeline, _ = self._make()

        def over_limit(prev, item, idx):
            raise WorkflowError("额度尽")

        with pytest.raises(WorkflowError):
            pipeline([1], over_limit)

    def test_pipeline_empty_stages_raises(self):
        _, pipeline, _ = self._make()
        with pytest.raises(WorkflowError):
            pipeline([1, 2])


class TestWorkflowImpl:
    def _impl(self, monkeypatch, manager, **overrides):
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(
            wt.PluginConfigStore,
            "get",
            lambda self, plugin, key: {
                "max_concurrent_agents": 2,
                "max_total_agents": 10,
                "max_items_per_call": 10,
                "max_duration_sec": 60,
                "default_agent": "build",
                "max_result_chars": 1000,
            }.get(key),
        )
        ctx = {"sub_agent_manager": manager, "session_id": "s1"}
        kwargs = {
            "meta": {"name": "audit", "description": "审计"},
            "script": "result = {'n': agent('x')}",
        }
        kwargs.update(overrides)
        return wt._workflow_impl(ctx, **kwargs)

    def test_happy_path(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(routes={"build": "ok"}))
        assert r.success is True
        assert r.content["result"] == {"n": "ok"}
        assert r.content["agents_started"] == 1
        assert r.content["workflow"] == "audit"

    def test_meta_validation_fails_fast(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(), meta={"name": "x"})
        assert r.success is False
        assert "description" in r.error

    def test_syntax_error_fails_fast(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(), script="def oops(:")
        assert r.success is False
        assert "语法" in r.error

    def test_missing_result_is_null(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(), script="agent('x')")
        assert r.success is True
        assert r.content["result"] is None

    def test_unserializable_result_degrades(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(), script="result = {'f': len}")
        assert r.success is True
        assert isinstance(r.content["result"], dict)
        assert "_repr" in r.content["result"]

    def test_script_exception_reported(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(), script="result = 1 / 0")
        assert r.success is False
        assert "脚本异常" in r.error

    def test_manager_missing(self, monkeypatch):
        from plugins.workflow.tools import workflow_tool as wt

        monkeypatch.setattr(wt.PluginConfigStore, "get", lambda self, p, k: None)
        r = wt._workflow_impl(
            {"session_id": "s1"}, meta={"name": "a", "description": "b"}, script="result = 1"
        )
        assert r.success is False

    def test_preview_shows_name(self):
        from plugins.workflow.tools.workflow_tool import _preview_workflow

        assert "audit" in _preview_workflow({"meta": {"name": "audit", "description": "d"}, "script": ""})

    def test_register(self):
        from plugins.workflow.tools.workflow_tool import register

        class _R:
            def register(self, name, schema, **kw):
                assert name == "workflow"
                assert kw["danger"] == "dangerous"
                assert kw["group"] == "子智能体"
                assert kw["keep_in_content"] is True

        register(_R())


class TestDynamicDescription:
    def test_workflow_description_lists_agents(self):
        from plugins.workflow.tools.workflow_tool import _workflow_description

        d = _workflow_description(["build", "explore"])
        assert "build" in d and "explore" in d
        assert "subagent_dag" in d  # 分工指引在描述里
        assert "result" in d  # 结果约定在描述里

    def test_workflow_description_empty_agents(self):
        from plugins.workflow.tools.workflow_tool import _workflow_description

        d = _workflow_description([])
        assert "agent(" in d  # 钩子用法仍在
