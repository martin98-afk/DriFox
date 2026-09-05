# -*- coding: utf-8 -*-
"""workflow 工具单测：受限命名空间 / 钩子语义 / 上限 / impl 组装。"""
import json
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


class TestWorkflowResultRendering:
    """结果出口与完成框渲染闭包。

    宿主 render_helpers 没有插件 render 闭包时会把结果文本原样塞进 <pre>，
    workflow 的结果是一整个 JSON —— 不注册闭包就是一大坨 Python repr。
    """

    class _R:
        def __init__(self, content):
            self.content = content

    def _body(self, **content):
        from plugins.workflow.tools.workflow_tool import _dump_content, _render_workflow_body

        return _render_workflow_body(self._R(_dump_content(content)), "workflow", {}, True)

    def test_dump_content_is_json_not_python_repr(self):
        import orjson

        from plugins.workflow.tools.workflow_tool import _dump_content

        s = _dump_content({"a": 1, "b": None, "c": True, "d": [1, 2]})
        assert orjson.loads(s) == {"a": 1, "b": None, "c": True, "d": [1, 2]}
        assert "'" not in s and "None" not in s  # 不是 str(dict) 的 Python repr

    def test_render_shows_metrics_phases_and_logs(self):
        html = self._body(
            workflow="audit", agents_started=3, phases=["扫描", "复核"], logs=["扫到 12 个文件"], result={"ok": 1}
        )
        assert "audit" in html
        assert "3" in html and "子智能体" in html
        assert "扫描" in html and "复核" in html
        assert "执行日志" in html

    def test_render_hints_when_result_unset(self):
        html = self._body(workflow="w", agents_started=0, phases=[], logs=[], result=None)
        assert "result 赋值" in html

    def test_render_escapes_html(self):
        html = self._body(
            workflow="<img src=x onerror=alert(1)>", agents_started=0, phases=[], logs=[], result="<script>"
        )
        assert "<img src=x" not in html and "<script>" not in html
        assert "&lt;" in html

    def test_render_shows_dict_phases_with_detail(self):
        # Task 3: phases 条目 dict 化（title+detail），渲染层兼容并与旧字符串条目共存
        html = self._body(
            workflow="audit",
            agents_started=1,
            phases=[{"title": "plan", "detail": "读文档"}, {"title": "build", "detail": None}, "旧字符串阶段"],
            logs=[],
            result={"ok": 1},
        )
        assert "plan" in html and "build" in html and "旧字符串阶段" in html
        assert "读文档" in html
        assert "detail" not in html  # dict 不得以 repr 形态漏出（escape 后仍含 detail 字样）

    def test_render_falls_back_for_unparseable(self):
        from plugins.workflow.tools.workflow_tool import _render_workflow_body

        html = _render_workflow_body(self._R("not json at all"), "workflow", {}, True)
        assert "not json at all" in html

    def test_salvage_truncated_json(self):
        """宿主按 _MAX_OUTPUT_CHARS 截断后仍要救回可解析的前缀。"""
        from plugins.workflow.tools.workflow_tool import _parse_workflow_payload

        full = '{"workflow":"a","agents_started":2,"phases":[],"logs":[],"result":"' + "x" * 9000 + '"}'
        data = _parse_workflow_payload(full[:5000])
        assert data.get("workflow") == "a"
        assert data.get("_truncated") is True

    def test_preview_shows_phase_count(self):
        from plugins.workflow.tools.workflow_tool import _preview_workflow

        assert "2 个阶段" in _preview_workflow({"meta": {"name": "a", "phases": [{"title": "x"}, {"title": "y"}]}})
        assert _preview_workflow({"meta": {"name": "a"}}) == "workflow: a"

    def test_register_passes_render(self):
        from plugins.workflow.tools.workflow_tool import _render_workflow_body, register

        class _Reg:
            def register(self, name, schema, **kw):
                assert kw["render"] is _render_workflow_body

        register(_Reg())


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


class TestTranslateTypeError:
    """TypeError 人话翻译层：钩子签名报错（<lambda> 或含钩子名）翻译成自愈提示。"""

    _HOOKS = ("agent", "parallel", "pipeline", "phase", "log")

    def _catch(self, boom) -> TypeError:
        try:
            boom()
        except TypeError as e:
            return e
        raise AssertionError("未触发 TypeError")

    def test_lambda_positional_error_translated(self):
        from plugins.workflow.tools.workflow_tool import translate_type_error

        def boom():
            phase = lambda title: None  # noqa: E731
            phase("a", "b")

        msg = translate_type_error(self._catch(boom), self._HOOKS)
        assert msg is not None
        assert "签名" in msg and "phase(title" in msg  # 带正确签名提示

    def test_def_named_hook_extracted(self):
        from plugins.workflow.tools.workflow_tool import translate_type_error

        def phase(title, detail=None):
            return None

        def boom():
            phase("a", "b", "c")  # 多于新签名

        msg = translate_type_error(self._catch(boom), self._HOOKS)
        assert msg is not None and "phase" in msg

    def test_kwarg_error_translated(self):
        from plugins.workflow.tools.workflow_tool import translate_type_error

        def boom():
            agent = lambda prompt: None  # noqa: E731
            agent("x", model="sonnet")

        msg = translate_type_error(self._catch(boom), self._HOOKS)
        assert msg is not None and "model" in msg  # 提示里点出肇事参数

    def test_unrelated_error_passthrough(self):
        from plugins.workflow.tools.workflow_tool import translate_type_error

        assert translate_type_error(TypeError("int object is not callable"), self._HOOKS) is None


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
        # 出口是 JSON 字符串（不是 Python repr）：模型侧和渲染闭包侧都要能直接 parse
        payload = json.loads(r.content)
        assert payload["result"] == {"n": "ok"}
        assert payload["agents_started"] == 1
        assert payload["workflow"] == "audit"

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
        assert json.loads(r.content)["result"] is None

    def test_unserializable_result_degrades(self, monkeypatch):
        r = self._impl(monkeypatch, _FakeManager(), script="result = {'f': len}")
        assert r.success is True
        payload = json.loads(r.content)
        assert isinstance(payload["result"], dict)
        assert "_repr" in payload["result"]

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


class TestImportPrecheck:
    """Task 6: import 预检——拦截脚本 import，报错列出预置模块清单。"""

    def test_blocks_import_and_lists_presets(self):
        from plugins.workflow.tools.workflow_tool import _check_imports, _PRESET_MODULES

        err = _check_imports("import os, json\nresult = 1", _PRESET_MODULES)
        assert err is not None
        assert "os" in err and "json" in err  # 被禁名与预置清单都在报错里

    def test_from_import_blocked(self):
        from plugins.workflow.tools.workflow_tool import _check_imports, _PRESET_MODULES

        assert _check_imports("from pathlib import Path\nresult = 1", _PRESET_MODULES) is not None

    def test_plain_script_passes(self):
        from plugins.workflow.tools.workflow_tool import _check_imports, _PRESET_MODULES

        assert _check_imports("x = json.loads('{}')\nresult = x", _PRESET_MODULES) is None


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

    def test_workflow_description_documents_full_contract(self):
        # Task 6: description 是模型的第一信息源，契约必须完整显式
        from plugins.workflow.tools.workflow_tool import _workflow_description

        d = _workflow_description(["build"])
        assert "model=" in d and "schema=" in d  # agent 钩子完整签名
        assert "phase(title, detail=None)" in d  # phase 双参
        assert "禁止 import" in d  # import 契约显式化
        assert "model_aliases" in d  # 别名需配置的提示


class TestPhaseLogHooks:
    """Task 3: phase/log 钩子 def 化——phase 带 detail、log 容忍多余参数。"""

    def test_phase_accepts_detail(self):
        from plugins.workflow.tools.workflow_tool import _make_phase_log_hooks

        phases, logs = [], []
        phase, log = _make_phase_log_hooks(phases, logs)
        phase("1/4 plan", "读取计划与设计")
        phase("2/4 build")
        log("done")
        log("另一条", "多余参数容忍")
        assert phases[0] == {"title": "1/4 plan", "detail": "读取计划与设计"}
        assert phases[1] == {"title": "2/4 build", "detail": None}
        assert logs == ["done", "另一条"]

    def test_phase_beyond_detail_still_typeerror_but_translatable(self):
        # 超过 (title, detail) 的调用仍 TypeError，但 def 化后报错含函数名，翻译层能点名
        from plugins.workflow.tools.workflow_tool import (
            _make_phase_log_hooks,
            translate_type_error,
        )

        phase, _ = _make_phase_log_hooks([], [])
        try:
            phase("a", "b", "c")
            raise AssertionError("未触发 TypeError")
        except TypeError as e:
            msg = translate_type_error(e, ("agent", "parallel", "pipeline", "phase", "log"))
        assert msg is not None and "phase" in msg


class TestAgentHookModel:
    """Task 4: agent 钩子 model 别名——解析映射传宿主，未知别名降级并写人话日志。"""

    def _make(self, manager, aliases=None):
        from plugins.workflow.tools.workflow_tool import _make_agent_hook

        st = _RunState(50, time.monotonic() + 60)
        logs: list = []
        hook = _make_agent_hook(
            manager, "s1", st, "build", 900.0, model_aliases=aliases, log_fn=lambda m: logs.append(str(m))
        )
        return hook, st, logs

    def test_alias_resolved_into_dispatch(self):
        mgr = _FakeManager(routes={"build": "ok"})
        hook, _, _ = self._make(mgr, aliases={"sonnet": "m-sonnet"})
        assert hook("x", model="sonnet") == "ok"
        assert mgr.calls[0][2].get("model") == "m-sonnet"

    def test_unknown_alias_drops_none_and_logs_hint(self):
        mgr = _FakeManager()
        hook, _, logs = self._make(mgr, aliases={"sonnet": "m-sonnet"})
        assert hook("x", model="opus") is None
        assert mgr.calls == []  # 未派发，不消耗额度
        assert any("opus" in m and "sonnet" in m for m in logs)  # 人话提示列出可用别名

    def test_no_model_arg_keeps_call_unchanged(self):
        mgr = _FakeManager()
        hook, _, _ = self._make(mgr)
        assert hook("x") == "done:build"
        assert "model" not in mgr.calls[0][2]

    def test_host_without_model_kwarg_skips_it(self):
        from plugins.workflow.tools.workflow_tool import _make_agent_hook

        class _Strict:
            """旧宿主：基础键都收（比 model 更早存在），唯独无 **kw、无 model 形参。"""

            def __init__(self):
                self.passed = {}

            def execute_task(
                self,
                task_id,
                agent_name,
                task_description,
                on_finished=None,
                on_error=None,
                share_context=False,
                session_id="",
            ):
                self.passed = dict(task_id=task_id)
                on_finished(task_id, "ok")
                return True

        st = _RunState(50, time.monotonic() + 60)
        strict = _Strict()
        hook = _make_agent_hook(strict, "s1", st, "build", 900.0, model_aliases={"sonnet": "m1"})
        assert hook("x", model="sonnet") == "ok"  # 不炸，model 被静默跳过（宿主旧签名自适应）


class TestAgentHookSchema:
    """Task 5: agent 钩子 schema 结构化输出——注入指令/校验/失败带错重试 1 次。"""

    SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}

    def _make(self, responses):
        from plugins.workflow.tools.workflow_tool import _make_agent_hook

        class _Seq:
            def __init__(self):
                self.responses = list(responses)
                self.calls = 0
                self.descs = []

            def execute_task(self, task_id, agent_name, task_description, on_finished=None, on_error=None, **kw):
                self.calls += 1
                self.descs.append(task_description)
                r = self.responses.pop(0)
                on_finished(task_id, r)
                return True

        mgr = _Seq()
        st = _RunState(50, time.monotonic() + 60)
        logs: list = []
        hook = _make_agent_hook(mgr, "s1", st, "build", 900.0, log_fn=lambda m: logs.append(str(m)))
        return hook, mgr, st, logs

    def test_valid_json_returns_dict(self):
        hook, mgr, _, _ = self._make(['{"verdict": "通过"}'])
        assert hook("x", schema=self.SCHEMA) == {"verdict": "通过"}
        assert "JSON Schema" in mgr.descs[0]  # prompt 注入了输出格式指令

    def test_retry_once_then_success(self):
        hook, mgr, _, _ = self._make(["不是json", '{"verdict": "通过"}'])
        assert hook("x", schema=self.SCHEMA) == {"verdict": "通过"}
        assert mgr.calls == 2

    def test_both_bad_returns_none_and_logs(self):
        hook, mgr, _, logs = self._make(["bad1", "bad2"])
        assert hook("x", schema=self.SCHEMA) is None
        assert mgr.calls == 2
        assert any("schema_failed" in m for m in logs)

    def test_retries_count_toward_quota(self):
        hook, _, st, _ = self._make(["bad1", '{"verdict": "v"}'])
        hook("x", schema=self.SCHEMA)
        assert st.started == 2

    def test_schema_error_in_retry_prompt(self):
        hook, mgr, _, _ = self._make(["bad1", '{"verdict": "v"}'])
        hook("x", schema=self.SCHEMA)
        assert "重试" in mgr.descs[1] and "上次" in mgr.descs[1]  # 重试 prompt 带具体校验错误


class TestConfigParsing:
    """Task 2: 新配置项解析——model_aliases 别名映射 + 前台开关 + 卡片刷新间隔。"""

    def test_parse_aliases_basic(self):
        from plugins.workflow.tools.workflow_tool import _parse_aliases

        assert _parse_aliases("sonnet=m1, haiku=m2") == {"sonnet": "m1", "haiku": "m2"}

    def test_parse_aliases_edge_cases(self):
        from plugins.workflow.tools.workflow_tool import _parse_aliases

        assert _parse_aliases("") == {}
        assert _parse_aliases(None) == {}
        # 残缺段（无=、空 key、空 value）跳过，合法段保留
        assert _parse_aliases("bad, a=, =b, ok=v1") == {"ok": "v1"}

    def test_impl_tolerates_new_config_keys(self, monkeypatch):
        from plugins.workflow.tools import workflow_tool as wt

        def fake_get(self, plugin, key):
            return {
                "max_concurrent_agents": 2,
                "max_total_agents": 10,
                "max_items_per_call": 10,
                "max_duration_sec": 60,
                "max_agent_wait_sec": 60,
                "default_agent": "build",
                "max_result_chars": 1000,
                "model_aliases": "sonnet=m1",
                "default_foreground": "true",
                "card_refresh_ms": 2000,
            }.get(key)

        monkeypatch.setattr(wt.PluginConfigStore, "get", fake_get)
        ctx = {"sub_agent_manager": _FakeManager(routes={"build": "ok"}), "session_id": "s1"}
        r = wt._workflow_impl(
            ctx,
            meta={"name": "audit", "description": "审计"},
            script="result = {'n': agent('x')}",
        )
        assert r.success is True
